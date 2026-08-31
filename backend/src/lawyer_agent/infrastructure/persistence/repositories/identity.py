from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthIdentityModel,
    PasswordCredentialModel,
    UserModel,
)


@dataclass(frozen=True, slots=True)
class AuthenticationRecord:
    identity_id: UUID
    user_id: UUID
    user_status: str
    identity_status: str
    subject_ciphertext: bytes
    key_version: int
    password_hash: str
    credential_status: str
    locked_until: datetime | None


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_registration(
        self,
        *,
        user: UserModel,
        identity: AuthIdentityModel,
        credential: PasswordCredentialModel,
        audit: AuditEventModel,
    ) -> None:
        self._session.add(user)
        await self._session.flush()
        self._session.add_all([identity, credential, audit])

    async def flush(self) -> None:
        await self._session.flush()

    async def find_for_authentication(
        self,
        *,
        kind: str,
        issuer: str,
        subject_blind_index: bytes,
    ) -> AuthenticationRecord | None:
        row = (
            await self._session.execute(
                select(AuthIdentityModel, UserModel, PasswordCredentialModel)
                .join(UserModel, UserModel.id == AuthIdentityModel.user_id)
                .join(
                    PasswordCredentialModel,
                    PasswordCredentialModel.user_id == UserModel.id,
                )
                .where(
                    AuthIdentityModel.kind == kind,
                    AuthIdentityModel.issuer == issuer,
                    AuthIdentityModel.subject_blind_index == subject_blind_index,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        identity, user, credential = row
        return AuthenticationRecord(
            identity_id=identity.id,
            user_id=user.id,
            user_status=user.status,
            identity_status=identity.status,
            subject_ciphertext=identity.subject_ciphertext,
            key_version=identity.key_version,
            password_hash=credential.password_hash,
            credential_status=credential.status,
            locked_until=credential.locked_until,
        )

    async def update_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
        parameters: dict[str, int | str],
        now: datetime,
    ) -> None:
        credential = await self._session.get(PasswordCredentialModel, user_id)
        if credential is None:
            return
        credential.password_hash = password_hash
        credential.parameters_json = parameters
        credential.version += 1
        credential.updated_at = now
        await self._session.flush()
