from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyMutationEffect,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.security_locks import (
    SecurityWriteLockRepositoryPort,
    TenantSecurityWriteLockRequest,
)
from lawyer_agent.domain.authorization import (
    Action,
    PolicyEngine,
    Principal,
    ResourceAccessPath,
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


class MembershipMutationDenied(Exception):
    code = "membership_mutation_denied"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


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
    idempotency_key: str = field(repr=False)
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
    idempotency_key: str = field(repr=False)
    audit_context: AuditContext

    def __post_init__(self) -> None:
        _require_positive_version(self.expected_version)
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class TenantMutationResult:
    tenant: Tenant
    replayed: bool
    changed: bool


@dataclass(frozen=True, slots=True)
class UpdateMemberCommand:
    membership_id: UUID
    expected_version: int
    idempotency_key: str = field(repr=False)
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
    idempotency_key: str = field(repr=False)
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
class MemberCollectionScope:
    tenant_wide: bool
    department_ids: frozenset[UUID] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_wide, bool) or not isinstance(
            self.department_ids, frozenset
        ):
            raise ValueError("member collection scope must be strongly typed")
        if any(not is_uuid7(value) for value in self.department_ids):
            raise ValueError("member collection scope contains an invalid department")
        if self.tenant_wide == bool(self.department_ids):
            raise ValueError("member collection scope must be tenant-wide or department-scoped")

    @property
    def cursor_fingerprint(self) -> str:
        material = (
            b"tenant-wide"
            if self.tenant_wide
            else b"departments:" + b",".join(
                value.hex.encode("ascii") for value in sorted(self.department_ids, key=str)
            )
        )
        return sha256(b"member-collection-scope:v1:" + material).hexdigest()


@dataclass(frozen=True, slots=True)
class ActorSessionState:
    id: UUID
    user_id: UUID
    tenant_id: UUID | None
    membership_id: UUID | None
    auth_version_at_issue: int
    authz_version_at_issue: int | None
    revoked_at: datetime | None
    expires_at: datetime


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
    actor_session: ActorSessionState | None


class AuthorizationCacheInvalidationPort(Protocol):
    async def invalidate(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        authz_version: int,
    ) -> None: ...


class AuthorizationCacheInvalidationStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class AuthorizationCacheInvalidationTask:
    id: UUID
    tenant_id: UUID
    membership_id: UUID
    authz_version: int
    idempotency_record_id: UUID
    status: AuthorizationCacheInvalidationStatus
    attempt_count: int
    available_at: datetime
    processing_started_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "outbox id"),
            (self.tenant_id, "outbox tenant_id"),
            (self.membership_id, "outbox membership_id"),
            (self.idempotency_record_id, "outbox idempotency_record_id"),
        ):
            require_uuid7(value, field=name)
        _require_positive_version(self.authz_version)
        if not isinstance(self.status, AuthorizationCacheInvalidationStatus):
            raise ValueError("outbox status must be strongly typed")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or not 0 <= self.attempt_count <= _MAX_SIGNED_INTEGER
        ):
            raise ValueError("outbox attempt_count is invalid")
        _require_utc(self.available_at, field_name="outbox available_at")
        if self.processing_started_at is not None:
            _require_utc(
                self.processing_started_at,
                field_name="outbox processing_started_at",
            )


@dataclass(frozen=True, slots=True)
class NewAuthorizationCacheInvalidation:
    id: UUID
    tenant_id: UUID
    membership_id: UUID
    authz_version: int
    idempotency_record_id: UUID
    available_at: datetime

    def __post_init__(self) -> None:
        AuthorizationCacheInvalidationTask(
            id=self.id,
            tenant_id=self.tenant_id,
            membership_id=self.membership_id,
            authz_version=self.authz_version,
            idempotency_record_id=self.idempotency_record_id,
            status=AuthorizationCacheInvalidationStatus.PENDING,
            attempt_count=0,
            available_at=self.available_at,
        )


