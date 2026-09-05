from __future__ import annotations

from datetime import date
from typing import Protocol
from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_publish import LegalCorpusQualityGate
from lawyer_agent.application.legal_dataset_gated_publish import (
    LegalDatasetGatePublishService,
)
from lawyer_agent.application.legal_index_publish import (
    DatasetPublishResult,
    LegalDatasetPublishError,
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

VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
INSTRUMENT = UUID("01a06ae2-6000-7000-8000-0000000000c1")


def _version() -> LegalVersion:
    return LegalVersion(
        id=VERSION,
        instrument_id=INSTRUMENT,
        version_label="2024 修正版",
        status=LegalVersionStatus.CURRENT,
        published_on=date(2024, 1, 1),
        effective_on=date(2024, 6, 1),
        repealed_on=None,
        content_hash=bytes(32),
    )


def _instrument() -> LegalInstrument:
    return LegalInstrument(
        id=INSTRUMENT,
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
    )


def _provision(number: str, index: int) -> Provision:
    text = f"{number} 条文内容。"
    return Provision(
        id=new_uuid7(),
        version_id=VERSION,
        provision_no=number,
        level=ProvisionLevel.ARTICLE,
        structure_path=(),
        title=None,
        full_text=text,
        content_hash=content_sha256(text),
        char_start=index * 20,
        char_end=index * 20 + len(text),
    )


class _Corpus(Protocol):
    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


class _FakeCorpus:
    def __init__(self, provisions: tuple[Provision, ...] = ()) -> None:
        self.provisions = provisions

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None:
        if version_id != VERSION:
            return None
        return _version(), _instrument()

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]:
        del version_id
        return self.provisions


class _FakePublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def publish_version(self, **kwargs: object) -> DatasetPublishResult:
        self.calls.append(kwargs)
        return DatasetPublishResult(
            index_name="legal_idx_v1",
            indexed_documents=2,
            previous_target=None,
        )


def _service(
    corpus: _FakeCorpus, publisher: _FakePublisher
) -> LegalDatasetGatePublishService:
    return LegalDatasetGatePublishService(
        gate=LegalCorpusQualityGate(),
        corpus=corpus,
        publish=publisher,
    )


async def test_gate_passes_and_delegates_with_arguments() -> None:
    corpus = _FakeCorpus(
        (_provision("第一条", 0), _provision("第二条", 1))
    )
    publisher = _FakePublisher()
    result = await _service(corpus, publisher).publish_version(
        version_id=VERSION,
        index_name="legal_idx_v1",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    assert result.indexed_documents == 2
    assert len(publisher.calls) == 1
    assert publisher.calls[0] == {
        "version_id": VERSION,
        "index_name": "legal_idx_v1",
        "alias": "dataset_v1",
        "model_ref": "m",
        "dimension": 8,
        "batch_size": 64,
    }


async def test_gate_refuses_empty_version_without_publishing() -> None:
    publisher = _FakePublisher()
    service = _service(_FakeCorpus(()), publisher)
    with pytest.raises(LegalDatasetPublishError, match="no_articles"):
        await service.publish_version(
            version_id=VERSION,
            index_name="legal_idx_v1",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    assert publisher.calls == []


async def test_gate_refuses_sequence_break_without_publishing() -> None:
    corpus = _FakeCorpus(
        (_provision("第一条", 0), _provision("第三条", 1))
    )
    publisher = _FakePublisher()
    with pytest.raises(LegalDatasetPublishError, match="sequence"):
        await _service(corpus, publisher).publish_version(
            version_id=VERSION,
            index_name="legal_idx_v1",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    assert publisher.calls == []


async def test_gate_refuses_missing_version() -> None:
    publisher = _FakePublisher()
    service = _service(_FakeCorpus(()), publisher)
    with pytest.raises(LegalDatasetPublishError, match="no legal version"):
        await service.publish_version(
            version_id=UUID("01a06ae2-9900-7000-8000-0000000000ff"),
            index_name="legal_idx_v1",
            alias="dataset_v1",
            model_ref="m",
            dimension=8,
        )
    assert publisher.calls == []


async def test_gate_allows_non_numeric_provision_labels_as_unknown() -> None:
    # A provision number that the article regex cannot parse is skipped, not fatal.
    corpus = _FakeCorpus(
        (
            _provision("第一条", 0),
            _provision("附则", 1),
        )
    )
    publisher = _FakePublisher()
    result = await _service(corpus, publisher).publish_version(
        version_id=VERSION,
        index_name="legal_idx_v1",
        alias="dataset_v1",
        model_ref="m",
        dimension=8,
    )
    assert result.indexed_documents == 2
    assert len(publisher.calls) == 1


async def test_gate_requires_typed_gate() -> None:
    with pytest.raises(ValueError, match="quality gate"):
        LegalDatasetGatePublishService(
            gate=object(),  # type: ignore[arg-type]
            corpus=_FakeCorpus(),
            publish=_FakePublisher(),
        )
