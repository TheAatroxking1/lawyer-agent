from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.security_locks import SecurityWriteLockRepositoryPort
from lawyer_agent.application.tenancy import TenantAuditEvent, TenantAuditRepositoryPort
from lawyer_agent.domain.authorization import Principal, PrincipalAudience
from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.sessions import StepUpGrant

_REASON_PATTERN = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z", re.ASCII)
_BOOTSTRAP_SECRET_DOMAIN = b"lawyer-agent:bootstrap-platform-admin:v1\x00"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class PlatformAuthorizationDenied(Exception):
    code = "authorization_denied"

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("platform action is not authorized")


class PlatformApplicationUnavailable(Exception):
    code = "tenant_application_unavailable"

    def __init__(self) -> None:
        super().__init__("tenant application is unavailable")


class StepUpRequired(Exception):
    code = "step_up_required"

    def __init__(self) -> None:
        super().__init__("a matching single-use step-up grant is required")


class BootstrapUnavailable(Exception):
    code = "platform_admin_bootstrap_unavailable"

    def __init__(self) -> None:
        super().__init__("platform administrator bootstrap is unavailable")


class BootstrapAuthenticationFailed(Exception):
    code = "platform_admin_bootstrap_authentication_failed"

    def __init__(self) -> None:
        super().__init__("platform administrator bootstrap authentication failed")


class PlatformCommittedWithCleanupWarning(Exception):
    code = "platform_committed_cleanup_warning"
    committed = True

    def __init__(self) -> None:
        super().__init__("platform operation committed but cleanup was not confirmed")


class BootstrapCommittedWithCleanupWarning(Exception):
    code = "platform_admin_bootstrap_committed_cleanup_warning"
    committed = True

    def __init__(self) -> None:
        super().__init__(
            "platform administrator bootstrap committed but cleanup was not confirmed"
        )


@dataclass(frozen=True, slots=True)
class PlatformActor:
    principal: Principal

    def __post_init__(self) -> None:
        if (
            not isinstance(self.principal, Principal)
            or self.principal.audience is not PrincipalAudience.PLATFORM
            or self.principal.tenant_id is not None
            or self.principal.membership_id is not None
        ):
            raise ValueError("platform actor requires platform audience")


@dataclass(frozen=True, slots=True)
class ReviewTenantApplicationCommand:
    tenant_id: UUID
    decision: ReviewDecision
    reason_code: str
    step_up_grant: StepUpGrant = field(repr=False)
    idempotency_key: str = field(repr=False)
    audit_context: AuditContext

    def __post_init__(self) -> None:
        require_uuid7(self.tenant_id, field="review tenant_id")
        if not isinstance(self.decision, ReviewDecision):
            raise ValueError("review decision must be strongly typed")
        if _REASON_PATTERN.fullmatch(self.reason_code) is None:
            raise ValueError("review reason code is invalid")
        if not isinstance(self.step_up_grant, StepUpGrant):
            raise ValueError("step-up grant must be strongly typed")
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")

    @property
    def step_up_action(self) -> str:
        return f"tenant_application.review:{self.decision.value}"


@dataclass(frozen=True, slots=True)
class PlatformApplicationProjection:
    tenant_id: UUID
    name: str
    tenant_type: str
    status: str
    review_status: str
    created_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class PlatformReviewResult:
    application: PlatformApplicationProjection
    replayed: bool


@dataclass(frozen=True, slots=True)
class PlatformAuthorizationSnapshot:
    user_id: UUID
    user_status: str
    user_auth_version: int
    session_id: UUID
    session_user_id: UUID
    session_auth_version: int
    session_revoked_at: datetime | None
    session_expires_at: datetime
    permissions: frozenset[str]
    role_codes: frozenset[str]
    catalog_valid: bool


@dataclass(frozen=True, slots=True)
class BootstrapPlatformAdminCommand:
    user_id: UUID
    secret: str = field(repr=False)
    audit_context: AuditContext

    def __post_init__(self) -> None:
        require_uuid7(self.user_id, field="bootstrap user_id")
        if not isinstance(self.secret, str):
            raise ValueError("bootstrap secret must be text")
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    assignment_id: UUID
    user_id: UUID


