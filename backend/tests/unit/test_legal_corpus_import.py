from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalCorpusImportError,
    LegalCorpusImportPort,
    LegalCorpusImportService,
    LegalImportCommand,
    LegalImportResult,
    LegalProvisionDraft,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)

TITLE = "中华人民共和国民法典"
AUTHORITY = "全国人民代表大会"
JURISDICTION = "national"


def _command(
    *,
    title: str = TITLE,
    authority: str = AUTHORITY,
    jurisdiction: str = JURISDICTION,
    version_label: str = "2020-05-28 公布版",
    status: LegalVersionStatus = LegalVersionStatus.CURRENT,
    law_number: str = "主席令第四十五号",
    text_a: str = "第一条 为了保护民事主体的合法权益，调整民事关系，制定本法。",
    text_b: str | None = (
        "第二条 民法调整平等主体的自然人、法人和非法人组织之间的人身关系和财产关系。"
    ),
) -> LegalImportCommand:
    provisions: list[LegalProvisionDraft] = []
    if text_a is not None:
        provisions.append(
            LegalProvisionDraft(
                provision_no="第一条",
                level=ProvisionLevel.ARTICLE,
                structure_path=("第一编", "第一章", "第一条"),
                title=None,
                full_text=text_a,
            )
        )
    if text_b is not None:
        provisions.append(
            LegalProvisionDraft(
                provision_no="第二条",
                level=ProvisionLevel.ARTICLE,
                structure_path=("第一编", "第一章", "第二条"),
                title=None,
                full_text=text_b,
            )
        )
    return LegalImportCommand(
        title=title,
        issuing_authority=authority,
        jurisdiction=jurisdiction,
        region_code=None,
        version_label=version_label,
        status=status,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        law_number=law_number,
        source_ref="object://corpus/civil-code.docx",
        dataset_version="dataset_v1",
        parser_version="docx-v1",
        provisions=tuple(provisions),
    )


class _Repo(LegalCorpusImportPort):
    def __init__(
        self,
        instrument: LegalInstrument | None = None,
        versions: dict[UUID, LegalVersion] | None = None,
    ) -> None:
        self.instrument = instrument
        self.versions: dict[UUID, LegalVersion] = dict(versions or {})
        self.created_instruments: list[LegalInstrument] = []
        self.created_versions: list[LegalVersion] = []
        self.created_provisions: list[Provision] = []

    async def find_instrument_by_identity(
        self, title: str, jurisdiction: str
    ) -> LegalInstrument | None:
        return self.instrument

    async def create_instrument(self, instrument: LegalInstrument) -> None:
        self.created_instruments.append(instrument)
        self.instrument = instrument

    async def find_version(
        self, instrument_id: UUID, version_label: str
    ) -> LegalVersion | None:
        for version in self.versions.values():
            if (
                version.instrument_id == instrument_id
                and version.version_label == version_label
            ):
                return version
        return None

    async def create_version(self, version: LegalVersion) -> None:
        self.created_versions.append(version)
        self.versions[version.id] = version

    async def create_provisions(self, provisions: tuple[Provision, ...]) -> None:
        self.created_provisions.extend(provisions)


def _service(repo: _Repo) -> LegalCorpusImportService:
    return LegalCorpusImportService(repo)


async def test_import_creates_instrument_version_and_provisions() -> None:
    repo = _Repo()
    result = await _service(repo).import_version(_command())
    assert isinstance(result, LegalImportResult)
    assert result.replayed is False
    assert repo.instrument is not None
    assert repo.instrument.title == TITLE
    assert repo.instrument.issuing_authority == AUTHORITY
    assert len(repo.created_instruments) == 1
    assert len(repo.created_versions) == 1
    assert len(repo.created_provisions) == 2
    assert repo.created_versions[0].instrument_id == repo.instrument.id
    joined = "".join(provision.full_text for provision in repo.created_provisions)
    assert repo.created_versions[0].content_hash == content_sha256(joined)
    # Ordered continuous character ranges.
    first, second = repo.created_provisions
    assert first.char_start == 0
    assert first.char_end == len(first.full_text)
    assert second.char_start == first.char_end
    assert second.char_end == second.char_start + len(second.full_text)


