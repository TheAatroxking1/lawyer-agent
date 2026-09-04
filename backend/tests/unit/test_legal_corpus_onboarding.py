from __future__ import annotations

from datetime import date
from typing import Protocol
from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_chunks import derive_chunks
from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalCorpusImportError,
    LegalImportCommand,
    LegalImportResult,
    LegalProvisionDraft,
)
from lawyer_agent.application.legal_corpus_onboarding import (
    LegalCorpusOnboardingService,
    OnboardingResult,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalChunk,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)

TITLE = "中华人民共和国民法典"
AUTHORITY = "全国人民代表大会"
INSTRUMENT = UUID("01a06ae2-6000-7000-8000-0000000000c1")
VERSION_A = UUID("01a06ae2-6100-7000-8000-0000000000c2")
VERSION_B = UUID("01a06ae2-6200-7000-8000-0000000000c3")


class _ImportPort(Protocol):
    async def import_version(
        self, command: LegalImportCommand
    ) -> LegalImportResult: ...


def _command(version_label: str = "2020 版") -> LegalImportCommand:
    return LegalImportCommand(
        title=TITLE,
        issuing_authority=AUTHORITY,
        jurisdiction="national",
        region_code=None,
        version_label=version_label,
        status=LegalVersionStatus.CURRENT,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        law_number="主席令第四十五号",
        source_ref="object://corpus/civil-code.docx",
        dataset_version="dataset_v1",
        parser_version="docx-zip-v1",
        provisions=(
            LegalProvisionDraft(
                provision_no="第一条",
                level=ProvisionLevel.ARTICLE,
                structure_path=(),
                title=None,
                full_text="第一条 内容甲。",
            ),
            LegalProvisionDraft(
                provision_no="第二条",
                level=ProvisionLevel.ARTICLE,
                structure_path=(),
                title=None,
                full_text="第二条 内容乙。",
            ),
        ),
    )


class _Recorder:
    def __init__(self) -> None:
        self.replaced: list[tuple[UUID, tuple[LegalChunk, ...]]] = []
        self.queries: list[UUID] = []


class _OnboardingFakes:
    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.commands: list[LegalImportCommand] = []
        self.version_rows: dict[UUID, tuple[Provision, ...]] = {}

    async def import_version(self, command: LegalImportCommand) -> LegalImportResult:
        self.commands.append(command)
        if command.title == "有冲突机关":
            raise LegalCorpusImportConflict("instrument identity conflicts")
        return LegalImportResult(
            instrument_id=INSTRUMENT, version_id=VERSION_A, replayed=False
        )

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]:
        self.recorder.queries.append(version_id)
        return self.version_rows.get(version_id, ())

    async def replace_chunks_for_version(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...]
    ) -> None:
        self.recorder.replaced.append((version_id, chunks))


def _provision(version_id: UUID, no: str, text: str) -> Provision:
    return Provision(
        id=new_uuid7(),
        version_id=version_id,
        provision_no=no,
        level=ProvisionLevel.ARTICLE,
        structure_path=(),
        title=None,
        full_text=text,
        content_hash=content_sha256(text),
        char_start=0,
        char_end=len(text),
    )


def _onboarding(fakes: _OnboardingFakes) -> LegalCorpusOnboardingService:
    return LegalCorpusOnboardingService(
        import_version=fakes.import_version,
        provisions=fakes.provisions_for_version,
        chunks=fakes.replace_chunks_for_version,
    )


async def test_onboard_imports_then_replaces_two_chunks() -> None:
    fakes = _OnboardingFakes()
    fakes.version_rows[VERSION_A] = (
        _provision(VERSION_A, "第一条", "第一条 内容甲。"),
        _provision(VERSION_A, "第二条", "第二条 内容乙。"),
    )
    result = await _onboarding(fakes).onboard_version(_command())
    assert isinstance(result, OnboardingResult)
    assert result.instrument_id == INSTRUMENT
    assert result.version_id == VERSION_A
    assert result.chunk_count == 2
    assert len(fakes.commands) == 1
    (replaced_version, chunks) = fakes.recorder.replaced[0]
    assert replaced_version == VERSION_A
    assert [chunk.provision_id for chunk in chunks]
    assert all(chunk.version_id == VERSION_A for chunk in chunks)
    assert all(chunk.content_hash == content_sha256(chunk.content) for chunk in chunks)
    assert [chunk.parser_version for chunk in chunks] == ["docx-zip-v1"] * 2


async def test_onboard_replay_rebuilds_same_chunk_set() -> None:
    fakes = _OnboardingFakes()
    fakes.version_rows[VERSION_A] = (
        _provision(VERSION_A, "第一条", "第一条 内容甲。"),
        _provision(VERSION_A, "第二条", "第二条 内容乙。"),
    )
    service = _onboarding(fakes)
    first = await service.onboard_version(_command())
    second = await service.onboard_version(_command())
    assert first.chunk_count == 2 and second.chunk_count == 2
    assert len(fakes.recorder.replaced) == 2
    # Replace is delete-then-insert so both runs persist exactly two chunks.
    assert all(len(chunks) == 2 for _, chunks in fakes.recorder.replaced)


async def test_onboard_empty_provisions_writes_nothing() -> None:
    fakes = _OnboardingFakes()
    result = await _onboarding(fakes).onboard_version(_command())
    assert result.chunk_count == 0
    assert fakes.recorder.replaced == []


async def test_onboard_import_conflict_propagates() -> None:
    fakes = _OnboardingFakes()
    command = _command()
    object.__setattr__(command, "title", "有冲突机关")
    with pytest.raises(LegalCorpusImportError):
        await _onboarding(fakes).onboard_version(command)


async def test_onboard_derives_chunks_matching_command_parser() -> None:
    command = _command()
    derived = derive_chunks(
        version_id=VERSION_A,
        provisions=(
            _provision(VERSION_A, "第一条", "第一条 内容甲。"),
            _provision(VERSION_A, "第二条", "第二条 内容乙。"),
        ),
        parser_version=command.parser_version,
    )
    assert len(derived) == 2
    assert {chunk.parser_version for chunk in derived} == {"docx-zip-v1"}
