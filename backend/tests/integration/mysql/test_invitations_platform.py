from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.idempotency import IdempotencyService
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.invitations import (
    AcceptInvitationCommand,
    CreateInvitationCommand,
    InvitationService,
    InvitationTargetKind,
    InvitationTokenHasher,
    InvitationUnavailable,
)
from lawyer_agent.application.platform import (
    BootstrapAuthenticationFailed,
    BootstrapPlatformAdminCommand,
    BootstrapSecretVerifier,
    BootstrapUnavailable,
    PlatformActor,
    PlatformApplicationUnavailable,
    PlatformAuthorizationDenied,
    PlatformBootstrapService,
    PlatformReviewService,
    ReviewDecision,
    ReviewTenantApplicationCommand,
    StepUpRequired,
)
from lawyer_agent.application.tenancy import TenantActor, TenantResourceNotFound
from lawyer_agent.domain.authorization import AuthorizationScope, Principal, PrincipalAudience
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.invitations_uow import (
    SqlAlchemyInvitationWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    AuthSessionModel,
    IdempotencyRecordModel,
    MembershipRoleAssignmentModel,
    PlatformRoleAssignmentModel,
    PlatformRoleModel,
    TenantInvitationModel,
    TenantInvitationRoleAssignmentModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.platform_uow import (
    SqlAlchemyPlatformWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.repositories.invitations import (
    InvitationRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.tenant_workflows import (
    TenantRoleWorkflowRepository,
)
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog
from lawyer_agent.infrastructure.providers.development import TestInvitationDeliveryAdapter
from lawyer_agent.infrastructure.redis.step_up import StepUpGrant
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


@pytest.fixture(scope="module")
def migrated_mysql_url(mysql_url: URL) -> Iterator[URL]:
    command.upgrade(_alembic_config(mysql_url), "head")
    yield mysql_url


@pytest.fixture
async def database(
    migrated_mysql_url: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_mysql_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory.begin() as session:
            await session.execute(delete(AuditEventModel))
            await session.execute(delete(IdempotencyRecordModel))
            await session.execute(delete(TenantInvitationRoleAssignmentModel))
            await session.execute(delete(TenantInvitationModel))
            await session.execute(delete(AuthSessionModel))
            await session.execute(delete(MembershipRoleAssignmentModel))
            await session.execute(delete(TenantRolePermissionModel))
            await session.execute(delete(TenantRoleModel))
            await session.execute(delete(TenantMembershipModel))
            await session.execute(delete(PlatformRoleAssignmentModel))
            await session.execute(delete(TenantModel))
            await session.execute(delete(AuthIdentityModel))
            await session.execute(delete(UserModel))
        await engine.dispose()


def _audit(trace: str) -> AuditContext:
    return AuditContext(trace, b"p" * 32, b"u" * 32)


def _blind() -> BlindIndexService:
    return BlindIndexService({1: b"b" * 32}, active_key_version=1)


def _cipher() -> SensitiveValueCipher:
    return SensitiveValueCipher({1: b"c" * 32}, active_key_version=1)


async def _tenant_graph(
    database: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    owner_id = new_uuid7()
    invitee_id = new_uuid7()
    other_user_id = new_uuid7()
    tenant_a = new_uuid7()
    tenant_b = new_uuid7()
    owner_membership = new_uuid7()
    owner_session = new_uuid7()
    identity_id = new_uuid7()
    normalized_email = "invitee@example.cn"
    cipher = _cipher()
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add_all(
            [
                UserModel(id=owner_id, status="active", display_name="合成管理员"),
                UserModel(id=invitee_id, status="active", display_name="合成受邀人"),
                UserModel(id=other_user_id, status="active", display_name="其他用户"),
                TenantModel(
                    id=tenant_a,
                    name="甲租户",
                    normalized_name="甲租户",
                    tenant_type="law_firm",
                    status="active",
                    created_by_user_id=owner_id,
                    review_status="approved",
                ),
                TenantModel(
                    id=tenant_b,
                    name="乙租户",
                    normalized_name="乙租户",
                    tenant_type="enterprise",
                    status="active",
                    created_by_user_id=owner_id,
                    review_status="approved",
                ),
            ]
        )
        await session.flush()
        membership = TenantMembershipModel(
            id=owner_membership,
            tenant_id=tenant_a,
            user_id=owner_id,
            department_id=None,
            member_type="owner",
            status="active",
            valid_from=(NOW - timedelta(days=1)).replace(tzinfo=None),
            valid_until=None,
            authz_version=1,
        )
        session.add(membership)
        await session.flush()
        roles = TenantRoleWorkflowRepository(session)
        role_a = await roles.clone_templates_for_tenant(tenant_a)
        role_b = await roles.clone_templates_for_tenant(tenant_b)
        await roles.assign_owner(
            tenant_id=tenant_a,
            membership_id=owner_membership,
            owner_role_id=role_a["tenant_owner"],
            now=NOW,
        )
        session.add(
            AuthSessionModel(
                id=owner_session,
                user_id=owner_id,
                tenant_id=tenant_a,
                membership_id=owner_membership,
                current_family_id=new_uuid7(),
                auth_version_at_issue=1,
                authz_version_at_issue=1,
                revoked_at=None,
                revocation_reason=None,
                last_seen_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None),
            )
        )
        session.add(
            AuthIdentityModel(
                id=identity_id,
                user_id=invitee_id,
                kind="email",
                provider="email",
                issuer="email",
                display_value=None,
                subject_ciphertext=cipher.encrypt(
                    normalized_email,
                    aad=f"auth_identity:{identity_id}:subject".encode("ascii"),
                ),
                subject_blind_index=_blind().digest("identity:email", normalized_email),
                key_version=1,
                blind_index_key_version=1,
                verified_at=NOW.replace(tzinfo=None),
                status="active",
            )
        )
    context = TenantContext(
        tenant_id=tenant_a,
        membership_id=owner_membership,
        membership_user_id=owner_id,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=NOW - timedelta(days=1),
        valid_until=None,
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )
    principal = Principal(
        user_id=owner_id,
        session_id=owner_session,
        audience=PrincipalAudience.TENANT,
        tenant_id=tenant_a,
        membership_id=owner_membership,
        user_status="active",
        session_valid=True,
        auth_version=1,
        session_auth_version=1,
        permissions=frozenset(),
        role_codes=frozenset(),
        authenticated_at=NOW,
    )
    return {
        "owner_id": owner_id,
        "invitee_id": invitee_id,
        "other_user_id": other_user_id,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "role_assistant_a": role_a["assistant"],
        "role_assistant_b": role_b["assistant"],
        "actor": TenantActor(principal, context),
    }


def _invitation_service(
    database: async_sessionmaker[AsyncSession],
    delivery: TestInvitationDeliveryAdapter,
) -> InvitationService:
    return InvitationService(
        uow_factory=lambda: SqlAlchemyInvitationWorkflowUnitOfWork(database),
        idempotency=IdempotencyService(key_hash_secret=b"i" * 32),
        token_hasher=InvitationTokenHasher(hmac_key=b"t" * 32),
        blind_index=_blind(),
        cipher=_cipher(),
        delivery=delivery,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_invitation_create_accept_is_atomic_hashed_and_idempotent(
    database: async_sessionmaker[AsyncSession],
) -> None:
    graph = await _tenant_graph(database)
    delivery = TestInvitationDeliveryAdapter(environment="test")
    service = _invitation_service(database, delivery)
    created = await service.create(
        graph["actor"],  # type: ignore[arg-type]
        CreateInvitationCommand(
            target_kind=InvitationTargetKind.EMAIL,
            target=" Invitee@Example.cn ",
            role_ids=(graph["role_assistant_a"],),  # type: ignore[arg-type]
            expires_at=NOW + timedelta(days=1),
            idempotency_key="create-invitation-key-0001",
            audit_context=_audit("trace-invite-create"),
        ),
    )
    raw_token = delivery.take(created.invitation_id)
    assert raw_token is not None

    async with database() as session:
        stored = await session.get(TenantInvitationModel, created.invitation_id)
        assert stored is not None
        assert stored.token_hash == InvitationTokenHasher(hmac_key=b"t" * 32).digest(
            raw_token
        )
        assert raw_token.encode() not in stored.token_hash

    command = AcceptInvitationCommand(
        actor_user_id=graph["invitee_id"],  # type: ignore[arg-type]
        token=raw_token,
        idempotency_key="accept-invitation-key-0001",
        audit_context=_audit("trace-invite-accept"),
    )
    accepted = await service.accept(command)
    replayed = await service.accept(command)
    assert not accepted.replayed and replayed.replayed
    assert accepted.membership.user_id == graph["invitee_id"]
    assert accepted.role_ids == (graph["role_assistant_a"],)

    async with database() as session:
        stored = await session.get(TenantInvitationModel, created.invitation_id)
        assert stored is not None and stored.status == "accepted"
        assert await session.scalar(
            select(func.count()).select_from(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == graph["tenant_a"],
                TenantMembershipModel.user_id == graph["invitee_id"],
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(MembershipRoleAssignmentModel).where(
                MembershipRoleAssignmentModel.tenant_id == graph["tenant_a"],
                MembershipRoleAssignmentModel.membership_id == accepted.membership.id,
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.action.in_(("membership.invite", "invitation.accept"))
            )
        ) == 2
        repository = InvitationRepository(session)
        assert await repository.get_for_actor(
            tenant_id=graph["tenant_b"],  # type: ignore[arg-type]
            invitation_id=created.invitation_id,
        ) is None
        assert await repository.get_membership(
            tenant_id=graph["tenant_b"],  # type: ignore[arg-type]
            membership_id=accepted.membership.id,
        ) is None

    with pytest.raises(InvitationUnavailable):
        await service.accept(
            AcceptInvitationCommand(
                actor_user_id=graph["invitee_id"],  # type: ignore[arg-type]
                token=raw_token,
                idempotency_key="accept-invitation-key-0002",
                audit_context=_audit("trace-invite-reuse"),
            )
        )


@pytest.mark.asyncio
async def test_invitation_rejects_cross_tenant_role_and_identity_mismatch(
    database: async_sessionmaker[AsyncSession],
) -> None:
    graph = await _tenant_graph(database)
    service = _invitation_service(
        database, TestInvitationDeliveryAdapter(environment="test")
    )
    with pytest.raises(TenantResourceNotFound):
        await service.create(
            graph["actor"],  # type: ignore[arg-type]
            CreateInvitationCommand(
                target_kind=InvitationTargetKind.EMAIL,
                target="invitee@example.cn",
                role_ids=(graph["role_assistant_b"],),  # type: ignore[arg-type]
                expires_at=NOW + timedelta(days=1),
                idempotency_key="cross-tenant-role-key-0001",
                audit_context=_audit("trace-cross-role"),
            ),
        )
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(TenantInvitationModel)) == 0

    delivery = TestInvitationDeliveryAdapter(environment="test")
    service = _invitation_service(database, delivery)
    created = await service.create(
        graph["actor"],  # type: ignore[arg-type]
        CreateInvitationCommand(
            target_kind=InvitationTargetKind.EMAIL,
            target="invitee@example.cn",
            role_ids=(graph["role_assistant_a"],),  # type: ignore[arg-type]
            expires_at=NOW + timedelta(days=1),
            idempotency_key="identity-mismatch-create-0001",
            audit_context=_audit("trace-mismatch-create"),
        ),
    )
    token = delivery.take(created.invitation_id)
    assert token is not None
    with pytest.raises(InvitationUnavailable):
        await service.accept(
            AcceptInvitationCommand(
                actor_user_id=graph["other_user_id"],  # type: ignore[arg-type]
                token=token,
                idempotency_key="identity-mismatch-accept-0001",
                audit_context=_audit("trace-mismatch-accept"),
            )
        )
    async with database() as session:
        invitation = await session.get(TenantInvitationModel, created.invitation_id)
        assert invitation is not None and invitation.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["suspended", "closed"])
async def test_invitation_accept_rejects_inactive_target_tenant_without_side_effects(
    database: async_sessionmaker[AsyncSession],
    tenant_status: str,
) -> None:
    graph = await _tenant_graph(database)
    delivery = TestInvitationDeliveryAdapter(environment="test")
    service = _invitation_service(database, delivery)
    created = await service.create(
        graph["actor"],  # type: ignore[arg-type]
        CreateInvitationCommand(
            target_kind=InvitationTargetKind.EMAIL,
            target="invitee@example.cn",
            role_ids=(graph["role_assistant_a"],),  # type: ignore[arg-type]
            expires_at=NOW + timedelta(days=1),
            idempotency_key="suspended-tenant-create-0001",
            audit_context=_audit("trace-suspended-create"),
        ),
    )
    token = delivery.take(created.invitation_id)
    assert token is not None
    async with database.begin() as session:
        tenant = await session.get(TenantModel, graph["tenant_a"])
        assert tenant is not None
        tenant.status = tenant_status
    with pytest.raises(InvitationUnavailable):
        await service.accept(
            AcceptInvitationCommand(
                actor_user_id=graph["invitee_id"],  # type: ignore[arg-type]
                token=token,
                idempotency_key="suspended-tenant-accept-0001",
                audit_context=_audit("trace-suspended-accept"),
            )
        )
    async with database() as session:
        invitation = await session.get(TenantInvitationModel, created.invitation_id)
        assert invitation is not None and invitation.status == "pending"
        assert await session.scalar(
            select(func.count()).select_from(TenantMembershipModel).where(
                TenantMembershipModel.user_id == graph["invitee_id"]
            )
        ) == 0


@pytest.mark.asyncio
async def test_parallel_invitation_accept_has_exactly_one_effect(
    database: async_sessionmaker[AsyncSession],
) -> None:
    graph = await _tenant_graph(database)
    delivery = TestInvitationDeliveryAdapter(environment="test")
    service = _invitation_service(database, delivery)
    created = await service.create(
        graph["actor"],  # type: ignore[arg-type]
        CreateInvitationCommand(
            target_kind=InvitationTargetKind.EMAIL,
            target="invitee@example.cn",
            role_ids=(graph["role_assistant_a"],),  # type: ignore[arg-type]
            expires_at=NOW + timedelta(days=1),
            idempotency_key="parallel-create-key-0001",
            audit_context=_audit("trace-parallel-create"),
        ),
    )
    token = delivery.take(created.invitation_id)
    assert token is not None
    results = await asyncio.gather(
        *(
            service.accept(
                AcceptInvitationCommand(
                    actor_user_id=graph["invitee_id"],  # type: ignore[arg-type]
                    token=token,
                    idempotency_key=f"parallel-accept-key-000{index}",
                    audit_context=_audit(f"trace-parallel-{index}"),
                )
            )
            for index in (1, 2)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, BaseException) for value in results) == 1
    assert sum(isinstance(value, InvitationUnavailable) for value in results) == 1
    async with database() as session:
        assert await session.scalar(
            select(func.count()).select_from(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == graph["tenant_a"],
                TenantMembershipModel.user_id == graph["invitee_id"],
            )
        ) == 1


@pytest.mark.asyncio
async def test_invitation_accept_rolls_back_every_effect_when_role_assignment_fails(
    database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = await _tenant_graph(database)
    delivery = TestInvitationDeliveryAdapter(environment="test")
    service = _invitation_service(database, delivery)
    created = await service.create(
        graph["actor"],  # type: ignore[arg-type]
        CreateInvitationCommand(
            target_kind=InvitationTargetKind.EMAIL,
            target="invitee@example.cn",
            role_ids=(graph["role_assistant_a"],),  # type: ignore[arg-type]
            expires_at=NOW + timedelta(days=1),
            idempotency_key="rollback-create-key-0001",
            audit_context=_audit("trace-rollback-create"),
        ),
    )
    token = delivery.take(created.invitation_id)
    assert token is not None

    async def fail_role_assignment(
        self: InvitationRepository,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        role_ids: tuple[UUID, ...],
        assigned_by_membership_id: UUID,
        now: datetime,
    ) -> None:
        del self, tenant_id, membership_id, role_ids, assigned_by_membership_id, now
        raise RuntimeError("synthetic role persistence failure")

    monkeypatch.setattr(InvitationRepository, "assign_roles", fail_role_assignment)
    with pytest.raises(RuntimeError, match="synthetic role persistence failure"):
        await service.accept(
            AcceptInvitationCommand(
                actor_user_id=graph["invitee_id"],  # type: ignore[arg-type]
                token=token,
                idempotency_key="rollback-accept-key-0001",
                audit_context=_audit("trace-rollback-accept"),
            )
        )

    async with database() as session:
        invitation = await session.get(TenantInvitationModel, created.invitation_id)
        assert invitation is not None and invitation.status == "pending"
        assert await session.scalar(
            select(func.count()).select_from(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == graph["tenant_a"],
                TenantMembershipModel.user_id == graph["invitee_id"],
            )
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.action == "invitation.accept"
            )
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(IdempotencyRecordModel).where(
                IdempotencyRecordModel.operation == "invitation.accept"
            )
        ) == 0


class _SingleUseStepUp:
    def __init__(self) -> None:
        self._bindings: dict[str, tuple[UUID, UUID, UUID, str]] = {}

    def issue(
        self, *, user_id: UUID, session_id: UUID, tenant_id: UUID, action: str
    ) -> StepUpGrant:
        grant = StepUpGrant("G" * 43)
        self._bindings[grant.value] = (user_id, session_id, tenant_id, action)
        return grant

    async def consume(
        self,
        grant: StepUpGrant,
        *,
        user_id: UUID,
        session_id: UUID,
        tenant_id: UUID,
        action: str,
    ) -> bool:
        return self._bindings.pop(grant.value, None) == (
            user_id,
            session_id,
            tenant_id,
            action,
        )


async def _platform_graph(
    database: async_sessionmaker[AsyncSession], *, grant_review: bool = True
) -> tuple[PlatformActor, UUID]:
    reviewer = new_uuid7()
    session_id = new_uuid7()
    tenant_id = new_uuid7()
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add(UserModel(id=reviewer, status="active", display_name="平台审核员"))
        session.add(
            TenantModel(
                id=tenant_id,
                name="待审核租户",
                normalized_name="待审核租户",
                tenant_type="law_firm",
                status="pending_verification",
                created_by_user_id=reviewer,
                review_status="pending",
            )
        )
        await session.flush()
        role_code = "super_admin" if grant_review else "security_auditor"
        role_id = await session.scalar(
            select(PlatformRoleModel.id).where(PlatformRoleModel.code == role_code)
        )
        assert role_id is not None
        session.add(
            PlatformRoleAssignmentModel(
                id=new_uuid7(),
                user_id=reviewer,
                platform_role_id=role_id,
                assigned_by_user_id=None,
                status="active",
                expires_at=None,
                revoked_at=None,
                revocation_reason=None,
            )
        )
        session.add(
            AuthSessionModel(
                id=session_id,
                user_id=reviewer,
                tenant_id=None,
                membership_id=None,
                current_family_id=new_uuid7(),
                auth_version_at_issue=1,
                authz_version_at_issue=None,
                revoked_at=None,
                revocation_reason=None,
                last_seen_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None),
            )
        )
    principal = Principal(
        user_id=reviewer,
        session_id=session_id,
        audience=PrincipalAudience.PLATFORM,
        tenant_id=None,
        membership_id=None,
        user_status="active",
        session_valid=True,
        auth_version=1,
        session_auth_version=1,
        permissions=frozenset(),
        role_codes=frozenset(),
        authenticated_at=NOW,
    )
    return PlatformActor(principal), tenant_id


@pytest.mark.asyncio
async def test_platform_review_requires_authoritative_permission_and_bound_single_use_step_up(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor, tenant_id = await _platform_graph(database)
    step_up = _SingleUseStepUp()
    service = PlatformReviewService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(database),
        idempotency=IdempotencyService(key_hash_secret=b"i" * 32),
        step_up=step_up,
        clock=lambda: NOW,
    )
    listed = await service.list(actor, audit_context=_audit("trace-platform-list"))
    assert [(item.tenant_id, item.review_status) for item in listed] == [
        (tenant_id, "pending")
    ]
    wrong = step_up.issue(
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        tenant_id=tenant_id,
        action="tenant_application.review:reject",
    )
    with pytest.raises(StepUpRequired):
        await service.approve(
            actor,
            ReviewTenantApplicationCommand(
                tenant_id=tenant_id,
                decision=ReviewDecision.APPROVE,
                reason_code="approved",
                step_up_grant=wrong,
                idempotency_key="platform-approve-key-0001",
                audit_context=_audit("trace-platform-wrong-grant"),
            ),
        )
    wrong_tenant = step_up.issue(
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        tenant_id=new_uuid7(),
        action="tenant_application.review:approve",
    )
    with pytest.raises(StepUpRequired):
        await service.approve(
            actor,
            ReviewTenantApplicationCommand(
                tenant_id=tenant_id,
                decision=ReviewDecision.APPROVE,
                reason_code="approved",
                step_up_grant=wrong_tenant,
                idempotency_key="platform-approve-key-0001",
                audit_context=_audit("trace-platform-wrong-tenant-grant"),
            ),
        )
    correct = step_up.issue(
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        tenant_id=tenant_id,
        action="tenant_application.review:approve",
    )
    command_value = ReviewTenantApplicationCommand(
        tenant_id=tenant_id,
        decision=ReviewDecision.APPROVE,
        reason_code="approved",
        step_up_grant=correct,
        idempotency_key="platform-approve-key-0001",
        audit_context=_audit("trace-platform-approved"),
    )
    reviewed = await service.approve(actor, command_value)
    replayed = await service.approve(actor, command_value)
    assert reviewed.application.status == "active"
    assert reviewed.application.review_status == "approved"
    assert replayed.replayed

    async with database() as session:
        tenant = await session.get(TenantModel, tenant_id)
        assert tenant is not None and tenant.status == "active"
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.action == "tenant_application.approve"
            )
        ) == 1


@pytest.mark.asyncio
async def test_platform_review_rejects_read_only_platform_role(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor, tenant_id = await _platform_graph(database, grant_review=False)
    step_up = _SingleUseStepUp()
    service = PlatformReviewService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(database),
        idempotency=IdempotencyService(key_hash_secret=b"i" * 32),
        step_up=step_up,
        clock=lambda: NOW,
    )
    grant = step_up.issue(
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        tenant_id=tenant_id,
        action="tenant_application.review:approve",
    )
    with pytest.raises(PlatformAuthorizationDenied):
        await service.approve(
            actor,
            ReviewTenantApplicationCommand(
                tenant_id=tenant_id,
                decision=ReviewDecision.APPROVE,
                reason_code="approved",
                step_up_grant=grant,
                idempotency_key="read-only-review-key-0001",
                audit_context=_audit("trace-read-only-review"),
            ),
        )


@pytest.mark.asyncio
async def test_platform_reject_is_atomic_and_cannot_be_replayed_as_a_new_action(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor, tenant_id = await _platform_graph(database)
    step_up = _SingleUseStepUp()
    service = PlatformReviewService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(database),
        idempotency=IdempotencyService(key_hash_secret=b"i" * 32),
        step_up=step_up,
        clock=lambda: NOW,
    )
    grant = step_up.issue(
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        tenant_id=tenant_id,
        action="tenant_application.review:reject",
    )
    rejected = await service.reject(
        actor,
        ReviewTenantApplicationCommand(
            tenant_id=tenant_id,
            decision=ReviewDecision.REJECT,
            reason_code="invalid_application",
            step_up_grant=grant,
            idempotency_key="platform-reject-key-0001",
            audit_context=_audit("trace-platform-reject"),
        ),
    )
    assert rejected.application.status == "closed"
    assert rejected.application.review_status == "rejected"
    fresh_grant = step_up.issue(
        user_id=actor.principal.user_id,
        session_id=actor.principal.session_id,
        tenant_id=tenant_id,
        action="tenant_application.review:reject",
    )
    with pytest.raises(PlatformApplicationUnavailable):
        await service.reject(
            actor,
            ReviewTenantApplicationCommand(
                tenant_id=tenant_id,
                decision=ReviewDecision.REJECT,
                reason_code="invalid_application",
                step_up_grant=fresh_grant,
                idempotency_key="platform-reject-key-0002",
                audit_context=_audit("trace-platform-reject-replay"),
            ),
        )
    async with database() as session:
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.action == "tenant_application.reject"
            )
        ) == 1


