from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import ApplicationServices
from lawyer_agent.application.invitations import (
    InvitationAcceptanceResult,
    InvitationDeliveryCapability,
    InvitationResult,
)
from lawyer_agent.application.tenancy import (
    MemberMutationResult,
    MemberPage,
    MemberPageItem,
    TenantApplicationResult,
    TenantMutationResult,
)
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import Audience, InvalidToken
from lawyer_agent.domain.tenancy import (
    Membership,
    MembershipStatus,
    MemberType,
    Tenant,
    TenantStatus,
)
from lawyer_agent.main import create_app


@dataclass(frozen=True, slots=True)
class _Validated:
    user_id: UUID
    session_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None


class _Sessions:
    def __init__(self, user_id: UUID, tenant_id: UUID, membership_id: UUID) -> None:
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.membership_id = membership_id
        self.session_id = new_uuid7()

    async def validate_access(self, encoded: str, *, audience: Audience) -> _Validated:
        if audience is Audience.ACCOUNT and encoded == "account-token":
            return _Validated(self.user_id, self.session_id, None, None)
        if audience is Audience.TENANT and encoded == "tenant-token":
            return _Validated(
                self.user_id,
                self.session_id,
                self.tenant_id,
                self.membership_id,
            )
        from lawyer_agent.application.sessions import InvalidSession

        raise InvalidSession


class _Tokens:
    def verify(self, token: str, *, audience: Audience, now: datetime) -> object:
        if token != "tenant-token" or audience is not Audience.TENANT:  # noqa: S105
            raise InvalidToken
        return type(
            "Claims",
            (),
            {"auth_version": 1, "authz_version": 1, "issued_at": now},
        )()


class _Tenancy:
    def __init__(self, tenant: Tenant, member: Membership) -> None:
        self.tenant = tenant
        self.member = member

    async def create_application(self, command: object) -> TenantApplicationResult:
        assert command.actor_user_id == self.member.user_id
        return TenantApplicationResult(self.tenant, self.member, False)

    async def get(self, actor: object) -> Tenant:
        assert actor.context.tenant_id == self.tenant.id
        return self.tenant

    async def update(self, actor: object, command: object) -> TenantMutationResult:
        assert actor.context.tenant_id == self.tenant.id
        assert command.expected_version == 1
        return TenantMutationResult(self.tenant, False, False)

    async def list_members(self, actor: object, query: object) -> MemberPage:
        assert actor.context.tenant_id == self.tenant.id
        assert query.limit == 50
        return MemberPage(
            (MemberPageItem(self.member, (new_uuid7(),), datetime.now(UTC)),),
            None,
        )

    async def update_member(self, actor: object, command: object) -> MemberMutationResult:
        assert actor.context.tenant_id == self.tenant.id
        assert command.membership_id == self.member.id
        return MemberMutationResult(self.member, command.role_ids or (), False)

    async def revoke_member(self, actor: object, command: object) -> MemberMutationResult:
        assert actor.context.tenant_id == self.tenant.id
        assert command.membership_id == self.member.id
        return MemberMutationResult(self.member, (), False)


class _Invitations:
    def __init__(
        self,
        tenant: Tenant,
        member: Membership,
        *,
        fail_create_if_called: bool = False,
    ) -> None:
        self.tenant = tenant
        self.member = member
        self.invitation_id = new_uuid7()
        self.fail_create_if_called = fail_create_if_called

    async def create(self, actor: object, command: object) -> InvitationResult:
        if self.fail_create_if_called:
            raise AssertionError("invitation service must not run without delivery capability")
        assert actor.context.tenant_id == self.tenant.id
        assert command.target == "invitee@example.cn"
        return InvitationResult(
            self.invitation_id,
            self.tenant.id,
            command.expires_at,
            "pending",
            False,
        )

    async def accept(self, command: object) -> InvitationAcceptanceResult:
        assert command.token == "T" * 43
        return InvitationAcceptanceResult(
            self.invitation_id,
            self.member,
            (new_uuid7(),),
            False,
        )


class _Delivery:
    async def deliver(self, *, invitation_id: UUID, token: str) -> None:
        del invitation_id, token


def _client(
    *, delivery_available: bool = True
) -> tuple[TestClient, Tenant, Membership]:
    now = datetime.now(UTC).replace(microsecond=0)
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    member = Membership(
        new_uuid7(),
        tenant_id,
        user_id,
        None,
        MemberType.OWNER,
        MembershipStatus.ACTIVE,
        now,
        None,
        1,
        1,
    )
    tenant = Tenant(
        tenant_id,
        "合成律所",
        "合成律所",
        "law_firm",
        TenantStatus.ACTIVE,
        1,
        user_id,
        "approved",
    )
    services = ApplicationServices(
        identity=object(),
        sessions=_Sessions(user_id, tenant_id, member.id),
        tenancy=_Tenancy(tenant, member),
        invitations=_Invitations(
            tenant,
            member,
            fail_create_if_called=not delivery_available,
        ),
        platform=object(),
        rate_limiter=object(),
        step_up=object(),
        csrf=object(),
        token_service=_Tokens(),
        accounts=object(),
        invitation_delivery=InvitationDeliveryCapability(
            _Delivery() if delivery_available else None
        ),
    )

    @asynccontextmanager
    async def factory(settings: Settings):
        del settings
        yield services

    settings = Settings(environment="test", secret_key="x" * 32)
    return (
        TestClient(
            create_app(settings, service_factory=factory),
            raise_server_exceptions=False,
        ),
        tenant,
        member,
    )


