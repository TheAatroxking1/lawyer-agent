from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRequest,
    IdempotencyService,
    InvalidIdempotencyKey,
    InvalidIdempotencyRequest,
)
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.tenancy import (
    CreateTenantApplicationCommand,
    InvalidMemberCursor,
    InvalidStrongETag,
    MemberCollectionScope,
    MemberCursor,
    MemberCursorCodec,
    PreconditionRequired,
    RevokeMemberCommand,
    StrongETag,
    TenantService,
    TenantType,
    UpdateMemberCommand,
    UpdateTenantCommand,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import MembershipStatus
from lawyer_agent.infrastructure.persistence.repositories.audit import AuditRepository


def test_idempotency_key_is_bounded_safe_ascii_and_only_a_hash_is_prepared() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)
    key = "tenant-create-key-0001"

    prepared = service.prepare(
        IdempotencyRequest(
            key=key,
            method="post",
            canonical_route="/api/v1/tenants/",
            body=IdempotencyFingerprintPayload(
                values={"name": "合成律所", "tenant_type": "law_firm"},
                business_paths=frozenset({("name",), ("tenant_type",)}),
            ),
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
                    body=IdempotencyFingerprintPayload(values={}, business_paths=frozenset()),
                )
            )


def test_fingerprint_uses_explicit_secret_paths_without_dropping_business_codes() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)
    first = service.prepare(
        IdempotencyRequest(
            key="canonical-key-000001",
            method="POST",
            canonical_route="/api/v1/auth/register",
            body=IdempotencyFingerprintPayload(values={
                "profile": {"display_name": "甲", "password": "first-password"},
                "oauth": {"code": "one-time-code-a"},
                "role_code": "assistant",
                "department_code": "legal",
                "secretary_name": "甲秘书",
                "tokenization_strategy": "legal-structure-v1",
            }, business_paths=frozenset({
                ("profile", "display_name"), ("role_code",), ("department_code",),
                ("secretary_name",), ("tokenization_strategy",),
            }), secret_paths=frozenset({("profile", "password"), ("oauth", "code")})),
        )
    )
    same_business_request = service.prepare(
        IdempotencyRequest(
            key="canonical-key-000001",
            method="post",
            canonical_route="/api/v1/auth/register/",
            body=IdempotencyFingerprintPayload(values={
                "tokenization_strategy": "legal-structure-v1",
                "secretary_name": "甲秘书",
                "department_code": "legal",
                "role_code": "assistant",
                "oauth": {"code": "one-time-code-b"},
                "profile": {"password": "different-password", "display_name": "甲"},
            }, business_paths=frozenset({
                ("profile", "display_name"), ("role_code",), ("department_code",),
                ("secretary_name",), ("tokenization_strategy",),
            }), secret_paths=frozenset({("profile", "password"), ("oauth", "code")})),
        )
    )
    changed_business_request = service.prepare(
        IdempotencyRequest(
            key="canonical-key-000001",
            method="POST",
            canonical_route="/api/v1/auth/register",
            body=IdempotencyFingerprintPayload(values={
                "profile": {"display_name": "甲", "password": "different-password"},
                "oauth": {"code": "one-time-code-b"},
                "role_code": "tenant_admin",
                "department_code": "legal",
                "secretary_name": "甲秘书",
                "tokenization_strategy": "legal-structure-v1",
            }, business_paths=frozenset({
                ("profile", "display_name"), ("role_code",), ("department_code",),
                ("secretary_name",), ("tokenization_strategy",),
            }), secret_paths=frozenset({("profile", "password"), ("oauth", "code")})),
        )
    )

    assert first.request_fingerprint == same_business_request.request_fingerprint
    assert first.request_fingerprint != changed_business_request.request_fingerprint


def test_fingerprint_rejects_every_unclassified_request_field() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)

    with pytest.raises(InvalidIdempotencyRequest, match="unclassified"):
        service.prepare(
            IdempotencyRequest(
                key="canonical-key-000002",
                method="POST",
                canonical_route="/api/v1/auth/register",
                body=IdempotencyFingerprintPayload(
                    values={"display_name": "甲", "future_sensitive_value": "raw"},
                    business_paths=frozenset({("display_name",)}),
                ),
            )
        )


def test_fingerprint_business_paths_must_classify_exact_scalar_leaves() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)

    with pytest.raises(InvalidIdempotencyRequest, match="leaf"):
        service.prepare(
            IdempotencyRequest(
                key="canonical-key-000003",
                method="POST",
                canonical_route="/api/v1/auth/register",
                body=IdempotencyFingerprintPayload(
                    values={
                        "profile": {
                            "name": "甲",
                            "password": "synthetic-password-material",
                        }
                    },
                    business_paths=frozenset({("profile",)}),
                ),
            )
        )


