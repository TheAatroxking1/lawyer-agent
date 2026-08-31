from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy.exc import IntegrityError

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import IdentityKind, NormalizedIdentity, normalize_identifier
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    PasswordCredentialModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.identity import IdentityRepository
from lawyer_agent.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher


class IdentityConflictError(Exception):
    code = "identity_conflict"

    def __init__(self) -> None:
        super().__init__("identity conflict")


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


class IdentityService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], SqlAlchemyUnitOfWork],
        password_hasher: Argon2PasswordHasher,
        cipher: SensitiveValueCipher,
        blind_index: BlindIndexService,
    ) -> None:
        if cipher.active_key_version != blind_index.active_key_version:
            raise ValueError("cipher and blind-index active key versions must match")
        self._uow_factory = uow_factory
        self._password_hasher = password_hasher
        self._cipher = cipher
        self._blind_index = blind_index

    async def register(self, command: RegisterCommand) -> RegisteredUser:
        normalized = normalize_identifier(IdentityKind.USERNAME, command.username)
        password_hash = self._password_hasher.hash(command.password)
        display_name = command.display_name.strip()
        if not display_name:
            raise ValueError("display name must not be blank")
        if len(display_name) > 255:
            raise ValueError("display name exceeds 255 characters")

        try:
            async with self._uow_factory() as uow:
                return await self._register_in_transaction(
                    uow,
                    normalized=normalized,
                    password_hash=password_hash,
                    display_name=display_name,
                )
        except IntegrityError:
            raise IdentityConflictError from None

    async def _register_in_transaction(
        self,
        uow: SqlAlchemyUnitOfWork,
        *,
        normalized: NormalizedIdentity,
        password_hash: str,
        display_name: str,
    ) -> RegisteredUser:
        repository = IdentityRepository(uow.session)
        now = _utc_database_now()
        user_id = new_uuid7()
        identity_id = new_uuid7()
        purpose = f"identity:{normalized.kind.value}"
        blind_index = self._blind_index.digest(purpose, normalized.subject)
        ciphertext = self._cipher.encrypt(
            normalized.subject,
            aad=_identity_subject_aad(identity_id),
        )
        await repository.add_registration(
            user=UserModel(
                id=user_id,
                status="active",
                display_name=display_name,
                auth_version=1,
            ),
            identity=AuthIdentityModel(
                id=identity_id,
                user_id=user_id,
                kind=normalized.kind.value,
                provider="local",
                issuer=normalized.issuer,
                display_value=normalized.display_value,
                subject_ciphertext=ciphertext,
                subject_blind_index=blind_index,
                key_version=self._cipher.active_key_version,
                verified_at=now,
                status="active",
            ),
            credential=PasswordCredentialModel(
                user_id=user_id,
                password_hash=password_hash,
                algorithm="argon2id",
                parameters_json=self._password_hasher.parameters,
                password_changed_at=now,
                failed_attempt_count=0,
                locked_until=None,
                status="active",
            ),
            audit=AuditEventModel(
                id=new_uuid7(),
                actor_user_id=user_id,
                tenant_id=None,
                actor_membership_id=None,
                action="identity.register",
                result="success",
                reason_code="registered",
                target_type="user",
                target_id=user_id,
                trace_id=str(new_uuid7()),
                client_ip_hash=None,
                user_agent_hash=None,
                metadata_json=None,
                occurred_at=now,
            ),
        )
        await repository.flush()
        return RegisteredUser(user_id=user_id)

    async def authenticate(
        self,
        identifier: LoginIdentifier,
        password: str,
    ) -> AuthenticatedUser | None:
        try:
            normalized = normalize_identifier(
                identifier.kind,
                identifier.value,
                issuer=identifier.issuer,
            )
        except ValueError:
            self._password_hasher.verify_dummy(password)
            return None

        async with self._uow_factory() as uow:
            return await self._authenticate_normalized(uow, normalized, password)

    async def _authenticate_normalized(
        self,
        uow: SqlAlchemyUnitOfWork,
        normalized: NormalizedIdentity,
        password: str,
    ) -> AuthenticatedUser | None:
        repository = IdentityRepository(uow.session)
        record = await repository.find_for_authentication(
            kind=normalized.kind.value,
            issuer=normalized.issuer,
            subject_blind_index=self._blind_index.digest(
                f"identity:{normalized.kind.value}",
                normalized.subject,
            ),
        )
        if record is None:
            self._password_hasher.verify_dummy(password)
            return None

        try:
            stored_subject = self._cipher.decrypt(
                record.subject_ciphertext,
                aad=_identity_subject_aad(record.identity_id),
            )
        except (InvalidTag, UnicodeDecodeError, ValueError):
            self._password_hasher.verify_dummy(password)
            return None
        if not hmac.compare_digest(
            stored_subject.encode("utf-8"),
            normalized.subject.encode("utf-8"),
        ):
            self._password_hasher.verify_dummy(password)
            return None

        verification = self._password_hasher.verify(record.password_hash, password)
        if not verification.valid:
            return None
        if not _record_can_authenticate(
            record.user_status,
            record.identity_status,
            record.credential_status,
        ):
            return None
        now = _utc_database_now()
        if record.locked_until is not None and record.locked_until > now:
            return None

        if verification.needs_rehash:
            await repository.update_password_hash(
                user_id=record.user_id,
                password_hash=self._password_hasher.hash(password),
                parameters=self._password_hasher.parameters,
                now=now,
            )
        return AuthenticatedUser(user_id=record.user_id)


def _identity_subject_aad(identity_id: UUID) -> bytes:
    return f"auth_identity:{identity_id}:subject".encode("ascii")


def _record_can_authenticate(
    user_status: str,
    identity_status: str,
    credential_status: str,
) -> bool:
    return (
        user_status == "active"
        and identity_status == "active"
        and credential_status == "active"
    )


def _utc_database_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
