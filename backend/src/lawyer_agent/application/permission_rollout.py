from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from lawyer_agent.domain.ai_jobs import (
    AI_JOB_BASE_MANIFEST_VERSION,
    AI_JOB_FEATURE_CODE,
    AI_JOB_FEATURE_MANIFEST_VERSION,
    ai_job_manifest_digest,
)


class RolloutDirection(StrEnum):
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"


class TenantFeaturePhase(StrEnum):
    CATALOG_ONLY = "catalog_only"
    ACTIVATING = "activating"
    ACTIVATED = "activated"
    DEACTIVATING = "deactivating"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PermissionFeatureManifest:
    feature_code: str
    base_version: str
    feature_version: str
    permission_codes: frozenset[str]
    target_role_codes: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.feature_code, str) or not self.feature_code:
            raise ValueError("manifest feature code is invalid")
        if not isinstance(self.base_version, str) or not self.base_version:
            raise ValueError("manifest base version is invalid")
        if not isinstance(self.feature_version, str) or not self.feature_version:
            raise ValueError("manifest feature version is invalid")
        if not isinstance(self.permission_codes, frozenset) or not self.permission_codes:
            raise ValueError("manifest permission codes must be a non-empty frozenset")
        if not isinstance(self.target_role_codes, frozenset) or not self.target_role_codes:
            raise ValueError("manifest target roles must be a non-empty frozenset")

    @property
    def base_digest(self) -> bytes:
        return ai_job_manifest_digest(self.base_version, ())

    @property
    def feature_digest(self) -> bytes:
        return ai_job_manifest_digest(
            self.feature_version,
            tuple(sorted(self.permission_codes)),
        )


AI_JOB_PERMISSION_MANIFEST = PermissionFeatureManifest(
    feature_code=AI_JOB_FEATURE_CODE,
    base_version=AI_JOB_BASE_MANIFEST_VERSION,
    feature_version=AI_JOB_FEATURE_MANIFEST_VERSION,
    permission_codes=frozenset({"ai_job.create", "ai_job.read", "ai_job.cancel"}),
    target_role_codes=frozenset(
        {
            "tenant_owner",
            "tenant_admin",
            "department_admin",
            "lawyer_or_legal",
            "assistant",
            "teacher",
        }
    ),
)


@dataclass(frozen=True, slots=True)
class WritersDrainedEvidence:
    deployment_reference: str
    drained_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_reference, str) or not self.deployment_reference:
            raise ValueError("drain evidence deployment reference is invalid")
        if not isinstance(self.drained_at, datetime) or self.drained_at.tzinfo is None:
            raise ValueError("drain evidence timestamp must be UTC-aware")


@dataclass(frozen=True, slots=True)
class ReconciliationProof:
    tenant_count: int
    state_count: int
    digest: str

    @property
    def balanced(self) -> bool:
        return self.tenant_count == self.state_count


@dataclass(frozen=True, slots=True)
class RolloutBatchResult:
    claimed: int
    converged: int
    failed: int


@dataclass(frozen=True, slots=True)
class GlobalFeatureSnapshot:
    feature_code: str
    phase: str
    manifest_version: str
    manifest_digest: bytes
    rollout_generation: int
    version: int


@dataclass(frozen=True, slots=True)
class TenantFeatureSnapshot:
    tenant_id: UUID
    feature_code: str
    phase: TenantFeaturePhase
    applied_manifest_version: str
    applied_rollout_generation: int
    target_manifest_version: str
    target_rollout_generation: int
    version: int


class RolloutEnvironmentMismatch(RuntimeError):
    code = "rollout_environment_mismatch"

    def __init__(self) -> None:
        super().__init__("permission rollout is not allowed in this environment")


class RolloutPhaseError(RuntimeError):
    code = "rollout_phase_invalid"

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"permission rollout phase transition failed: {reason_code}")


class PermissionRolloutRepositoryPort(Protocol):
    async def load_global(self, *, for_update: bool = False) -> GlobalFeatureSnapshot: ...

    async def reconcile_expand_window(
        self, manifest: PermissionFeatureManifest
    ) -> ReconciliationProof: ...

    async def begin_phase_transition(
        self,
        direction: RolloutDirection,
        manifest: PermissionFeatureManifest,
        evidence: WritersDrainedEvidence | None,
    ) -> int: ...

    async def finalize_phase_transition(
        self,
        direction: RolloutDirection,
        generation: int,
        manifest: PermissionFeatureManifest,
    ) -> None: ...

    async def claim_tenant(
        self,
        tenant_id: UUID,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        direction: RolloutDirection,
    ) -> TenantFeatureSnapshot: ...

    async def claim_activation_batch(
        self,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        limit: int,
    ) -> tuple[UUID, ...]: ...

    async def claim_deactivation_batch(
        self,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        limit: int,
    ) -> tuple[UUID, ...]: ...

    async def apply_tenant_activation(
        self,
        *,
        tenant_id: UUID,
        generation: int,
        manifest: PermissionFeatureManifest,
    ) -> None: ...

    async def apply_tenant_deactivation(
        self,
        *,
        tenant_id: UUID,
        generation: int,
        manifest: PermissionFeatureManifest,
    ) -> None: ...