class AuthorizationCacheInvalidationOutboxPort(Protocol):
    async def add(self, task: NewAuthorizationCacheInvalidation) -> None: ...

    async def claim_batch(
        self,
        *,
        now: datetime,
        processing_expired_before: datetime,
        limit: int,
    ) -> tuple[AuthorizationCacheInvalidationTask, ...]: ...

    async def mark_completed(self, *, task_id: UUID, now: datetime) -> None: ...

    async def release_failed(
        self,
        *,
        task_id: UUID,
        available_at: datetime,
        error_code: str,
        now: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AuthorizationCacheDispatchResult:
    claimed: int
    completed: int
    failed: int


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
        collection_scope: MemberCollectionScope,
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

    async def role_codes_for_ids(
        self, *, context: TenantContext, role_ids: tuple[UUID, ...]
    ) -> Mapping[UUID, str] | None: ...

    async def count_active_owners_locked(self, *, context: TenantContext) -> int: ...

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
        principal: Principal,
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
    security_locks: SecurityWriteLockRepositoryPort
    cache_outbox: AuthorizationCacheInvalidationOutboxPort
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
        authorization_cache: AuthorizationCacheInvalidationPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        policy: PolicyEngine | None = None,
    ) -> None:
        if (
            not callable(uow_factory)
            or not isinstance(idempotency, IdempotencyService)
            or authorization_cache is None
            or not callable(getattr(authorization_cache, "invalidate", None))
        ):
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
                    body=IdempotencyFingerprintPayload(
                        values={
                            "name": normalized_name.display_value,
                            "tenant_type": command.tenant_type.value,
                        },
                        business_paths=frozenset({("name",), ("tenant_type",)}),
                    ),
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
            if not await uow.security_locks.acquire_tenant_write(
                _tenant_security_write_lock(actor)
            ):
                raise TenantResourceNotFound
            locked = await self._require_snapshot(uow, actor, for_update=True)
            self._authorize(actor, locked, Action.TENANT_UPDATE)
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=_membership_scope(actor),
                operation="tenant.update",
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="PATCH",
                    canonical_route=f"/api/v1/tenants/{actor.context.tenant_id}",
                    body=IdempotencyFingerprintPayload(
                        values={
                            "name": normalized.display_value,
                            "if_match": command.expected_version,
                        },
                        business_paths=frozenset({("name",), ("if_match",)}),
                    ),
                ),
                now=now,
            )
            if reservation.replay is not None:
                if (
                    reservation.replay.result_type != "tenant"
                    or reservation.replay.result_id != actor.context.tenant_id
                ):
                    raise IdempotentResultUnavailable("tenant update replay is invalid")
                return TenantMutationResult(
                    locked.tenant,
                    True,
                    reservation.replay.mutation_effect
                    is not IdempotencyMutationEffect.NO_CHANGE,
                )

            if locked.tenant.version != command.expected_version:
                raise VersionConflict
            changed = (
                locked.tenant.name != normalized.display_value
                or locked.tenant.normalized_name != normalized.normalized_value
            )
            updated = locked.tenant
            if changed:
                stored = await uow.tenants.update_name(
                    context=locked.context,
                    name=normalized.display_value,
                    normalized_name=normalized.normalized_value,
                    expected_version=command.expected_version,
                    now=now,
                )
                if stored is None:
                    raise VersionConflict
                updated = stored
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
                IdempotencyResultReference(
                    "tenant",
                    updated.id,
                    mutation_effect=(
                        IdempotencyMutationEffect.CHANGED
                        if changed
                        else IdempotencyMutationEffect.NO_CHANGE
                    ),
                ),
                now=now,
            )
            await uow.tenants.flush()
            return TenantMutationResult(updated, False, changed)

    async def list_members(
        self,
        actor: TenantActor,
        query: MemberPageQuery,
    ) -> MemberPage:
        if not isinstance(query, MemberPageQuery):
            raise ValueError("member page query must be strongly typed")
        async with self._uow_factory() as uow:
            snapshot = await self._require_snapshot(uow, actor, for_update=False)
            collection_scope = self._authorize_member_collection(actor, snapshot)
            after = (
                None
                if query.cursor is None
                else self._cursor_codec.decode(
                    tenant_id=actor.context.tenant_id,
                    collection_scope=collection_scope,
                    encoded=query.cursor,
                )
            )
            rows = await uow.memberships.list_page(
                context=snapshot.context,
                collection_scope=collection_scope,
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
                    collection_scope=collection_scope,
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
        cache_outbox_task_id: UUID | None = None
        changed = False
        result: MemberMutationResult
        async with self._uow_factory() as uow:
            initial = await self._require_snapshot(
                uow,
                actor,
                target_membership_ids=(membership_id,),
                for_update=False,
            )
            initial_target = initial.target_memberships.get(membership_id)
            if initial_target is None:
                raise TenantResourceNotFound
            self._authorize_member_action(actor, initial, action, initial_target)
            if desired_role_ids is not None:
                self._authorize_member_action(
                    actor, initial, Action.ROLE_ASSIGN, initial_target
                )
            if not await uow.security_locks.acquire_tenant_write(
                _tenant_security_write_lock(actor, target_membership_ids=(membership_id,))
            ):
                raise TenantResourceNotFound
            locked = await self._require_snapshot(
                uow,
                actor,
                target_membership_ids=(membership_id,),
                for_update=True,
            )
            current = locked.target_memberships.get(membership_id)
            if current is None:
                raise TenantResourceNotFound
            self._authorize_member_action(actor, locked, action, current)
            if desired_role_ids is not None:
                self._authorize_member_action(actor, locked, Action.ROLE_ASSIGN, current)
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
                    body=IdempotencyFingerprintPayload(
                        values={
                            "status": None if status is None else status.value,
                            "role_ids": (
                                None
                                if desired_role_ids is None
                                else [str(value) for value in desired_role_ids]
                            ),
                            "change_department": change_department,
                            "department_id": (
                                None if department_id is None else str(department_id)
                            ),
                            "if_match": expected_version,
                        },
                        business_paths=frozenset(
                            {
                                ("status",),
                                ("role_ids",),
                                ("change_department",),
                                ("department_id",),
                                ("if_match",),
                            }
                        ),
                    ),
                ),
                now=now,
            )
            if reservation.replay is not None:
                if (
                    reservation.replay.result_type != "membership"
                    or reservation.replay.result_id != membership_id
                ):
                    raise IdempotentResultUnavailable("membership replay is invalid")
                replay_roles = await uow.roles.role_ids(
                    context=locked.context,
                    membership_id=membership_id,
                )
                replay_result = MemberMutationResult(current, replay_roles, True)
                return replay_result
            if current.version != expected_version:
                raise VersionConflict
            current_roles = await uow.roles.role_ids(
                context=locked.context,
                membership_id=membership_id,
            )
            current_role_map = await uow.roles.role_codes_for_ids(
                context=locked.context,
                role_ids=current_roles,
            )
            requested_role_map = await uow.roles.role_codes_for_ids(
                context=locked.context,
                role_ids=(current_roles if desired_role_ids is None else desired_role_ids),
            )
            if current_role_map is None or requested_role_map is None:
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
            removes_active_owner = _validate_membership_mutation(
                actor_membership=locked.actor_membership,
                actor_role_codes=locked.role_codes,
                actor_context=locked.context,
                target=current,
                current_role_codes=frozenset(current_role_map.values()),
                desired_status=desired_status,
                desired_department_id=desired_department,
                desired_role_codes=frozenset(requested_role_map.values()),
                action=action,
            )
            if removes_active_owner and await uow.roles.count_active_owners_locked(
                context=locked.context
            ) <= 1:
                raise MembershipMutationDenied("cannot remove the last active owner")
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
                IdempotencyResultReference(
                    "membership",
                    membership_id,
                    mutation_effect=(
                        IdempotencyMutationEffect.CHANGED
                        if changed
                        else IdempotencyMutationEffect.NO_CHANGE
                    ),
                    cache_authz_version=old_authz_version,
                ),
                now=now,
            )
            if changed and old_authz_version is not None:
                cache_outbox_task_id = new_uuid7()
                await uow.cache_outbox.add(
                    NewAuthorizationCacheInvalidation(
                        id=cache_outbox_task_id,
                        tenant_id=actor.context.tenant_id,
                        membership_id=membership_id,
                        authz_version=old_authz_version,
                        idempotency_record_id=reservation.record_id,
                        available_at=now,
                    )
                )
            await uow.tenants.flush()
            result = MemberMutationResult(updated, desired_roles, False)

        if changed and old_authz_version is not None:
            try:
                await self._authorization_cache.invalidate(
                    tenant_id=actor.context.tenant_id,
                    membership_id=membership_id,
                    authz_version=old_authz_version,
                )
            except Exception as exc:
                raise PostCommitCacheInvalidationError(result) from exc
            assert cache_outbox_task_id is not None
            async with self._uow_factory() as completion_uow:
                await completion_uow.cache_outbox.mark_completed(
                    task_id=cache_outbox_task_id,
                    now=self._now(),
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
            principal=actor.principal,
            context=actor.context,
            target_membership_ids=target_membership_ids,
            for_update=for_update,
        )
        if snapshot is None:
            raise TenantResourceNotFound
        self._validate_authoritative_actor(actor, snapshot)
        return snapshot

    def _validate_authoritative_actor(
        self,
        actor: TenantActor,
        snapshot: TenantAuthorizationSnapshot,
    ) -> None:
        session = snapshot.actor_session
        principal = actor.principal
        original_context = actor.context
        authoritative_context = snapshot.context
        now = self._now()
        if (
            session is None
            or session.id != principal.session_id
            or session.user_id != principal.user_id
            or session.tenant_id != principal.tenant_id
            or session.membership_id != principal.membership_id
            or principal.user_id != original_context.membership_user_id
            or principal.tenant_id != original_context.tenant_id
            or principal.membership_id != original_context.membership_id
            or authoritative_context.membership_user_id != principal.user_id
            or authoritative_context.membership_id != principal.membership_id
            or session.revoked_at is not None
            or session.expires_at <= now
            or principal.session_valid is not True
            or principal.auth_version != snapshot.user_auth_version
            or principal.session_auth_version != snapshot.user_auth_version
            or session.auth_version_at_issue != snapshot.user_auth_version
            or original_context.authz_version != snapshot.actor_membership.authz_version
            or original_context.session_authz_version
            != snapshot.actor_membership.authz_version
            or session.authz_version_at_issue != snapshot.actor_membership.authz_version
        ):
            raise TenantAuthorizationDenied("session_invalid")

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

    def _authorize_member_action(
        self,
        actor: TenantActor,
        snapshot: TenantAuthorizationSnapshot,
        action: Action,
        target: Membership,
    ) -> None:
        principal = replace(
            actor.principal,
            user_status=snapshot.user_status,
            auth_version=snapshot.user_auth_version,
            permissions=snapshot.permissions,
            role_codes=snapshot.role_codes,
        )
        access_paths = (
            frozenset({ResourceAccessPath.DEPARTMENT})
            if target.department_id is not None
            else frozenset()
        )
        decision = self._policy.decide(
            principal,
            snapshot.context,
            action,
            ResourceAttributes(
                tenant_id=snapshot.tenant.id,
                state=ResourceState.ACTIVE,
                access_paths=access_paths,
                department_id=target.department_id,
            ),
            self._now(),
        )
        if not decision.allowed:
            raise TenantAuthorizationDenied(decision.reason_code)

    def _authorize_member_collection(
        self,
        actor: TenantActor,
        snapshot: TenantAuthorizationSnapshot,
    ) -> MemberCollectionScope:
        scope = snapshot.context.scope
        if scope.allow_tenant_wide:
            self._authorize(actor, snapshot, Action.MEMBERSHIP_READ)
            return MemberCollectionScope(tenant_wide=True)
        department_ids = scope.department_ids
        if "department_admin" in snapshot.role_codes:
            actor_department = snapshot.actor_membership.department_id
            if actor_department is None or actor_department not in department_ids:
                raise TenantAuthorizationDenied("resource_scope_denied")
            department_ids = frozenset({actor_department})
        if not department_ids:
            raise TenantAuthorizationDenied("resource_scope_denied")
        first_department = min(department_ids, key=str)
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
            Action.MEMBERSHIP_READ,
            ResourceAttributes(
                tenant_id=snapshot.tenant.id,
                state=ResourceState.ACTIVE,
                access_paths=frozenset({ResourceAccessPath.DEPARTMENT}),
                department_id=first_department,
            ),
            self._now(),
        )
        if not decision.allowed:
            raise TenantAuthorizationDenied(decision.reason_code)
        return MemberCollectionScope(
            tenant_wide=False,
            department_ids=department_ids,
        )

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


