from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7


@dataclass(frozen=True, slots=True)
class PermissionFeatureSnapshot:
    feature_code: str
    phase: str
    manifest_version: str
    manifest_digest: bytes
    rollout_generation: int
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.feature_code, str) or not self.feature_code:
            raise ValueError("feature snapshot code is invalid")
        if self.phase not in {"catalog_only", "activating", "activated", "deactivating"}:
            raise ValueError("feature snapshot phase is unknown")
        if not isinstance(self.manifest_version, str) or not self.manifest_version:
            raise ValueError("feature snapshot manifest version is invalid")
        if not isinstance(self.manifest_digest, bytes) or len(self.manifest_digest) != 32:
            raise ValueError("feature snapshot manifest digest must be 32 bytes")
        if (
            isinstance(self.rollout_generation, bool)
            or not isinstance(self.rollout_generation, int)
            or self.rollout_generation < 0
        ):
            raise ValueError("feature snapshot generation must be non-negative")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("feature snapshot version must be positive")


@dataclass(frozen=True, slots=True)
class TenantFeatureSnapshot:
    tenant_id: UUID
    feature_code: str
    phase: str
    applied_manifest_version: str
    applied_rollout_generation: int
    target_manifest_version: str
    target_rollout_generation: int
    version: int

    def __post_init__(self) -> None:
        require_uuid7(self.tenant_id, field="tenant feature snapshot tenant_id")
        if not isinstance(self.feature_code, str) or not self.feature_code:
            raise ValueError("tenant feature snapshot code is invalid")
        if self.phase not in {"catalog_only", "activating", "activated", "deactivating", "failed"}:
            raise ValueError("tenant feature snapshot phase is unknown")
        if not isinstance(self.applied_manifest_version, str) or not self.applied_manifest_version:
            raise ValueError("tenant feature applied manifest is invalid")
        if not isinstance(self.target_manifest_version, str) or not self.target_manifest_version:
            raise ValueError("tenant feature target manifest is invalid")
        for generation in (self.applied_rollout_generation, self.target_rollout_generation):
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError("tenant feature generation must be non-negative")
        if self.target_rollout_generation < self.applied_rollout_generation:
            raise ValueError("tenant feature target generation precedes applied generation")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("tenant feature version must be positive")


@dataclass(frozen=True, slots=True)
class TenantSecurityWriteLockRequest:
    tenant_id: UUID
    actor_user_id: UUID
    actor_session_id: UUID
    actor_membership_id: UUID
    target_membership_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "tenant security lock tenant_id"),
            (self.actor_user_id, "tenant security lock actor_user_id"),
            (self.actor_session_id, "tenant security lock actor_session_id"),
            (self.actor_membership_id, "tenant security lock actor_membership_id"),
        ):
            require_uuid7(value, field=name)
        for value in self.target_membership_ids:
            require_uuid7(value, field="tenant security lock target_membership_id")
        if self.target_membership_ids != tuple(
            sorted(set(self.target_membership_ids), key=str)
        ):
            raise ValueError("tenant security lock targets must be unique and ordered")


@dataclass(frozen=True, slots=True)
class SessionFamilyWriteLockRequest:
    session_id: UUID
    family_id: UUID
    tenant_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        require_uuid7(self.session_id, field="session security lock session_id")
        require_uuid7(self.family_id, field="session security lock family_id")
        for tenant_id in self.tenant_ids:
            require_uuid7(tenant_id, field="session security lock tenant_id")
        if self.tenant_ids != tuple(sorted(set(self.tenant_ids), key=str)):
            raise ValueError("session security lock tenants must be unique and ordered")


class SecurityWriteLockRepositoryPort(Protocol):
    async def acquire_tenant_gate(self, tenant_id: UUID) -> bool: ...

    async def acquire_tenant_write(self, request: TenantSecurityWriteLockRequest) -> bool: ...

    async def acquire_session_family(
        self, request: SessionFamilyWriteLockRequest
    ) -> bool: ...

    async def lock_global_feature_for_tenant_create(self) -> PermissionFeatureSnapshot: ...

    async def lock_tenant_feature_shared(self, tenant_id: UUID) -> TenantFeatureSnapshot: ...
