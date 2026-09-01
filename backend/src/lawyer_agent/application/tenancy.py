from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.application.idempotency import (
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.domain.authorization import (
    Action,
    PolicyEngine,
    Principal,
    ResourceAttributes,
    ResourceState,
)
from lawyer_agent.domain.common import is_uuid7, new_uuid7, require_uuid7
from lawyer_agent.domain.tenancy import (
    Membership,
    MembershipStatus,
    MemberType,
    Tenant,
    TenantContext,
    TenantStatus,
    normalize_tenant_name,
)

_STRONG_ETAG_PATTERN = re.compile(r'"([1-9][0-9]{0,18})"\Z', re.ASCII)
_MAX_SIGNED_INTEGER = (1 << 31) - 1
_CURSOR_SCHEMA_VERSION = 1


class PreconditionRequired(Exception):
    code = "precondition_required"


class InvalidStrongETag(ValueError):
    code = "invalid_if_match"


class InvalidMemberCursor(ValueError):
    code = "invalid_member_cursor"


class TenantType(StrEnum):
    LAW_FIRM = "law_firm"
    ENTERPRISE = "enterprise"
    UNIVERSITY = "university"


class RoleTemplateUnavailable(RuntimeError):
    code = "role_template_unavailable"

    def __init__(self) -> None:
        super().__init__("required tenant role templates are unavailable")


class TenantApplicantUnavailable(Exception):
    code = "tenant_applicant_unavailable"

    def __init__(self) -> None:
        super().__init__("tenant applicant is unavailable")


class IdempotentResultUnavailable(RuntimeError):
    code = "idempotent_result_unavailable"


class TenantResourceNotFound(Exception):
    code = "tenant_resource_not_found"

    def __init__(self) -> None:
        super().__init__("tenant resource was not found")


class VersionConflict(Exception):
    code = "version_conflict"

    def __init__(self) -> None:
        super().__init__("resource version conflicts with If-Match")


class TenantAuthorizationDenied(Exception):
    code = "authorization_denied"

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("tenant action is not authorized")


class InvalidMemberUpdate(ValueError):
    code = "invalid_member_update"


class PostCommitCacheInvalidationError(RuntimeError):
    code = "security_dependency_unavailable"
    committed = True

    def __init__(self, result: MemberMutationResult) -> None:
        self.result = result
        super().__init__("authorization cache invalidation failed after commit")


@dataclass(frozen=True, slots=True)
class CreateTenantApplicationCommand:
    actor_user_id: UUID
    name: str
    tenant_type: TenantType
    idempotency_key: str
    audit_context: AuditContext

    def __post_init__(self) -> None:
        require_uuid7(self.actor_user_id, field="actor_user_id")
        if not isinstance(self.tenant_type, TenantType):
            raise ValueError("tenant type must be strongly typed")
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class TenantApplicationResult:
    tenant: Tenant
    owner_membership: Membership
    replayed: bool


@dataclass(frozen=True, slots=True)
class TenantActor:
    principal: Principal
    context: TenantContext

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal) or not isinstance(
            self.context, TenantContext
        ):
            raise ValueError("tenant actor must contain strong principal and context types")


@dataclass(frozen=True, slots=True)
class UpdateTenantCommand:
    name: str
    expected_version: int
    idempotency_key: str
    audit_context: AuditContext

    def __post_init__(self) -> None:
        _require_positive_version(self.expected_version)
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class TenantMutationResult:
    tenant: Tenant
    replayed: bool


@dataclass(frozen=True, slots=True)
class UpdateMemberCommand:
    membership_id: UUID
    expected_version: int
    idempotency_key: str
    audit_context: AuditContext
    status: MembershipStatus | None = None
    role_ids: tuple[UUID, ...] | None = None
    change_department: bool = False
    department_id: UUID | None = None

    def __post_init__(self) -> None:
        require_uuid7(self.membership_id, field="membership_id")
        _require_positive_version(self.expected_version)
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")
        if self.status is not None and self.status not in {
            MembershipStatus.ACTIVE,
            MembershipStatus.SUSPENDED,
        }:
            raise InvalidMemberUpdate("PATCH membership status must be active or suspended")
        if self.role_ids is not None:
            if len(set(self.role_ids)) != len(self.role_ids):
                raise InvalidMemberUpdate("role_ids must not contain duplicates")
            for role_id in self.role_ids:
                require_uuid7(role_id, field="role_id")
        if self.department_id is not None:
            require_uuid7(self.department_id, field="department_id")
        if not self.change_department and self.department_id is not None:
            raise InvalidMemberUpdate("department_id requires change_department=true")
        if self.status is None and self.role_ids is None and not self.change_department:
            raise InvalidMemberUpdate("membership PATCH must change at least one field")


