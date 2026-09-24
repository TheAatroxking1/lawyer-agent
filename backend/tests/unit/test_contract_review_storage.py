from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import ReviewError
from lawyer_agent.infrastructure.contract_review.storage import (
    FailureLease,
    RunRecord,
    _failure_lease_digest,
    _validate_failure_lease,
    _validate_finish_payloads,
)
from lawyer_agent.infrastructure.persistence.models.contract_review import (
    ContractReviewRunModel,
)

RUN_ID = UUID("01995a80-0000-7000-8000-000000000001")
TENANT_ID = UUID("01995a80-0000-7000-8000-000000000002")
USER_ID = UUID("01995a80-0000-7000-8000-000000000003")
MEMBER_ID = UUID("01995a80-0000-7000-8000-000000000004")


def test_run_record_is_frozen_and_does_not_expose_idempotency_hash() -> None:
    record = RunRecord(
        id=RUN_ID,
        tenant_id=TENANT_ID,
        created_by_user_id=USER_ID,
        created_by_membership_id=MEMBER_ID,
        filename="contract.pdf",
        instruction="请审查",
        as_of=date(2026, 9, 16),
        status="awaiting_upload",
        pdf_object_key=None,
        pdf_sha256=None,
        pdf_size=None,
        parsed_document=None,
        result=None,
        evidence_manifest=None,
        failure_code=None,
        version=1,
        created_at=datetime(2026, 9, 16, tzinfo=UTC),
        updated_at=datetime(2026, 9, 16, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        record.status = "running"  # type: ignore[misc]
    assert "idempotency" not in record.__dataclass_fields__
    assert record.failure_lease is None


def test_failure_lease_secret_is_hidden_and_bound_to_tenant_run() -> None:
    lease = FailureLease(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        token=SecretStr("ab" * 32),
    )
    assert "abab" not in repr(lease)
    assert _failure_lease_digest(lease) == _failure_lease_digest(lease)
    other = FailureLease(tenant_id=TENANT_ID, run_id=RUN_ID, token=SecretStr("cd" * 32))
    assert _failure_lease_digest(other) != _failure_lease_digest(lease)


def test_failure_lease_rejects_fake_cross_scope_and_expired_capabilities() -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    lease = FailureLease(TENANT_ID, RUN_ID, SecretStr("ab" * 32))
    digest = _failure_lease_digest(lease)
    _validate_failure_lease(
        lease,
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        digest=digest,
        expires_at=now + timedelta(minutes=1),
        now=now,
    )

    invalid = (
        FailureLease(TENANT_ID, RUN_ID, SecretStr("cd" * 32)),
        FailureLease(UUID("01995a80-0000-7000-8000-000000000005"), RUN_ID, lease.token),
        FailureLease(TENANT_ID, UUID("01995a80-0000-7000-8000-000000000006"), lease.token),
    )
    for candidate in invalid:
        with pytest.raises(ReviewError, match="resource_unavailable"):
            _validate_failure_lease(
                candidate,
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                digest=digest,
                expires_at=now + timedelta(minutes=1),
                now=now,
            )
    with pytest.raises(ReviewError, match="resource_unavailable"):
        _validate_failure_lease(
            lease,
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            digest=digest,
            expires_at=now,
            now=now,
        )


def test_finish_payloads_are_bounded_and_evidence_is_metadata_only() -> None:
    parsed = {
        "document_version_id": "d" * 64,
        "page_dimensions": [[595.0, 842.0]],
        "blocks": [{"id": "b1", "text": "合成文本"}],
    }
    result = {"status": "draft", "issues": []}
    document = {
        "document_id": "a" * 64,
        "title": "民法典",
        "version": "b" * 64,
        "content_hash": "c" * 64,
        "source_ref": "offline-export-v1:" + "a" * 64,
        "quality_flags": [],
        "legal_metadata": {"status": "unknown"},
    }
    evidence = {"documents": [document], "model_calls": []}
    _validate_finish_payloads(result, parsed, evidence)
    _validate_finish_payloads(result, parsed, {**evidence, "mcp_tools": [{
        "name": "document_read_blocks", "capability": "document.read_blocks",
        "description": "读取合同文字块", "status": "discovered",
    }]})

    with pytest.raises(ValueError, match="evidence_manifest_invalid"):
        _validate_finish_payloads(
            result,
            parsed,
            {"documents": [{**document, "text": "不应持久化"}], "model_calls": []},
        )
    with pytest.raises(ValueError, match="evidence_manifest_invalid"):
        _validate_finish_payloads(
            result, parsed, {"documents": [], "model_calls": [], "mcp_tools": [{
                "name": "bad", "capability": "x", "description": "x", "status": "x",
                "schema": {"type": "object"},
            }]},
        )
    with pytest.raises(ValueError, match="evidence_manifest_invalid"):
        _validate_finish_payloads(result, parsed, {"documents": [document] * 4, "model_calls": []})
    with pytest.raises(ValueError, match="evidence_manifest_invalid"):
        _validate_finish_payloads(
            result,
            parsed,
            {"documents": [], "model_calls": [{"operation": "review", "raw": "secret"}]},
        )
    with pytest.raises(ValueError, match="parsed_document_too_large"):
        _validate_finish_payloads(
            result,
            {**parsed, "blocks": [{"text": "x" * 2_100_000}]},
            {"documents": [], "model_calls": []},
        )


def test_contract_review_model_has_tenant_owner_and_cas_fields() -> None:
    columns = ContractReviewRunModel.__table__.columns
    assert set(columns.keys()) >= {
        "id",
        "tenant_id",
        "created_by_user_id",
        "created_by_membership_id",
        "idempotency_hash",
        "request_fingerprint",
        "status",
        "pdf_object_key",
        "pdf_sha256",
        "pdf_size",
        "parsed_document",
        "result_json",
        "evidence_manifest",
        "failure_code",
        "failure_lease_hash",
        "failure_lease_expires_at",
        "version",
    }
    assert ContractReviewRunModel.__table__.c.idempotency_hash.type.length == 32
    assert ContractReviewRunModel.__table__.c.pdf_sha256.type.length == 32


def test_failure_manifest_allows_bounded_page_codes_but_never_model_text() -> None:
    from lawyer_agent.infrastructure.contract_review.storage import _validate_failure_manifest

    base = {"documents": [], "model_calls": []}
    _validate_failure_manifest({**base, "verification_report": {
        "pages": [{"page": 9, "reasons": ["unconfirmed_coverage"]}],
    }})
    for report in (
        {"pages": [{"page": 31, "reasons": ["missing_text"]}]},
        {"pages": [{"page": 1, "reasons": ["PRIVATE_RAW_MODEL_TEXT"]}]},
        {"pages": [{"page": 1, "reasons": ["missing_text"], "quote": "PRIVATE"}]},
        {"pages": [{"page": 1, "reasons": ["missing_text"]}] * 31},
    ):
        with pytest.raises(ValueError, match="evidence_manifest_invalid"):
            _validate_failure_manifest({**base, "verification_report": report})