class AuthorizationCacheInvalidationDispatcher:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], TenantWorkflowUnitOfWork],
        authorization_cache: AuthorizationCacheInvalidationPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        processing_lease: timedelta = timedelta(minutes=5),
    ) -> None:
        if (
            not callable(uow_factory)
            or authorization_cache is None
            or not callable(getattr(authorization_cache, "invalidate", None))
            or not isinstance(processing_lease, timedelta)
            or processing_lease <= timedelta(0)
        ):
            raise ValueError("cache invalidation dispatcher dependencies are invalid")
        self._uow_factory = uow_factory
        self._authorization_cache = authorization_cache
        self._clock = clock
        self._processing_lease = processing_lease

    async def run_once(self, *, limit: int = 50) -> AuthorizationCacheDispatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("cache invalidation batch limit must be between 1 and 100")
        now = self._now()
        async with self._uow_factory() as claim_uow:
            tasks = await claim_uow.cache_outbox.claim_batch(
                now=now,
                processing_expired_before=now - self._processing_lease,
                limit=limit,
            )
        completed = 0
        failed = 0
        for task in tasks:
            try:
                await self._authorization_cache.invalidate(
                    tenant_id=task.tenant_id,
                    membership_id=task.membership_id,
                    authz_version=task.authz_version,
                )
            except Exception:
                failed += 1
                retry_at = now + timedelta(
                    seconds=min(300, 2 ** min(task.attempt_count, 8))
                )
                async with self._uow_factory() as failure_uow:
                    await failure_uow.cache_outbox.release_failed(
                        task_id=task.id,
                        available_at=retry_at,
                        error_code="cache_unavailable",
                        now=now,
                    )
            else:
                completed += 1
                async with self._uow_factory() as completion_uow:
                    await completion_uow.cache_outbox.mark_completed(
                        task_id=task.id,
                        now=now,
                    )
        return AuthorizationCacheDispatchResult(len(tasks), completed, failed)

    def _now(self) -> datetime:
        value = self._clock()
        _require_utc(value, field_name="cache dispatcher clock")
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


