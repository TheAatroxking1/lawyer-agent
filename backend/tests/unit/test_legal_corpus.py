from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    DatasetSnapshot,
    DatasetState,
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    LoadBatch,
    LoadStatus,
    Provision,
    ProvisionLevel,
    QualityIssue,
    content_sha256,
)


def _instrument() -> LegalInstrument:
    return LegalInstrument(
        id=new_uuid7(),
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
    )


def _version(instrument: LegalInstrument) -> LegalVersion:
    return LegalVersion(
        id=new_uuid7(),
        instrument_id=instrument.id,
        version_label="2020-05-28 公布版",
        status=LegalVersionStatus.CURRENT,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        law_number="中华人民共和国主席令第四十五号",
        content_hash=bytes(32),
        dataset_version="dataset_v1",
        parser_version="docx-v1",
    )


def _provision(version: LegalVersion) -> Provision:
    text = "第一条 为了保护民事主体的合法权益，调整民事关系，制定本法。"
    return Provision(
        id=new_uuid7(),
        version_id=version.id,
        provision_no="第一条",
        level=ProvisionLevel.ARTICLE,
        structure_path=("第一编", "第一章", "第一条"),
        title=None,
        full_text=text,
        content_hash=content_sha256(text),
        char_start=0,
        char_end=len(text),
    )


def test_instrument_and_version_round_trip() -> None:
    instrument = _instrument()
    version = _version(instrument)
    assert version.instrument_id == instrument.id
    assert version.status is LegalVersionStatus.CURRENT
    assert version.effective_on == date(2021, 1, 1)


def test_unknown_status_must_not_be_current() -> None:
    instrument = _instrument()
    version = LegalVersion(
        id=new_uuid7(),
        instrument_id=instrument.id,
        version_label="2026 整理稿",
        status=LegalVersionStatus.STATUS_UNKNOWN,
        published_on=None,
        effective_on=None,
        repealed_on=None,
    )
    assert version.status is not LegalVersionStatus.CURRENT


def test_provision_content_hash_and_range() -> None:
    provision = _provision(_version(_instrument()))
    assert provision.content_hash == content_sha256(provision.full_text)


def test_provision_content_hash_mismatch_rejected() -> None:
    version = _version(_instrument())
    text = "第一条 内容。"
    with pytest.raises(ValueError, match="content hash mismatch"):
        Provision(
            id=new_uuid7(),
            version_id=version.id,
            provision_no="第一条",
            level=ProvisionLevel.ARTICLE,
            structure_path=("第一章",),
            title=None,
            full_text=text,
            content_hash=bytes(32),
            char_start=0,
            char_end=len(text),
        )


def test_chunk_round_trip() -> None:
    instrument = _instrument()
    version = _version(instrument)
    provision = _provision(version)
    chunk = LegalChunk(
        id=new_uuid7(),
        version_id=version.id,
        provision_id=provision.id,
        chunk_type=ChunkType.PROVISION,
        quality=ChunkQuality.OK,
        content=provision.full_text,
        content_hash=content_sha256(provision.full_text),
    )
    assert chunk.quality is ChunkQuality.OK
    assert chunk.content_hash == content_sha256(chunk.content)


def test_snapshot_and_batch() -> None:
    snapshot = DatasetSnapshot(
        id=new_uuid7(),
        dataset_name="dataset_v1",
        parser_version="docx-v1",
        state=DatasetState.PUBLISHED,
        manifest={"files": 1},
        quality_metrics={"coverage": 1.0},
        released_at=datetime.now(UTC),
    )
    batch = LoadBatch(
        id=new_uuid7(),
        batch_no="B0001",
        source_ref="object://corpus/0001.docx",
        file_sha256=bytes(32),
        parser_version="docx-v1",
        status=LoadStatus.COMPLETED,
        item_counts={"provisions": 1},
    )
    issue = QualityIssue(
        id=new_uuid7(),
        batch_id=batch.id,
        file_sha256=bytes(32),
        issue_type="missing_required_field",
        message="no effective date",
    )
    assert snapshot.state is DatasetState.PUBLISHED
    assert batch.item_counts["provisions"] == 1
    assert issue.issue_type == "missing_required_field"


def test_dataset_state_unknown_rejected() -> None:
    with pytest.raises(ValueError):
        DatasetSnapshot(
            id=new_uuid7(),
            dataset_name="dataset_v1",
            parser_version="docx-v1",
            state="oops",  # type: ignore[arg-type]
            manifest={},
            quality_metrics={},
        )
