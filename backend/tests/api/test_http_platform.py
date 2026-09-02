from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import ApplicationServices
from lawyer_agent.application.platform import (
    PlatformApplicationProjection,
    PlatformReviewResult,
    StepUpRequired,
)
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import Audience, InvalidToken
from lawyer_agent.main import create_app


@dataclass(frozen=True, slots=True)
class _Validated:
    user_id: UUID
    session_id: UUID
    tenant_id: None = None
    membership_id: None = None


class _Sessions:
    def __init__(self) -> None:
        self.user_id = new_uuid7()
        self.session_id = new_uuid7()

    async def validate_access(self, encoded: str, *, audience: Audience) -> _Validated:
        from lawyer_agent.application.sessions import InvalidSession

        if encoded != "platform-token" or audience is not Audience.PLATFORM:
            raise InvalidSession
        return _Validated(self.user_id, self.session_id)


class _Tokens:
    def verify(self, token: str, *, audience: Audience, now: datetime) -> object:
        if token != "platform-token" or audience is not Audience.PLATFORM:  # noqa: S105
            raise InvalidToken
        return type(
            "Claims",
            (),
            {"auth_version": 1, "authz_version": None, "issued_at": now},
        )()


class _Platform:
    def __init__(self) -> None:
        self.application = PlatformApplicationProjection(
            new_uuid7(),
            "合成律所",
            "law_firm",
            "pending_verification",
            "pending",
            datetime.now(UTC),
            1,
        )

    async def list(
        self, actor: object, *, audit_context: object, limit: int
    ) -> tuple[PlatformApplicationProjection, ...]:
        del audit_context
        assert actor.principal.audience.value == "platform"
        assert limit == 100
        return (self.application,)

    async def approve(self, actor: object, command: object) -> PlatformReviewResult:
        return await self._review(actor, command, "approve")

    async def reject(self, actor: object, command: object) -> PlatformReviewResult:
        return await self._review(actor, command, "reject")

    async def _review(
        self, actor: object, command: object, expected: str
    ) -> PlatformReviewResult:
        assert actor.principal.audience.value == "platform"
        assert command.decision.value == expected
        if command.step_up_grant.value != "G" * 43:
            raise StepUpRequired
        return PlatformReviewResult(self.application, False)


def _client() -> tuple[TestClient, PlatformApplicationProjection]:
    platform = _Platform()
    services = ApplicationServices(
        identity=object(),
        sessions=_Sessions(),
        tenancy=object(),
        invitations=object(),
        platform=platform,
        rate_limiter=object(),
        step_up=object(),
        csrf=object(),
        token_service=_Tokens(),
        accounts=object(),
    )

    @asynccontextmanager
    async def factory(settings: Settings):
        del settings
        yield services

    settings = Settings(environment="test", secret_key="x" * 32)
    return TestClient(create_app(settings, service_factory=factory)), platform.application


def test_platform_list_and_step_up_review_contracts() -> None:
    client, application = _client()
    auth = {"Authorization": "Bearer platform-token"}
    with client:
        listed = client.get("/api/v1/platform/tenant-applications", headers=auth)
        missing_grant = client.post(
            f"/api/v1/platform/tenant-applications/{application.tenant_id}/approve",
            headers={**auth, "Idempotency-Key": "approve-1"},
            json={"reason_code": "verified"},
        )
        approved = client.post(
            f"/api/v1/platform/tenant-applications/{application.tenant_id}/approve",
            headers={
                **auth,
                "Idempotency-Key": "approve-1",
                "X-Step-Up-Grant": "G" * 43,
            },
            json={"reason_code": "verified"},
        )
        rejected = client.post(
            f"/api/v1/platform/tenant-applications/{application.tenant_id}/reject",
            headers={
                **auth,
                "Idempotency-Key": "reject-1",
                "X-Step-Up-Grant": "G" * 43,
            },
            json={"reason_code": "invalid_registration"},
        )
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert missing_grant.status_code == 422
    assert approved.status_code == 200
    assert rejected.status_code == 200
    assert "permissions" not in listed.text
    assert "role_codes" not in listed.text


def test_platform_rejects_invalid_or_wrongly_bound_step_up_grant() -> None:
    client, application = _client()
    with client:
        response = client.post(
            f"/api/v1/platform/tenant-applications/{application.tenant_id}/approve",
            headers={
                "Authorization": "Bearer platform-token",
                "Idempotency-Key": "approve-2",
                "X-Step-Up-Grant": "X" * 43,
            },
            json={"reason_code": "verified"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "step_up_required"
    assert "X" * 43 not in response.text
