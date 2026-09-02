from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.application.accounts import AccountRecord, AccountTenantRecord
from lawyer_agent.infrastructure.persistence.models import (
    TenantMembershipModel,
    TenantModel,
    UserModel,
)


class SqlAlchemyAccountQueryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID) -> AccountRecord | None:
        user = await self._session.get(UserModel, user_id)
        if user is None:
            return None
        return AccountRecord(
            id=user.id,
            display_name=user.display_name,
            status=user.status,
            created_at=_utc(user.created_at),
        )

    async def list_tenants(self, user_id: UUID) -> tuple[AccountTenantRecord, ...]:
        statement = (
            select(TenantMembershipModel, TenantModel)
            .join(TenantModel, TenantModel.id == TenantMembershipModel.tenant_id)
            .where(TenantMembershipModel.user_id == user_id)
            .order_by(TenantMembershipModel.created_at, TenantMembershipModel.id)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(
            AccountTenantRecord(
                tenant_id=tenant.id,
                membership_id=membership.id,
                name=tenant.name,
                tenant_type=tenant.tenant_type,
                tenant_status=tenant.status,
                membership_status=membership.status,
                valid_from=_utc(membership.valid_from),
                valid_until=(
                    None
                    if membership.valid_until is None
                    else _utc(membership.valid_until)
                ),
            )
            for membership, tenant in rows
        )


class SqlAlchemyAccountQueryUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session = session_factory()
        self.accounts = SqlAlchemyAccountQueryRepository(self._session)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self._session.close()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