@pytest.mark.asyncio
async def test_bootstrap_is_advisory_locked_audited_and_permanently_single_use(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = new_uuid7()
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add(UserModel(id=user_id, status="active", display_name="首个平台管理员"))
    service = PlatformBootstrapService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(database),
        verifier=BootstrapSecretVerifier.from_secret("S" * 32),
        clock=lambda: NOW,
    )
    command_value = BootstrapPlatformAdminCommand(
        user_id=user_id,
        secret="S" * 32,
        audit_context=_audit("trace-bootstrap"),
    )
    result = await service.bootstrap(command_value)
    assert result.user_id == user_id
    with pytest.raises(BootstrapUnavailable):
        await service.bootstrap(command_value)
    async with database() as session:
        assert await session.scalar(
            select(func.count()).select_from(PlatformRoleAssignmentModel).where(
                PlatformRoleAssignmentModel.user_id == user_id
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.action == "platform_admin.bootstrap"
            )
        ) == 1


@pytest.mark.asyncio
async def test_parallel_bootstrap_creates_exactly_one_super_admin(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = new_uuid7()
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add(UserModel(id=user_id, status="active", display_name="并发引导用户"))
    service = PlatformBootstrapService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(database),
        verifier=BootstrapSecretVerifier.from_secret("Q" * 32),
        clock=lambda: NOW,
    )
    results = await asyncio.gather(
        *(
            service.bootstrap(
                BootstrapPlatformAdminCommand(
                    user_id=user_id,
                    secret="Q" * 32,
                    audit_context=_audit(f"trace-bootstrap-{index}"),
                )
            )
            for index in (1, 2)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, BaseException) for value in results) == 1
    assert sum(isinstance(value, BootstrapUnavailable) for value in results) == 1


@pytest.mark.asyncio
async def test_bootstrap_wrong_secret_creates_no_assignment_or_audit(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = new_uuid7()
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add(UserModel(id=user_id, status="active", display_name="未授权引导用户"))
    service = PlatformBootstrapService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(database),
        verifier=BootstrapSecretVerifier.from_secret("V" * 32),
        clock=lambda: NOW,
    )
    with pytest.raises(BootstrapAuthenticationFailed):
        await service.bootstrap(
            BootstrapPlatformAdminCommand(
                user_id=user_id,
                secret="W" * 32,
                audit_context=_audit("trace-bootstrap-wrong-secret"),
            )
        )
    async with database() as session:
        assert await session.scalar(
            select(func.count()).select_from(PlatformRoleAssignmentModel)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(AuditEventModel).where(
                AuditEventModel.action == "platform_admin.bootstrap"
            )
        ) == 0