@dataclass(frozen=True, slots=True)
class RevokeMemberCommand:
    membership_id: UUID
    expected_version: int
    idempotency_key: str
    audit_context: AuditContext

    def __post_init__(self) -> None:
        require_uuid7(self.membership_id, field="membership_id")
        _require_positive_version(self.expected_version)
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class MemberMutationResult:
    membership: Membership
    role_ids: tuple[UUID, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class MemberPageQuery:
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise ValueError("member page limit must be between 1 and 100")
        if self.cursor is not None and (not isinstance(self.cursor, str) or not self.cursor):
            raise InvalidMemberCursor("member cursor is invalid")


@dataclass(frozen=True, slots=True)
class MemberPageItem:
    membership: Membership
    role_ids: tuple[UUID, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemberPage:
    items: tuple[MemberPageItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TenantAuthorizationSnapshot:
    tenant: Tenant
    actor_membership: Membership
    context: TenantContext
    user_status: str
    user_auth_version: int
    permissions: frozenset[str]
    role_codes: frozenset[str]
    target_memberships: Mapping[UUID, Membership]


class AuthorizationCacheInvalidationPort(Protocol):
    async def invalidate(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        authz_version: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TenantAuditEvent:
    id: UUID
    actor_user_id: UUID | None
    tenant_id: UUID | None
    actor_membership_id: UUID | None
    action: str
    result: str
    reason_code: str
    target_type: str | None
    target_id: UUID | None
    trace_id: str
    client_ip_hash: bytes | None
    user_agent_hash: bytes | None
    occurred_at: datetime


class TenantApplicationRepositoryPort(Protocol):
    async def lock_active_user(self, user_id: UUID) -> bool: ...

    async def add_application(self, tenant: Tenant) -> None: ...

    async def get_application_for_creator(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
    ) -> Tenant | None: ...

    async def flush(self) -> None: ...

    async def update_name(
        self,
        *,
        context: TenantContext,
        name: str,
        normalized_name: str,
        expected_version: int,
        now: datetime,
    ) -> Tenant | None: ...


class MembershipWorkflowRepositoryPort(Protocol):
    async def add_owner_for_application(self, membership: Membership) -> None: ...

    async def get_owner_for_application(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
    ) -> Membership | None: ...

    async def get(self, *, context: TenantContext, membership_id: UUID) -> Membership | None: ...

    async def update_locked(
        self,
        *,
        context: TenantContext,
        current: Membership,
        status: MembershipStatus,
        department_id: UUID | None,
        expected_version: int,
        now: datetime,
    ) -> Membership | None: ...

    async def department_exists(
        self, *, context: TenantContext, department_id: UUID
    ) -> bool: ...

    async def list_page(
        self,
        *,
        context: TenantContext,
        after: MemberCursor | None,
        limit: int,
    ) -> tuple[MemberPageItem, ...]: ...


class TenantRoleWorkflowRepositoryPort(Protocol):
    async def clone_templates_for_tenant(self, tenant_id: UUID) -> Mapping[str, UUID]: ...

    async def assign_owner(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        owner_role_id: UUID,
        now: datetime,
    ) -> None: ...

    async def role_ids(
        self, *, context: TenantContext, membership_id: UUID
    ) -> tuple[UUID, ...]: ...

    async def roles_exist(
        self, *, context: TenantContext, role_ids: tuple[UUID, ...]
    ) -> bool: ...

    async def replace_roles(
        self,
        *,
        context: TenantContext,
        membership_id: UUID,
        role_ids: tuple[UUID, ...],
        assigned_by_membership_id: UUID,
        now: datetime,
    ) -> None: ...


class TenantAuthorizationWorkflowRepositoryPort(Protocol):
    async def load_snapshot(
        self,
        *,
        context: TenantContext,
        target_membership_ids: tuple[UUID, ...] = (),
        for_update: bool = False,
    ) -> TenantAuthorizationSnapshot | None: ...


class TenantSessionRevocationRepositoryPort(Protocol):
    async def revoke_for_membership(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        reason: str,
        now: datetime,
    ) -> None: ...


class TenantAuditRepositoryPort(Protocol):
    async def append(self, event: TenantAuditEvent) -> None: ...


class TenantWorkflowUnitOfWork(Protocol):
    tenants: TenantApplicationRepositoryPort
    memberships: MembershipWorkflowRepositoryPort
    roles: TenantRoleWorkflowRepositoryPort
    authorization: TenantAuthorizationWorkflowRepositoryPort
    sessions: TenantSessionRevocationRepositoryPort
    idempotency: IdempotencyRepositoryPort
    audit: TenantAuditRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class TenantService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], TenantWorkflowUnitOfWork],
        idempotency: IdempotencyService,
        cursor_secret: bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        authorization_cache: AuthorizationCacheInvalidationPort | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        if not callable(uow_factory) or not isinstance(idempotency, IdempotencyService):
            raise ValueError("tenant service dependencies are invalid")
        self._uow_factory = uow_factory
        self._idempotency = idempotency
        self._cursor_codec = MemberCursorCodec(secret=cursor_secret)
        self._clock = clock
        self._authorization_cache = authorization_cache
        self._policy = policy or PolicyEngine()

    async def create_application(
        self,
        command: CreateTenantApplicationCommand,
    ) -> TenantApplicationResult:
        if not isinstance(command, CreateTenantApplicationCommand):
            raise ValueError("tenant application command must be strongly typed")
        normalized_name = normalize_tenant_name(command.name)
        now = self._now()
        async with self._uow_factory() as uow:
            if not await uow.tenants.lock_active_user(command.actor_user_id):
                raise TenantApplicantUnavailable
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=IdempotencyScope(
                    IdempotencyScopeType.USER,
                    command.actor_user_id,
                ),
                operation="tenant.create",
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="POST",
                    canonical_route="/api/v1/tenants",
                    body={
                        "name": normalized_name.display_value,
                        "tenant_type": command.tenant_type.value,
                    },
                ),
                now=now,
            )
            if reservation.replay is not None:
                return await self._reload_application_result(
                    uow,
                    actor_user_id=command.actor_user_id,
                    reference=reservation.replay,
                )

            tenant = Tenant(
                id=new_uuid7(),
                name=normalized_name.display_value,
                normalized_name=normalized_name.normalized_value,
                tenant_type=command.tenant_type.value,
                status=TenantStatus.PENDING_VERIFICATION,
                version=1,
                created_by_user_id=command.actor_user_id,
                review_status="pending",
            )
            await uow.tenants.add_application(tenant)
            await uow.tenants.flush()
            role_ids = await uow.roles.clone_templates_for_tenant(tenant.id)
            owner_role_id = role_ids.get("tenant_owner")
            if not isinstance(owner_role_id, UUID):
                raise RoleTemplateUnavailable
            owner = Membership(
                id=new_uuid7(),
                tenant_id=tenant.id,
                user_id=command.actor_user_id,
                department_id=None,
                member_type=MemberType.OWNER,
                status=MembershipStatus.ACTIVE,
                valid_from=now,
                valid_until=None,
                authz_version=1,
                version=1,
            )
            await uow.memberships.add_owner_for_application(owner)
            await uow.tenants.flush()
            await uow.roles.assign_owner(
                tenant_id=tenant.id,
                membership_id=owner.id,
                owner_role_id=owner_role_id,
                now=now,
            )
            await uow.audit.append(
                _tenant_audit_event(
                    command.audit_context,
                    actor_user_id=command.actor_user_id,
                    tenant_id=tenant.id,
                    actor_membership_id=owner.id,
                    action="tenant.application.create",
                    reason_code="pending_verification",
                    target_type="tenant",
                    target_id=tenant.id,
                    now=now,
                )
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference("tenant", tenant.id),
                now=now,
            )
            await uow.tenants.flush()
            return TenantApplicationResult(tenant, owner, False)

    async def get(self, actor: TenantActor) -> Tenant:
        async with self._uow_factory() as uow:
            snapshot = await self._require_snapshot(uow, actor, for_update=False)
            self._authorize(actor, snapshot, Action.TENANT_READ)
            return snapshot.tenant

    async def update(
        self,
        actor: TenantActor,
        command: UpdateTenantCommand,
    ) -> TenantMutationResult:
        if not isinstance(command, UpdateTenantCommand):
            raise ValueError("tenant update command must be strongly typed")
        normalized = normalize_tenant_name(command.name)
        now = self._now()
        async with self._uow_factory() as uow:
            initial = await self._require_snapshot(uow, actor, for_update=False)
            self._authorize(actor, initial, Action.TENANT_UPDATE)
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=_membership_scope(actor),
                operation="tenant.update",
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="PATCH",
                    canonical_route=f"/api/v1/tenants/{actor.context.tenant_id}",
                    body={
                        "name": normalized.display_value,
                        "if_match": command.expected_version,
                    },
                ),
                now=now,
            )
            if reservation.replay is not None:
                if (
                    reservation.replay.result_type != "tenant"
                    or reservation.replay.result_id != actor.context.tenant_id
                ):
                    raise IdempotentResultUnavailable("tenant update replay is invalid")
                replay = await uow.tenants.get_application_for_creator(
                    user_id=initial.tenant.created_by_user_id,
                    tenant_id=actor.context.tenant_id,
                )
                if replay is None:
                    raise IdempotentResultUnavailable("tenant update result is unavailable")
                return TenantMutationResult(replay, True)

            locked = await self._require_snapshot(uow, actor, for_update=True)
            self._authorize(actor, locked, Action.TENANT_UPDATE)
            updated = await uow.tenants.update_name(
                context=locked.context,
                name=normalized.display_value,
                normalized_name=normalized.normalized_value,
                expected_version=command.expected_version,
                now=now,
            )
            if updated is None:
                raise VersionConflict
            await uow.audit.append(
                _tenant_audit_event(
                    command.audit_context,
                    actor_user_id=actor.principal.user_id,
                    tenant_id=updated.id,
                    actor_membership_id=locked.actor_membership.id,
                    action="tenant.update",
                    reason_code="tenant_updated",
                    target_type="tenant",
                    target_id=updated.id,
                    now=now,
                )
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference("tenant", updated.id),
                now=now,
            )
            await uow.tenants.flush()
            return TenantMutationResult(updated, False)

    async def list_members(
        self,
        actor: TenantActor,
        query: MemberPageQuery,
    ) -> MemberPage:
        if not isinstance(query, MemberPageQuery):
            raise ValueError("member page query must be strongly typed")
        async with self._uow_factory() as uow:
            snapshot = await self._require_snapshot(uow, actor, for_update=False)
            self._authorize(actor, snapshot, Action.MEMBERSHIP_READ)
            after = (
                None
                if query.cursor is None
                else self._cursor_codec.decode(
                    tenant_id=actor.context.tenant_id,
                    encoded=query.cursor,
                )
            )
            rows = await uow.memberships.list_page(
                context=snapshot.context,
                after=after,
                limit=query.limit + 1,
            )
            has_more = len(rows) > query.limit
            items = rows[: query.limit]
            next_cursor = None
            if has_more:
                last = items[-1]
                next_cursor = self._cursor_codec.encode(
                    tenant_id=actor.context.tenant_id,
                    cursor=MemberCursor(last.created_at, last.membership.id),
                )
            return MemberPage(items=items, next_cursor=next_cursor)

    async def update_member(
        self,
        actor: TenantActor,
        command: UpdateMemberCommand,
    ) -> MemberMutationResult:
        if not isinstance(command, UpdateMemberCommand):
            raise ValueError("member update command must be strongly typed")
        return await self._mutate_member(
            actor,
            membership_id=command.membership_id,
            expected_version=command.expected_version,
            idempotency_key=command.idempotency_key,
            audit_context=command.audit_context,
            status=command.status,
            role_ids=command.role_ids,
            change_department=command.change_department,
            department_id=command.department_id,
            operation="membership.update",
            method="PATCH",
            action=Action.MEMBERSHIP_UPDATE,
            revocation_reason="authorization_changed",
        )

    async def revoke_member(
        self,
        actor: TenantActor,
        command: RevokeMemberCommand,
    ) -> MemberMutationResult:
        if not isinstance(command, RevokeMemberCommand):
            raise ValueError("member revocation command must be strongly typed")
        return await self._mutate_member(
            actor,
            membership_id=command.membership_id,
            expected_version=command.expected_version,
            idempotency_key=command.idempotency_key,
            audit_context=command.audit_context,
            status=MembershipStatus.REVOKED,
            role_ids=None,
            change_department=False,
            department_id=None,
            operation="membership.revoke",
            method="DELETE",
            action=Action.MEMBERSHIP_REVOKE,
            revocation_reason="membership_revoked",
        )

    async def _mutate_member(
        self,
        actor: TenantActor,
        *,
        membership_id: UUID,
        expected_version: int,
        idempotency_key: str,
        audit_context: AuditContext,
        status: MembershipStatus | None,
        role_ids: tuple[UUID, ...] | None,
        change_department: bool,
        department_id: UUID | None,
        operation: str,
        method: str,
        action: Action,
        revocation_reason: str,
    ) -> MemberMutationResult:
        now = self._now()
        desired_role_ids = None if role_ids is None else tuple(sorted(role_ids, key=str))
        old_authz_version: int | None = None
        changed = False
        result: MemberMutationResult
        async with self._uow_factory() as uow:
            initial = await self._require_snapshot(uow, actor, for_update=False)
            self._authorize(actor, initial, action)
            if desired_role_ids is not None:
                self._authorize(actor, initial, Action.ROLE_ASSIGN)
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=_membership_scope(actor),
                operation=operation,
                request=IdempotencyRequest(
                    key=idempotency_key,
                    method=method,
                    canonical_route=(
                        f"/api/v1/tenants/{actor.context.tenant_id}/members/{membership_id}"
                    ),
                    body={
                        "status": None if status is None else status.value,
                        "role_ids": (
                            None
                            if desired_role_ids is None
                            else [str(value) for value in desired_role_ids]
                        ),
                        "change_department": change_department,
                        "department_id": None if department_id is None else str(department_id),
                        "if_match": expected_version,
                    },
                ),
                now=now,
            )
            if reservation.replay is not None:
                if (
                    reservation.replay.result_type != "membership"
                    or reservation.replay.result_id != membership_id
                ):
                    raise IdempotentResultUnavailable("membership replay is invalid")
                replay_member = await uow.memberships.get(
                    context=initial.context,
                    membership_id=membership_id,
                )
                if replay_member is None:
                    raise IdempotentResultUnavailable("membership replay is unavailable")
                replay_roles = await uow.roles.role_ids(
                    context=initial.context,
                    membership_id=membership_id,
                )
                return MemberMutationResult(replay_member, replay_roles, True)

            locked = await self._require_snapshot(
                uow,
                actor,
                target_membership_ids=(membership_id,),
                for_update=True,
            )
            self._authorize(actor, locked, action)
            if desired_role_ids is not None:
                self._authorize(actor, locked, Action.ROLE_ASSIGN)
            current = locked.target_memberships.get(membership_id)
            if current is None:
                raise TenantResourceNotFound
            if current.version != expected_version:
                raise VersionConflict
            current_roles = await uow.roles.role_ids(
                context=locked.context,
                membership_id=membership_id,
            )
            if desired_role_ids is not None and not await uow.roles.roles_exist(
                context=locked.context,
                role_ids=desired_role_ids,
            ):
                raise TenantResourceNotFound
            if (
                change_department
                and department_id is not None
                and not await uow.memberships.department_exists(
                    context=locked.context,
                    department_id=department_id,
                )
            ):
                raise TenantResourceNotFound

            desired_status = current.status if status is None else status
            desired_department = current.department_id if not change_department else department_id
            desired_roles = current_roles if desired_role_ids is None else desired_role_ids
            changed = (
                desired_status is not current.status
                or desired_department != current.department_id
                or desired_roles != current_roles
            )
            updated = current
            if changed:
                old_authz_version = current.authz_version
                stored = await uow.memberships.update_locked(
                    context=locked.context,
                    current=current,
                    status=desired_status,
                    department_id=desired_department,
                    expected_version=expected_version,
                    now=now,
                )
                if stored is None:
                    raise VersionConflict
                updated = stored
                if desired_roles != current_roles:
                    await uow.roles.replace_roles(
                        context=locked.context,
                        membership_id=membership_id,
                        role_ids=desired_roles,
                        assigned_by_membership_id=locked.actor_membership.id,
                        now=now,
                    )
                await uow.sessions.revoke_for_membership(
                    tenant_id=actor.context.tenant_id,
                    membership_id=membership_id,
                    reason=revocation_reason,
                    now=now,
                )
            await uow.audit.append(
                _tenant_audit_event(
                    audit_context,
                    actor_user_id=actor.principal.user_id,
                    tenant_id=actor.context.tenant_id,
                    actor_membership_id=locked.actor_membership.id,
                    action=operation,
                    reason_code=("changed" if changed else "no_change"),
                    target_type="membership",
                    target_id=membership_id,
                    now=now,
                )
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference("membership", membership_id),
                now=now,
            )
            await uow.tenants.flush()
            result = MemberMutationResult(updated, desired_roles, False)

        if changed and old_authz_version is not None:
            await self._invalidate_authorization_cache(
                tenant_id=actor.context.tenant_id,
                membership_id=membership_id,
                authz_version=old_authz_version,
                result=result,
            )
        return result

    async def _require_snapshot(
        self,
        uow: TenantWorkflowUnitOfWork,
        actor: TenantActor,
        *,
        target_membership_ids: tuple[UUID, ...] = (),
        for_update: bool,
    ) -> TenantAuthorizationSnapshot:
        if not isinstance(actor, TenantActor):
            raise ValueError("tenant actor must be strongly typed")
        snapshot = await uow.authorization.load_snapshot(
            context=actor.context,
            target_membership_ids=target_membership_ids,
            for_update=for_update,
        )
        if snapshot is None:
            raise TenantResourceNotFound
        return snapshot

    def _authorize(
        self,
        actor: TenantActor,
        snapshot: TenantAuthorizationSnapshot,
        action: Action,
    ) -> None:
        principal = replace(
            actor.principal,
            user_status=snapshot.user_status,
            auth_version=snapshot.user_auth_version,
            permissions=snapshot.permissions,
            role_codes=snapshot.role_codes,
        )
        decision = self._policy.decide(
            principal,
            snapshot.context,
            action,
            ResourceAttributes(
                tenant_id=snapshot.tenant.id,
                state=ResourceState.ACTIVE,
            ),
            self._now(),
        )
        if not decision.allowed:
            raise TenantAuthorizationDenied(decision.reason_code)

    async def _invalidate_authorization_cache(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        authz_version: int,
        result: MemberMutationResult,
    ) -> None:
        if self._authorization_cache is None:
            return
        try:
            await self._authorization_cache.invalidate(
                tenant_id=tenant_id,
                membership_id=membership_id,
                authz_version=authz_version,
            )
        except Exception as exc:
            raise PostCommitCacheInvalidationError(result) from exc

    async def _reload_application_result(
        self,
        uow: TenantWorkflowUnitOfWork,
        *,
        actor_user_id: UUID,
        reference: IdempotencyResultReference,
    ) -> TenantApplicationResult:
        if reference.result_type != "tenant":
            raise IdempotentResultUnavailable("idempotent result type does not match")
        tenant = await uow.tenants.get_application_for_creator(
            user_id=actor_user_id,
            tenant_id=reference.result_id,
        )
        if tenant is None:
            raise IdempotentResultUnavailable("idempotent tenant result is unavailable")
        owner = await uow.memberships.get_owner_for_application(
            tenant_id=tenant.id,
            user_id=actor_user_id,
        )
        if owner is None:
            raise IdempotentResultUnavailable("idempotent membership result is unavailable")
        return TenantApplicationResult(tenant, owner, True)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("tenant service clock must return a UTC-aware datetime")
        return value.astimezone(UTC)


