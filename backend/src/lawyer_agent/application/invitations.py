from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
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
from lawyer_agent.application.identity import (
    AuditContext,
    BlindIndexPort,
    SensitiveValueCipherPort,
)
from lawyer_agent.application.security_locks import (
    SecurityWriteLockRepositoryPort,
    TenantSecurityWriteLockRequest,
)
from lawyer_agent.application.tenancy import (
    TenantActor,
    TenantAuditEvent,
    TenantAuditRepositoryPort,
    TenantAuthorizationSnapshot,
    TenantAuthorizationWorkflowRepositoryPort,
    TenantResourceNotFound,
)
from lawyer_agent.domain.authorization import (
    Action,
    PolicyEngine,
    ResourceAttributes,
    ResourceState,
)
from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.identity import (
    IdentityKind,
    VersionedBlindIndex,
    normalize_identifier,
)
from lawyer_agent.domain.tenancy import (
    Membership,
    MembershipStatus,
    MemberType,
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_TOKEN_DOMAIN = b"lawyer-agent:tenant-invitation-token:v1\x00"
_TARGET_FINGERPRINT_DOMAIN = b"lawyer-agent:invitation-target-fingerprint:v1\x00"
_DEPLOYMENT_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
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


class InvitationTargetKind(StrEnum):
    PHONE = "phone"
    EMAIL = "email"


class InvitationUnavailable(Exception):
    code = "invitation_unavailable"

    def __init__(self) -> None:
        super().__init__("invitation is unavailable")


class InvitationAuthorizationDenied(Exception):
    code = "authorization_denied"

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("invitation action is not authorized")


class InvitationRoleDenied(Exception):
    code = "invitation_role_denied"

    def __init__(self) -> None:
        super().__init__("invitation roles are not allowed")


class InvitationBlindIndexRetirementBlocked(Exception):
    code = "invitation_blind_index_retirement_blocked"

    def __init__(self) -> None:
        super().__init__("invitation blind-index key retirement is blocked")


class InvitationDeliveryError(RuntimeError):
    code = "invitation_delivery_unavailable"
    committed = True

    def __init__(self, result: InvitationResult) -> None:
        self.result = result
        super().__init__("invitation was created but delivery failed")


@dataclass(frozen=True, slots=True)
class CreateInvitationCommand:
    target_kind: InvitationTargetKind
    target: str = field(repr=False)
    role_ids: tuple[UUID, ...]
    expires_at: datetime
    idempotency_key: str = field(repr=False)
    audit_context: AuditContext

    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, InvitationTargetKind):
            raise ValueError("invitation target kind must be strongly typed")
        if not isinstance(self.target, str):
            raise ValueError("invitation target must be text")
        if not self.role_ids or self.role_ids != tuple(sorted(set(self.role_ids), key=str)):
            raise ValueError("invitation role_ids must be unique and ordered")
        for role_id in self.role_ids:
            require_uuid7(role_id, field="invitation role_id")
        _require_utc(self.expires_at, "invitation expires_at")
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class AcceptInvitationCommand:
    actor_user_id: UUID
    token: str = field(repr=False)
    idempotency_key: str = field(repr=False)
    audit_context: AuditContext

    def __post_init__(self) -> None:
        require_uuid7(self.actor_user_id, field="invitation actor_user_id")
        if _TOKEN_PATTERN.fullmatch(self.token) is None:
            raise InvitationUnavailable
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class InvitationResult:
    invitation_id: UUID
    tenant_id: UUID
    expires_at: datetime
    status: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class InvitationAcceptanceResult:
    invitation_id: UUID
    membership: Membership
    role_ids: tuple[UUID, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class InvitationBlindIndexWritersDrainedAck:
    deployment_reference: str
    confirmed_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.deployment_reference, str)
            or _DEPLOYMENT_REFERENCE_PATTERN.fullmatch(self.deployment_reference) is None
        ):
            raise ValueError("deployment reference is invalid")
        _require_utc(self.confirmed_at, "writers-drained confirmed_at")


@dataclass(frozen=True, slots=True)
class ReconcileLegacyInvitationBlindIndexesCommand:
    writers_drained: InvitationBlindIndexWritersDrainedAck
    audit_context: AuditContext

    def __post_init__(self) -> None:
        if not isinstance(
            self.writers_drained, InvitationBlindIndexWritersDrainedAck
        ):
            raise ValueError("writers-drained acknowledgement must be strongly typed")
        if not isinstance(self.audit_context, AuditContext):
            raise ValueError("audit context must be strongly typed")


