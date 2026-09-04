"""HTTP-facing Rule Pack administration orchestration (non-model).

Wraps the tenant rule pack repository write methods behind a unit of work so
HTTP endpoints only handle tenant actors, commands and projections. Every
read/write stays inside the tenant context; there is no un-scoped ``get_by_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.audit import new_tenant_user_audit_event
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
)
from lawyer_agent.domain.rule_pack_management import build_pack_rule
from lawyer_agent.domain.tenancy import TenantContext


class RulePackAdminError(Exception):
    status: int = 500
    code: str = "rule_pack_admin_error"
    title: str = "Rule pack administration failed"


class RulePackAdminNotFound(RulePackAdminError):
    status = 404
    code = "rule_pack_admin_not_found"
    title = "Rule pack not found for this tenant"


class RulePackAdminConflict(RulePackAdminError):
    status = 409
    code = "rule_pack_admin_conflict"
    title = "Rule pack request conflicts with current state"


class RulePackAdminInvalidRequest(RulePackAdminError):
    status = 422
    code = "rule_pack_admin_invalid_request"
    title = "Rule pack request is invalid"


class RulePackAdminStorePort(Protocol):
    async def list_packs(self, context: TenantContext) -> tuple[RulePack, ...]: ...

    async def create_pack(self, context: TenantContext, *, name: str) -> RulePack: ...

    async def add_rule(
        self, context: TenantContext, rule: RulePackRule
    ) -> RulePackRule: ...

    async def set_rule_enabled(
        self,
        context: TenantContext,
        pack_id: UUID,
        rule_id: UUID,
        *,
        enabled: bool,
    ) -> bool: ...

    async def activate_pack(
        self, context: TenantContext, pack_id: UUID
    ) -> bool: ...

    async def get_active_pack(self, context: TenantContext) -> RulePack | None: ...

    async def rules_for_pack(
        self, context: TenantContext, pack_id: UUID
    ) -> tuple[RulePackRule, ...]: ...


class RulePackAdminUnitOfWorkPort(Protocol):
    rule_pack: RulePackAdminStorePort

    async def __aenter__(self) -> RulePackAdminUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class RulePackAdminHttpService:
    """Composition facade used by the tenant HTTP endpoints."""

    def __init__(self, uow_factory: Callable[[], object]) -> None:
        if not callable(uow_factory):
            raise ValueError("rule pack admin service requires a unit of work factory")
        self._uow_factory = uow_factory

    async def create_pack(self, *, context: TenantContext, name: str) -> RulePack:
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            try:
                return await uow.rule_pack.create_pack(context, name=name)
            except ValueError as exc:
                raise RulePackAdminInvalidRequest from exc

    async def list_packs(self, *, context: TenantContext) -> tuple[RulePack, ...]:
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            return tuple(await uow.rule_pack.list_packs(context))

    async def add_rule(
        self,
        *,
        context: TenantContext,
        pack_id: UUID,
        trigger_kind: RuleTriggerKind,
        label: str,
        pattern: str,
        risk_level: RiskLevel,
        suggestion: str,
    ) -> RulePackRule:
        require_uuid7(pack_id, field="pack_id")
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_pack(uow, context, pack_id)
            try:
                rule = build_pack_rule(
                    tenant_id=context.tenant_id,
                    pack_id=pack_id,
                    trigger_kind=trigger_kind,
                    label=label,
                    pattern=pattern,
                    risk_level=risk_level,
                    suggestion=suggestion,
                    enabled=True,
                )
                return await uow.rule_pack.add_rule(context, rule)
            except ValueError as exc:
                raise RulePackAdminInvalidRequest from exc

    async def set_rule_enabled(
        self,
        *,
        context: TenantContext,
        pack_id: UUID,
        rule_id: UUID,
        enabled: bool,
    ) -> None:
        require_uuid7(pack_id, field="pack_id")
        require_uuid7(rule_id, field="rule_id")
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_pack(uow, context, pack_id)
            changed = await uow.rule_pack.set_rule_enabled(
                context, pack_id, rule_id, enabled=enabled
            )
            if not changed:
                raise RulePackAdminNotFound

    async def activate_pack(
        self,
        *,
        context: TenantContext,
        pack_id: UUID,
        trace_id: str | None = None,
    ) -> RulePack:
        require_uuid7(pack_id, field="pack_id")
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_pack(uow, context, pack_id)
            activated = await uow.rule_pack.activate_pack(context, pack_id)
            if not activated:
                raise RulePackAdminConflict
            await _append_audit(
                uow,
                context=context,
                action="rule_pack.activate",
                reason_code="activated",
                target_type="rule_pack",
                target_id=pack_id,
                trace_id=trace_id,
            )
            return await self._require_pack(uow, context, pack_id)

    async def _require_pack(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        context: TenantContext,
        pack_id: UUID,
    ) -> RulePack:
        packs = tuple(await uow.rule_pack.list_packs(context))
        for pack in packs:
            if pack.id == pack_id:
                return pack
        raise RulePackAdminNotFound


async def _append_audit(
    uow: object,
    *,
    context: TenantContext,
    action: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    audit = getattr(uow, "audit", None)
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if audit is None or user_id is None or membership_id is None:
        return
    event = new_tenant_user_audit_event(
        tenant_id=context.tenant_id,
        actor_user_id=user_id,
        actor_membership_id=membership_id,
        action=action,
        reason_code=reason_code,
        trace_id=trace_id or "http",
        target_type=target_type,
        target_id=target_id,
        result="success",
    )
    await audit.append_structured(event)