def _tenant_audit_event(
    context: AuditContext,
    *,
    actor_user_id: UUID | None,
    tenant_id: UUID | None,
    actor_membership_id: UUID | None,
    action: str,
    reason_code: str,
    target_type: str | None,
    target_id: UUID | None,
    now: datetime,
) -> TenantAuditEvent:
    return TenantAuditEvent(
        id=new_uuid7(),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        actor_membership_id=actor_membership_id,
        action=action,
        result="success",
        reason_code=reason_code,
        target_type=target_type,
        target_id=target_id,
        trace_id=context.trace_id,
        client_ip_hash=context.client_ip_hash,
        user_agent_hash=context.user_agent_hash,
        occurred_at=now,
    )


def _membership_scope(actor: TenantActor) -> IdempotencyScope:
    if actor.context.membership_id is None:
        raise TenantAuthorizationDenied("membership_inactive")
    return IdempotencyScope(
        IdempotencyScopeType.MEMBERSHIP,
        actor.context.membership_id,
        tenant_id=actor.context.tenant_id,
    )


def _require_positive_version(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_SIGNED_INTEGER
    ):
        raise ValueError("version must be a positive integer")


@dataclass(frozen=True, slots=True)
class StrongETag:
    version: int

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InvalidStrongETag("ETag version must be a positive integer")

    @classmethod
    def parse(cls, value: str | None) -> StrongETag:
        if value is None:
            raise PreconditionRequired("If-Match is required")
        if not isinstance(value, str):
            raise InvalidStrongETag("If-Match must be a strong version ETag")
        matched = _STRONG_ETAG_PATTERN.fullmatch(value)
        if matched is None:
            raise InvalidStrongETag("If-Match must be a strong version ETag")
        version = int(matched.group(1))
        if version > _MAX_SIGNED_INTEGER:
            raise InvalidStrongETag("If-Match version exceeds the supported range")
        return cls(version)

    @classmethod
    def format(cls, version: int) -> str:
        return f'"{cls(version).version}"'