def test_fingerprint_nested_sequences_require_an_explicit_safe_schema() -> None:
    service = IdempotencyService(key_hash_secret=b"k" * 32)

    with pytest.raises(InvalidIdempotencyRequest, match="sequence"):
        service.prepare(
            IdempotencyRequest(
                key="canonical-key-000004",
                method="POST",
                canonical_route="/api/v1/auth/register",
                body=IdempotencyFingerprintPayload(
                    values={"profiles": [{"name": "甲", "password": "raw"}]},
                    business_paths=frozenset({("profiles",)}),
                ),
            )
        )


def test_task_7_commands_and_raw_idempotency_material_do_not_leak_via_repr() -> None:
    raw_key = "raw-idempotency-key-0001"
    raw_password = "-".join(("synthetic", "password", "material"))
    payload = IdempotencyFingerprintPayload(
        values={"password": raw_password},
        business_paths=frozenset(),
        secret_paths=frozenset({("password",)}),
    )
    request = IdempotencyRequest(
        key=raw_key,
        method="POST",
        canonical_route="/api/v1/auth/register",
        body=payload,
    )
    audit = AuditContext(trace_id="trace-repr", client_ip_hash=None, user_agent_hash=None)
    commands = (
        CreateTenantApplicationCommand(
            new_uuid7(), "合成律所", TenantType.LAW_FIRM, raw_key, audit
        ),
        UpdateTenantCommand("合成律所", 1, raw_key, audit),
        UpdateMemberCommand(new_uuid7(), 1, raw_key, audit, status=MembershipStatus.SUSPENDED),
        RevokeMemberCommand(new_uuid7(), 1, raw_key, audit),
    )

    assert raw_key not in repr(request)
    assert raw_password not in repr(request)
    assert raw_password not in repr(payload)
    assert all(raw_key not in repr(command) for command in commands)
    assert {field.name for field in fields(IdempotencyRequest) if not field.repr} >= {"key", "body"}


def test_tenant_service_requires_a_production_authorization_cache_dependency() -> None:
    with pytest.raises((TypeError, ValueError), match="authorization_cache"):
        TenantService(  # type: ignore[call-arg]
            uow_factory=lambda: None,  # type: ignore[arg-type,return-value]
            idempotency=IdempotencyService(key_hash_secret=b"k" * 32),
            cursor_secret=b"c" * 32,
        )


def test_if_match_accepts_only_an_exact_strong_positive_version_etag() -> None:
    assert StrongETag.parse('"7"').version == 7
    assert StrongETag.format(7) == '"7"'
    assert StrongETag.parse(StrongETag.format((1 << 31) - 1)).version == (1 << 31) - 1

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
    with pytest.raises(InvalidStrongETag):
        StrongETag(1 << 31)
    with pytest.raises(InvalidStrongETag):
        StrongETag.format(1 << 31)


def test_member_cursor_is_opaque_signed_and_bound_to_the_tenant() -> None:
    codec = MemberCursorCodec(secret=b"c" * 32)
    tenant_id = new_uuid7()
    other_tenant_id = new_uuid7()
    membership_id = new_uuid7()
    created_at = datetime(2026, 9, 1, 8, 0, 0, 123456, tzinfo=UTC)
    scope = MemberCollectionScope(tenant_wide=True)

    encoded = codec.encode(
        tenant_id=tenant_id,
        collection_scope=scope,
        cursor=MemberCursor(created_at=created_at, membership_id=membership_id),
    )

    assert str(tenant_id) not in encoded
    assert str(membership_id) not in encoded
    assert codec.decode(
        tenant_id=tenant_id, collection_scope=scope, encoded=encoded
    ) == MemberCursor(
        created_at=created_at,
        membership_id=membership_id,
    )
    with pytest.raises(InvalidMemberCursor):
        codec.decode(
            tenant_id=other_tenant_id,
            collection_scope=scope,
            encoded=encoded,
        )
    with pytest.raises(InvalidMemberCursor, match="scope"):
        codec.decode(
            tenant_id=tenant_id,
            collection_scope=MemberCollectionScope(
                tenant_wide=False,
                department_ids=frozenset({new_uuid7()}),
            ),
            encoded=encoded,
        )
    with pytest.raises(InvalidMemberCursor):
        replacement = "A" if encoded[-1] != "A" else "B"
        codec.decode(
            tenant_id=tenant_id,
            collection_scope=scope,
            encoded=encoded[:-1] + replacement,
        )


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