class PermissionRolloutUnitOfWork(Protocol):
    rollout: PermissionRolloutRepositoryPort

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class PermissionFeatureRolloutService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], PermissionRolloutUnitOfWork],
        environment: str,
        manifest: PermissionFeatureManifest = AI_JOB_PERMISSION_MANIFEST,
    ) -> None:
        if environment not in {"development", "test", "staging", "production"}:
            raise ValueError("rollout environment is invalid")
        self._uow_factory = uow_factory
        self._environment = environment
        self._manifest = manifest

    async def reconcile_expand_window(
        self, evidence: WritersDrainedEvidence
    ) -> ReconciliationProof:
        if not isinstance(evidence, WritersDrainedEvidence):
            raise ValueError("drain evidence must be strongly typed")
        async with self._uow_factory() as uow:
            return await uow.rollout.reconcile_expand_window(self._manifest)

    async def begin_activation(self, evidence: WritersDrainedEvidence) -> int:
        self._require_non_production()
        if not isinstance(evidence, WritersDrainedEvidence):
            raise ValueError("drain evidence must be strongly typed")
        async with self._uow_factory() as uow:
            return await uow.rollout.begin_phase_transition(
                RolloutDirection.ACTIVATE,
                self._manifest,
                evidence,
            )

    async def activate_batch(self, *, generation: int, limit: int) -> RolloutBatchResult:
        self._require_non_production()
        _require_positive(limit, "activation batch limit")
        converged = 0
        failed = 0
        claimed = 0
        async with self._uow_factory() as uow:
            tenant_ids = await uow.rollout.claim_activation_batch(
                generation=generation,
                manifest=self._manifest,
                limit=limit,
            )
        for tenant_id in tenant_ids:
            try:
                async with self._uow_factory() as uow:
                    await uow.rollout.apply_tenant_activation(
                        tenant_id=tenant_id,
                        generation=generation,
                        manifest=self._manifest,
                    )
                converged += 1
            except Exception:
                failed += 1
        claimed = len(tenant_ids)
        return RolloutBatchResult(claimed, converged, failed)

    async def begin_deactivation(self) -> int:
        self._require_non_production()
        async with self._uow_factory() as uow:
            return await uow.rollout.begin_phase_transition(
                RolloutDirection.DEACTIVATE,
                self._manifest,
                None,
            )

    async def deactivate_batch(self, *, generation: int, limit: int) -> RolloutBatchResult:
        self._require_non_production()
        _require_positive(limit, "deactivation batch limit")
        async with self._uow_factory() as uow:
            tenant_ids = await uow.rollout.claim_deactivation_batch(
                generation=generation,
                manifest=self._manifest,
                limit=limit,
            )
        converged = 0
        failed = 0
        for tenant_id in tenant_ids:
            try:
                async with self._uow_factory() as uow:
                    await uow.rollout.apply_tenant_deactivation(
                        tenant_id=tenant_id,
                        generation=generation,
                        manifest=self._manifest,
                    )
                converged += 1
            except Exception:
                failed += 1
        return RolloutBatchResult(len(tenant_ids), converged, failed)

    async def finalize(self, *, direction: RolloutDirection, generation: int) -> None:
        self._require_non_production()
        async with self._uow_factory() as uow:
            await uow.rollout.finalize_phase_transition(
                direction,
                generation,
                self._manifest,
            )

    async def claim_tenant(
        self,
        tenant_id: UUID,
        *,
        generation: int,
        direction: RolloutDirection = RolloutDirection.ACTIVATE,
    ) -> TenantFeatureSnapshot:
        async with self._uow_factory() as uow:
            return await uow.rollout.claim_tenant(
                tenant_id,
                generation=generation,
                manifest=self._manifest,
                direction=direction,
            )

    def _require_non_production(self) -> None:
        if self._environment == "production":
            raise RolloutEnvironmentMismatch


def _require_positive(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def rollout_digest(*uuids: UUID) -> str:
    material = b"".join(uuid.bytes for uuid in uuids)
    return hashlib.sha256(material).hexdigest()
