from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

import lawyer_agent.application.accounts as accounts_module
from lawyer_agent.application.accounts import (
    AccountQueryService,
    AccountRecord,
    AccountTenantRecord,
)
from lawyer_agent.application.sessions import InvalidSession
from lawyer_agent.domain.common import new_uuid7

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class _Repository:
    def __init__(
        self,
        account: AccountRecord | None,
        tenants: tuple[AccountTenantRecord, ...],
    ) -> None:
        self.account = account
        self.tenants = tenants

    async def get(self, user_id: UUID) -> AccountRecord | None:
        return self.account if self.account is not None and self.account.id == user_id else None

    async def list_tenants(self, user_id: UUID) -> tuple[AccountTenantRecord, ...]:
        del user_id
        return self.tenants


class _Uow:
    def __init__(self, repository: _Repository) -> None:
        self.accounts = repository

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


def _tenant(
    *,
    membership_status: str,
    valid_from: datetime = NOW - timedelta(days=1),
    valid_until: datetime | None = None,
) -> AccountTenantRecord:
    return AccountTenantRecord(
        tenant_id=new_uuid7(),
        membership_id=new_uuid7(),
        name=f"{membership_status}-tenant",
        tenant_type="law_firm",
        tenant_status="active",
        membership_status=membership_status,
        valid_from=valid_from,
        valid_until=valid_until,
    )


@pytest.mark.asyncio
async def test_account_query_policy_shows_current_relationships_without_expired_access() -> None:
    user_id = new_uuid7()
    records = (
        _tenant(membership_status="active"),
        _tenant(membership_status="suspended"),
        _tenant(membership_status="invited"),
        _tenant(membership_status="revoked"),
        _tenant(membership_status="active", valid_until=NOW),
        _tenant(membership_status="active", valid_from=NOW + timedelta(seconds=1)),
    )
    repository = _Repository(
        AccountRecord(user_id, "合成用户", "active", NOW - timedelta(days=2)),
        records,
    )
    service = AccountQueryService(
        uow_factory=lambda: _Uow(repository),
        clock=lambda: NOW,
    )

    account = await service.get(user_id)
    visible = await service.list_tenants(user_id)

    assert account.id == user_id
    assert [item.membership_status for item in visible] == [
        "active",
        "suspended",
        "invited",
    ]
    assert all(not hasattr(item, "valid_from") for item in visible)


@pytest.mark.asyncio
async def test_account_query_missing_user_is_an_invalid_authoritative_session() -> None:
    service = AccountQueryService(
        uow_factory=lambda: _Uow(_Repository(None, ())),
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidSession):
        await service.get(new_uuid7())


def test_account_application_query_boundary_has_no_fastapi_or_sqlalchemy_imports() -> None:
    tree = ast.parse(inspect.getsource(accounts_module))
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "fastapi" not in imported_roots
    assert "sqlalchemy" not in imported_roots