@dataclass(frozen=True, slots=True)
class MemberCursor:
    created_at: datetime
    membership_id: UUID

    def __post_init__(self) -> None:
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() != UTC.utcoffset(self.created_at)
        ):
            raise InvalidMemberCursor("member cursor timestamp must be UTC-aware")
        if not is_uuid7(self.membership_id):
            raise InvalidMemberCursor("member cursor membership_id must be UUIDv7")


class MemberCursorCodec:
    def __init__(self, *, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("member cursor secret must contain at least 32 bytes")
        self._secret = secret

    def encode(self, *, tenant_id: UUID, cursor: MemberCursor) -> str:
        if not is_uuid7(tenant_id):
            raise InvalidMemberCursor("member cursor tenant_id must be UUIDv7")
        if not isinstance(cursor, MemberCursor):
            raise InvalidMemberCursor("member cursor must be strongly typed")
        payload = json.dumps(
            {
                "v": _CURSOR_SCHEMA_VERSION,
                "t": tenant_id.hex,
                "c": cursor.created_at.astimezone(UTC).isoformat(timespec="microseconds"),
                "i": cursor.membership_id.hex,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        signature = hmac.digest(self._secret, b"member-cursor:v1:" + payload, sha256)
        return _b64url_encode(payload + signature)

    def decode(self, *, tenant_id: UUID, encoded: str) -> MemberCursor:
        if not is_uuid7(tenant_id) or not isinstance(encoded, str) or not encoded:
            raise InvalidMemberCursor("member cursor is invalid")
        try:
            packed = _b64url_decode(encoded)
            if len(packed) <= 32:
                raise InvalidMemberCursor("member cursor is invalid")
            payload, signature = packed[:-32], packed[-32:]
            expected = hmac.digest(self._secret, b"member-cursor:v1:" + payload, sha256)
            if not hmac.compare_digest(signature, expected):
                raise InvalidMemberCursor("member cursor is invalid")
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or set(decoded) != {"v", "t", "c", "i"}:
                raise InvalidMemberCursor("member cursor is invalid")
            if type(decoded["v"]) is not int or decoded["v"] != _CURSOR_SCHEMA_VERSION:
                raise InvalidMemberCursor("member cursor is invalid")
            if not isinstance(decoded["t"], str) or not hmac.compare_digest(
                decoded["t"], tenant_id.hex
            ):
                raise InvalidMemberCursor("member cursor belongs to another tenant")
            created_at = datetime.fromisoformat(decoded["c"])
            membership_id = UUID(hex=decoded["i"])
            return MemberCursor(created_at=created_at, membership_id=membership_id)
        except (
            binascii.Error,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            if isinstance(exc, InvalidMemberCursor):
                raise
            raise InvalidMemberCursor("member cursor is invalid") from exc


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value, re.ASCII) is None:
        raise InvalidMemberCursor("member cursor is invalid")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
