from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from lawyer_agent.application.evidence import EvidenceAccessPort
from lawyer_agent.application.legal_evidence_assembly import (
    LegalEvidenceAssemblyError,
    LegalEvidenceAssemblyService,
    LegalEvidenceQueryPort,
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
from lawyer_agent.domain.legal_search import LegalSearchHit

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
INSTRUMENT = UUID("01a06ae2-6000-7000-8000-0000000000c1")
PROVISION_A = UUID("01a06ae2-6300-7000-8000-0000000000c4")
PROVISION_B = UUID("01a06ae2-6400-7000-8000-0000000000c5")
VERSION_OTHER = UUID("01a06ae2-6500-7000-8000-0000000000c6")


def _instrument() -> LegalInstrument:
    return LegalInstrument(
        id=INSTRUMENT,
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
    )


def _version(
    *,
    version_id: UUID = VERSION,
    label: str = "2020 公布版",
    status: LegalVersionStatus = LegalVersionStatus.CURRENT,
    effective_on: date | None = date(2021, 1, 1),
    repealed_on: date | None = None,
    source_ref: str | None = "object://corpus/civil-code.docx",
    dataset_version: str | None = "dataset_v1",
) -> LegalVersion:
    return LegalVersion(
        id=version_id,
        instrument_id=INSTRUMENT,
        version_label=label,
        status=status,
        published_on=date(2020, 5, 28),
        effective_on=effective_on,
        repealed_on=repealed_on,
        law_number="主席令第四十五号",
        source_ref=source_ref,
        dataset_version=dataset_version,
        parser_version="docx-zip-v1",
    )


def _provision(
    *,
    provision_id: UUID,
    provision_no: str,
    full_text: str,
    version_id: UUID = VERSION,
) -> Provision:
    return Provision(
        id=provision_id,
        version_id=version_id,
        provision_no=provision_no,
        level=ProvisionLevel.ARTICLE,
        structure_path=(provision_no,),
        title=None,
        full_text=full_text,
        content_hash=content_sha256(full_text),
        char_start=0,
        char_end=len(full_text),
    )


def _hit(
    *,
    provision_id: UUID,
    chunk_id: UUID | None = None,
    version_id: UUID = VERSION,
    score: float = 1.0,
) -> LegalSearchHit:
    return LegalSearchHit(
        chunk_id=chunk_id or new_uuid7(),
        provision_id=provision_id,
        version_id=version_id,
        score=score,
    )


class _Query(LegalEvidenceQueryPort):
    def __init__(
        self,
        *versions: tuple[LegalVersion, LegalInstrument, tuple[Provision, ...]],
    ) -> None:
        self._versions = {
            version.id: (version, instrument, provisions)
            for version, instrument, provisions in versions
        }

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None:
        row = self._versions.get(version_id)
        return None if row is None else (row[0], row[1])

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]:
        row = self._versions.get(version_id)
        return () if row is None else row[2]


class _Access:
    def __init__(self, allowed: bool = True) -> None:
        self._allowed = allowed
        self.calls: list[UUID] = []

    async def authorize_evidence(self, evidence_id: UUID) -> bool:
        self.calls.append(evidence_id)
        return self._allowed


def _default_query() -> _Query:
    provisions = (
        _provision(
            provision_id=PROVISION_A,
            provision_no="第一条",
            full_text="第一条 民法典内容。",
        ),
        _provision(
            provision_id=PROVISION_B,
            provision_no="第二条",
            full_text="第二条 其他内容。",
        ),
    )
    return _Query((_version(), _instrument(), provisions))


def _service(
    query: _Query, access: EvidenceAccessPort | None = None
) -> LegalEvidenceAssemblyService:
    return LegalEvidenceAssemblyService(query, access=access)


async def test_assemble_maps_hits_to_evidence_in_hit_order() -> None:
    service = _service(_default_query())
    bundle = await service.assemble(
        hits=(_hit(provision_id=PROVISION_A, score=8.0), _hit(provision_id=PROVISION_B, score=4.0))
    )
    assert bundle is not None
    items = bundle.items
    assert [item.evidence_id for item in items] == [PROVISION_A, PROVISION_B]
    first = items[0]
    assert first.instrument_title == "中华人民共和国民法典"
    assert first.version_id == VERSION
    assert first.version_label == "2020 公布版"
    assert first.status is LegalVersionStatus.CURRENT
    assert first.published_on == date(2020, 5, 28)
    assert first.effective_on == date(2021, 1, 1)
    assert first.repealed_on is None
    assert first.provision_no == "第一条"
    assert first.provision_text == "第一条 民法典内容。"
    assert first.source_ref == "object://corpus/civil-code.docx"
    assert first.dataset_version == "dataset_v1"
    assert first.authorized