@dataclass(frozen=True, slots=True)
class LegacyInvitationBlindIndexReconciliationResult:
    deployment_reference: str
    revoked_count: int
    reconciled_at: datetime


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    id: UUID
    tenant_id: UUID
    target_kind: InvitationTargetKind
    target_blind_index: bytes = field(repr=False)
    target_blind_index_key_version: int | None
    token_hash: bytes = field(repr=False)
    invited_by_membership_id: UUID
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    status: str
    version: int


@dataclass(frozen=True, slots=True)
class InvitationLocator:
    invitation_id: UUID
    tenant_id: UUID


@dataclass(frozen=True, slots=True)
class VerifiedIdentityRecord:
    identity_id: UUID
    kind: InvitationTargetKind
    subject_ciphertext: bytes = field(repr=False)
    verified_at: datetime


class InvitationTokenHasher:
    def __init__(self, *, hmac_key: bytes) -> None:
        if len(hmac_key) != 32:
            raise ValueError("invitation token HMAC key must contain exactly 32 bytes")
        self._key = hmac_key

    def digest(self, token: str) -> bytes:
        if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
            raise InvitationUnavailable
        return hmac.digest(self._key, _TOKEN_DOMAIN + token.encode("ascii"), sha256)


class InvitationTargetFingerprintHasher:
    def __init__(self, *, hmac_key: bytes) -> None:
        if len(hmac_key) != 32:
            raise ValueError(
                "invitation target fingerprint HMAC key must contain exactly 32 bytes"
            )
        self._key = hmac_key

    def digest(self, kind: InvitationTargetKind, normalized_target: str) -> bytes:
        if not isinstance(kind, InvitationTargetKind):
            raise ValueError("invitation target kind must be strongly typed")
        if not isinstance(normalized_target, str) or not normalized_target:
            raise ValueError("normalized invitation target must be non-empty text")
        return hmac.digest(
            self._key,
            _TARGET_FINGERPRINT_DOMAIN
            + kind.value.encode("ascii")
            + b"\x00"
            + normalized_target.encode("utf-8"),
            sha256,
        )


class InvitationRepositoryPort(Protocol):
    async def locate_by_token_hash(self, token_hash: bytes) -> InvitationLocator | None: ...

    async def lock_by_token_hash(
        self, *, tenant_id: UUID, token_hash: bytes
    ) -> InvitationRecord | None: ...

    async def tenant_status_locked(self, *, tenant_id: UUID) -> str | None: ...

    async def count_pending_unexpired_by_blind_index_versions(
        self, *, key_versions: tuple[int, ...], now: datetime
    ) -> int: ...

    async def revoke_unversioned_pending(self, *, now: datetime) -> int: ...

    async def add(self, invitation: InvitationRecord) -> None: ...

    async def add_roles(
        self, *, tenant_id: UUID, invitation_id: UUID, role_ids: tuple[UUID, ...]
    ) -> None: ...

    async def get_for_actor(
        self, *, tenant_id: UUID, invitation_id: UUID
    ) -> InvitationRecord | None: ...

    async def role_codes_locked(
        self, *, tenant_id: UUID, invitation_id: UUID
    ) -> Mapping[UUID, str] | None: ...

    async def role_codes_for_ids_locked(
        self, *, tenant_id: UUID, role_ids: tuple[UUID, ...]
    ) -> Mapping[UUID, str] | None: ...

    async def lock_active_user_identities(
        self, *, user_id: UUID, kinds: tuple[InvitationTargetKind, ...]
    ) -> tuple[VerifiedIdentityRecord, ...] | None: ...

    async def lock_membership_by_user(
        self, *, tenant_id: UUID, user_id: UUID
    ) -> Membership | None: ...

    async def get_membership(
        self, *, tenant_id: UUID, membership_id: UUID
    ) -> Membership | None: ...

    async def add_membership(self, membership: Membership) -> None: ...

    async def activate_membership(
        self, *, membership: Membership, now: datetime
    ) -> Membership: ...

    async def assign_roles(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        role_ids: tuple[UUID, ...],
        assigned_by_membership_id: UUID,
        now: datetime,
    ) -> None: ...

    async def consume(
        self, *, invitation: InvitationRecord, now: datetime
    ) -> InvitationRecord: ...

    async def flush(self) -> None: ...


