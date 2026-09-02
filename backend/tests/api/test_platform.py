from __future__ import annotations

import inspect
import io
from dataclasses import fields

import pytest

from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.platform import (
    BootstrapCommittedWithCleanupWarning,
    BootstrapPlatformAdminCommand,
    BootstrapSecretVerifier,
    PlatformActor,
    PlatformReviewService,
    ReviewDecision,
    ReviewTenantApplicationCommand,
)
from lawyer_agent.cli.bootstrap_platform_admin import build_parser
from lawyer_agent.domain.authorization import Principal, PrincipalAudience
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.redis.step_up import StepUpGrant


def _principal() -> Principal:
    from datetime import UTC, datetime

    return Principal(
        user_id=new_uuid7(),
        session_id=new_uuid7(),
        audience=PrincipalAudience.PLATFORM,
        tenant_id=None,
        membership_id=None,
        user_status="active",
        session_valid=True,
        auth_version=1,
        session_auth_version=1,
        permissions=frozenset({"tenant_application.review"}),
        role_codes=frozenset({"super_admin"}),
        authenticated_at=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
    )


def test_platform_commands_hide_step_up_idempotency_and_bootstrap_secret() -> None:
    raw_grant = "D" * 43
    raw_key = "platform-review-key-0001"
    raw_secret = "synthetic-bootstrap-secret-material"  # noqa: S105
    review = ReviewTenantApplicationCommand(
        tenant_id=new_uuid7(),
        decision=ReviewDecision.APPROVE,
        reason_code="approved",
        step_up_grant=StepUpGrant(raw_grant),
        idempotency_key=raw_key,
        audit_context=AuditContext("trace-platform-review", None, None),
    )
    bootstrap = BootstrapPlatformAdminCommand(
        user_id=new_uuid7(),
        secret=raw_secret,
        audit_context=AuditContext("trace-bootstrap", None, None),
    )

    assert raw_grant not in repr(review)
    assert raw_key not in repr(review)
    assert raw_secret not in repr(bootstrap)
    assert {item.name for item in fields(ReviewTenantApplicationCommand) if not item.repr} >= {
        "step_up_grant",
        "idempotency_key",
    }
    assert {item.name for item in fields(BootstrapPlatformAdminCommand) if not item.repr} >= {
        "secret"
    }


def test_bootstrap_secret_verifier_is_constant_time_digest_based() -> None:
    verifier = BootstrapSecretVerifier.from_secret("E" * 32)

    assert verifier.verify("E" * 32)
    assert not verifier.verify("F" * 32)
    assert "E" * 32 not in repr(verifier)


def test_bootstrap_cli_exposes_no_secret_value_argument() -> None:
    parser = build_parser()
    option_strings = {
        option
        for action in parser._actions
        for option in action.option_strings
    }

    assert "--secret" not in option_strings
    assert "--bootstrap-secret" not in option_strings
    assert {"--secret-stdin", "--secret-file"} <= option_strings


def test_bootstrap_cli_treats_committed_cleanup_warning_as_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import lawyer_agent.cli.bootstrap_platform_admin as bootstrap_cli

    raw_secret = "K" * 32

    async def committed_with_cleanup_warning(**_: object) -> None:
        raise BootstrapCommittedWithCleanupWarning

    monkeypatch.setattr(bootstrap_cli, "_bootstrap", committed_with_cleanup_warning)
    monkeypatch.setenv("LAWYER_BOOTSTRAP_ADMIN_SECRET_DIGEST", "0" * 64)
    monkeypatch.setattr(bootstrap_cli.sys, "stdin", io.StringIO(raw_secret))

    exit_code = bootstrap_cli.main(
        ["--user-id", str(new_uuid7()), "--secret-stdin"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "completed" in captured.out
    assert "warning" in captured.out
    assert raw_secret not in captured.out + captured.err


def test_platform_actor_requires_platform_audience() -> None:
    actor = PlatformActor(_principal())
    assert actor.principal.audience is PrincipalAudience.PLATFORM

    with pytest.raises(ValueError, match="platform audience"):
        PlatformActor(
            _principal().__class__(
                **{
                    **{
                        field.name: getattr(_principal(), field.name)
                        for field in fields(Principal)
                    },
                    "audience": PrincipalAudience.TENANT,
                    "tenant_id": new_uuid7(),
                    "membership_id": new_uuid7(),
                }
            )
        )


def test_platform_application_layer_has_no_framework_or_persistence_imports() -> None:
    import lawyer_agent.application.platform as platform_module

    source = inspect.getsource(platform_module).lower()
    assert "fastapi" not in source
    assert "sqlalchemy" not in source
    assert "import redis" not in source


def test_platform_list_projection_and_repository_do_not_join_tenant_content() -> None:
    from lawyer_agent.application.platform import PlatformApplicationProjection
    from lawyer_agent.infrastructure.persistence.repositories.platform import (
        PlatformRepository,
    )

    assert {item.name for item in fields(PlatformApplicationProjection)} == {
        "tenant_id",
        "name",
        "tenant_type",
        "status",
        "review_status",
        "created_at",
        "version",
    }
    list_source = inspect.getsource(PlatformRepository.list_applications).lower()
    assert ".join(" not in list_source
    assert "tenantmodel" in list_source


@pytest.mark.asyncio
async def test_platform_review_entrypoints_validate_command_type_before_fields() -> None:
    service = object.__new__(PlatformReviewService)

    with pytest.raises(ValueError, match="strongly typed"):
        await service.approve(object(), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="strongly typed"):
        await service.reject(object(), object())  # type: ignore[arg-type]