def _tenant_security_write_lock(
    actor: TenantActor,
    *,
    target_membership_ids: tuple[UUID, ...] = (),
) -> TenantSecurityWriteLockRequest:
    membership_id = actor.context.membership_id
    if membership_id is None:
        raise TenantAuthorizationDenied("membership_inactive")
    return TenantSecurityWriteLockRequest(
        tenant_id=actor.context.tenant_id,
        actor_user_id=actor.principal.user_id,
        actor_session_id=actor.principal.session_id,
        actor_membership_id=membership_id,
        target_membership_ids=tuple(sorted(set(target_membership_ids), key=str)),
    )


def _require_positive_version(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_SIGNED_INTEGER
    ):
        raise ValueError("version must be a positive integer")


_ROLE_HIERARCHY = {
    "tenant_owner": 100,
    "tenant_admin": 80,
    "department_admin": 60,
    "lawyer_or_legal": 40,
    "teacher": 40,
    "assistant": 30,
    "student": 10,
    "external_client": 10,
}


def _validate_membership_mutation(
    *,
    actor_membership: Membership,
    actor_role_codes: frozenset[str],
    actor_context: TenantContext,
    target: Membership,
    current_role_codes: frozenset[str],
    desired_status: MembershipStatus,
    desired_department_id: UUID | None,
    desired_role_codes: frozenset[str],
    action: Action,
) -> bool:
    if target.status in {MembershipStatus.INVITED, MembershipStatus.REVOKED}:
        raise MembershipMutationDenied("membership state cannot be changed by Task 7")
    if action is Action.MEMBERSHIP_UPDATE:
        if target.status not in {MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED} or (
            desired_status not in {MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED}
        ):
            raise MembershipMutationDenied("membership state transition is not allowed")
    elif action is Action.MEMBERSHIP_REVOKE:
        if desired_status is not MembershipStatus.REVOKED:
            raise MembershipMutationDenied("membership revoke transition is invalid")
    else:
        raise MembershipMutationDenied("membership mutation action is unsupported")

    if not actor_role_codes or any(code not in _ROLE_HIERARCHY for code in actor_role_codes):
        raise MembershipMutationDenied("actor role hierarchy is invalid")
    if any(code not in _ROLE_HIERARCHY for code in current_role_codes | desired_role_codes):
        raise MembershipMutationDenied("target role hierarchy is invalid")
    actor_rank = max(_ROLE_HIERARCHY[code] for code in actor_role_codes)
    target_rank = max((_ROLE_HIERARCHY[code] for code in current_role_codes), default=0)
    desired_rank = max((_ROLE_HIERARCHY[code] for code in desired_role_codes), default=0)
    actor_is_owner = "tenant_owner" in actor_role_codes
    actor_is_admin = "tenant_admin" in actor_role_codes and not actor_is_owner
    actor_is_department_admin = (
        "department_admin" in actor_role_codes and not actor_is_owner and not actor_is_admin
    )

    if actor_is_admin and (
        "tenant_owner" in current_role_codes or "tenant_owner" in desired_role_codes
    ):
        raise MembershipMutationDenied("tenant admin cannot grant or remove owner role")
    if not actor_is_owner and target_rank >= actor_rank and target.id != actor_membership.id:
        raise MembershipMutationDenied("actor cannot manage an equal or higher role")
    if desired_rank > actor_rank:
        raise MembershipMutationDenied("actor cannot grant a role above its hierarchy")
    if "tenant_owner" in (current_role_codes ^ desired_role_codes) and not actor_is_owner:
        raise MembershipMutationDenied("only an active owner may grant or remove owner role")
    if "tenant_owner" in desired_role_codes and target.member_type is not MemberType.OWNER:
        raise MembershipMutationDenied("owner role is incompatible with target member type")

    if actor_is_department_admin:
        if (
            actor_membership.department_id is None
            or target.department_id != actor_membership.department_id
            or desired_department_id != actor_membership.department_id
            or actor_context.department_id != actor_membership.department_id
        ):
            raise MembershipMutationDenied("department admin can manage only the same department")
        if desired_rank > _ROLE_HIERARCHY["department_admin"]:
            raise MembershipMutationDenied("department admin cannot grant a higher role")

    if target.member_type is MemberType.STUDENT and desired_role_codes != frozenset({"student"}):
        raise MembershipMutationDenied("roles are incompatible with student member type")
    if target.member_type is MemberType.EXTERNAL_CLIENT and desired_role_codes != frozenset(
        {"external_client"}
    ):
        raise MembershipMutationDenied("roles are incompatible with external member type")
    if target.member_type is MemberType.INTERNAL and "tenant_owner" in desired_role_codes:
        raise MembershipMutationDenied("roles are incompatible with internal member type")

    was_active_owner = (
        target.status is MembershipStatus.ACTIVE and "tenant_owner" in current_role_codes
    )
    remains_active_owner = (
        desired_status is MembershipStatus.ACTIVE and "tenant_owner" in desired_role_codes
    )
    return was_active_owner and not remains_active_owner


