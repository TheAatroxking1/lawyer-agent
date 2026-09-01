from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import (
    CiphertextAuthenticationError,
    IdentityKind,
    NormalizedIdentity,
    PasswordVerification,
    VersionedBlindIndex,
    normalize_identifier,
)

_TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}", re.ASCII)


class IdentityConflictError(Exception):
    code = "identity_conflict"

    def __init__(self) -> None:
        super().__init__("identity conflict")


class BlindIndexKeyUnavailableError(RuntimeError):
    code = "blind_index_key_unavailable"

    def __init__(self) -> None:
        super().__init__("required blind-index key version is unavailable")


class BlindIndexRolloutConfigurationError(ValueError):
    pass


class BlindIndexRolloutPhase(StrEnum):
    LEGACY_COMPATIBLE = "legacy-compatible"
    ROTATION_READY = "rotation-ready"


@dataclass(frozen=True, slots=True)
class AuditContext:
    trace_id: str
    client_ip_hash: bytes | None
    user_agent_hash: bytes | None

    def __post_init__(self) -> None:
        if _TRACE_ID_PATTERN.fullmatch(self.trace_id) is None:
            raise ValueError(
                "trace_id must contain 1 to 64 safe ASCII letters, digits, '.', '_', ':', or '-'"
            )
        for value in (self.client_ip_hash, self.user_agent_hash):
            if value is not None and len(value) != 32:
                raise ValueError("client context hashes must be 32-byte values")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: UUID
    actor_user_id: UUID | None
    action: str
    result: str
    reason_code: str
    target_type: str | None
    target_id: UUID | None
    trace_id: str
    client_ip_hash: bytes | None
    user_agent_hash: bytes | None
    metadata: Mapping[str, object] | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class NewUser:
    id: UUID
    status: str
    display_name: str
    auth_version: int


@dataclass(frozen=True, slots=True)
class NewIdentity:
    id: UUID
    user_id: UUID
    kind: str
    provider: str
    issuer: str
    display_value: str | None
    subject_ciphertext: bytes
    subject_blind_index: bytes
    cipher_key_version: int
    blind_index_key_version: int
    verified_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class NewPasswordCredential:
    user_id: UUID
    password_hash: str
    parameters: Mapping[str, int | str]
    password_changed_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticationRecord:
    identity_id: UUID
    user_id: UUID
    user_status: str
    identity_status: str
    subject_ciphertext: bytes
    cipher_key_version: int
    blind_index_key_version: int
    password_hash: str
    credential_status: str
    locked_until: datetime | None


@dataclass(frozen=True, slots=True)
class RegisterCommand:
    username: str
    password: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RegisteredUser:
    user_id: UUID


@dataclass(frozen=True, slots=True)
class LoginIdentifier:
    kind: IdentityKind
    value: str
    issuer: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: UUID


class PasswordHasherPort(Protocol):
    @property
    def parameters(self) -> Mapping[str, int | str]: ...

    def hash(self, password: str) -> str: ...

    def verify(self, encoded_hash: str, password: str) -> PasswordVerification: ...

    def verify_dummy(self, password: str) -> None: ...

    def is_verification_input_within_limits(self, password: str) -> bool: ...


class SensitiveValueCipherPort(Protocol):
    @property
    def active_key_version(self) -> int: ...

    def encrypt(self, value: str, *, aad: bytes) -> bytes: ...

    def decrypt(self, envelope: bytes, *, aad: bytes) -> str: ...


class BlindIndexPort(Protocol):
    @property
    def active_key_version(self) -> int: ...

    @property
    def key_versions(self) -> tuple[int, ...]: ...

    def digests(self, purpose: str, value: str) -> tuple[VersionedBlindIndex, ...]: ...


