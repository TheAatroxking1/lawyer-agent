from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.invitations import (
    AcceptInvitationCommand,
    CreateInvitationCommand,
    InvitationAcceptanceResult,
    InvitationResult,
    InvitationTargetKind,
    InvitationTokenHasher,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.providers.development import (
    TestInvitationDeliveryAdapter,
)

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def _audit() -> AuditContext:
    return AuditContext("trace-invitation", None, None)


def test_invitation_commands_keep_raw_credentials_out_of_repr() -> None:
    raw_token = "A" * 43
    raw_key = "invite-command-key-0001"
    accept = AcceptInvitationCommand(
        actor_user_id=new_uuid7(),
        token=raw_token,
        idempotency_key=raw_key,
        audit_context=_audit(),
    )
    create = CreateInvitationCommand(
        target_kind=InvitationTargetKind.EMAIL,
        target="synthetic@example.cn",
        role_ids=(new_uuid7(),),
        expires_at=NOW + timedelta(days=1),
        idempotency_key=raw_key,
        audit_context=_audit(),
    )

    assert raw_token not in repr(accept)
    assert raw_key not in repr(accept)
    assert raw_key not in repr(create)
    assert {item.name for item in fields(AcceptInvitationCommand) if not item.repr} >= {
        "token",
        "idempotency_key",
    }
    assert "token" not in {item.name for item in fields(InvitationResult)}
    assert "token" not in {item.name for item in fields(InvitationAcceptanceResult)}


def test_invitation_token_hash_is_purpose_isolated_and_never_contains_raw_value() -> None:
    hasher = InvitationTokenHasher(hmac_key=b"i" * 32)
    raw = "B" * 43

    digest = hasher.digest(raw)

    assert len(digest) == 32
    assert raw.encode() not in digest
    assert digest != InvitationTokenHasher(hmac_key=b"j" * 32).digest(raw)


@pytest.mark.asyncio
async def test_test_delivery_adapter_captures_once_only_in_test_environment() -> None:
    invitation_id = new_uuid7()
    adapter = TestInvitationDeliveryAdapter(environment="test")

    await adapter.deliver(invitation_id=invitation_id, token="C" * 43)

    assert adapter.take(invitation_id) == "C" * 43
    assert adapter.take(invitation_id) is None
    with pytest.raises(ValueError, match="test environment"):
        TestInvitationDeliveryAdapter(environment="development")


def test_invitation_application_layer_has_no_framework_or_persistence_imports() -> None:
    import lawyer_agent.application.invitations as invitation_module

    source = inspect.getsource(invitation_module).lower()
    assert "fastapi" not in source
    assert "sqlalchemy" not in source
    assert "import redis" not in source
