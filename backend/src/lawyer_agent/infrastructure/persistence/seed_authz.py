from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.infrastructure.persistence.models import (
    PermissionModel,
    PlatformRoleModel,
    PlatformRolePermissionModel,
    RoleTemplateModel,
    RoleTemplatePermissionModel,
)


class SeedDriftError(RuntimeError):
    """Raised when a stable authorization code has changed meaning."""


@dataclass(frozen=True, slots=True)
class PermissionSeed:
    code: str
    resource: str
    action: str
    risk_level: str


@dataclass(frozen=True, slots=True)
class RoleSeed:
    code: str
    name: str
    description: str
    permission_codes: frozenset[str]


PERMISSIONS = (
    PermissionSeed("tenant.read", "tenant", "read", "medium"),
    PermissionSeed("tenant.update", "tenant", "update", "high"),
    PermissionSeed("department.read", "department", "read", "low"),
    PermissionSeed("department.manage", "department", "manage", "medium"),
    PermissionSeed("membership.read", "membership", "read", "medium"),
    PermissionSeed("membership.invite", "membership", "invite", "high"),
    PermissionSeed("membership.update", "membership", "update", "high"),
    PermissionSeed("membership.revoke", "membership", "revoke", "high"),
    PermissionSeed("role.read", "role", "read", "medium"),
    PermissionSeed("role.assign", "role", "assign", "high"),
    PermissionSeed("external_service.enable", "external_service", "enable", "high"),
    PermissionSeed("tenant_application.read", "tenant_application", "read", "high"),
    PermissionSeed("tenant_application.review", "tenant_application", "review", "critical"),
    PermissionSeed("platform_admin.bootstrap", "platform_admin", "bootstrap", "critical"),
    PermissionSeed("ai_job.create", "ai_job", "create", "medium"),
    PermissionSeed("ai_job.read", "ai_job", "read", "medium"),
    PermissionSeed("ai_job.cancel", "ai_job", "cancel", "high"),
)

TENANT_ROLE_TEMPLATES = (
    RoleSeed(
        "tenant_owner",
        "租户所有者",
        "管理租户、组织、成员和角色。",
        frozenset(
            {
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
            }
        ),
    ),
    RoleSeed(
        "tenant_admin",
        "租户管理员",
        "管理组织、成员和角色，不拥有租户所有权。",
        frozenset(
            {
                "tenant.read",
                "department.read",
                "department.manage",
                "membership.read",
                "membership.invite",
                "membership.update",
                "membership.revoke",
                "role.read",
                "role.assign",
            }
        ),
    ),
    RoleSeed(
        "department_admin",
        "部门管理员",
        "在 ABAC 限定的部门范围内管理成员。",
        frozenset(
            {
                "tenant.read",
                "department.read",
                "department.manage",
                "membership.read",
                "membership.invite",
                "membership.update",
                "role.read",
            }
        ),
    ),
    RoleSeed(
        "lawyer_or_legal",
        "律师或法务",
        "处理获授权的法律业务资源。",
        frozenset({"tenant.read", "department.read", "membership.read"}),
    ),
    RoleSeed(
        "assistant",
        "助理",
        "协助处理获授权的业务资源。",
        frozenset({"tenant.read", "department.read", "membership.read"}),
    ),
    RoleSeed(
        "teacher",
        "教师",
        "访问获授权的教学组织信息。",
        frozenset({"tenant.read", "department.read", "membership.read"}),
    ),
    RoleSeed(
        "student",
        "学生",
        "访问明确授权的教学入口。",
        frozenset({"tenant.read"}),
    ),
    RoleSeed(
        "external_client",
        "外部客户",
        "不授予租户目录权限，仅依赖后续资源级显式共享。",
        frozenset(),
    ),
)

PLATFORM_ROLES = (
    RoleSeed(
        "super_admin",
        "超级管理员",
        "执行平台管理与租户申请审核，不隐式读取租户内容。",
        frozenset(
            {
                "tenant_application.read",
                "tenant_application.review",
                "platform_admin.bootstrap",
            }
        ),
    ),
    RoleSeed(
        "security_auditor",
        "安全审计员",
        "只读平台租户申请投影。",
        frozenset({"tenant_application.read"}),
    ),
    RoleSeed(
        "operations_support",
        "运维支持",
        "只读平台租户申请投影，不读取租户内容。",
        frozenset({"tenant_application.read"}),
    ),
    RoleSeed(
        "content_operator",
        "内容运营",
        "本增量不授予身份授权相关权限。",
        frozenset(),
    ),
)