@dataclass(frozen=True, slots=True)
class BlindIndexRolloutPolicy:
    phase: BlindIndexRolloutPhase
    legacy_key_version: int
    legacy_writers_drained: bool

    def __post_init__(self) -> None:
        if not isinstance(self.phase, BlindIndexRolloutPhase) or not isinstance(
            self.legacy_writers_drained, bool
        ):
            raise BlindIndexRolloutConfigurationError(
                "rollout phase and drained acknowledgement must be strongly typed"
            )
        if (
            isinstance(self.legacy_key_version, bool)
            or not isinstance(self.legacy_key_version, int)
            or not 1 <= self.legacy_key_version <= 32767
        ):
            raise BlindIndexRolloutConfigurationError(
                "legacy key version must be an integer from 1 to 32767"
            )
        if (
            self.phase is BlindIndexRolloutPhase.ROTATION_READY
            and not self.legacy_writers_drained
        ):
            raise BlindIndexRolloutConfigurationError(
                "rotation-ready requires an explicit legacy-writers-drained acknowledgement"
            )

    def validate(self, blind_index: BlindIndexPort) -> None:
        if self.legacy_key_version not in blind_index.key_versions:
            raise BlindIndexRolloutConfigurationError(
                "legacy key version must remain available in the blind-index key ring"
            )
        if (
            self.phase is BlindIndexRolloutPhase.LEGACY_COMPATIBLE
            and blind_index.active_key_version != self.legacy_key_version
        ):
            raise BlindIndexRolloutConfigurationError(
                "legacy-compatible rollout requires the active blind-index version "
                "to equal the legacy key version"
            )


class IdentityRepositoryPort(Protocol):
    async def has_unsupported_blind_index_versions(self, supported: tuple[int, ...]) -> bool: ...

    async def find_by_blind_indexes(
        self,
        *,
        kind: str,
        issuer: str,
        indexes: tuple[VersionedBlindIndex, ...],
        for_update: bool = False,
    ) -> tuple[AuthenticationRecord, ...]: ...

    async def add_user(self, user: NewUser) -> None: ...

    async def add_identity(self, identity: NewIdentity) -> None: ...

    async def add_credential(self, credential: NewPasswordCredential) -> None: ...

    async def update_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
        parameters: Mapping[str, int | str],
        now: datetime,
    ) -> None: ...

    async def update_blind_index(
        self,
        *,
        identity_id: UUID,
        blind_index: VersionedBlindIndex,
        now: datetime,
    ) -> None: ...

    async def flush(self) -> None: ...