class InvitationDeliveryPort(Protocol):
    async def deliver(self, *, invitation_id: UUID, token: str) -> None: ...


class InvitationWorkflowUnitOfWork(Protocol):
    invitations: InvitationRepositoryPort
    authorization: TenantAuthorizationWorkflowRepositoryPort
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


class InvitationService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], InvitationWorkflowUnitOfWork],
        idempotency: IdempotencyService,
        token_hasher: InvitationTokenHasher,
        blind_index: BlindIndexPort,
        target_fingerprint_hasher: InvitationTargetFingerprintHasher,
        cipher: SensitiveValueCipherPort,
        delivery: InvitationDeliveryPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        policy: PolicyEngine | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._idempotency = idempotency
        self._token_hasher = token_hasher
        self._blind_index = blind_index
        self._target_fingerprint_hasher = target_fingerprint_hasher
        self._cipher = cipher
        self._delivery = delivery
        self._clock = clock
        self._policy = policy or PolicyEngine()

    async def assert_blind_index_versions_retirable(
        self, key_versions: tuple[int, ...]
    ) -> None:
        if (
            not key_versions
            or key_versions != tuple(sorted(set(key_versions)))
            or any(
                isinstance(version, bool)
                or not isinstance(version, int)
                or not 1 <= version <= 32767
                for version in key_versions
            )
        ):
            raise ValueError("retiring blind-index versions must be unique and ordered")
        now = self._now()
        async with self._uow_factory() as uow:
            count = await uow.invitations.count_pending_unexpired_by_blind_index_versions(
                key_versions=key_versions,
                now=now,
            )
        if count:
            raise InvitationBlindIndexRetirementBlocked

    async def reconcile_legacy_unversioned_invitations(
        self, command: ReconcileLegacyInvitationBlindIndexesCommand
    ) -> LegacyInvitationBlindIndexReconciliationResult:
        if not isinstance(command, ReconcileLegacyInvitationBlindIndexesCommand):
            raise ValueError("legacy invitation reconciliation must be strongly typed")
        now = self._now()
        if command.writers_drained.confirmed_at > now:
            raise ValueError("writers-drained acknowledgement cannot be in the future")
        async with self._uow_factory() as uow:
            revoked_count = await uow.invitations.revoke_unversioned_pending(now=now)
            await uow.audit.append(
                _audit_event(
                    command.audit_context,
                    actor_user_id=None,
                    tenant_id=None,
                    actor_membership_id=None,
                    action="invitation.blind_index_legacy_reconcile",
                    reason_code="legacy_unversioned_invitations_revoked",
                    target_type="tenant_invitation",
                    target_id=None,
                    now=now,
                )
            )
            await uow.invitations.flush()
        return LegacyInvitationBlindIndexReconciliationResult(
            deployment_reference=command.writers_drained.deployment_reference,
            revoked_count=revoked_count,
            reconciled_at=now,
        )

    async def create(
        self, actor: TenantActor, command: CreateInvitationCommand
    ) -> InvitationResult:
        if not isinstance(actor, TenantActor) or not isinstance(
            command, CreateInvitationCommand
        ):
            raise ValueError("invitation creation inputs must be strongly typed")
        now = self._now()
        if command.expires_at <= now:
            raise ValueError("invitation expiry must be in the future")
        normalized = normalize_identifier(
            IdentityKind(command.target_kind.value), command.target
        )
        target_fingerprint = self._target_fingerprint_hasher.digest(
            command.target_kind,
            normalized.subject,
        )
        raw_token: str | None = None
        result: InvitationResult
        async with self._uow_factory() as uow:
            initial = await self._require_tenant_snapshot(uow, actor, for_update=False)
            self._authorize(actor, initial, Action.MEMBERSHIP_INVITE)
            self._authorize(actor, initial, Action.ROLE_ASSIGN)
            if actor.context.membership_id is None:
                raise InvitationAuthorizationDenied("membership_inactive")
            if not await uow.security_locks.acquire_tenant_write(
                TenantSecurityWriteLockRequest(
                    tenant_id=actor.context.tenant_id,
                    actor_user_id=actor.principal.user_id,
                    actor_session_id=actor.principal.session_id,
                    actor_membership_id=actor.context.membership_id,
                )
            ):
                raise TenantResourceNotFound
            locked = await self._require_tenant_snapshot(uow, actor, for_update=True)
            self._authorize(actor, locked, Action.MEMBERSHIP_INVITE)
            self._authorize(actor, locked, Action.ROLE_ASSIGN)
            role_map = await uow.invitations.role_codes_for_ids_locked(
                tenant_id=actor.context.tenant_id,
                role_ids=command.role_ids,
            )
            if role_map is None:
                raise TenantResourceNotFound
            _validate_invitation_roles(locked.role_codes, frozenset(role_map.values()))
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=IdempotencyScope(
                    IdempotencyScopeType.MEMBERSHIP,
                    actor.context.membership_id,
                    tenant_id=actor.context.tenant_id,
                ),
                operation="membership.invite",
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="POST",
                    canonical_route=(
                        f"/api/v1/tenants/{actor.context.tenant_id}/invitations"
                    ),
                    body=IdempotencyFingerprintPayload(
                        values={
                            "target_kind": command.target_kind.value,
                            "target_fingerprint": target_fingerprint.hex(),
                            "role_ids": [str(value) for value in command.role_ids],
                            "expires_at": command.expires_at.isoformat(),
                        },
                        business_paths=frozenset(
                            {
                                ("target_kind",),
                                ("target_fingerprint",),
                                ("role_ids",),
                                ("expires_at",),
                            }
                        ),
                    ),
                ),
                now=now,
            )
            if reservation.replay is not None:
                reference = reservation.replay
                if reference.result_type != "invitation":
                    raise InvitationUnavailable
                replay = await uow.invitations.get_for_actor(
                    tenant_id=actor.context.tenant_id,
                    invitation_id=reference.result_id,
                )
                if replay is None:
                    raise InvitationUnavailable
                return _invitation_result(replay, replayed=True)

            raw_token = secrets.token_urlsafe(32)
            active_index = _active_invite_index(
                self._blind_index,
                command.target_kind,
                normalized.subject,
            )
            invitation = InvitationRecord(
                id=new_uuid7(),
                tenant_id=actor.context.tenant_id,
                target_kind=command.target_kind,
                target_blind_index=active_index.digest,
                target_blind_index_key_version=active_index.key_version,
                token_hash=self._token_hasher.digest(raw_token),
                invited_by_membership_id=actor.context.membership_id,
                expires_at=command.expires_at,
                accepted_at=None,
                revoked_at=None,
                status="pending",
                version=1,
            )
            await uow.invitations.add(invitation)
            await uow.invitations.flush()
            await uow.invitations.add_roles(
                tenant_id=invitation.tenant_id,
                invitation_id=invitation.id,
                role_ids=command.role_ids,
            )
            await uow.audit.append(
                _audit_event(
                    command.audit_context,
                    actor_user_id=actor.principal.user_id,
                    tenant_id=invitation.tenant_id,
                    actor_membership_id=actor.context.membership_id,
                    action="membership.invite",
                    reason_code="invitation_created",
                    target_type="invitation",
                    target_id=invitation.id,
                    now=now,
                )
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference("invitation", invitation.id),
                now=now,
            )
            await uow.invitations.flush()
            result = _invitation_result(invitation, replayed=False)

        assert raw_token is not None
        delivery_error: InvitationDeliveryError | None = None
        try:
            await self._delivery.deliver(
                invitation_id=result.invitation_id,
                token=raw_token,
            )
        except Exception:
            delivery_error = InvitationDeliveryError(result)
        if delivery_error is not None:
            raise delivery_error from None
        return result

    async def accept(
        self, command: AcceptInvitationCommand
    ) -> InvitationAcceptanceResult:
        if not isinstance(command, AcceptInvitationCommand):
            raise ValueError("invitation acceptance command must be strongly typed")
        now = self._now()
        token_hash = self._token_hasher.digest(command.token)
        async with self._uow_factory() as locator_uow:
            locator = await locator_uow.invitations.locate_by_token_hash(token_hash)
        if locator is None:
            raise InvitationUnavailable

        async with self._uow_factory() as uow:
            if not await uow.security_locks.acquire_tenant_gate(locator.tenant_id):
                raise InvitationUnavailable
            if await uow.invitations.tenant_status_locked(
                tenant_id=locator.tenant_id
            ) not in {"pending_verification", "active"}:
                raise InvitationUnavailable
            invitation = await uow.invitations.lock_by_token_hash(
                tenant_id=locator.tenant_id,
                token_hash=token_hash,
            )
            if invitation is None or invitation.id != locator.invitation_id:
                raise InvitationUnavailable
            identities = await uow.invitations.lock_active_user_identities(
                user_id=command.actor_user_id,
                kinds=(invitation.target_kind,),
            )
            membership = await uow.invitations.lock_membership_by_user(
                tenant_id=invitation.tenant_id,
                user_id=command.actor_user_id,
            )
            roles = await uow.invitations.role_codes_locked(
                tenant_id=invitation.tenant_id,
                invitation_id=invitation.id,
            )
            if identities is None or not roles:
                raise InvitationUnavailable
            reservation = await self._idempotency.reserve(
                uow.idempotency,
                scope=IdempotencyScope(
                    IdempotencyScopeType.USER,
                    command.actor_user_id,
                ),
                operation="invitation.accept",
                request=IdempotencyRequest(
                    key=command.idempotency_key,
                    method="POST",
                    canonical_route="/api/v1/invitations/accept",
                    body=IdempotencyFingerprintPayload(
                        values={"token_digest": token_hash.hex()},
                        business_paths=frozenset({("token_digest",)}),
                    ),
                ),
                now=now,
            )
            if reservation.replay is not None:
                reference = reservation.replay
                if reference.result_type != "membership":
                    raise InvitationUnavailable
                replay_membership = await uow.invitations.get_membership(
                    tenant_id=invitation.tenant_id,
                    membership_id=reference.result_id,
                )
                if replay_membership is None or replay_membership.user_id != command.actor_user_id:
                    raise InvitationUnavailable
                return InvitationAcceptanceResult(
                    invitation.id,
                    replay_membership,
                    tuple(sorted(roles, key=str)),
                    True,
                )
            if (
                invitation.status != "pending"
                or invitation.accepted_at is not None
                or invitation.revoked_at is not None
                or invitation.expires_at <= now
                or not self._matches_target(invitation, identities)
            ):
                raise InvitationUnavailable
            role_ids = tuple(sorted(roles, key=str))
            member_type = _member_type_for_roles(frozenset(roles.values()))
            if membership is None:
                membership = Membership(
                    id=new_uuid7(),
                    tenant_id=invitation.tenant_id,
                    user_id=command.actor_user_id,
                    department_id=None,
                    member_type=member_type,
                    status=MembershipStatus.ACTIVE,
                    valid_from=now,
                    valid_until=None,
                    authz_version=1,
                    version=1,
                )
                await uow.invitations.add_membership(membership)
                await uow.invitations.flush()
            elif membership.status is MembershipStatus.INVITED:
                if membership.member_type is not member_type:
                    raise InvitationUnavailable
                membership = await uow.invitations.activate_membership(
                    membership=membership,
                    now=now,
                )
            else:
                raise InvitationUnavailable
            await uow.invitations.assign_roles(
                tenant_id=invitation.tenant_id,
                membership_id=membership.id,
                role_ids=role_ids,
                assigned_by_membership_id=invitation.invited_by_membership_id,
                now=now,
            )
            await uow.invitations.consume(invitation=invitation, now=now)
            await uow.audit.append(
                _audit_event(
                    command.audit_context,
                    actor_user_id=command.actor_user_id,
                    tenant_id=invitation.tenant_id,
                    actor_membership_id=membership.id,
                    action="invitation.accept",
                    reason_code="invitation_accepted",
                    target_type="membership",
                    target_id=membership.id,
                    now=now,
                )
            )
            await self._idempotency.complete(
                uow.idempotency,
                reservation,
                IdempotencyResultReference("membership", membership.id),
                now=now,
            )
            await uow.invitations.flush()
            return InvitationAcceptanceResult(
                invitation.id,
                membership,
                role_ids,
                False,
            )

    def _matches_target(
        self,
        invitation: InvitationRecord,
        identities: tuple[VerifiedIdentityRecord, ...],
    ) -> bool:
        matched = False
        for identity in identities:
            try:
                subject = self._cipher.decrypt(
                    identity.subject_ciphertext,
                    aad=f"auth_identity:{identity.identity_id}:subject".encode("ascii"),
                )
                normalized = normalize_identifier(
                    IdentityKind(identity.kind.value), subject
                )
                candidates = self._blind_index.digests(
                    f"invite:{identity.kind.value}", normalized.subject
                )
            except (UnicodeError, ValueError):
                continue
            if invitation.target_blind_index_key_version is None:
                continue
            for candidate in candidates:
                if candidate.key_version == invitation.target_blind_index_key_version:
                    matched = hmac.compare_digest(
                        invitation.target_blind_index,
                        candidate.digest,
                    ) or matched
        return matched

    async def _require_tenant_snapshot(
        self,
        uow: InvitationWorkflowUnitOfWork,
        actor: TenantActor,
        *,
        for_update: bool,
    ) -> TenantAuthorizationSnapshot:
        snapshot = await uow.authorization.load_snapshot(
            principal=actor.principal,
            context=actor.context,
            for_update=for_update,
        )
        if snapshot is None:
            raise TenantResourceNotFound
        principal = actor.principal
        session = snapshot.actor_session
        now = self._now()
        if (
            session is None
            or session.id != principal.session_id
            or session.user_id != principal.user_id
            or session.tenant_id != principal.tenant_id
            or session.membership_id != principal.membership_id
            or session.revoked_at is not None
            or session.expires_at <= now
            or session.auth_version_at_issue != snapshot.user_auth_version
            or session.authz_version_at_issue != snapshot.actor_membership.authz_version
            or principal.auth_version != snapshot.user_auth_version
            or principal.session_auth_version != snapshot.user_auth_version
            or actor.context.authz_version != snapshot.actor_membership.authz_version
            or actor.context.session_authz_version != snapshot.actor_membership.authz_version
        ):
            raise InvitationAuthorizationDenied("session_invalid")
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
            raise InvitationAuthorizationDenied(decision.reason_code)

    def _now(self) -> datetime:
        value = self._clock()
        _require_utc(value, "clock")
        return value