_PERMISSION_IDS = {
    "tenant.read": UUID("01a05a43-fc00-71d7-b192-012802c8a32f"),
    "tenant.update": UUID("01a05a43-fc01-7331-8edb-ef187552d6c8"),
    "department.read": UUID("01a05a43-fc02-7c5a-ab62-d845d4e75c2b"),
    "department.manage": UUID("01a05a43-fc03-701b-9e92-27a81dc1ad0f"),
    "membership.read": UUID("01a05a43-fc04-745f-a5e1-7454eacef1b3"),
    "membership.invite": UUID("01a05a43-fc05-7e99-a50e-0cd0111d7f0c"),
    "membership.update": UUID("01a05a43-fc06-7a7d-a791-a38b88378676"),
    "membership.revoke": UUID("01a05a43-fc07-764b-9bb3-3bc9331b379b"),
    "role.read": UUID("01a05a43-fc08-7836-b0b7-d179bd0edfd4"),
    "role.assign": UUID("01a05a43-fc09-7d87-a342-d34974baa746"),
    "external_service.enable": UUID("01a05a43-fc0a-786c-8484-fb1ab7800344"),
    "tenant_application.read": UUID("01a05a43-fc0b-7e93-accf-c44aaa047bbc"),
    "tenant_application.review": UUID("01a05a43-fc0c-7a7e-8984-a2d33422d48c"),
    "platform_admin.bootstrap": UUID("01a05a43-fc0d-7e1d-80bb-a8fc52c8d9ef"),
    "ai_job.create": UUID("01a05a44-fc00-7000-8000-0000000000c1"),
    "ai_job.read": UUID("01a05a44-fc01-7000-8000-0000000000c2"),
    "ai_job.cancel": UUID("01a05a44-fc02-7000-8000-0000000000c3"),
}

_TENANT_ROLE_TEMPLATE_IDS = {
    "tenant_owner": UUID("01a05a43-fc0e-7127-81c4-55ed27723e46"),
    "tenant_admin": UUID("01a05a43-fc0f-77ff-a078-846d42487291"),
    "department_admin": UUID("01a05a43-fc10-78e1-a29d-f40177fda51e"),
    "lawyer_or_legal": UUID("01a05a43-fc11-7864-a543-ff7460bddff7"),
    "assistant": UUID("01a05a43-fc12-7ddb-bebe-a6b1bce3249c"),
    "teacher": UUID("01a05a43-fc13-7234-982e-6c6e15288d75"),
    "student": UUID("01a05a43-fc14-79c4-84d1-88232617ea3c"),
    "external_client": UUID("01a05a43-fc15-761c-9c64-2d87d30d47d2"),
}

_PLATFORM_ROLE_IDS = {
    "super_admin": UUID("01a05a43-fc16-7840-b2b8-12f41393c818"),
    "security_auditor": UUID("01a05a43-fc17-7108-aec9-3ecc9dd96678"),
    "operations_support": UUID("01a05a43-fc18-7787-95bb-949b4824004e"),
    "content_operator": UUID("01a05a43-fc19-7ec5-8aaf-70edafb4c3ac"),
}


async def seed_authorization_catalog(session: AsyncSession) -> None:
    permissions = await _seed_permissions(session)
    await _seed_role_templates(session, permissions)
    await _seed_platform_roles(session, permissions)
    await session.flush()


async def _seed_permissions(session: AsyncSession) -> dict[str, PermissionModel]:
    expected_codes = {seed.code.casefold() for seed in PERMISSIONS}
    existing: dict[str, PermissionModel] = {}
    for stored_model in await session.scalars(select(PermissionModel)):
        normalized_code = stored_model.code.casefold()
        if normalized_code not in expected_codes:
            continue
        if normalized_code in existing:
            raise SeedDriftError(f"duplicate permission code: {stored_model.code}")
        existing[normalized_code] = stored_model
    for seed in PERMISSIONS:
        normalized_code = seed.code.casefold()
        model = existing.get(normalized_code)
        if model is None:
            model = PermissionModel(
                id=_PERMISSION_IDS[seed.code],
                code=seed.code,
                resource=seed.resource,
                action=seed.action,
                risk_level=seed.risk_level,
                status="active",
            )
            session.add(model)
            existing[normalized_code] = model
            continue
        if model.code != seed.code:
            raise SeedDriftError(f"permission code drift: {seed.code}")
        actual = (model.resource, model.action, model.risk_level, model.status)
        expected = (seed.resource, seed.action, seed.risk_level, "active")
        if actual != expected:
            raise SeedDriftError(f"permission semantic drift: {seed.code}")
    await session.flush()
    return existing