class AuditRepositoryPort(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


class IdentityUnitOfWork(Protocol):
    identities: IdentityRepositoryPort
    audit: AuditRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def lock_identity(self, lock_digest: bytes) -> None: ...


class IdentityService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], IdentityUnitOfWork],
        password_hasher: PasswordHasherPort,
        cipher: SensitiveValueCipherPort,
        blind_index: BlindIndexPort,
        rollout_policy: BlindIndexRolloutPolicy,
    ) -> None:
        rollout_policy.validate(blind_index)
        self._uow_factory = uow_factory
        self._password_hasher = password_hasher
        self._cipher = cipher
        self._blind_index = blind_index
        self._rollout_policy = rollout_policy

    async def register(
        self,
        command: RegisterCommand,
        *,
        audit_context: AuditContext,
    ) -> RegisteredUser:
        normalized = normalize_identifier(IdentityKind.USERNAME, command.username)
        password_hash = self._password_hasher.hash(command.password)
        display_name = command.display_name.strip()
        if not display_name:
            raise ValueError("display name must not be blank")
        if len(display_name) > 255:
            raise ValueError("display name exceeds 255 characters")

        indexes = self._identity_indexes(normalized)
        async with self._uow_factory() as uow:
            await self._lock_identity_indexes(uow, indexes)
            await self._require_supported_blind_indexes(uow)
            if await uow.identities.find_by_blind_indexes(
                kind=normalized.kind.value,
                issuer=normalized.issuer,
                indexes=indexes,
                for_update=True,
            ):
                raise IdentityConflictError
            return await self._register_in_transaction(
                uow,
                normalized=normalized,
                password_hash=password_hash,
                display_name=display_name,
                active_index=_active_index(indexes, self._blind_index.active_key_version),
                audit_context=audit_context,
            )

    async def _register_in_transaction(
        self,
        uow: IdentityUnitOfWork,
        *,
        normalized: NormalizedIdentity,
        password_hash: str,
        display_name: str,
        active_index: VersionedBlindIndex,
        audit_context: AuditContext,
    ) -> RegisteredUser:
        now = _utc_database_now()
        user_id = new_uuid7()
        identity_id = new_uuid7()
        ciphertext = self._cipher.encrypt(
            normalized.subject,
            aad=_identity_subject_aad(identity_id),
        )
        await uow.identities.add_user(NewUser(user_id, "active", display_name, 1))
        await uow.identities.flush()
        await uow.identities.add_identity(
            NewIdentity(
                identity_id,
                user_id,
                normalized.kind.value,
                "local",
                normalized.issuer,
                normalized.display_value,
                ciphertext,
                active_index.digest,
                self._cipher.active_key_version,
                active_index.key_version,
                now,
                "active",
            )
        )
        await uow.identities.add_credential(
            NewPasswordCredential(
                user_id,
                password_hash,
                self._password_hasher.parameters,
                now,
            )
        )
        await uow.audit.append(
            _audit_event(
                audit_context,
                actor_user_id=user_id,
                action="identity.register",
                result="success",
                reason_code="registered",
                target_type="user",
                target_id=user_id,
                now=now,
            )
        )
        await uow.identities.flush()
        return RegisteredUser(user_id)

    async def authenticate(
        self,
        identifier: LoginIdentifier,
        password: str,
        *,
        audit_context: AuditContext,
    ) -> AuthenticatedUser | None:
        if not self._password_hasher.is_verification_input_within_limits(password):
            self._password_hasher.verify_dummy(password)
            await self._audit_authentication_failure(audit_context, "authentication_failed")
            return None
        try:
            normalized = normalize_identifier(
                identifier.kind,
                identifier.value,
                issuer=identifier.issuer,
            )
        except ValueError:
            self._password_hasher.verify_dummy(password)
            await self._audit_authentication_failure(audit_context, "authentication_failed")
            return None

        indexes = self._identity_indexes(normalized)
        async with self._uow_factory() as uow:
            if await uow.identities.has_unsupported_blind_index_versions(
                self._blind_index.key_versions
            ):
                await uow.audit.append(
                    _audit_event(
                        audit_context,
                        actor_user_id=None,
                        action="identity.authenticate",
                        result="failure",
                        reason_code="blind_index_key_unavailable",
                        target_type=None,
                        target_id=None,
                        now=_utc_database_now(),
                    )
                )
                await uow.identities.flush()
                unavailable = True
                result = None
            else:
                unavailable = False
                result = await self._authenticate_in_transaction(
                    uow,
                    normalized,
                    indexes,
                    password,
                    audit_context,
                )
        if unavailable:
            raise BlindIndexKeyUnavailableError
        return result

    async def _authenticate_in_transaction(
        self,
        uow: IdentityUnitOfWork,
        normalized: NormalizedIdentity,
        indexes: tuple[VersionedBlindIndex, ...],
        password: str,
        audit_context: AuditContext,
    ) -> AuthenticatedUser | None:
        records = await uow.identities.find_by_blind_indexes(
            kind=normalized.kind.value,
            issuer=normalized.issuer,
            indexes=indexes,
        )
        if not records:
            self._password_hasher.verify_dummy(password)
            await self._append_authentication_result(uow, audit_context, None, False)
            return None
        if len(records) != 1:
            self._password_hasher.verify_dummy(password)
            await self._append_authentication_result(uow, audit_context, None, False)
            return None
        record = records[0]
        if record.blind_index_key_version != self._blind_index.active_key_version:
            await self._lock_identity_indexes(uow, indexes)
            records = await uow.identities.find_by_blind_indexes(
                kind=normalized.kind.value,
                issuer=normalized.issuer,
                indexes=indexes,
                for_update=True,
            )
            if len(records) != 1:
                self._password_hasher.verify_dummy(password)
                await self._append_authentication_result(uow, audit_context, None, False)
                return None
            record = records[0]

        try:
            stored_subject = self._cipher.decrypt(
                record.subject_ciphertext,
                aad=_identity_subject_aad(record.identity_id),
            )
        except (CiphertextAuthenticationError, UnicodeDecodeError, ValueError):
            self._password_hasher.verify_dummy(password)
            await self._append_authentication_result(uow, audit_context, None, False)
            return None
        if not hmac.compare_digest(
            stored_subject.encode("utf-8"),
            normalized.subject.encode("utf-8"),
        ):
            self._password_hasher.verify_dummy(password)
            await self._append_authentication_result(uow, audit_context, None, False)
            return None

        verification = self._password_hasher.verify(record.password_hash, password)
        if not verification.valid or not _record_can_authenticate(record):
            await self._append_authentication_result(uow, audit_context, None, False)
            return None
        now = _utc_database_now()
        if record.locked_until is not None and record.locked_until > now:
            await self._append_authentication_result(uow, audit_context, None, False)
            return None

        if verification.needs_rehash:
            await uow.identities.update_password_hash(
                user_id=record.user_id,
                password_hash=self._password_hasher.hash(password),
                parameters=self._password_hasher.parameters,
                now=now,
            )
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=record.user_id,
                    action="credential.rehash",
                    result="success",
                    reason_code="parameters_upgraded",
                    target_type="user",
                    target_id=record.user_id,
                    now=now,
                )
            )
        if record.blind_index_key_version != self._blind_index.active_key_version:
            await uow.identities.update_blind_index(
                identity_id=record.identity_id,
                blind_index=_active_index(indexes, self._blind_index.active_key_version),
                now=now,
            )
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=record.user_id,
                    action="identity.blind_index_reindex",
                    result="success",
                    reason_code="key_rotated",
                    target_type="user",
                    target_id=record.user_id,
                    now=now,
                )
            )
        await self._append_authentication_result(uow, audit_context, record.user_id, True)
        await uow.identities.flush()
        return AuthenticatedUser(record.user_id)

    async def _audit_authentication_failure(
        self,
        audit_context: AuditContext,
        reason_code: str,
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.audit.append(
                _audit_event(
                    audit_context,
                    actor_user_id=None,
                    action="identity.authenticate",
                    result="failure",
                    reason_code=reason_code,
                    target_type=None,
                    target_id=None,
                    now=_utc_database_now(),
                )
            )
            await uow.identities.flush()

    async def _append_authentication_result(
        self,
        uow: IdentityUnitOfWork,
        audit_context: AuditContext,
        user_id: UUID | None,
        succeeded: bool,
    ) -> None:
        await uow.audit.append(
            _audit_event(
                audit_context,
                actor_user_id=user_id if succeeded else None,
                action="identity.authenticate",
                result="success" if succeeded else "failure",
                reason_code="authenticated" if succeeded else "authentication_failed",
                target_type="user" if succeeded else None,
                target_id=user_id if succeeded else None,
                now=_utc_database_now(),
            )
        )
        await uow.identities.flush()

    async def _require_supported_blind_indexes(self, uow: IdentityUnitOfWork) -> None:
        if await uow.identities.has_unsupported_blind_index_versions(
            self._blind_index.key_versions
        ):
            raise BlindIndexKeyUnavailableError

    def _identity_indexes(
        self,
        normalized: NormalizedIdentity,
    ) -> tuple[VersionedBlindIndex, ...]:
        indexes = self._blind_index.digests(
            f"identity:{normalized.kind.value}",
            normalized.subject,
        )
        if not indexes:
            raise BlindIndexKeyUnavailableError
        return indexes

    async def _lock_identity_indexes(
        self,
        uow: IdentityUnitOfWork,
        indexes: tuple[VersionedBlindIndex, ...],
    ) -> None:
        # Adjacent keyrings share at least one lock during a rolling rotation. Taking
        # every configured version in a stable order prevents old/new workers from
        # concurrently registering the same normalized identity under different keys.
        for index in sorted(indexes, key=lambda item: (item.key_version, item.digest)):
            await uow.lock_identity(index.digest)


def _active_index(
    indexes: tuple[VersionedBlindIndex, ...],
    active_version: int,
) -> VersionedBlindIndex:
    try:
        return next(index for index in indexes if index.key_version == active_version)
    except StopIteration:
        raise BlindIndexKeyUnavailableError from None


def _identity_subject_aad(identity_id: UUID) -> bytes:
    return f"auth_identity:{identity_id}:subject".encode("ascii")


def _record_can_authenticate(record: AuthenticationRecord) -> bool:
    return (
        record.user_status == "active"
        and record.identity_status == "active"
        and record.credential_status == "active"
    )


def _audit_event(
    context: AuditContext,
    *,
    actor_user_id: UUID | None,
    action: str,
    result: str,
    reason_code: str,
    target_type: str | None,
    target_id: UUID | None,
    now: datetime,
) -> AuditEvent:
    return AuditEvent(
        id=new_uuid7(),
        actor_user_id=actor_user_id,
        action=action,
        result=result,
        reason_code=reason_code,
        target_type=target_type,
        target_id=target_id,
        trace_id=context.trace_id,
        client_ip_hash=context.client_ip_hash,
        user_agent_hash=context.user_agent_hash,
        metadata=None,
        occurred_at=now,
    )


def _utc_database_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
