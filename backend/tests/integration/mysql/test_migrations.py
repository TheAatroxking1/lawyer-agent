from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    AuthSessionModel,
    PermissionModel,
    PlatformRoleAssignmentModel,
    PlatformRoleModel,
    PlatformRolePermissionModel,
    RefreshTokenRecordModel,
    RoleTemplateModel,
    RoleTemplatePermissionModel,
    TenantMembershipModel,
    TenantModel,
    UserModel,
)
from lawyer_agent.infrastructure.persistence.seed_authz import (
    PERMISSIONS,
    SeedDriftError,
    seed_authorization_catalog,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

REQUIRED_TABLES = {
    "alembic_version",
    "audit_events",
    "authz_cache_invalidation_outbox",
    "auth_identities",
    "auth_sessions",
    "departments",
    "idempotency_records",
    "identity_verification_challenges",
    "membership_role_assignments",
    "password_credentials",
    "permission_catalog",
    "platform_role_assignments",
    "platform_role_permissions",
    "platform_roles",
    "refresh_token_records",
    "role_template_permissions",
    "role_templates",
    "tenant_invitation_role_assignments",
    "tenant_invitations",
    "tenant_memberships",
    "tenant_role_permissions",
    "tenant_roles",
    "tenants",
    "users",
}


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    rendered_url = mysql_url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered_url)
    return config


async def _inspect_schema(mysql_url: URL) -> dict[str, object]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "auth_session_columns": {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("auth_sessions")
                    },
                    "refresh_indexes": inspect(sync_connection).get_indexes(
                        "refresh_token_records"
                    ),
                    "department_uniques": inspect(sync_connection).get_unique_constraints(
                        "departments"
                    ),
                    "membership_fks": inspect(sync_connection).get_foreign_keys(
                        "membership_role_assignments"
                    ),
                    "cache_outbox_fks": inspect(sync_connection).get_foreign_keys(
                        "authz_cache_invalidation_outbox"
                    ),
                    "cache_outbox_uniques": inspect(sync_connection).get_unique_constraints(
                        "authz_cache_invalidation_outbox"
                    ),
                    "cache_outbox_columns": {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(
                            "authz_cache_invalidation_outbox"
                        )
                    },
                }
            )
    finally:
        await engine.dispose()


def _column_sets(
    items: Iterable[dict[str, object]], key: str = "column_names"
) -> set[tuple[str, ...]]:
    return {tuple(item[key]) for item in items}


