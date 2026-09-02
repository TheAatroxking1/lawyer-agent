from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import ApplicationServices
from lawyer_agent.application.sessions import InvalidSession
from lawyer_agent.application.tenancy import TenantAuthorizationDenied, TenantResourceNotFound
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import Audience, InvalidToken
from lawyer_agent.main import create_app


@dataclass(frozen=True)
class _Validated:
    user_id: UUID
    session_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None


class _TenantSessions:
    def __init__(self, tenant_id: UUID, membership_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.membership_id = membership_id
        self.user_id = new_uuid7()
        self.session_id = new_uuid7()
        self.revoked = False

    async def validate_access(self, encoded: str, *, audience: object) -> _Validated:
        if self.revoked:
            raise InvalidSession
        expected = {
            Audience.ACCOUNT: "account-token",
            Audience.TENANT: "tenant-token",
            Audience.PLATFORM: "platform-token",
        }.get(audience)
        if encoded != expected:
            raise InvalidSession
        if audience is Audience.PLATFORM:
            return _Validated(self.user_id, self.session_id, None, None)
        return _Validated(
            self.user_id, self.session_id, self.tenant_id, self.membership_id
        )


class _Tenancy:
    def __init__(self) -> None:
        self.mode = "success"

    async def get(self, actor: object) -> object:
        if self.mode == "not_found":
            raise TenantResourceNotFound
        if self.mode == "denied":
            raise TenantAuthorizationDenied("permission_denied")
        return type(
            "Tenant",
            (),
            {
                "id": actor.context.tenant_id,
                "name": "合成租户",
                "tenant_type": "law_firm",
                "status": "active",
                "review_status": "approved",
                "version": 1,
            },
        )()


class _TokenService:
    def verify(self, token: str, *, audience: object, now: object) -> object:
        expected = {
            Audience.TENANT: "tenant-token",
            Audience.PLATFORM: "platform-token",
        }.get(audience)
        if token != expected:
            raise InvalidToken
        return type(
            "Claims",
            (),
            {"auth_version": 1, "authz_version": 1, "issued_at": now},
        )()


def _client() -> tuple[TestClient, _TenantSessions, _Tenancy, UUID]:
    tenant_a = new_uuid7()
    tenant_b = new_uuid7()
    sessions = _TenantSessions(tenant_a, new_uuid7())
    tenancy = _Tenancy()
    services = ApplicationServices(
        identity=object(),
        sessions=sessions,
        tenancy=tenancy,
        invitations=object(),
        platform=object(),
        rate_limiter=object(),
        step_up=object(),
        csrf=object(),
        token_service=_TokenService(),
        accounts=object(),
    )

    @asynccontextmanager
    async def service_factory(settings: Settings):
        del settings
        yield services

    settings = Settings(environment="test", secret_key="x" * 32)
    return (
        TestClient(create_app(settings, service_factory=service_factory)),
        sessions,
        tenancy,
        tenant_b,
    )


def test_tenant_a_token_cannot_read_tenant_b_and_service_is_not_called() -> None:
    client, _, tenancy, tenant_b = _client()
    tenancy.mode = "denied"
    with client:
        response = client.get(
            f"/api/v1/tenants/{tenant_b}",
            headers={"Authorization": "Bearer tenant-token"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "tenant_resource_not_found"


def test_same_tenant_known_resource_permission_denial_is_403() -> None:
    client, sessions, tenancy, _ = _client()
    tenancy.mode = "denied"
    with client:
        response = client.get(
            f"/api/v1/tenants/{sessions.tenant_id}",
            headers={"Authorization": "Bearer tenant-token"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "authorization_denied"


def test_same_tenant_unknown_resource_is_404() -> None:
    client, sessions, tenancy, _ = _client()
    tenancy.mode = "not_found"
    with client:
        response = client.get(
            f"/api/v1/tenants/{sessions.tenant_id}",
            headers={"Authorization": "Bearer tenant-token"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "tenant_resource_not_found"


def test_revoked_session_is_uniform_401() -> None:
    client, sessions, _, _ = _client()
    sessions.revoked = True
    with client:
        response = client.get(
            f"/api/v1/tenants/{sessions.tenant_id}",
            headers={"Authorization": "Bearer revoked-token"},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"


def test_tokens_are_rejected_from_path_and_query_surfaces() -> None:
    client, _, _, _ = _client()
    secret = "T" * 43
    with client:
        path = client.post(f"/api/v1/invitations/{secret}/accept")
        query = client.post(f"/api/v1/invitations/accept?token={secret}")
    assert path.status_code == 404
    assert query.status_code == 422
    assert secret not in path.text + query.text


def test_account_tenant_platform_and_step_up_credentials_are_not_interchangeable() -> None:
    client, sessions, _, _ = _client()
    with client:
        platform_with_account = client.get(
            "/api/v1/platform/tenant-applications",
            headers={"Authorization": "Bearer account-token"},
        )
        tenant_with_platform = client.get(
            f"/api/v1/tenants/{sessions.tenant_id}",
            headers={"Authorization": "Bearer platform-token"},
        )
        tenant_with_grant = client.get(
            f"/api/v1/tenants/{sessions.tenant_id}",
            headers={"Authorization": f"Bearer {'G' * 43}"},
        )
    for response in (platform_with_account, tenant_with_platform, tenant_with_grant):
        assert response.status_code == 401
        assert response.json()["code"] == "authentication_failed"
