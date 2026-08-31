from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Mapping
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import and_, exists, not_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from lawyer_agent.application.identity import (
    AuditEvent,
    AuthenticationRecord,
    IdentityConflictError,
    NewIdentity,
    NewPasswordCredential,
    NewUser,
)
from lawyer_agent.domain.identity import VersionedBlindIndex
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    PasswordCredentialModel,
    UserModel,
)

_IDENTITY_UNIQUE_CONSTRAINT = "uq_auth_identities_subject"
_MYSQL_DUPLICATE_ENTRY = 1062


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_unsupported_blind_index_versions(self, supported: tuple[int, ...]) -> bool:
        if not supported:
            return True
        statement = select(
            exists().where(not_(AuthIdentityModel.blind_index_key_version.in_(supported)))
        )
        return bool(await self._session.scalar(statement))

    async def find_by_blind_indexes(
        self,
        *,
        kind: str,
        issuer: str,
        indexes: tuple[VersionedBlindIndex, ...],
        for_update: bool = False,
    ) -> tuple[AuthenticationRecord, ...]:
        if not indexes:
            return ()
        index_predicate = or_(
            *(
                and_(
                    AuthIdentityModel.blind_index_key_version == index.key_version,
                    AuthIdentityModel.subject_blind_index == index.digest,
                )
                for index in indexes
            )
        )
        statement = (
            select(AuthIdentityModel, UserModel, PasswordCredentialModel)
            .join(UserModel, UserModel.id == AuthIdentityModel.user_id)
            .join(
                PasswordCredentialModel,
                PasswordCredentialModel.user_id == UserModel.id,
            )
            .where(
                AuthIdentityModel.kind == kind,
                AuthIdentityModel.issuer == issuer,
                index_predicate,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        rows = (await self._session.execute(statement)).all()
        return tuple(
            AuthenticationRecord(
                identity_id=identity.id,
                user_id=user.id,
                user_status=user.status,
                identity_status=identity.status,
                subject_ciphertext=identity.subject_ciphertext,
                cipher_key_version=identity.key_version,
                blind_index_key_version=identity.blind_index_key_version,
                password_hash=credential.password_hash,
                credential_status=credential.status,
                locked_until=credential.locked_until,
            )
            for identity, user, credential in rows
        )

    async def add_user(self, user: NewUser) -> None:
        self._session.add(
            UserModel(
                id=user.id,
                status=user.status,
                display_name=user.display_name,
                auth_version=user.auth_version,
            )
        )

    async def add_identity(self, identity: NewIdentity) -> None:
        self._session.add(
            AuthIdentityModel(
                id=identity.id,
                user_id=identity.user_id,
                kind=identity.kind,
                provider=identity.provider,
                issuer=identity.issuer,
                display_value=identity.display_value,
                subject_ciphertext=identity.subject_ciphertext,
                subject_blind_index=identity.subject_blind_index,
                key_version=identity.cipher_key_version,
                blind_index_key_version=identity.blind_index_key_version,
                verified_at=identity.verified_at,
                status=identity.status,
            )
        )

    async def add_credential(self, credential: NewPasswordCredential) -> None:
        self._session.add(
            PasswordCredentialModel(
                user_id=credential.user_id,
                password_hash=credential.password_hash,
                algorithm="argon2id",
                parameters_json=dict(credential.parameters),
                password_changed_at=credential.password_changed_at,
                failed_attempt_count=0,
                locked_until=None,
                status="active",
            )
        )

    async def update_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
        parameters: Mapping[str, int | str],
        now: datetime,
    ) -> None:
        credential = await self._session.get(PasswordCredentialModel, user_id)
        if credential is None:
            raise RuntimeError("password credential disappeared during authentication")
        credential.password_hash = password_hash
        credential.parameters_json = dict(parameters)
        credential.version += 1
        credential.updated_at = now

    async def update_blind_index(
        self,
        *,
        identity_id: UUID,
        blind_index: VersionedBlindIndex,
        now: datetime,
    ) -> None:
        identity = await self._session.get(AuthIdentityModel, identity_id)
        if identity is None:
            raise RuntimeError("identity disappeared during blind-index rotation")
        identity.subject_blind_index = blind_index.digest
        identity.blind_index_key_version = blind_index.key_version
        identity.updated_at = now

    async def flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if _is_target_identity_duplicate(exc):
                raise IdentityConflictError from None
            raise


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventModel(
                id=event.id,
                actor_user_id=event.actor_user_id,
                tenant_id=None,
                actor_membership_id=None,
                action=event.action,
                result=event.result,
                reason_code=event.reason_code,
                target_type=event.target_type,
                target_id=event.target_id,
                trace_id=event.trace_id,
                client_ip_hash=event.client_ip_hash,
                user_agent_hash=event.user_agent_hash,
                metadata_json=None if event.metadata is None else dict(event.metadata),
                occurred_at=event.occurred_at,
            )
        )


class SqlAlchemyIdentityUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        bind = session_factory.kw.get("bind")
        if not isinstance(bind, AsyncEngine):
            raise TypeError("identity unit of work requires an AsyncEngine-bound session factory")
        self._lock_engine = bind
        self._session: AsyncSession | None = None
        self._lock_connection: AsyncConnection | None = None
        self._lock_names: list[str] = []
        self.identities: IdentityRepository
        self.audit: AuditRepository

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self.identities = IdentityRepository(self._session)
        self.audit = AuditRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        session = self._require_session()
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        except BaseException:
            await session.rollback()
            raise
        finally:
            try:
                await self._release_locks()
            finally:
                await session.close()
                self._session = None
                self._lock_names.clear()

    async def lock_identity(self, lock_digest: bytes) -> None:
        self._require_session()
        if self._lock_connection is None:
            self._lock_connection = await self._lock_engine.connect()
        encoded = urlsafe_b64encode(lock_digest).rstrip(b"=").decode("ascii")
        lock_name = f"lawyer_identity:{encoded}"
        acquired = await self._lock_connection.scalar(
            select(text("GET_LOCK(:lock_name, 5)")).params(lock_name=lock_name)
        )
        if acquired != 1:
            raise TimeoutError("identity registration lock is unavailable")
        self._lock_names.append(lock_name)

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        return self._session

    async def _release_locks(self) -> None:
        connection = self._lock_connection
        if connection is None:
            return
        try:
            for lock_name in reversed(self._lock_names):
                await connection.scalar(
                    select(text("RELEASE_LOCK(:lock_name)")).params(lock_name=lock_name)
                )
        finally:
            await connection.close()
            self._lock_connection = None


def _is_target_identity_duplicate(exc: IntegrityError) -> bool:
    args: tuple[object, ...] = getattr(exc.orig, "args", ())
    return (
        len(args) >= 2
        and args[0] == _MYSQL_DUPLICATE_ENTRY
        and _IDENTITY_UNIQUE_CONSTRAINT in str(args[1])
    )