@dataclass(frozen=True, slots=True)
class BootstrapState:
    user_is_active: bool
    target_can_reauthenticate: bool
    super_admin_role_id: UUID | None
    has_current_super_admin: bool
    was_bootstrapped: bool
    user_has_super_admin_assignment: bool


@dataclass(frozen=True, slots=True)
class BootstrapSecretVerifier:
    _expected_digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self._expected_digest) != 32:
            raise ValueError("bootstrap secret digest must contain exactly 32 bytes")

    @classmethod
    def from_secret(cls, secret: str) -> BootstrapSecretVerifier:
        return cls(_bootstrap_secret_digest(secret))

    @classmethod
    def from_hex_digest(cls, digest: str) -> BootstrapSecretVerifier:
        try:
            decoded = bytes.fromhex(digest)
        except ValueError:
            raise ValueError("bootstrap secret digest must be lowercase hexadecimal") from None
        if digest != digest.lower() or len(digest) != 64:
            raise ValueError("bootstrap secret digest must be lowercase hexadecimal")
        return cls(decoded)

    def verify(self, secret: str) -> bool:
        try:
            candidate = _bootstrap_secret_digest(secret)
        except ValueError:
            candidate = b"\x00" * 32
        return hmac.compare_digest(self._expected_digest, candidate)


class StepUpConsumerPort(Protocol):
    async def consume(
        self,
        grant: StepUpGrant,
        *,
        user_id: UUID,
        session_id: UUID,
        tenant_id: UUID,
        action: str,
    ) -> bool: ...


class PlatformRepositoryPort(Protocol):
    async def load_actor(
        self, *, principal: Principal, for_update: bool
    ) -> PlatformAuthorizationSnapshot | None: ...

    async def list_applications(
        self, *, limit: int
    ) -> tuple[PlatformApplicationProjection, ...]: ...

    async def get_application(
        self, *, tenant_id: UUID, for_update: bool
    ) -> PlatformApplicationProjection | None: ...

    async def review_application(
        self,
        *,
        application: PlatformApplicationProjection,
        reviewer_user_id: UUID,
        decision: ReviewDecision,
        reason_code: str,
        now: datetime,
    ) -> PlatformApplicationProjection: ...

    async def load_bootstrap_state(
        self, *, user_id: UUID, now: datetime
    ) -> BootstrapState: ...

    async def add_super_admin_assignment(
        self,
        *,
        assignment_id: UUID,
        user_id: UUID,
        role_id: UUID,
        now: datetime,
    ) -> None: ...

    async def flush(self) -> None: ...


class PlatformWorkflowUnitOfWork(Protocol):
    platform: PlatformRepositoryPort
    security_locks: SecurityWriteLockRepositoryPort
    idempotency: IdempotencyRepositoryPort
    audit: TenantAuditRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def acquire_bootstrap_lock(self) -> None: ...