async def _seed_role_templates(
    session: AsyncSession,
    permissions: dict[str, PermissionModel],
) -> None:
    expected_codes = {seed.code.casefold() for seed in TENANT_ROLE_TEMPLATES}
    existing: dict[str, RoleTemplateModel] = {}
    for model in await session.scalars(select(RoleTemplateModel)):
        normalized_code = model.code.casefold()
        if normalized_code not in expected_codes:
            continue
        if normalized_code in existing:
            raise SeedDriftError(f"duplicate role code: {model.code}")
        existing[normalized_code] = model
    for seed in TENANT_ROLE_TEMPLATES:
        normalized_code = seed.code.casefold()
        role = existing.get(normalized_code)
        if role is None:
            role = RoleTemplateModel(
                id=_TENANT_ROLE_TEMPLATE_IDS[seed.code],
                code=seed.code,
                name=seed.name,
                description=seed.description,
                status="active",
            )
            session.add(role)
            await session.flush()
            for permission_code in sorted(seed.permission_codes):
                session.add(
                    RoleTemplatePermissionModel(
                        role_template_id=role.id,
                        permission_id=permissions[permission_code].id,
                    )
                )
            await session.flush()
            existing[normalized_code] = role
            continue

        if role.code != seed.code:
            raise SeedDriftError(f"role code drift: {seed.code}")

        actual_semantics = (role.name, role.description, role.status)
        expected_semantics = (seed.name, seed.description, "active")
        if actual_semantics != expected_semantics:
            raise SeedDriftError(f"role semantic drift: {seed.code}")

        actual_permission_codes = frozenset(
            (
                await session.scalars(
                    select(PermissionModel.code)
                    .join(
                        RoleTemplatePermissionModel,
                        RoleTemplatePermissionModel.permission_id == PermissionModel.id,
                    )
                    .where(RoleTemplatePermissionModel.role_template_id == role.id)
                )
            ).all()
        )
        if actual_permission_codes != seed.permission_codes:
            raise SeedDriftError(f"role permission drift: {seed.code}")


async def _seed_platform_roles(
    session: AsyncSession,
    permissions: dict[str, PermissionModel],
) -> None:
    expected_codes = {seed.code.casefold() for seed in PLATFORM_ROLES}
    existing: dict[str, PlatformRoleModel] = {}
    for model in await session.scalars(select(PlatformRoleModel)):
        normalized_code = model.code.casefold()
        if normalized_code not in expected_codes:
            continue
        if normalized_code in existing:
            raise SeedDriftError(f"duplicate role code: {model.code}")
        existing[normalized_code] = model
    for seed in PLATFORM_ROLES:
        normalized_code = seed.code.casefold()
        role = existing.get(normalized_code)
        if role is None:
            role = PlatformRoleModel(
                id=_PLATFORM_ROLE_IDS[seed.code],
                code=seed.code,
                name=seed.name,
                description=seed.description,
                status="active",
            )
            session.add(role)
            await session.flush()
            for permission_code in sorted(seed.permission_codes):
                session.add(
                    PlatformRolePermissionModel(
                        platform_role_id=role.id,
                        permission_id=permissions[permission_code].id,
                    )
                )
            await session.flush()
            existing[normalized_code] = role
            continue

        if role.code != seed.code:
            raise SeedDriftError(f"role code drift: {seed.code}")

        actual_semantics = (role.name, role.description, role.status)
        expected_semantics = (seed.name, seed.description, "active")
        if actual_semantics != expected_semantics:
            raise SeedDriftError(f"role semantic drift: {seed.code}")

        actual_permission_codes = frozenset(
            (
                await session.scalars(
                    select(PermissionModel.code)
                    .join(
                        PlatformRolePermissionModel,
                        PlatformRolePermissionModel.permission_id == PermissionModel.id,
                    )
                    .where(PlatformRolePermissionModel.platform_role_id == role.id)
                )
            ).all()
        )
        if actual_permission_codes != seed.permission_codes:
            raise SeedDriftError(f"role permission drift: {seed.code}")
