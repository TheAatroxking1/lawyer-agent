from sqlalchemy import CheckConstraint, Enum, ForeignKeyConstraint, String, UniqueConstraint

from lawyer_agent.infrastructure.persistence import models as persistence_models  # noqa: F401
from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.seed_authz import (
    PERMISSIONS,
    PLATFORM_ROLES,
    TENANT_ROLE_TEMPLATES,
)


def test_permission_catalog_is_stable_and_complete() -> None:
    assert tuple(permission.code for permission in PERMISSIONS) == (
        "tenant.read",
        "tenant.update",
        "department.read",
        "department.manage",
        "membership.read",
        "membership.invite",
        "membership.update",
        "membership.revoke",
        "role.read",
        "role.assign",
        "external_service.enable",
        "tenant_application.read",
        "tenant_application.review",
        "platform_admin.bootstrap",
    )
    assert len({permission.code for permission in PERMISSIONS}) == len(PERMISSIONS)


def test_platform_roles_never_include_tenant_content_permissions() -> None:
    tenant_permissions = {
        permission.code
        for permission in PERMISSIONS
        if permission.resource not in {"tenant_application", "platform_admin"}
    }
    assert PLATFORM_ROLES
    assert all(
        not tenant_permissions.intersection(role.permission_codes) for role in PLATFORM_ROLES
    )


def test_role_seed_codes_and_permission_sets_are_deterministic() -> None:
    assert tuple(role.code for role in TENANT_ROLE_TEMPLATES) == (
        "tenant_owner",
        "tenant_admin",
        "department_admin",
        "lawyer_or_legal",
        "assistant",
        "teacher",
        "student",
        "external_client",
    )
    assert tuple(role.code for role in PLATFORM_ROLES) == (
        "super_admin",
        "security_auditor",
        "operations_support",
        "content_operator",
    )
    known_permissions = {permission.code for permission in PERMISSIONS}
    assert all(role.permission_codes <= known_permissions for role in TENANT_ROLE_TEMPLATES)
    assert all(role.permission_codes <= known_permissions for role in PLATFORM_ROLES)


def test_status_columns_use_varchar_with_check_constraints() -> None:
    for table in Base.metadata.tables.values():
        assert all(not isinstance(column.type, Enum) for column in table.columns)
        status_columns = [column for column in table.columns if "status" in column.name]
        check_sql = " ".join(
            str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        for column in status_columns:
            assert isinstance(column.type, String), f"{table.name}.{column.name} must be VARCHAR"
            assert column.name in check_sql, f"{table.name}.{column.name} requires a CHECK"


def test_composite_foreign_key_parents_have_tenant_unique_identity() -> None:
    for table_name in (
        "auth_sessions",
        "departments",
        "tenant_invitations",
        "tenant_memberships",
        "tenant_roles",
    ):
        table = Base.metadata.tables[table_name]
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert ("tenant_id", "id") in unique_columns


def test_constraint_names_are_unique_within_each_table() -> None:
    for table in Base.metadata.tables.values():
        names = [constraint.name for constraint in table.constraints if constraint.name is not None]
        assert len(names) == len(set(names)), f"{table.name} has duplicate constraint names"
        assert all(len(name) <= 64 for name in names), f"{table.name} exceeds MySQL name limit"


def test_idempotency_membership_scope_is_explicitly_tenant_bound() -> None:
    table = Base.metadata.tables["idempotency_records"]
    assert "tenant_id" in table.columns
    foreign_keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "scope_id") in foreign_keys
    assert ("tenant_id", "scope_type", "scope_id", "operation", "key_hash") in unique_columns


def test_nullable_tenant_session_links_reject_partial_context() -> None:
    for table_name in ("auth_sessions", "refresh_token_records"):
        table = Base.metadata.tables[table_name]
        check_sql = " ".join(
            str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        assert "tenant_id IS NULL AND membership_id IS NULL" in check_sql
        assert "tenant_id IS NOT NULL AND membership_id IS NOT NULL" in check_sql

        foreign_keys = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert ("tenant_id", "user_id", "membership_id") in foreign_keys


def test_refresh_session_and_replacement_links_are_context_scoped() -> None:
    session_table = Base.metadata.tables["auth_sessions"]
    refresh_table = Base.metadata.tables["refresh_token_records"]
    session_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in session_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    refresh_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in refresh_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    refresh_foreign_keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in refresh_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert {("tenant_id", "id"), ("user_id", "id")} <= session_uniques
    assert {("tenant_id", "id"), ("session_id", "id")} <= refresh_uniques
    assert {
        ("tenant_id", "session_id"),
        ("user_id", "session_id"),
        ("tenant_id", "replaced_by_id"),
        ("session_id", "replaced_by_id"),
    } <= refresh_foreign_keys