class PlatformReviewService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], PlatformWorkflowUnitOfWork],
        idempotency: IdempotencyService,
        step_up: StepUpConsumerPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._idempotency = idempotency
        self._step_up = step_up
        self._clock = clock

    async def list(
        self,
        actor: PlatformActor,
        *,
        audit_context: AuditContext,
        limit: int = 100,
    ) -> tuple[PlatformApplicationProjection, ...]:
        if not isinstance(actor, PlatformActor) or not isinstance(
            audit_context, AuditContext
        ):
            raise ValueError("platform list inputs must be strongly typed")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("platform application limit must be from 1 to 100")
        now = self._now()
        async with self._uow_factory() as uow:
            snapshot = await self._require_actor(uow, actor, for_update=False)
            self._require_permission(snapshot, "tenant_application.read")
            applications = await uow.platform.list_applications(limit=limit)
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=actor.principal.user_id,
                    tenant_id=None,
                    action="tenant_application.list",
                    reason_code="applications_listed",
                    target_type="tenant_application",
                    target_id=None,
                    now=now,
                )
            )
            return applications

    async def approve(
        self, actor: PlatformActor, command: ReviewTenantApplicationCommand
    ) -> PlatformReviewResult:
        if not isinstance(actor, PlatformActor) or not isinstance(
            command, ReviewTenantApplicationCommand
        ):
            raise ValueError("platform review inputs must be strongly typed")
        if command.decision is not ReviewDecision.APPROVE:
            raise ValueError("approve requires an approve command")
        return await self._review(actor, command)

    async def reject(
        self, actor: PlatformActor, command: ReviewTenantApplicationCommand
    ) -> PlatformReviewResult:
        if not isinstance(actor, PlatformActor) or not isinstance(
            command, ReviewTenantApplicationCommand
        ):
            raise ValueError("platform review inputs must be strongly typed")
        if command.decision is not ReviewDecision.REJECT:
            raise ValueError("reject requires a reject command")
        return await self._review(actor, command)

    async def _review(
        self, actor: PlatformActor, command: ReviewTenantApplicationCommand
    ) -> PlatformReviewResult:
        if not isinstance(actor, PlatformActor) or not isinstance(
            command, ReviewTenantApplicationCommand
        ):
            raise ValueError("platform review inputs must be strongly typed")
        now = self._now()
        async with self._uow_factory() as initial_uow:
            snapshot = await self._require_actor(initial_uow, actor, for_update=False)
            self._require_permission(snapshot, "tenant_application.review")
            initial = await initial_uow.platform.get_application(
                tenant_id=command.tenant_id,
                for_update=False,
            )
            if initial is None:
                raise PlatformApplicationUnavailable

        async with self._uow_factory() as uow:
            if not await uow.security_locks.acquire_tenant_gate(command.tenant_id):
                raise PlatformApplicationUnavailable
            locked = await uow.platform.get_application(
                tenant_id=command.tenant_id,
                for_update=True,
            )
            if locked is None:
                raise PlatformApplicationUnavailable
            authoritative = await self._require_actor(uow, actor, for_update=True)
            self._require_permission(authoritative, "tenant_application.review")
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=IdempotencyScope(
                    IdempotencyScopeType.PLATFORM,
                    actor.principal.user_id,
                ),
                operation=f"tenant_application.{command.decision.value}",
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="POST",
                    canonical_route=(
                        "/api/v1/platform/tenant-applications/"
                        f"{command.tenant_id}/{command.decision.value}"
                    ),
                    body=IdempotencyFingerprintPayload(
                        values={
                            "decision": command.decision.value,
                            "reason_code": command.reason_code,
                        },
                        business_paths=frozenset({("decision",), ("reason_code",)}),
                    ),
                ),
                now=now,
            )
            if reservation.replay is not None:
                reference = reservation.replay
                if reference.result_type != "tenant" or reference.result_id != command.tenant_id:
                    raise PlatformApplicationUnavailable
                replay = await uow.platform.get_application(
                    tenant_id=command.tenant_id,
                    for_update=False,
                )
                if replay is None:
                    raise PlatformApplicationUnavailable
                return PlatformReviewResult(replay, True)
            if not _reviewable(locked):
                raise PlatformApplicationUnavailable
            consumed = await self._step_up.consume(
                command.step_up_grant,
                user_id=actor.principal.user_id,
                session_id=actor.principal.session_id,
                tenant_id=command.tenant_id,
                action=command.step_up_action,
            )
            if not consumed:
                raise StepUpRequired
            reviewed = await uow.platform.review_application(
                application=locked,
                reviewer_user_id=actor.principal.user_id,
                decision=command.decision,
                reason_code=command.reason_code,
                now=now,
            )
            await uow.audit.append(
                _audit_event(
                    command.audit_context,
                    actor_user_id=actor.principal.user_id,
                    tenant_id=command.tenant_id,
                    action=f"tenant_application.{command.decision.value}",
                    reason_code=command.reason_code,
                    target_type="tenant",
                    target_id=command.tenant_id,
                    now=now,
                )
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference("tenant", command.tenant_id),
                now=now,
            )
            await uow.platform.flush()
            return PlatformReviewResult(reviewed, False)

    async def _require_actor(
        self,
        uow: PlatformWorkflowUnitOfWork,
        actor: PlatformActor,
        *,
        for_update: bool,
    ) -> PlatformAuthorizationSnapshot:
        snapshot = await uow.platform.load_actor(
            principal=actor.principal,
            for_update=for_update,
        )
        if snapshot is None:
            raise PlatformAuthorizationDenied("session_invalid")
        if not snapshot.catalog_valid:
            raise PlatformAuthorizationDenied("platform_catalog_invalid")
        principal = actor.principal
        now = self._now()
        if (
            snapshot.user_id != principal.user_id
            or snapshot.session_id != principal.session_id
            or snapshot.session_user_id != principal.user_id
            or snapshot.user_status != "active"
            or snapshot.session_revoked_at is not None
            or snapshot.session_expires_at <= now
            or snapshot.user_auth_version != principal.auth_version
            or snapshot.user_auth_version != principal.session_auth_version
            or snapshot.session_auth_version != snapshot.user_auth_version
        ):
            raise PlatformAuthorizationDenied("session_invalid")
        return snapshot

    @staticmethod
    def _require_permission(
        snapshot: PlatformAuthorizationSnapshot, permission: str
    ) -> None:
        if permission not in snapshot.permissions:
            raise PlatformAuthorizationDenied("permission_denied")

    def _now(self) -> datetime:
        value = self._clock()
        _require_utc(value)
        return value


