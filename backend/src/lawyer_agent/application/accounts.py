from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.application.sessions import InvalidSession
from lawyer_agent.domain.common import require_uuid7

_VISIBLE_MEMBERSHIP_STATUSES = frozenset({"active", "suspended", "invited"})


@dataclass(frozen=True, slots=True)
class AccountRecord:
    id: UUID
    display_name: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccountTenantRecord:
    tenant_id: UUID
    membership_id: UUID
    name: str
    tenant_type: str
    tenant_status: str
    membership_status: str
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class AccountTenantProjection:
    tenant_id: UUID
    membership_id: UUID
    name: str
    tenant_type: str
    tenant_status: str
    membership_status: str


class AccountQueryRepositoryPort(Protocol):
    async def get(self, user_id: UUID) -> AccountRecord | None: ...

    async def list_tenants(self, user_id: UUID) -> tuple[AccountTenantRecord, ...]: ...


class AccountQueryUnitOfWork(Protocol):
    accounts: AccountQueryRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class AccountQueryPort(Protocol):
    async def get(self, user_id: UUID) -> AccountRecord: ...

    async def list_tenants(self, user_id: UUID) -> tuple[AccountTenantProjection, ...]: ...


class AccountQueryService:
    """Expose account-owned relationship metadata, never tenant-private content."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], AccountQueryUnitOfWork],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def get(self, user_id: UUID) -> AccountRecord:
        require_uuid7(user_id, field="account query user_id")
        async with self._uow_factory() as uow:
            account = await uow.accounts.get(user_id)
        if account is None:
            raise InvalidSession
        return account

    async def list_tenants(self, user_id: UUID) -> tuple[AccountTenantProjection, ...]:
        require_uuid7(user_id, field="account query user_id")
        now = self._now()
        async with self._uow_factory() as uow:
            records = await uow.accounts.list_tenants(user_id)
        return tuple(
            AccountTenantProjection(
                tenant_id=record.tenant_id,
                membership_id=record.membership_id,
                name=record.name,
                tenant_type=record.tenant_type,
                tenant_status=record.tenant_status,
                membership_status=record.membership_status,
            )
            for record in records
            if record.membership_status in _VISIBLE_MEMBERSHIP_STATUSES
            and record.valid_from <= now
            and (record.valid_until is None or now < record.valid_until)
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("account query clock must return a UTC-aware datetime")
        return value