@dataclass(frozen=True, slots=True)
class StrongETag:
    version: int

    def __post_init__(self) -> None:
        try:
            _require_positive_version(self.version)
        except ValueError:
            raise InvalidStrongETag("ETag version must be a positive integer") from None

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

    def encode(
        self,
        *,
        tenant_id: UUID,
        collection_scope: MemberCollectionScope,
        cursor: MemberCursor,
    ) -> str:
        if not is_uuid7(tenant_id):
            raise InvalidMemberCursor("member cursor tenant_id must be UUIDv7")
        if not isinstance(cursor, MemberCursor):
            raise InvalidMemberCursor("member cursor must be strongly typed")
        if not isinstance(collection_scope, MemberCollectionScope):
            raise InvalidMemberCursor("member cursor scope must be strongly typed")
        payload = json.dumps(
            {
                "v": _CURSOR_SCHEMA_VERSION,
                "t": tenant_id.hex,
                "s": collection_scope.cursor_fingerprint,
                "c": cursor.created_at.astimezone(UTC).isoformat(timespec="microseconds"),
                "i": cursor.membership_id.hex,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        signature = hmac.digest(self._secret, b"member-cursor:v1:" + payload, sha256)
        return _b64url_encode(payload + signature)

    def decode(
        self,
        *,
        tenant_id: UUID,
        collection_scope: MemberCollectionScope,
        encoded: str,
    ) -> MemberCursor:
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
            if not isinstance(decoded, dict) or set(decoded) != {"v", "t", "s", "c", "i"}:
                raise InvalidMemberCursor("member cursor is invalid")
            if type(decoded["v"]) is not int or decoded["v"] != _CURSOR_SCHEMA_VERSION:
                raise InvalidMemberCursor("member cursor is invalid")
            if not isinstance(decoded["t"], str) or not hmac.compare_digest(
                decoded["t"], tenant_id.hex
            ):
                raise InvalidMemberCursor("member cursor belongs to another tenant")
            if (
                not isinstance(collection_scope, MemberCollectionScope)
                or not isinstance(decoded["s"], str)
                or not hmac.compare_digest(
                    decoded["s"], collection_scope.cursor_fingerprint
                )
            ):
                raise InvalidMemberCursor("member cursor belongs to another scope")
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


def _require_utc(value: object, *, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be UTC-aware")