def test_tenant_member_and_invitation_http_contracts() -> None:
    client, tenant, member = _client()
    account = {"Authorization": "Bearer account-token", "Idempotency-Key": "create-1"}
    scoped = {"Authorization": "Bearer tenant-token", "Idempotency-Key": "write-1"}
    with client:
        created = client.post(
            "/api/v1/tenants",
            headers=account,
            json={"name": "合成律所", "tenant_type": "law_firm"},
        )
        read = client.get(
            f"/api/v1/tenants/{tenant.id}",
            headers={"Authorization": "Bearer tenant-token"},
        )
        precondition = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            headers=scoped,
            json={"name": "合成律所"},
        )
        updated = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            headers={**scoped, "If-Match": '"1"'},
            json={"name": "合成律所"},
        )
        members = client.get(
            f"/api/v1/tenants/{tenant.id}/members",
            headers={"Authorization": "Bearer tenant-token"},
        )
        invited = client.post(
            f"/api/v1/tenants/{tenant.id}/invitations",
            headers=scoped,
            json={
                "target_kind": "email",
                "target": "invitee@example.cn",
                "role_ids": [str(new_uuid7())],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        accepted = client.post(
            "/api/v1/invitations/accept",
            headers={**account, "Idempotency-Key": "accept-1"},
            json={"token": "T" * 43},
        )
        member_updated = client.patch(
            f"/api/v1/tenants/{tenant.id}/members/{member.id}",
            headers={**scoped, "If-Match": '"1"'},
            json={"status": "active", "role_ids": [str(new_uuid7())]},
        )
        member_deleted = client.delete(
            f"/api/v1/tenants/{tenant.id}/members/{member.id}",
            headers={**scoped, "If-Match": '"1"'},
        )

    assert created.status_code == 201
    assert created.json()["tenant"]["id"] == str(tenant.id)
    assert read.status_code == 200 and read.headers["ETag"] == '"1"'
    assert precondition.status_code == 428
    assert updated.status_code == 200 and updated.headers["ETag"] == '"1"'
    assert members.status_code == 200 and len(members.json()["items"]) == 1
    assert invited.status_code == 201
    assert "token" not in invited.text.lower()
    assert accepted.status_code == 200
    assert member_updated.status_code == 200
    assert member_deleted.status_code == 200
    assert all("authz_version" not in response.text for response in (created, members))


def test_write_bodies_forbid_unknown_fields_and_invitation_token_is_body_only() -> None:
    client, tenant, _ = _client()
    with client:
        extra = client.post(
            "/api/v1/tenants",
            headers={"Authorization": "Bearer account-token", "Idempotency-Key": "k"},
            json={"name": "合成律所", "tenant_type": "law_firm", "authz_version": 1},
        )
        query = client.post(
            f"/api/v1/invitations/accept?token={'T' * 43}",
            headers={"Authorization": "Bearer account-token", "Idempotency-Key": "k"},
        )
        cross = client.post(
            f"/api/v1/tenants/{new_uuid7()}/invitations",
            headers={"Authorization": "Bearer tenant-token", "Idempotency-Key": "k"},
            json={
                "target_kind": "email",
                "target": "invitee@example.cn",
                "role_ids": [str(new_uuid7())],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        blank_tenant = client.post(
            "/api/v1/tenants",
            headers={"Authorization": "Bearer account-token", "Idempotency-Key": "blank"},
            json={"name": "   ", "tenant_type": "law_firm"},
        )
        invalid_invitation_target = client.post(
            f"/api/v1/tenants/{tenant.id}/invitations",
            headers={
                "Authorization": "Bearer tenant-token",
                "Idempotency-Key": "invalid-target",
            },
            json={
                "target_kind": "email",
                "target": "not-an-email",
                "role_ids": [str(new_uuid7())],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    assert extra.status_code == 422
    assert query.status_code == 422
    assert cross.status_code == 404
    assert str(tenant.id) not in cross.text
    assert blank_tenant.status_code == 422
    assert invalid_invitation_target.status_code == 422


def test_invitation_creation_fails_before_service_when_delivery_is_unavailable() -> None:
    client, tenant, _ = _client(delivery_available=False)
    with client:
        response = client.post(
            f"/api/v1/tenants/{tenant.id}/invitations",
            headers={
                "Authorization": "Bearer tenant-token",
                "Idempotency-Key": "provider-missing",
            },
            json={
                "target_kind": "email",
                "target": "invitee@example.cn",
                "role_ids": [str(new_uuid7())],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "invitation_delivery_unavailable"