async def test_assemble_deduplicates_same_provision_across_chunk_hits() -> None:
    service = _service(_default_query())
    bundle = await service.assemble(
        hits=(
            _hit(provision_id=PROVISION_A, chunk_id=new_uuid7(), score=9.0),
            _hit(provision_id=PROVISION_B, chunk_id=new_uuid7(), score=8.0),
            _hit(provision_id=PROVISION_A, chunk_id=new_uuid7(), score=7.0),
        )
    )
    assert bundle is not None
    assert [item.evidence_id for item in bundle.items] == [PROVISION_A, PROVISION_B]


async def test_assemble_returns_none_for_no_hits() -> None:
    service = _service(_default_query())
    assert await service.assemble(hits=()) is None


async def test_assemble_rejects_unknown_version() -> None:
    query = _Query((_version(), _instrument(), ()))
    service = _service(query)
    with pytest.raises(LegalEvidenceAssemblyError, match="version"):
        await service.assemble(hits=(_hit(provision_id=PROVISION_A, version_id=VERSION_OTHER),))


async def test_assemble_rejects_provision_missing_from_version() -> None:
    query = _Query(
        (
            _version(),
            _instrument(),
            (
                _provision(
                    provision_id=PROVISION_A,
                    provision_no="第一条",
                    full_text="第一条 内容。",
                ),
            ),
        )
    )
    service = _service(query)
    with pytest.raises(LegalEvidenceAssemblyError, match="provision"):
        await service.assemble(hits=(_hit(provision_id=PROVISION_B),))


async def test_assemble_rejects_version_without_source_ref() -> None:
    query = _Query(
        (
            _version(source_ref=None),
            _instrument(),
            (
                _provision(
                    provision_id=PROVISION_A,
                    provision_no="第一条",
                    full_text="第一条 内容。",
                ),
            ),
        )
    )
    service = _service(query)
    with pytest.raises(LegalEvidenceAssemblyError, match="source"):
        await service.assemble(hits=(_hit(provision_id=PROVISION_A),))


async def test_assemble_rejects_version_without_dataset_version() -> None:
    query = _Query(
        (
            _version(dataset_version=None),
            _instrument(),
            (
                _provision(
                    provision_id=PROVISION_A,
                    provision_no="第一条",
                    full_text="第一条 内容。",
                ),
            ),
        )
    )
    service = _service(query)
    with pytest.raises(LegalEvidenceAssemblyError, match="dataset"):
        await service.assemble(hits=(_hit(provision_id=PROVISION_A),))


async def test_assemble_uses_access_port_for_authorization() -> None:
    access = _Access(allowed=False)
    service = _service(_default_query(), access=access)
    bundle = await service.assemble(hits=(_hit(provision_id=PROVISION_A),))
    assert bundle is not None
    assert not bundle.items[0].authorized
    assert access.calls == [PROVISION_A]


async def test_assemble_preserves_version_metadata_per_hit_version() -> None:
    other_provision = _provision(
        provision_id=UUID("01a06ae2-6600-7000-8000-0000000000c7"),
        provision_no="第一条",
        full_text="第一条 另一版本内容。",
        version_id=VERSION_OTHER,
    )
    other_version = _version(
        version_id=VERSION_OTHER,
        label="2024 修正版",
        status=LegalVersionStatus.REPEALED,
        effective_on=date(2024, 1, 1),
        repealed_on=date(2025, 1, 1),
    )
    query = _Query(
        (
            _version(),
            _instrument(),
            (
                _provision(
                    provision_id=PROVISION_A,
                    provision_no="第一条",
                    full_text="第一条 2020 内容。",
                ),
            ),
        ),
        (other_version, _instrument(), (other_provision,)),
    )
    service = _service(query)
    bundle = await service.assemble(
        hits=(
            _hit(provision_id=other_provision.id, version_id=VERSION_OTHER, score=6.0),
            _hit(provision_id=PROVISION_A, score=5.0),
        )
    )
    assert bundle is not None
    first, second = bundle.items
    assert first.version_id == VERSION_OTHER
    assert first.version_label == "2024 修正版"
    assert first.status is LegalVersionStatus.REPEALED
    assert first.provision_text == "第一条 另一版本内容。"
    assert second.version_id == VERSION
    assert second.provision_text == "第一条 2020 内容。"
