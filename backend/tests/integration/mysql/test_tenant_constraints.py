from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.tenancy import (
    Department,
    DepartmentStatus,
    Membership,
    MembershipStatus,
    MemberType,
    TenantContext,
    TenantStatus,
)
from lawyer_agent.infrastructure.persistence.models import (
    DepartmentModel,
    MembershipRoleAssignmentModel,
    PermissionModel,
    TenantInvitationModel,
    TenantInvitationRoleAssignmentModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.repositories.authorization import (
    AuthorizationRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.tenancy import (
    DepartmentRepository,
    MembershipRepository,
    TenantRepository,
)
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


@pytest.fixture(scope="module")
def migrated_mysql_url(mysql_url: URL) -> Iterator[URL]:
    command.upgrade(_alembic_config(mysql_url), "head")
    yield mysql_url


@pytest.fixture
async def database(
    migrated_mysql_url: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_async_engine(migrated_mysql_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with factory.begin() as session:
            await session.execute(update(DepartmentModel).values(parent_id=None))
            for model in (
                TenantInvitationRoleAssignmentModel,
                MembershipRoleAssignmentModel,
                TenantRolePermissionModel,
                TenantInvitationModel,
                TenantRoleModel,
                TenantMembershipModel,
                DepartmentModel,
                TenantModel,
                UserModel,
            ):
                await session.execute(delete(model))
        await engine.dispose()


@pytest.fixture
async def graph(
    database: async_sessionmaker[AsyncSession],
) -> dict[str, UUID]:
    ids = {key: new_uuid7() for key in (
        "user_a", "user_b", "tenant_a", "tenant_b", "department_a", "department_b",
        "membership_a", "membership_b", "role_a", "role_b", "invitation_a", "user_c",
    )}
    naive_now = NOW.replace(tzinfo=None)
    async with database.begin() as session:
        await seed_authorization_catalog(session)
        session.add_all(
            [
                UserModel(id=ids["user_a"], status="active", display_name="合成用户甲"),
                UserModel(id=ids["user_b"], status="active", display_name="合成用户乙"),
                UserModel(id=ids["user_c"], status="active", display_name="合成用户丙"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TenantModel(
                    id=ids["tenant_a"], name="合成租户甲", normalized_name=str(ids["tenant_a"]),
                    tenant_type="law_firm", status="active", created_by_user_id=ids["user_a"],
                    review_status="approved",
                ),
                TenantModel(
                    id=ids["tenant_b"], name="合成租户乙", normalized_name=str(ids["tenant_b"]),
                    tenant_type="enterprise", status="active", created_by_user_id=ids["user_b"],
                    review_status="approved",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DepartmentModel(
                    id=ids["department_a"], tenant_id=ids["tenant_a"], parent_id=None,
                    name="甲部门", status="active",
                ),
                DepartmentModel(
                    id=ids["department_b"], tenant_id=ids["tenant_b"], parent_id=None,
                    name="乙部门", status="active",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TenantMembershipModel(
                    id=ids["membership_a"], tenant_id=ids["tenant_a"], user_id=ids["user_a"],
                    department_id=ids["department_a"], member_type="owner", status="active",
                    valid_from=naive_now - timedelta(days=1), valid_until=None, authz_version=1,
                ),
                TenantMembershipModel(
                    id=ids["membership_b"], tenant_id=ids["tenant_b"], user_id=ids["user_b"],
                    department_id=ids["department_b"], member_type="internal", status="active",
                    valid_from=naive_now - timedelta(days=1), valid_until=None, authz_version=1,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TenantRoleModel(
                    id=ids["role_a"], tenant_id=ids["tenant_a"], code="owner-a", name="甲角色",
                    is_custom=True, status="active",
                ),
                TenantRoleModel(
                    id=ids["role_b"], tenant_id=ids["tenant_b"], code="member-b", name="乙角色",
                    is_custom=True, status="active",
                ),
            ]
        )
        await session.flush()
        tenant_read = await session.scalar(
            select(PermissionModel).where(PermissionModel.code == "tenant.read")
        )
        assert tenant_read is not None
        session.add(
            TenantRolePermissionModel(
                tenant_id=ids["tenant_a"], tenant_role_id=ids["role_a"],
                permission_id=tenant_read.id,
            )
        )
        session.add(
            MembershipRoleAssignmentModel(
                tenant_id=ids["tenant_a"], membership_id=ids["membership_a"],
                tenant_role_id=ids["role_a"], assigned_by_membership_id=ids["membership_a"],
            )
        )
        session.add(
            TenantInvitationModel(
                id=ids["invitation_a"], tenant_id=ids["tenant_a"], target_kind="email",
                target_blind_index=b"i" * 32, token_hash=b"t" * 32,
                invited_by_membership_id=ids["membership_a"],
                expires_at=naive_now + timedelta(days=1), status="pending",
            )
        )
    return ids


def _context(ids: dict[str, UUID], tenant: str = "a") -> TenantContext:
    return TenantContext(
        tenant_id=ids[f"tenant_{tenant}"],
        membership_id=ids[f"membership_{tenant}"],
        membership_user_id=ids[f"user_{tenant}"],
        department_id=ids[f"department_{tenant}"],
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=NOW - timedelta(days=1),
        valid_until=None,
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )


async def _expect_integrity_error(session: AsyncSession, model: object) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(model)
            await session.flush()


@pytest.mark.asyncio
async def test_cross_tenant_composite_foreign_keys_reject_all_known_paths(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    async with database.begin() as session:
        await _expect_integrity_error(
            session,
            DepartmentModel(
                id=new_uuid7(), tenant_id=graph["tenant_a"], parent_id=graph["department_b"],
                name="非法跨租户父部门", status="active",
            ),
        )
        await _expect_integrity_error(
            session,
            TenantMembershipModel(
                id=new_uuid7(), tenant_id=graph["tenant_a"], user_id=graph["user_b"],
                department_id=graph["department_b"], member_type="internal", status="active",
                valid_from=NOW.replace(tzinfo=None), valid_until=None, authz_version=1,
            ),
        )
        await _expect_integrity_error(
            session,
            MembershipRoleAssignmentModel(
                tenant_id=graph["tenant_a"], membership_id=graph["membership_a"],
                tenant_role_id=graph["role_b"], assigned_by_membership_id=graph["membership_a"],
            ),
        )
        await _expect_integrity_error(
            session,
            TenantInvitationRoleAssignmentModel(
                tenant_id=graph["tenant_a"], invitation_id=graph["invitation_a"],
                tenant_role_id=graph["role_b"],
            ),
        )


@pytest.mark.asyncio
async def test_repository_uuid_guess_returns_no_cross_tenant_existence_or_dto(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    context_a = _context(graph, "a")
    async with database() as session:
        tenants = TenantRepository(session)
        departments = DepartmentRepository(session)
        memberships = MembershipRepository(session)

        assert await tenants.get(context_a, graph["tenant_b"]) is None
        assert not await tenants.exists(context_a, graph["tenant_b"])
        assert await departments.get(context_a, graph["department_b"]) is None
        assert not await departments.exists(context_a, graph["department_b"])
        assert await memberships.get(context_a, graph["membership_b"]) is None
        assert not await memberships.exists(context_a, graph["membership_b"])
        assert await memberships.list(context_a) == (
            await memberships.get(context_a, graph["membership_a"]),
        )
        assert isinstance(await memberships.get(context_a, graph["membership_a"]), Membership)
        assert not isinstance(
            await memberships.get(context_a, graph["membership_a"]),
            TenantMembershipModel,
        )


@pytest.mark.asyncio
async def test_repository_update_delete_and_role_operations_repeat_tenant_predicate(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    context_a = _context(graph, "a")
    async with database.begin() as session:
        tenants = TenantRepository(session)
        departments = DepartmentRepository(session)
        memberships = MembershipRepository(session)
        authorization = AuthorizationRepository(session)

        assert not await tenants.update_name(context_a, graph["tenant_b"], "越权名称", 1)
        assert not await tenants.delete(context_a, graph["tenant_b"], 1)
        assert not await departments.update_name(
            context_a, graph["department_b"], "越权部门", 1
        )
        assert not await departments.delete(context_a, graph["department_b"], 1)
        assert not await memberships.update_status(
            context_a, graph["membership_b"], MembershipStatus.REVOKED, 1
        )
        assert not await memberships.delete(context_a, graph["membership_b"], 1)
        assert not await authorization.assignment_exists(
            context_a, graph["membership_b"], graph["role_b"]
        )
        assert not await authorization.unassign_role(
            context_a, graph["membership_b"], graph["role_b"]
        )
        assert await authorization.permission_codes(context_a) == frozenset({"tenant.read"})

    async with database() as session:
        tenant_b = await session.get(TenantModel, graph["tenant_b"])
        department_b = await session.get(DepartmentModel, graph["department_b"])
        membership_b = await session.get(TenantMembershipModel, graph["membership_b"])
        assert tenant_b is not None and tenant_b.name == "合成租户乙"
        assert department_b is not None and department_b.name == "乙部门"
        assert membership_b is not None and membership_b.status == "active"


@pytest.mark.asyncio
async def test_repository_scoped_add_read_update_delete_lifecycle(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    context_a = _context(graph, "a")
    department_id = new_uuid7()
    async with database.begin() as session:
        repository = DepartmentRepository(session)
        await repository.add(
            context_a,
            Department(
                id=department_id,
                tenant_id=graph["tenant_a"],
                parent_id=graph["department_a"],
                name="诉讼部门",
                status=DepartmentStatus.ACTIVE,
                version=1,
            ),
        )
        await session.flush()
        assert await repository.exists(context_a, department_id)
        assert await repository.update_name(context_a, department_id, "争议解决部门", 1)
        loaded = await repository.get(context_a, department_id)
        assert loaded is not None and loaded.name == "争议解决部门" and loaded.version == 2
        assert await repository.delete(context_a, department_id, 2)
        loaded = await repository.get(context_a, department_id)
        assert loaded is not None and loaded.status is DepartmentStatus.DISABLED


@pytest.mark.asyncio
async def test_tenant_rename_atomically_updates_display_normalized_name_and_version(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    context_a = _context(graph, "a")
    async with database.begin() as session:
        repository = TenantRepository(session)
        assert await repository.update_name(
            context_a,
            graph["tenant_a"],
            "  ＬＡＷ　ＦＩＲＭ  ",
            1,
        )
        loaded = await repository.get(context_a, graph["tenant_a"])
        assert loaded is not None
        assert loaded.name == "LAW FIRM"
        assert loaded.normalized_name == "law firm"
        assert loaded.version == 2


@pytest.mark.asyncio
async def test_two_tenants_may_rename_to_the_same_normalized_display_name(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    async with database.begin() as session:
        repository = TenantRepository(session)
        assert await repository.update_name(
            _context(graph, "a"), graph["tenant_a"], "  同名租户  ", 1
        )
        assert await repository.update_name(
            _context(graph, "b"), graph["tenant_b"], "同名租户", 1
        )

        tenant_a = await repository.get(_context(graph, "a"), graph["tenant_a"])
        tenant_b = await repository.get(_context(graph, "b"), graph["tenant_b"])
        assert tenant_a is not None and tenant_b is not None
        assert tenant_a.normalized_name == tenant_b.normalized_name == "同名租户"
        assert tenant_a.name == tenant_b.name == "同名租户"


@pytest.mark.asyncio
async def test_repository_rejects_cross_tenant_dto_before_database_write(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    context_a = _context(graph, "a")
    async with database.begin() as session:
        repository = DepartmentRepository(session)
        with pytest.raises(ValueError, match="tenant context"):
            await repository.add(
                context_a,
                Department(
                    id=new_uuid7(), tenant_id=graph["tenant_b"], parent_id=None,
                    name="跨租户写入", status=DepartmentStatus.ACTIVE, version=1,
                ),
            )


@pytest.mark.asyncio
async def test_membership_and_authorization_write_paths_remain_tenant_scoped(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    context_a = _context(graph, "a")
    membership_id = new_uuid7()
    async with database.begin() as session:
        memberships = MembershipRepository(session)
        authorization = AuthorizationRepository(session)
        await memberships.add(
            context_a,
            Membership(
                id=membership_id,
                tenant_id=graph["tenant_a"],
                user_id=graph["user_c"],
                department_id=graph["department_a"],
                member_type=MemberType.INTERNAL,
                status=MembershipStatus.ACTIVE,
                valid_from=NOW - timedelta(days=1),
                valid_until=None,
                authz_version=1,
                version=1,
            ),
        )
        await session.flush()
        assert await authorization.role_exists(context_a, graph["role_a"])
        assert not await authorization.role_exists(context_a, graph["role_b"])
        assert await authorization.has_permission(context_a, "tenant.read")
        await authorization.assign_role(
            context_a,
            membership_id,
            graph["role_a"],
            assigned_by_membership_id=graph["membership_a"],
            now=NOW,
        )
        await session.flush()
        assert await authorization.assignment_exists(
            context_a, membership_id, graph["role_a"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guard", "message"),
    [
        ("target_suspended", "membership unavailable"),
        ("target_not_yet_valid", "membership unavailable"),
        ("target_expired", "membership unavailable"),
        ("assigner_suspended", "assigner unavailable"),
        ("assigner_not_yet_valid", "assigner unavailable"),
        ("assigner_expired", "assigner unavailable"),
        ("role_disabled", "role unavailable"),
    ],
)
async def test_role_assignment_requires_locked_active_memberships_and_role(
    database: async_sessionmaker[AsyncSession],
    graph: dict[str, UUID],
    guard: str,
    message: str,
) -> None:
    context_a = _context(graph, "a")
    target_id = new_uuid7()
    async with database.begin() as session:
        target = TenantMembershipModel(
            id=target_id,
            tenant_id=graph["tenant_a"],
            user_id=graph["user_c"],
            department_id=graph["department_a"],
            member_type="internal",
            status="active",
            valid_from=(NOW - timedelta(days=1)).replace(tzinfo=None),
            valid_until=None,
            authz_version=1,
        )
        session.add(target)
        await session.flush()
        if guard == "target_suspended":
            target.status = "suspended"
        elif guard == "target_not_yet_valid":
            target.valid_from = (NOW + timedelta(minutes=1)).replace(tzinfo=None)
        elif guard == "target_expired":
            target.valid_until = NOW.replace(tzinfo=None)
        elif guard.startswith("assigner_"):
            assigner = await session.get(TenantMembershipModel, graph["membership_a"])
            assert assigner is not None
            if guard == "assigner_suspended":
                assigner.status = "suspended"
            elif guard == "assigner_not_yet_valid":
                assigner.valid_from = (NOW + timedelta(minutes=1)).replace(tzinfo=None)
            else:
                assigner.valid_until = NOW.replace(tzinfo=None)
        else:
            role = await session.get(TenantRoleModel, graph["role_a"])
            assert role is not None
            role.status = "disabled"
        await session.flush()

        repository = AuthorizationRepository(session)
        with pytest.raises(ValueError, match=message):
            await repository.assign_role(
                context_a,
                target_id,
                graph["role_a"],
                assigned_by_membership_id=graph["membership_a"],
                now=NOW,
            )
        assert not await repository.assignment_exists(
            context_a, target_id, graph["role_a"]
        )


async def _insert_assignment_target(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> UUID:
    target_id = new_uuid7()
    async with database.begin() as session:
        session.add(
            TenantMembershipModel(
                id=target_id,
                tenant_id=graph["tenant_a"],
                user_id=graph["user_c"],
                department_id=graph["department_a"],
                member_type="internal",
                status="active",
                valid_from=(NOW - timedelta(days=1)).replace(tzinfo=None),
                valid_until=None,
                authz_version=1,
            )
        )
    return target_id


@pytest.mark.asyncio
async def test_role_assignment_waits_for_concurrent_membership_revocation_and_rejects(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    target_id = await _insert_assignment_target(database, graph)
    barrier = asyncio.Barrier(2)
    lock_sql_issued = asyncio.Event()
    loop = asyncio.get_running_loop()
    sync_engine = database.kw["bind"].sync_engine

    def observe_lock_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.casefold()
        if "tenant_memberships" in normalized and "for update" in normalized:
            loop.call_soon_threadsafe(lock_sql_issued.set)

    async def attempt_assignment() -> str:
        await barrier.wait()
        async with database.begin() as session:
            try:
                await AuthorizationRepository(session).assign_role(
                    _context(graph, "a"),
                    target_id,
                    graph["role_a"],
                    assigned_by_membership_id=graph["membership_a"],
                    now=NOW,
                )
            except ValueError as exc:
                return str(exc)
        return "inserted"

    event.listen(sync_engine, "before_cursor_execute", observe_lock_sql)
    try:
        async with database.begin() as revoker:
            target = await revoker.scalar(
                select(TenantMembershipModel)
                .where(
                    TenantMembershipModel.tenant_id == graph["tenant_a"],
                    TenantMembershipModel.id == target_id,
                )
                .with_for_update()
            )
            assert target is not None
            target.status = "revoked"
            await revoker.flush()
            lock_sql_issued.clear()
            attempt = asyncio.create_task(attempt_assignment())
            await barrier.wait()
            await asyncio.wait_for(lock_sql_issued.wait(), timeout=2)
            assert not attempt.done()
    finally:
        event.remove(sync_engine, "before_cursor_execute", observe_lock_sql)

    assert await asyncio.wait_for(attempt, timeout=3) == (
        "membership unavailable for role assignment"
    )
    async with database() as session:
        assert not await AuthorizationRepository(session).assignment_exists(
            _context(graph, "a"), target_id, graph["role_a"]
        )


@pytest.mark.asyncio
async def test_role_assignment_waits_for_concurrent_role_disable_and_rejects(
    database: async_sessionmaker[AsyncSession], graph: dict[str, UUID]
) -> None:
    target_id = await _insert_assignment_target(database, graph)
    barrier = asyncio.Barrier(2)
    lock_sql_issued = asyncio.Event()
    loop = asyncio.get_running_loop()
    sync_engine = database.kw["bind"].sync_engine

    def observe_lock_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.casefold()
        if "tenant_roles" in normalized and "for update" in normalized:
            loop.call_soon_threadsafe(lock_sql_issued.set)

    async def attempt_assignment() -> str:
        await barrier.wait()
        async with database.begin() as session:
            try:
                await AuthorizationRepository(session).assign_role(
                    _context(graph, "a"),
                    target_id,
                    graph["role_a"],
                    assigned_by_membership_id=graph["membership_a"],
                    now=NOW,
                )
            except ValueError as exc:
                return str(exc)
        return "inserted"

    event.listen(sync_engine, "before_cursor_execute", observe_lock_sql)
    try:
        async with database.begin() as disabler:
            role = await disabler.scalar(
                select(TenantRoleModel)
                .where(
                    TenantRoleModel.tenant_id == graph["tenant_a"],
                    TenantRoleModel.id == graph["role_a"],
                )
                .with_for_update()
            )
            assert role is not None
            role.status = "disabled"
            await disabler.flush()
            lock_sql_issued.clear()
            attempt = asyncio.create_task(attempt_assignment())
            await barrier.wait()
            await asyncio.wait_for(lock_sql_issued.wait(), timeout=2)
            assert not attempt.done()
    finally:
        event.remove(sync_engine, "before_cursor_execute", observe_lock_sql)

    assert await asyncio.wait_for(attempt, timeout=3) == (
        "role unavailable for role assignment"
    )
    async with database() as session:
        assert not await AuthorizationRepository(session).assignment_exists(
            _context(graph, "a"), target_id, graph["role_a"]
        )



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_tenant", "role_tenant", "assigner_tenant", "message"),
    [
        ("b", "a", "a", "membership unavailable"),
        ("a", "b", "a", "role unavailable"),
        ("a", "a", "b", "assigner unavailable"),
    ],
)
async def test_role_assignment_separately_rejects_each_cross_tenant_reference(
    database: async_sessionmaker[AsyncSession],
    graph: dict[str, UUID],
    target_tenant: str,
    role_tenant: str,
    assigner_tenant: str,
    message: str,
) -> None:
    context_a = _context(graph, "a")
    async with database.begin() as session:
        repository = AuthorizationRepository(session)
        before = await session.scalar(
            select(func.count()).select_from(MembershipRoleAssignmentModel)
        )
        with pytest.raises(ValueError, match=message):
            await repository.assign_role(
                context_a,
                graph[f"membership_{target_tenant}"],
                graph[f"role_{role_tenant}"],
                assigned_by_membership_id=graph[f"membership_{assigner_tenant}"],
                now=NOW,
            )
        await session.flush()
        after = await session.scalar(
            select(func.count()).select_from(MembershipRoleAssignmentModel)
        )
        assert after == before