def test_baseline_round_trip_and_tenant_constraints(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")

    schema = asyncio.run(_inspect_schema(mysql_url))
    assert REQUIRED_TABLES <= schema["tables"]
    assert schema["auth_session_columns"] == {
        "id",
        "user_id",
        "tenant_id",
        "membership_id",
        "current_family_id",
        "auth_version_at_issue",
        "authz_version_at_issue",
        "revoked_at",
        "revocation_reason",
        "last_seen_at",
        "expires_at",
        "version",
        "created_at",
        "updated_at",
    }
    assert ("tenant_id", "id") in _column_sets(schema["department_uniques"])
    assert {
        ("tenant_id", "membership_id"),
        ("tenant_id", "tenant_role_id"),
    } <= _column_sets(schema["membership_fks"], "constrained_columns")
    assert {
        ("tenant_id", "membership_id"),
        ("tenant_id", "idempotency_record_id"),
    } <= _column_sets(schema["cache_outbox_fks"], "constrained_columns")
    assert {
        ("tenant_id", "membership_id", "authz_version"),
        ("tenant_id", "idempotency_record_id"),
    } <= _column_sets(schema["cache_outbox_uniques"])
    assert "claim_token" in schema["cache_outbox_columns"]
    refresh_indexes = _column_sets(schema["refresh_indexes"])
    assert {("family_id",), ("replaced_by_id",), ("token_hash",)} <= refresh_indexes

    command.downgrade(config, "base")
    assert asyncio.run(_inspect_table_names(mysql_url)) == {"alembic_version"}
    command.upgrade(config, "head")
    assert REQUIRED_TABLES <= asyncio.run(_inspect_table_names(mysql_url))
    command.check(config)


async def _inspect_table_names(mysql_url: URL) -> set[str]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
    finally:
        await engine.dispose()


def test_seed_is_idempotent_rejects_drift_and_creates_no_admin(mysql_url: URL) -> None:
    command.upgrade(_alembic_config(mysql_url), "head")
    asyncio.run(_exercise_seed(mysql_url))


def test_account_refresh_cannot_reference_tenant_session(mysql_url: URL) -> None:
    command.upgrade(_alembic_config(mysql_url), "head")
    asyncio.run(_reject_cross_context_refresh(mysql_url))


def test_invitation_blind_index_migration_revokes_unknown_legacy_pending_rows(
    mysql_url: URL,
) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    command.downgrade(config, "20260902_03")
    invitation_id = asyncio.run(_insert_legacy_pending_invitation(mysql_url))

    command.upgrade(config, "head")
    migrated = asyncio.run(_read_migrated_invitation(mysql_url, invitation_id))
    assert migrated == ("revoked", True, None, 2)
    asyncio.run(_delete_legacy_invitation_graph(mysql_url, invitation_id))

    post_migration_legacy_id = asyncio.run(
        _insert_legacy_pending_invitation(mysql_url)
    )
    with pytest.raises(RuntimeError, match="pending invitations"):
        command.downgrade(config, "20260902_03")
    asyncio.run(
        _delete_legacy_invitation_graph(mysql_url, post_migration_legacy_id)
    )
    command.downgrade(config, "20260902_03")
    columns = asyncio.run(_invitation_columns(mysql_url))
    assert "target_blind_index_key_version" not in columns
    command.upgrade(config, "head")
    assert "target_blind_index_key_version" in asyncio.run(
        _invitation_columns(mysql_url)
    )


async def _insert_legacy_pending_invitation(mysql_url: URL) -> UUID:
    engine = create_async_engine(mysql_url)
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    membership_id = new_uuid7()
    invitation_id = new_uuid7()
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(id,status,display_name,auth_version,version) "
                    "VALUES (:id,'active','Legacy User',1,1)"
                ),
                {"id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants "
                    "(id,name,normalized_name,tenant_type,status,created_by_user_id,"
                    "review_status,version) VALUES "
                    "(:id,'Legacy Tenant','legacy-tenant','enterprise','active',"
                    ":user_id,'approved',1)"
                ),
                {"id": tenant_id.bytes, "user_id": user_id.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_memberships "
                    "(id,tenant_id,user_id,member_type,status,valid_from,authz_version,version) "
                    "VALUES (:id,:tenant_id,:user_id,'owner','active',:now,1,1)"
                ),
                {
                    "id": membership_id.bytes,
                    "tenant_id": tenant_id.bytes,
                    "user_id": user_id.bytes,
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_invitations "
                    "(id,tenant_id,target_kind,target_blind_index,token_hash,"
                    "invited_by_membership_id,expires_at,status,version) VALUES "
                    "(:id,:tenant_id,'email',:target,:token,:membership_id,:expires,'pending',1)"
                ),
                {
                    "id": invitation_id.bytes,
                    "tenant_id": tenant_id.bytes,
                    "target": b"b" * 32,
                    "token": b"t" * 32,
                    "membership_id": membership_id.bytes,
                    "expires": now + timedelta(days=1),
                },
            )
        return invitation_id
    finally:
        await engine.dispose()


async def _read_migrated_invitation(
    mysql_url: URL, invitation_id: UUID
) -> tuple[str, bool, int | None, int]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, revoked_at, target_blind_index_key_version, version "
                        "FROM tenant_invitations WHERE id = :id"
                    ),
                    {"id": invitation_id.bytes},
                )
            ).one()
            return (
                row.status,
                row.revoked_at is not None,
                row.target_blind_index_key_version,
                row.version,
            )
    finally:
        await engine.dispose()


