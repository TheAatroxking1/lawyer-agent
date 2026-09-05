from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from lawyer_agent.api.v1.legal_corpus import (
    DatasetSnapshotSummary,
    LegalInstrumentPage,
    LegalInstrumentSummary,
    LegalVersionSummary,
    LoadBatchPage,
    LoadBatchSummary,
    ProvisionSummary,
    QualityIssueSummary,
)
from lawyer_agent.application.legal_corpus_diff import (
    LegalVersionDiffCrossInstrument,
    LegalVersionDiffVersionNotFound,
)
from lawyer_agent.application.legal_corpus_read import (
    LegalCorpusDatasetSnapshotNotFound,
    LegalCorpusInstrumentCursorInvalid,
    LegalCorpusInstrumentNotFound,
    LegalCorpusInvalidRequest,
    LegalCorpusLoadBatchNotFound,
    LegalCorpusVersionNotFound,
)
from lawyer_agent.domain.common import new_uuid7


def test_version_summary_round_trip() -> None:
    summary = LegalVersionSummary(
        id=new_uuid7(),
        instrument_id=new_uuid7(),
        version_label="2020 修正",
        status="current",
    )
    assert summary.version_label == "2020 修正"
    assert summary.status == "current"


def test_provision_summary_round_trip() -> None:
    summary = ProvisionSummary(
        id=new_uuid7(),
        version_id=new_uuid7(),
        provision_no="第一条",
        level="article",
        structure_path=(),
        full_text="第一条 为了保护民事权益，制定本法。",
    )
    assert summary.provision_no == "第一条"


def test_instrument_summary_round_trip() -> None:
    summary = LegalInstrumentSummary(
        id=new_uuid7(),
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
        region_code=None,
    )
    assert summary.title == "中华人民共和国民法典"
    assert summary.issuing_authority == "全国人民代表大会"


def test_version_not_found_code() -> None:
    error = LegalCorpusVersionNotFound()
    assert error.status == 404
    assert error.code == "legal_corpus_version_not_found"


def test_instrument_not_found_code() -> None:
    error = LegalCorpusInstrumentNotFound()
    assert error.status == 404
    assert error.code == "legal_corpus_instrument_not_found"


def test_diff_version_not_found_code() -> None:
    error = LegalVersionDiffVersionNotFound(new_uuid7())
    assert error.status == 404
    assert error.code == "legal_corpus_version_not_found"


def test_diff_cross_instrument_code() -> None:
    error = LegalVersionDiffCrossInstrument("cross-instrument diff is refused")
    assert error.status == 409
    assert error.code == "legal_version_diff_cross_instrument"


def test_service_present_in_composition() -> None:
    placeholder = object()
    services = SimpleNamespace(
        legal_corpus_http=placeholder,
        legal_version_diff_http=placeholder,
    )
    assert services.legal_corpus_http is placeholder
    assert services.legal_version_diff_http is placeholder


def test_instrument_page_round_trip() -> None:
    instrument_id = new_uuid7()
    summary = LegalInstrumentSummary(
        id=instrument_id,
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
        region_code=None,
    )
    page = LegalInstrumentPage(items=[summary], next_before_id=None)
    assert page.items[0].id == instrument_id
    assert page.next_before_id is None
    follow = LegalInstrumentPage(items=[], next_before_id=instrument_id)
    assert follow.next_before_id == UUID(str(instrument_id))


def test_instrument_invalid_request_code() -> None:
    error = LegalCorpusInvalidRequest()
    assert error.status == 422
    assert error.code == "legal_corpus_invalid_request"


def test_instrument_cursor_invalid_code() -> None:
    error = LegalCorpusInstrumentCursorInvalid()
    assert error.status == 404
    assert error.code == "legal_corpus_instrument_cursor_invalid"


def test_dataset_snapshot_summary_round_trip() -> None:
    released = datetime(2026, 5, 1, tzinfo=UTC)
    summary = DatasetSnapshotSummary(
        dataset_name="dataset_v1",
        parser_version="docx-v1",
        state="published",
        released_at=released,
        quality_metrics={
            "article_count": 134,
            "coverage": 1.0,
            "parse_failures": 0,
        },
    )
    assert summary.dataset_name == "dataset_v1"
    assert summary.state == "published"
    assert summary.released_at == released
    assert summary.quality_metrics["article_count"] == 134


def test_dataset_snapshot_not_found_code() -> None:
    error = LegalCorpusDatasetSnapshotNotFound()
    assert error.status == 404
    assert error.code == "legal_dataset_snapshot_not_found"


def test_load_batch_summary_round_trip() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    summary = LoadBatchSummary(
        id=new_uuid7(),
        batch_no="B20260101000000AB",
        source_ref="object://corpus/a.docx",
        parser_version="docx-v1",
        status="completed",
        item_counts={"files": 1, "unique": 1, "duplicates": 0},
        started_at=started,
        completed_at=started,
        error_message=None,
    )
    assert summary.batch_no.startswith("B2")
    assert summary.item_counts["unique"] == 1


def test_load_batch_page_round_trip() -> None:
    batch_id = new_uuid7()
    page = LoadBatchPage(items=[], next_before_id=batch_id)
    assert page.next_before_id == batch_id


def test_load_batch_not_found_code() -> None:
    error = LegalCorpusLoadBatchNotFound()
    assert error.status == 404
    assert error.code == "legal_corpus_load_batch_not_found"


def test_quality_issue_summary_round_trip() -> None:
    summary = QualityIssueSummary(
        issue_type="missing_required_fields",
        message="missing required fields",
    )
    assert summary.issue_type == "missing_required_fields"
    assert summary.message == "missing required fields"
