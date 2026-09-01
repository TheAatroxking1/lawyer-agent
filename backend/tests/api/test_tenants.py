from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from lawyer_agent.application.idempotency import (
    IdempotencyRequest,
    IdempotencyService,
    InvalidIdempotencyKey,
)
from lawyer_agent.application.tenancy import (
    InvalidMemberCursor,
    InvalidStrongETag,
    MemberCursor,
    MemberCursorCodec,
    PreconditionRequired,
    StrongETag,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository


def test_idempotency_key_is_bounded_safe_ascii_and_only_a_hash_is_prepared() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)
    key = "tenant-create-key-0001"

    prepared = service.prepare(
        IdempotencyRequest(
            key=key,
            method="post",
            canonical_route="/api/v1/tenants/",
            body={"name": "合成律所", "tenant_type": "law_firm"},
        )
    )

    assert len(prepared.key_hash) == 32
    assert key.encode("ascii") not in prepared.key_hash
    assert len(prepared.request_fingerprint) == 32
    assert prepared.canonical_method == "POST"
    assert prepared.canonical_route == "/api/v1/tenants"

    for invalid in (
        None,
        "short",
        "a" * 129,
        "包含中文的幂等键-000000000",
        "contains space 0000000000",
        "contains/slash/0000000",
    ):
        with pytest.raises(InvalidIdempotencyKey):
            service.prepare(
                IdempotencyRequest(
                    key=invalid,  # type: ignore[arg-type]
                    method="POST",
                    canonical_route="/api/v1/tenants",
                    body={},
                )
            )


def test_fingerprint_is_canonical_and_never_depends_on_secret_fields() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)
    first = service.prepare(
        IdempotencyRequest(
            key="canonical-key-000001",
            method="POST",
            canonical_route="/api/v1/auth/register",
            body={
                "profile": {"display_name": "甲", "password": "first-password"},
                "verification_code": "123456",
                "code": "one-time-code-a",
                "token": "raw-token-value",
                "tenant_type": "law_firm",
            },
        )
    )
    same_business_request = service.prepare(
        IdempotencyRequest(
            key="canonical-key-000001",
            method="post",
            canonical_route="/api/v1/auth/register/",
            body={
                "tenant_type": "law_firm",
                "token": "different-token",
                "verification_code": "654321",
                "code": "one-time-code-b",
                "profile": {"password": "different-password", "display_name": "甲"},
            },
        )
    )
    changed_business_request = service.prepare(
        IdempotencyRequest(
            key="canonical-key-000001",
            method="POST",
            canonical_route="/api/v1/auth/register",
            body={
                "profile": {"display_name": "乙", "password": "different-password"},
                "tenant_type": "law_firm",
            },
        )
    )

    assert first.request_fingerprint == same_business_request.request_fingerprint
    assert first.request_fingerprint != changed_business_request.request_fingerprint


def test_if_match_accepts_only_an_exact_strong_positive_version_etag() -> None:
    assert StrongETag.parse('"7"').version == 7
    assert StrongETag.format(7) == '"7"'

    with pytest.raises(PreconditionRequired):
        StrongETag.parse(None)
    for invalid in (
        "",
        "7",
        'W/"7"',
        '"0"',
        '"01"',
        '"-1"',
        '"7',
        '"7" extra',
        '"99999999999999999999"',
    ):
        with pytest.raises(InvalidStrongETag):
            StrongETag.parse(invalid)


def test_member_cursor_is_opaque_signed_and_bound_to_the_tenant() -> None:
    codec = MemberCursorCodec(secret=b"c" * 32)
    tenant_id = new_uuid7()
    other_tenant_id = new_uuid7()
    membership_id = new_uuid7()
    created_at = datetime(2026, 9, 1, 8, 0, 0, 123456, tzinfo=UTC)

    encoded = codec.encode(
        tenant_id=tenant_id,
        cursor=MemberCursor(created_at=created_at, membership_id=membership_id),
    )

    assert str(tenant_id) not in encoded
    assert str(membership_id) not in encoded
    assert codec.decode(tenant_id=tenant_id, encoded=encoded) == MemberCursor(
        created_at=created_at,
        membership_id=membership_id,
    )
    with pytest.raises(InvalidMemberCursor):
        codec.decode(tenant_id=other_tenant_id, encoded=encoded)
    with pytest.raises(InvalidMemberCursor):
        replacement = "A" if encoded[-1] != "A" else "B"
        codec.decode(tenant_id=tenant_id, encoded=encoded[:-1] + replacement)


def test_tenant_application_layer_has_no_framework_persistence_or_cache_imports() -> None:
    import lawyer_agent.application.idempotency as idempotency_module
    import lawyer_agent.application.tenancy as tenancy_module

    source = inspect.getsource(idempotency_module) + inspect.getsource(tenancy_module)
    assert "fastapi" not in source.lower()
    assert "sqlalchemy" not in source.lower()
    assert "import redis" not in source.lower()


def test_audit_repository_exposes_append_only_contract() -> None:
    assert hasattr(AuditRepository, "append")
    assert not hasattr(AuditRepository, "update")
    assert not hasattr(AuditRepository, "delete")
