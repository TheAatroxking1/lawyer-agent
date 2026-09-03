from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType, TracebackType
from typing import Final, Protocol, Self
from uuid import UUID

from lawyer_agent.application.idempotency import IdempotencyRepositoryPort
from lawyer_agent.application.tenancy import (
    TenantAuditRepositoryPort,
    TenantAuthorizationSnapshot,
)
from lawyer_agent.domain.ai_jobs import (
    JOB_POLICY_VERSION,
    JOB_SCOPE_MANIFEST_VERSION,
    AIJob,
    AIJobPermission,
    JobAccess,
    JobAuthorizationScope,
    JobExecutionGrant,
    JobScopeCode,
    NewAIJobGraph,
)
from lawyer_agent.domain.tenancy import Membership, MembershipStatus, TenantContext

JOB_SCOPE_BY_ROLE: Final[Mapping[str, JobAuthorizationScope]] = MappingProxyType(
    {
        "tenant_owner": JobAuthorizationScope(True, True, True),
        "tenant_admin": JobAuthorizationScope(True, True, True),
        "department_admin": JobAuthorizationScope(True, True, False),
        "lawyer_or_legal": JobAuthorizationScope(True, True, False),
        "assistant": JobAuthorizationScope(True, True, False),
        "teacher": JobAuthorizationScope(True, True, False),
    }
)


def scope_from_code(code: JobScopeCode) -> JobAuthorizationScope:
    if code is JobScopeCode.TENANT_WIDE:
        return JobAuthorizationScope(True, True, True)
    return JobAuthorizationScope(True, True, False)


class JobAuthorizationDenied(Exception):
    code = "authorization_denied"

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"ai job action is not authorized: {reason_code}")


class JobAuthorizationResolver:
    def resolve(
        self,
        snapshot: TenantAuthorizationSnapshot,
        permission: AIJobPermission,
    ) -> JobAuthorizationScope:
        if not isinstance(permission, AIJobPermission):
            raise JobAuthorizationDenied("unknown_permission")
        if permission.value not in snapshot.permissions:
            raise JobAuthorizationDenied("permission_denied")
        role_codes = snapshot.role_codes
        if not role_codes or any(role not in JOB_SCOPE_BY_ROLE for role in role_codes):
            raise JobAuthorizationDenied("job_scope_unmapped")
        scopes = [JOB_SCOPE_BY_ROLE[role] for role in role_codes]
        return JobAuthorizationScope(
            owner=any(scope.owner for scope in scopes),
            shared=any(scope.shared for scope in scopes),
            tenant_wide=any(scope.tenant_wide for scope in scopes),
        )


@dataclass(frozen=True, slots=True)
class JobAuthorizationDecision:
    allowed: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class JobAuthoritySnapshot:
    job: AIJob
    grant: JobExecutionGrant
    user_active: bool
    tenant_active: bool
    membership: Membership
    role_codes: frozenset[str]
    permission_codes: frozenset[str]
    auth_version: int
    authz_version: int
    resolved_scope: JobAuthorizationScope
    resolved_scope_code: JobScopeCode
    granted_scope: JobAuthorizationScope
    unknown_or_mixed_role_codes: bool

    def membership_active_at(self, now: datetime) -> bool:
        membership = self.membership
        return (
            membership.status is MembershipStatus.ACTIVE
            and membership.valid_from <= now
            and (membership.valid_until is None or membership.valid_until > now)
        )

    def with_current_authz_version(self, authz_version: int) -> JobAuthoritySnapshot:
        return JobAuthoritySnapshot(
            job=self.job,
            grant=self.grant,
            user_active=self.user_active,
            tenant_active=self.tenant_active,
            membership=self.membership,
            role_codes=self.role_codes,
            permission_codes=self.permission_codes,
            auth_version=self.auth_version,
            authz_version=authz_version,
            resolved_scope=self.resolved_scope,
            resolved_scope_code=self.resolved_scope_code,
            granted_scope=self.granted_scope,
            unknown_or_mixed_role_codes=self.unknown_or_mixed_role_codes,
        )


class DurableGrantPolicy:
    def authorize(
        self,
        snapshot: JobAuthoritySnapshot,
        *,
        now: datetime,
    ) -> JobAuthorizationDecision:
        checks: tuple[tuple[bool, str], ...] = (
            (snapshot.job.tenant_id == snapshot.grant.tenant_id, "grant_tenant_mismatch"),
            (snapshot.job.id == snapshot.grant.job_id, "grant_job_mismatch"),
            (
                snapshot.job.created_by_user_id == snapshot.grant.user_id,
                "grant_owner_mismatch",
            ),
            (
                snapshot.job.created_by_membership_id == snapshot.grant.membership_id,
                "grant_membership_mismatch",
            ),
            (snapshot.job.expires_at > now, "job_expired"),
            (snapshot.grant.revoked_at is None, "grant_revoked"),
            (snapshot.user_active, "user_inactive"),
            (snapshot.tenant_active, "tenant_inactive"),
            (snapshot.membership_active_at(now), "membership_inactive"),
            (
                snapshot.membership.user_id == snapshot.grant.user_id,
                "membership_user_mismatch",
            ),
            (
                snapshot.membership.tenant_id == snapshot.grant.tenant_id,
                "membership_tenant_mismatch",
            ),
            (
                snapshot.grant.auth_version_at_submit == snapshot.auth_version,
                "auth_version_changed",
            ),
            (
                snapshot.grant.authz_version_at_submit == snapshot.authz_version,
                "authz_version_changed",
            ),
            (
                snapshot.grant.permission_code is AIJobPermission.CREATE,
                "grant_permission_invalid",
            ),
            (
                snapshot.grant.policy_version == JOB_POLICY_VERSION,
                "grant_policy_version_mismatch",
            ),
            (
                snapshot.grant.job_scope_manifest_version == JOB_SCOPE_MANIFEST_VERSION,
                "grant_manifest_version_mismatch",
            ),
            (
                snapshot.grant.job_scope_code == snapshot.resolved_scope_code,
                "grant_scope_code_mismatch",
            ),
            (AIJobPermission.CREATE.value in snapshot.permission_codes, "permission_denied"),
            (not snapshot.unknown_or_mixed_role_codes, "unknown_role"),
            (snapshot.resolved_scope == snapshot.granted_scope, "scope_mismatch"),
        )
        for ok, reason_code in checks:
            if not ok:
                return JobAuthorizationDecision(False, reason_code)
        return JobAuthorizationDecision(True, "allowed")


class AIJobAuthorityLoader(Protocol):
    async def load(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> JobAuthoritySnapshot: ...


class AIJobRepositoryPort(Protocol):
    async def get(
        self,
        context: TenantContext,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> AIJob | None: ...

    async def add_job_graph(self, graph: NewAIJobGraph) -> None: ...

    async def get_access(
        self,
        context: TenantContext,
        job_id: UUID,
        membership_id: UUID,
    ) -> JobAccess | None: ...


class AIJobUnitOfWork(Protocol):
    jobs: AIJobRepositoryPort
    runtime: object
    idempotency: IdempotencyRepositoryPort
    audit: TenantAuditRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
