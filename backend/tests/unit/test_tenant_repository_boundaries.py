from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid1, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.repositories.authorization import (
    AuthorizationRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.tenancy import (
    DepartmentRepository,
    MembershipRepository,
    TenantNameConflictError,
    TenantRepository,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
TENANT_ID = new_uuid7()
MEMBERSHIP_ID = new_uuid7()
USER_ID = new_uuid7()
DEPARTMENT_ID = new_uuid7()
ROLE_ID = new_uuid7()


class NoSqlSession:
    calls = 0

    async def scalar(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("SQL must not execute for an invalid UUID")

    async def scalars(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("SQL must not execute for an invalid UUID")

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("SQL must not execute for an invalid UUID")

    def add(self, _value: object) -> None:
        self.calls += 1
        raise AssertionError("write must not execute for an invalid UUID")


def _context() -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        membership_user_id=USER_ID,
        department_id=DEPARTMENT_ID,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )


INVALID_IDS = (
    uuid1(),
    uuid4(),
    UUID(int=0),
    True,
    "01990f00-0000-7000-8000-000000000501",
    object(),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", INVALID_IDS)
@pytest.mark.parametrize(
    ("repository_name", "method_name", "args", "kwargs"),
    [
        ("tenant", "get", (None,), {}),
        ("tenant", "exists", (None,), {}),
        ("tenant", "update_name", (None, "新名称", 1), {}),
        ("tenant", "delete", (None, 1), {}),
        ("department", "get", (None,), {}),
        ("department", "exists", (None,), {}),
        ("department", "update_name", (None, "新名称", 1), {}),
        ("department", "delete", (None, 1), {}),
        ("membership", "get", (None,), {}),
        ("membership", "exists", (None,), {}),
        ("membership", "update_status", (None, MembershipStatus.REVOKED, 1), {}),
        ("membership", "delete", (None, 1), {}),
        ("authorization", "role_exists", (None,), {}),
        ("authorization", "assignment_exists", (None, ROLE_ID), {}),
        ("authorization", "assignment_exists", (MEMBERSHIP_ID, None), {}),
        (
            "authorization",
            "assign_role",
            (None, ROLE_ID),
            {"assigned_by_membership_id": MEMBERSHIP_ID, "now": NOW},
        ),
        (
            "authorization",
            "assign_role",
            (MEMBERSHIP_ID, None),
            {"assigned_by_membership_id": MEMBERSHIP_ID, "now": NOW},
        ),
        (
            "authorization",
            "assign_role",
            (MEMBERSHIP_ID, ROLE_ID),
            {"assigned_by_membership_id": None, "now": NOW},
        ),
        ("authorization", "unassign_role", (None, ROLE_ID), {}),
        ("authorization", "unassign_role", (MEMBERSHIP_ID, None), {}),
    ],
)
async def test_repository_rejects_every_non_uuid7_argument_before_sql(
    invalid: object,
    repository_name: str,
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    session = NoSqlSession()
    repositories: dict[str, object] = {
        "tenant": TenantRepository(cast(AsyncSession, session)),
        "department": DepartmentRepository(cast(AsyncSession, session)),
        "membership": MembershipRepository(cast(AsyncSession, session)),
        "authorization": AuthorizationRepository(cast(AsyncSession, session)),
    }
    actual_args = tuple(invalid if value is None else value for value in args)
    actual_kwargs = {
        key: invalid if value is None else value for key, value in kwargs.items()
    }
    method = cast(Any, getattr(repositories[repository_name], method_name))

    with pytest.raises(ValueError, match="UUIDv7"):
        await method(_context(), *actual_args, **actual_kwargs)
    assert session.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["tenant_id", "membership_id", "membership_user_id", "department_id"]
)
@pytest.mark.parametrize("invalid", INVALID_IDS)
async def test_repository_rejects_every_invalid_context_id_before_sql(
    field: str,
    invalid: object,
) -> None:
    session = NoSqlSession()
    repository = TenantRepository(cast(AsyncSession, session))
    context = replace(_context(), **{field: invalid})

    with pytest.raises(ValueError, match="UUIDv7"):
        await repository.get(context, TENANT_ID)
    assert session.calls == 0


@pytest.mark.asyncio
async def test_tenant_name_unique_conflict_is_stable_and_hides_database_details() -> None:
    class ConflictingSession(NoSqlSession):
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1
            raise IntegrityError(
                "UPDATE tenants",
                {},
                RuntimeError("Duplicate entry 'secret-tenant' for key normalized_name"),
            )

    session = ConflictingSession()
    repository = TenantRepository(cast(AsyncSession, session))
    with pytest.raises(TenantNameConflictError) as caught:
        await repository.update_name(_context(), TENANT_ID, "新租户名称", 1)

    assert str(caught.value) == "tenant name conflicts with an existing tenant"
    assert caught.value.__cause__ is None
    assert "secret-tenant" not in str(caught.value)
    assert session.calls == 1
