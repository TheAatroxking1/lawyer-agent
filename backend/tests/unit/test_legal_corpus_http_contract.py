from __future__ import annotations

from types import SimpleNamespace

from lawyer_agent.api.v1.legal_corpus import (
    LegalVersionSummary,
    ProvisionSummary,
)
from lawyer_agent.application.legal_corpus_read import (
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


def test_version_not_found_code() -> None:
    error = LegalCorpusVersionNotFound()
    assert error.status == 404
    assert error.code == "legal_corpus_version_not_found"


def test_service_present_in_composition() -> None:
    placeholder = object()
    services = SimpleNamespace(legal_corpus_http=placeholder)
    assert services.legal_corpus_http is placeholder