def _validate_invitation_roles(
    actor_roles: frozenset[str], invited_roles: frozenset[str]
) -> None:
    if (
        not actor_roles
        or not invited_roles
        or any(role not in _ROLE_HIERARCHY for role in actor_roles | invited_roles)
        or "tenant_owner" in invited_roles
        or (
            "department_admin" in actor_roles
            and not actor_roles.intersection({"tenant_owner", "tenant_admin"})
        )
    ):
        raise InvitationRoleDenied
    actor_rank = max(_ROLE_HIERARCHY[role] for role in actor_roles)
    invited_rank = max(_ROLE_HIERARCHY[role] for role in invited_roles)
    if invited_rank > actor_rank or (
        "tenant_owner" not in actor_roles and invited_rank >= actor_rank
    ):
        raise InvitationRoleDenied
    if "tenant_admin" in actor_roles and "tenant_owner" in invited_roles:
        raise InvitationRoleDenied
    _member_type_for_roles(invited_roles)


def _active_invite_index(
    blind_index: BlindIndexPort,
    kind: InvitationTargetKind,
    subject: str,
) -> VersionedBlindIndex:
    indexes = blind_index.digests(f"invite:{kind.value}", subject)
    try:
        return next(
            item for item in indexes if item.key_version == blind_index.active_key_version
        )
    except StopIteration:
        raise RuntimeError("active invitation blind-index version is unavailable") from None


def _member_type_for_roles(role_codes: frozenset[str]) -> MemberType:
    if "tenant_owner" in role_codes or not role_codes:
        raise InvitationRoleDenied
    if "student" in role_codes:
        if role_codes != frozenset({"student"}):
            raise InvitationRoleDenied
        return MemberType.STUDENT
    if "external_client" in role_codes:
        if role_codes != frozenset({"external_client"}):
            raise InvitationRoleDenied
        return MemberType.EXTERNAL_CLIENT
    return MemberType.INTERNAL


def _invitation_result(
    invitation: InvitationRecord, *, replayed: bool
) -> InvitationResult:
    return InvitationResult(
        invitation_id=invitation.id,
        tenant_id=invitation.tenant_id,
        expires_at=invitation.expires_at,
        status=invitation.status,
        replayed=replayed,
    )


def _audit_event(
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


def _require_utc(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be UTC-aware")
