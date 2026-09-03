from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.application.permission_rollout import (
    GlobalFeatureSnapshot,
    PermissionFeatureManifest,
    ReconciliationProof,
    RolloutDirection,
    RolloutPhaseError,
    TenantFeaturePhase,
    TenantFeatureSnapshot,
    rollout_digest,
)
from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.infrastructure.persistence.models import (
    PermissionFeatureRolloutModel,
    PermissionFeatureTenantStateModel,
    PermissionModel,
    RoleTemplateModel,
    RoleTemplatePermissionModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
)

_FEATURE_CODE = "ai_job_runtime_v1"


class PermissionRolloutRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_global(self, *, for_update: bool = False) -> GlobalFeatureSnapshot:
        statement = select(PermissionFeatureRolloutModel).where(
            PermissionFeatureRolloutModel.feature_code == _FEATURE_CODE
        )
        if for_update:
            statement = statement.with_for_update()
        model = await self._session.scalar(statement)
        if model is None:
            raise RolloutPhaseError("feature_row_missing")
        return GlobalFeatureSnapshot(
            feature_code=model.feature_code,
            phase=model.phase,
            manifest_version=model.manifest_version,
            manifest_digest=bytes(model.manifest_digest),
            rollout_generation=model.rollout_generation,
            version=model.version,
        )

    async def reconcile_expand_window(
        self, manifest: PermissionFeatureManifest
    ) -> ReconciliationProof:
        tenant_ids = sorted((await self._session.scalars(select(TenantModel.id))).all(), key=str)
        state_tenant_ids = set(
            (await self._session.scalars(select(PermissionFeatureTenantStateModel.tenant_id))).all()
        )
        for tenant_id in tenant_ids:
            if tenant_id not in state_tenant_ids:
                self._session.add(
                    PermissionFeatureTenantStateModel(
                        id=new_uuid7(),
                        feature_code=_FEATURE_CODE,
                        tenant_id=tenant_id,
                        phase="catalog_only",
                        target_manifest_version=manifest.base_version,
                        applied_manifest_version=manifest.base_version,
                        target_rollout_generation=0,
                        applied_rollout_generation=0,
                    )
                )
        await self._session.flush()
        state_ids = sorted(
            (
                await self._session.scalars(select(PermissionFeatureTenantStateModel.tenant_id))
            ).all(),
            key=str,
        )
        tenant_digest = rollout_digest(*tenant_ids) if tenant_ids else ""
        state_digest = rollout_digest(*state_ids) if state_ids else ""
        return ReconciliationProof(
            len(tenant_ids),
            len(state_ids),
            f"{tenant_digest}:{state_digest}",
        )

    async def begin_phase_transition(
        self,
        direction: RolloutDirection,
        manifest: PermissionFeatureManifest,
        evidence: object,
    ) -> int:
        current = await self.load_global(for_update=True)
        if direction is RolloutDirection.ACTIVATE:
            expected_phase = "catalog_only"
            new_phase = "activating"
        else:
            expected_phase = "activated"
            new_phase = "deactivating"
        if current.phase != expected_phase:
            raise RolloutPhaseError(f"expected_{expected_phase}")
        if direction is RolloutDirection.ACTIVATE:
            await self._add_template_permissions(manifest)
        new_generation = current.rollout_generation + 1
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(PermissionFeatureRolloutModel)
                .where(
                    PermissionFeatureRolloutModel.feature_code == _FEATURE_CODE,
                    PermissionFeatureRolloutModel.phase == expected_phase,
                    PermissionFeatureRolloutModel.rollout_generation == current.rollout_generation,
                    PermissionFeatureRolloutModel.version == current.version,
                )
                .values(
                    phase=new_phase,
                    rollout_generation=new_generation,
                    version=PermissionFeatureRolloutModel.version + 1,
                )
            ),
        )
        if changed.rowcount != 1:
            raise RolloutPhaseError("generation_cas_failed")
        return new_generation

    async def finalize_phase_transition(
        self,
        direction: RolloutDirection,
        generation: int,
        manifest: PermissionFeatureManifest,
    ) -> None:
        current = await self.load_global(for_update=True)
        if current.rollout_generation != generation:
            raise RolloutPhaseError("generation_mismatch")
        if direction is RolloutDirection.ACTIVATE:
            expected_phase = "activating"
            new_phase = "activated"
        else:
            expected_phase = "deactivating"
            new_phase = "catalog_only"
        if current.phase != expected_phase:
            raise RolloutPhaseError(f"expected_{expected_phase}")
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(PermissionFeatureRolloutModel)
                .where(
                    PermissionFeatureRolloutModel.feature_code == _FEATURE_CODE,
                    PermissionFeatureRolloutModel.phase == expected_phase,
                    PermissionFeatureRolloutModel.rollout_generation == generation,
                    PermissionFeatureRolloutModel.version == current.version,
                )
                .values(
                    phase=new_phase,
                    version=PermissionFeatureRolloutModel.version + 1,
                )
            ),
        )
        if changed.rowcount != 1:
            raise RolloutPhaseError("finalize_cas_failed")
        if direction is RolloutDirection.DEACTIVATE:
            await self._remove_template_permissions(manifest)

    async def claim_tenant(
        self,
        tenant_id: UUID,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        direction: RolloutDirection,
    ) -> TenantFeatureSnapshot:
        require_uuid7(tenant_id, field="rollout tenant_id")
        model = await self._session.scalar(
            select(PermissionFeatureTenantStateModel)
            .where(
                PermissionFeatureTenantStateModel.feature_code == _FEATURE_CODE,
                PermissionFeatureTenantStateModel.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if model is None:
            raise RolloutPhaseError("tenant_state_missing")
        if model.applied_rollout_generation >= generation:
            return _tenant_snapshot(model)
        target_manifest = (
            manifest.feature_version
            if direction is RolloutDirection.ACTIVATE
            else manifest.base_version
        )
        await self._session.execute(
            update(PermissionFeatureTenantStateModel)
            .where(PermissionFeatureTenantStateModel.id == model.id)
            .values(
                target_manifest_version=target_manifest,
                target_rollout_generation=generation,
                claim_owner="rollout-worker",
                claim_token=new_uuid7(),
                claim_fence=(model.claim_fence or 0) + 1,
                claim_expires_at=_naive_utc_now() + timedelta(minutes=5),
                version=PermissionFeatureTenantStateModel.version + 1,
            )
        )
        await self._session.flush()
        refreshed = await self._session.get(PermissionFeatureTenantStateModel, model.id)
        if refreshed is None:
            raise RolloutPhaseError("tenant_state_missing")
        return _tenant_snapshot(refreshed)

    async def claim_activation_batch(
        self,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        limit: int,
    ) -> tuple[UUID, ...]:
        return await self._claim_batch(
            generation=generation,
            manifest=manifest,
            limit=limit,
            target_manifest=manifest.feature_version,
        )

    async def claim_deactivation_batch(
        self,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        limit: int,
    ) -> tuple[UUID, ...]:
        return await self._claim_batch(
            generation=generation,
            manifest=manifest,
            limit=limit,
            target_manifest=manifest.base_version,
        )

    async def _claim_batch(
        self,
        *,
        generation: int,
        manifest: PermissionFeatureManifest,
        limit: int,
        target_manifest: str,
    ) -> tuple[UUID, ...]:
        models = (
            await self._session.scalars(
                select(PermissionFeatureTenantStateModel)
                .where(
                    PermissionFeatureTenantStateModel.feature_code == _FEATURE_CODE,
                    PermissionFeatureTenantStateModel.applied_rollout_generation < generation,
                )
                .order_by(PermissionFeatureTenantStateModel.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        ids = tuple(model.tenant_id for model in models)
        for model in models:
            await self._session.execute(
                update(PermissionFeatureTenantStateModel)
                .where(PermissionFeatureTenantStateModel.id == model.id)
                .values(
                    target_manifest_version=target_manifest,
                    target_rollout_generation=generation,
                    claim_owner="rollout-worker",
                    claim_token=new_uuid7(),
                    claim_fence=(model.claim_fence or 0) + 1,
                    claim_expires_at=_naive_utc_now() + timedelta(minutes=5),
                    version=PermissionFeatureTenantStateModel.version + 1,
                )
            )
        await self._session.flush()
        return ids

    async def apply_tenant_activation(
        self,
        *,
        tenant_id: UUID,
        generation: int,
        manifest: PermissionFeatureManifest,
    ) -> None:
        state = await self._session.scalar(
            select(PermissionFeatureTenantStateModel)
            .where(
                PermissionFeatureTenantStateModel.feature_code == _FEATURE_CODE,
                PermissionFeatureTenantStateModel.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if state is None:
            raise RolloutPhaseError("tenant_state_missing")
        await self._add_tenant_role_permissions(tenant_id, manifest)
        await self._session.execute(
            update(PermissionFeatureTenantStateModel)
            .where(PermissionFeatureTenantStateModel.id == state.id)
            .values(
                phase="activated",
                applied_manifest_version=manifest.feature_version,
                applied_rollout_generation=generation,
                claim_owner=None,
                claim_token=None,
                claim_fence=None,
                claim_expires_at=None,
                version=PermissionFeatureTenantStateModel.version + 1,
            )
        )

    async def apply_tenant_deactivation(
        self,
        *,
        tenant_id: UUID,
        generation: int,
        manifest: PermissionFeatureManifest,
    ) -> None:
        state = await self._session.scalar(
            select(PermissionFeatureTenantStateModel)
            .where(
                PermissionFeatureTenantStateModel.feature_code == _FEATURE_CODE,
                PermissionFeatureTenantStateModel.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if state is None:
            raise RolloutPhaseError("tenant_state_missing")
        await self._remove_tenant_role_permissions(tenant_id, manifest)
        await self._session.execute(
            update(PermissionFeatureTenantStateModel)
            .where(PermissionFeatureTenantStateModel.id == state.id)
            .values(
                phase="catalog_only",
                applied_manifest_version=manifest.base_version,
                applied_rollout_generation=generation,
                claim_owner=None,
                claim_token=None,
                claim_fence=None,
                claim_expires_at=None,
                version=PermissionFeatureTenantStateModel.version + 1,
            )
        )

    async def _add_template_permissions(self, manifest: PermissionFeatureManifest) -> None:
        templates = (
            await self._session.scalars(
                select(RoleTemplateModel).where(
                    RoleTemplateModel.code.in_(manifest.target_role_codes)
                )
            )
        ).all()
        permission_ids = []
        for code in sorted(manifest.permission_codes):
            permission_ids.append(await self._permission_id(code))
        for template in templates:
            for permission_id in permission_ids:
                self._session.add(
                    RoleTemplatePermissionModel(
                        role_template_id=template.id,
                        permission_id=permission_id,
                    )
                )

    async def _remove_template_permissions(self, manifest: PermissionFeatureManifest) -> None:
        templates = (
            await self._session.scalars(
                select(RoleTemplateModel).where(
                    RoleTemplateModel.code.in_(manifest.target_role_codes)
                )
            )
        ).all()
        permission_ids = []
        for code in sorted(manifest.permission_codes):
            permission_ids.append(await self._permission_id(code))
        for template in templates:
            for permission_id in permission_ids:
                await self._session.execute(
                    text(
                        "DELETE FROM role_template_permissions "
                        "WHERE role_template_id = :t AND permission_id = :p"
                    ),
                    {"t": template.id.bytes, "p": permission_id.bytes},
                )

    async def _add_tenant_role_permissions(
        self, tenant_id: UUID, manifest: PermissionFeatureManifest
    ) -> None:
        roles = (
            await self._session.scalars(
                select(TenantRoleModel).where(
                    TenantRoleModel.tenant_id == tenant_id,
                    TenantRoleModel.code.in_(manifest.target_role_codes),
                    TenantRoleModel.is_custom.is_(False),
                )
            )
        ).all()
        permission_ids = []
        for code in sorted(manifest.permission_codes):
            permission_ids.append(await self._permission_id(code))
        for role in roles:
            for permission_id in permission_ids:
                self._session.add(
                    TenantRolePermissionModel(
                        tenant_id=tenant_id,
                        tenant_role_id=role.id,
                        permission_id=permission_id,
                    )
                )

    async def _remove_tenant_role_permissions(
        self, tenant_id: UUID, manifest: PermissionFeatureManifest
    ) -> None:
        roles = (
            await self._session.scalars(
                select(TenantRoleModel).where(
                    TenantRoleModel.tenant_id == tenant_id,
                    TenantRoleModel.code.in_(manifest.target_role_codes),
                    TenantRoleModel.is_custom.is_(False),
                )
            )
        ).all()
        permission_ids = []
        for code in sorted(manifest.permission_codes):
            permission_ids.append(await self._permission_id(code))
        for role in roles:
            for permission_id in permission_ids:
                await self._session.execute(
                    text(
                        "DELETE FROM tenant_role_permissions "
                        "WHERE tenant_id = :t AND tenant_role_id = :r AND permission_id = :p"
                    ),
                    {"t": tenant_id.bytes, "r": role.id.bytes, "p": permission_id.bytes},
                )

    async def _permission_id(self, code: str) -> UUID:
        permission = await self._session.scalar(
            select(PermissionModel).where(PermissionModel.code == code)
        )
        if permission is None:
            raise RolloutPhaseError(f"permission_missing:{code}")
        return permission.id


def _tenant_snapshot(model: PermissionFeatureTenantStateModel) -> TenantFeatureSnapshot:
    return TenantFeatureSnapshot(
        tenant_id=model.tenant_id,
        feature_code=model.feature_code,
        phase=TenantFeaturePhase(model.phase),
        applied_manifest_version=model.applied_manifest_version,
        applied_rollout_generation=model.applied_rollout_generation,
        target_manifest_version=model.target_manifest_version,
        target_rollout_generation=model.target_rollout_generation,
        version=model.version,
    )


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