class PlatformBootstrapService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], PlatformWorkflowUnitOfWork],
        verifier: BootstrapSecretVerifier,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._verifier = verifier
        self._clock = clock

    async def bootstrap(
        self, command: BootstrapPlatformAdminCommand
    ) -> BootstrapResult:
        if not isinstance(command, BootstrapPlatformAdminCommand):
            raise ValueError("bootstrap command must be strongly typed")
        if not self._verifier.verify(command.secret):
            raise BootstrapAuthenticationFailed
        now = self._clock()
        _require_utc(now)
        try:
            return await self._bootstrap_transaction(command, now=now)
        except PlatformCommittedWithCleanupWarning:
            translated_warning = BootstrapCommittedWithCleanupWarning()
        raise translated_warning from None

    async def _bootstrap_transaction(
        self,
        command: BootstrapPlatformAdminCommand,
        *,
        now: datetime,
    ) -> BootstrapResult:
        async with self._uow_factory() as uow:
            await uow.acquire_bootstrap_lock()
            state = await uow.platform.load_bootstrap_state(
                user_id=command.user_id,
                now=now,
            )
            if (
                not state.user_is_active
                or not state.target_can_reauthenticate
                or state.super_admin_role_id is None
                or state.has_current_super_admin
                or state.was_bootstrapped
                or state.user_has_super_admin_assignment
            ):
                raise BootstrapUnavailable
            assignment_id = new_uuid7()
            await uow.platform.add_super_admin_assignment(
                assignment_id=assignment_id,
                user_id=command.user_id,
                role_id=state.super_admin_role_id,
                now=now,
            )
            await uow.audit.append(
                _audit_event(
                    command.audit_context,
                    actor_user_id=None,
                    tenant_id=None,
                    action="platform_admin.bootstrap",
                    reason_code="first_super_admin_created",
                    target_type="user",
                    target_id=command.user_id,
                    now=now,
                )
            )
            await uow.platform.flush()
            return BootstrapResult(assignment_id, command.user_id)


def _bootstrap_secret_digest(secret: str) -> bytes:
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise ValueError("bootstrap secret must contain at least 32 UTF-8 bytes")
    return sha256(_BOOTSTRAP_SECRET_DOMAIN + secret.encode("utf-8")).digest()


def _reviewable(application: PlatformApplicationProjection) -> bool:
    return (
        application.status == "pending_verification"
        and application.review_status == "pending"
    )


def _audit_event(
    context: AuditContext,
    *,
    actor_user_id: UUID | None,
    tenant_id: UUID | None,
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
        actor_membership_id=None,
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


def _require_utc(value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("clock must return a UTC-aware datetime")