async def test_import_reuses_existing_instrument_by_identity() -> None:
    existing = LegalInstrument(
        id=new_uuid7(),
        title=TITLE,
        issuing_authority=AUTHORITY,
        jurisdiction=JURISDICTION,
    )
    repo = _Repo(instrument=existing)
    result = await _service(repo).import_version(_command())
    assert result.replayed is False
    assert repo.created_instruments == []
    assert repo.created_versions[0].instrument_id == existing.id


async def test_import_replays_same_version_when_content_matches() -> None:
    instrument = LegalInstrument(
        id=new_uuid7(),
        title=TITLE,
        issuing_authority=AUTHORITY,
        jurisdiction=JURISDICTION,
    )
    repo = _Repo(instrument=instrument)
    service = _service(repo)
    first = await service.import_version(_command())
    assert first.replayed is False
    second = await service.import_version(_command())
    assert second.replayed is True
    assert second.version_id == first.version_id
    assert len(repo.created_versions) == 1
    assert len(repo.created_provisions) == 2


async def test_import_conflicts_when_same_label_different_content() -> None:
    instrument = LegalInstrument(
        id=new_uuid7(),
        title=TITLE,
        issuing_authority=AUTHORITY,
        jurisdiction=JURISDICTION,
    )
    repo = _Repo(instrument=instrument)
    service = _service(repo)
    await service.import_version(_command())
    changed = _command(text_a="第一条 完全不同内容的条文。")
    with pytest.raises(LegalCorpusImportConflict, match="version"):
        await service.import_version(changed)
    assert len(repo.created_versions) == 1


async def test_import_conflicts_when_metadata_differs() -> None:
    instrument = LegalInstrument(
        id=new_uuid7(),
        title=TITLE,
        issuing_authority=AUTHORITY,
        jurisdiction=JURISDICTION,
    )
    repo = _Repo(instrument=instrument)
    service = _service(repo)
    await service.import_version(_command())
    relabeled = _command(
        status=LegalVersionStatus.HISTORICAL,
        law_number="不同文号",
    )
    with pytest.raises(LegalCorpusImportConflict, match="metadata"):
        await service.import_version(relabeled)
    assert len(repo.created_versions) == 1


async def test_import_conflicts_when_authority_mismatches() -> None:
    existing = LegalInstrument(
        id=new_uuid7(),
        title=TITLE,
        issuing_authority="国务院",
        jurisdiction=JURISDICTION,
    )
    repo = _Repo(instrument=existing)
    with pytest.raises(LegalCorpusImportConflict, match="instrument"):
        await _service(repo).import_version(_command())


async def test_import_rejects_empty_or_invalid_commands() -> None:
    repo = _Repo()
    service = _service(repo)
    with pytest.raises(LegalCorpusImportError, match="at least one provision"):
        await service.import_version(_command(text_a=None, text_b=None))
    with pytest.raises(LegalCorpusImportError, match="non-empty"):
        await service.import_version(_command(text_a=""))


async def test_import_rejects_duplicate_provision_numbers() -> None:
    command = _command()
    duplicate = LegalImportCommand(
        title=command.title,
        issuing_authority=command.issuing_authority,
        jurisdiction=command.jurisdiction,
        region_code=command.region_code,
        version_label=command.version_label,
        status=command.status,
        published_on=command.published_on,
        effective_on=command.effective_on,
        repealed_on=command.repealed_on,
        law_number=command.law_number,
        source_ref=command.source_ref,
        dataset_version=command.dataset_version,
        parser_version=command.parser_version,
        provisions=(
            LegalProvisionDraft(
                provision_no="第一条",
                level=ProvisionLevel.ARTICLE,
                structure_path=(),
                title=None,
                full_text="第一条 内容。",
            ),
            LegalProvisionDraft(
                provision_no="第一条",
                level=ProvisionLevel.ARTICLE,
                structure_path=(),
                title=None,
                full_text="第一条 内容。",
            ),
        ),
    )
    with pytest.raises(LegalCorpusImportError, match="duplicate"):
        await _service(_Repo()).import_version(duplicate)