async def _invitation_columns(mysql_url: URL) -> set[str]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "tenant_invitations"
                    )
                }
            )
    finally:
        await engine.dispose()


async def _delete_legacy_invitation_graph(
    mysql_url: URL, invitation_id: UUID
) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT tenant_id, invited_by_membership_id "
                        "FROM tenant_invitations WHERE id = :id"
                    ),
                    {"id": invitation_id.bytes},
                )
            ).one()
            user_id = await connection.scalar(
                text(
                    "SELECT user_id FROM tenant_memberships "
                    "WHERE tenant_id = :tenant_id AND id = :membership_id"
                ),
                {
                    "tenant_id": row.tenant_id,
                    "membership_id": row.invited_by_membership_id,
                },
            )
            await connection.execute(
                text("DELETE FROM tenant_invitations WHERE id = :id"),
                {"id": invitation_id.bytes},
            )
            await connection.execute(
                text(
                    "DELETE FROM tenant_memberships "
                    "WHERE tenant_id = :tenant_id AND id = :membership_id"
                ),
                {
                    "tenant_id": row.tenant_id,
                    "membership_id": row.invited_by_membership_id,
                },
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": row.tenant_id},
            )
            await connection.execute(
                text("DELETE FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
    finally:
        await engine.dispose()


async def _reject_cross_context_refresh(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(UTC).replace(tzinfo=None)
    user_id = new_uuid7()
    tenant_id = new_uuid7()
    membership_id = new_uuid7()
    session_id = new_uuid7()
    account_session_id = new_uuid7()
    try:
        async with session_factory() as session:
            session.add(
                UserModel(
                    id=user_id,
                    status="active",
                    display_name="Synthetic User",
                    auth_version=1,
                )
            )
            await session.flush()
            session.add(
                TenantModel(
                    id=tenant_id,
                    name="Synthetic Tenant",
                    normalized_name=f"synthetic-{tenant_id}",
                    tenant_type="enterprise",
                    status="active",
                    created_by_user_id=user_id,
                    review_status="approved",
                )
            )
            await session.flush()
            session.add(
                TenantMembershipModel(
                    id=membership_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    department_id=None,
                    member_type="internal",
                    status="active",
                    valid_from=now,
                    valid_until=None,
                    authz_version=1,
                )
            )
            await session.flush()
            session.add(
                AuthSessionModel(
                    id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    membership_id=membership_id,
                    current_family_id=new_uuid7(),
                    auth_version_at_issue=1,
                    authz_version_at_issue=1,
                    revoked_at=None,
                    revocation_reason=None,
                    last_seen_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )
            await session.flush()
            session.add(
                AuthSessionModel(
                    id=account_session_id,
                    user_id=user_id,
                    tenant_id=None,
                    membership_id=None,
                    current_family_id=new_uuid7(),
                    auth_version_at_issue=1,
                    authz_version_at_issue=None,
                    revoked_at=None,
                    revocation_reason=None,
                    last_seen_at=now,
                    expires_at=now + timedelta(days=30),
                )
            )
            await session.flush()

            tenant_refresh = RefreshTokenRecordModel(
                id=new_uuid7(),
                token_hash=b"t" * 32,
                family_id=new_uuid7(),
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                membership_id=membership_id,
                expires_at=now + timedelta(days=30),
                idle_expires_at=now + timedelta(days=7),
            )
            account_refresh = RefreshTokenRecordModel(
                id=new_uuid7(),
                token_hash=b"a" * 32,
                family_id=new_uuid7(),
                session_id=account_session_id,
                user_id=user_id,
                tenant_id=None,
                membership_id=None,
                expires_at=now + timedelta(days=30),
                idle_expires_at=now + timedelta(days=7),
            )
            session.add_all([tenant_refresh, account_refresh])
            await session.flush()

            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    account_refresh.replaced_by_id = tenant_refresh.id
                    await session.flush()

            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    session.add(
                        RefreshTokenRecordModel(
                            id=new_uuid7(),
                            token_hash=b"r" * 32,
                            family_id=new_uuid7(),
                            session_id=session_id,
                            user_id=user_id,
                            tenant_id=None,
                            membership_id=None,
                            expires_at=now + timedelta(days=30),
                            idle_expires_at=now + timedelta(days=7),
                        )
                    )
                    await session.flush()
            await session.rollback()
    finally:
        await engine.dispose()


async def _exercise_seed(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await seed_authorization_catalog(session)
            await seed_authorization_catalog(session)
            await session.commit()

        async with session_factory() as session:
            permission_codes = set((await session.scalars(select(PermissionModel.code))).all())
            assert permission_codes == {permission.code for permission in PERMISSIONS}
            assert len(permission_codes) == len(PERMISSIONS)
            seeded_ids = (
                list((await session.scalars(select(PermissionModel.id))).all())
                + list((await session.scalars(select(RoleTemplateModel.id))).all())
                + list((await session.scalars(select(PlatformRoleModel.id))).all())
            )
            assert seeded_ids
            assert all(seed_id.version == 7 for seed_id in seeded_ids)
            assert (
                await session.scalar(
                    select(text("count(*)")).select_from(PlatformRoleAssignmentModel)
                )
                == 0
            )
            await session.execute(
                update(PermissionModel)
                .where(PermissionModel.code == "tenant.read")
                .values(action="delete")
            )
            await session.flush()
            with pytest.raises(SeedDriftError, match="tenant.read"):
                await seed_authorization_catalog(session)
            await session.rollback()

            await session.execute(
                update(RoleTemplateModel)
                .where(RoleTemplateModel.code == "tenant_owner")
                .values(name="drifted")
            )
            await session.flush()
            with pytest.raises(SeedDriftError, match="tenant_owner"):
                await seed_authorization_catalog(session)
            await session.rollback()

            tenant_owner_id = await session.scalar(
                select(RoleTemplateModel.id).where(RoleTemplateModel.code == "tenant_owner")
            )
            tenant_read_id = await session.scalar(
                select(PermissionModel.id).where(PermissionModel.code == "tenant.read")
            )
            assert tenant_owner_id is not None and tenant_read_id is not None
            await session.execute(
                delete(RoleTemplatePermissionModel).where(
                    RoleTemplatePermissionModel.role_template_id == tenant_owner_id,
                    RoleTemplatePermissionModel.permission_id == tenant_read_id,
                )
            )
            await session.flush()
            with pytest.raises(SeedDriftError, match="tenant_owner"):
                await seed_authorization_catalog(session)
            await session.rollback()

            await session.execute(
                update(PlatformRoleModel)
                .where(PlatformRoleModel.code == "super_admin")
                .values(description="drifted")
            )
            await session.flush()
            with pytest.raises(SeedDriftError, match="super_admin"):
                await seed_authorization_catalog(session)
            await session.rollback()

            super_admin_id = await session.scalar(
                select(PlatformRoleModel.id).where(PlatformRoleModel.code == "super_admin")
            )
            application_read_id = await session.scalar(
                select(PermissionModel.id).where(PermissionModel.code == "tenant_application.read")
            )
            assert super_admin_id is not None and application_read_id is not None
            await session.execute(
                delete(PlatformRolePermissionModel).where(
                    PlatformRolePermissionModel.platform_role_id == super_admin_id,
                    PlatformRolePermissionModel.permission_id == application_read_id,
                )
            )
            await session.flush()
            with pytest.raises(SeedDriftError, match="super_admin"):
                await seed_authorization_catalog(session)
            await session.rollback()

            await session.execute(
                update(PermissionModel)
                .where(PermissionModel.code == "tenant.read")
                .values(code="Tenant.Read")
            )
            await session.flush()
            with pytest.raises(SeedDriftError, match="tenant.read"):
                await seed_authorization_catalog(session)
            await session.rollback()
    finally:
        await engine.dispose()
