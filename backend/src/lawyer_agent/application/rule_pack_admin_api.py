"""HTTP-facing Rule Pack administration orchestration (non-model).

Wraps the tenant rule pack repository write methods behind a unit of work so
HTTP endpoints only handle tenant actors, commands and projections. Every
read/write stays inside the tenant context; there is no un-scoped ``get_by_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from lawyer_agent.application.audit import new_tenant_user_audit_event
from lawyer_agent.application.idempotency import (
    IdempotencyFingerprintPayload,
    IdempotencyRepositoryPort,
    IdempotencyRequest,
    IdempotencyReservation,
    IdempotencyResultReference,
    IdempotencyScope,
    IdempotencyScopeType,
    IdempotencyService,
)
from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.rule_pack import (
    RiskLevel,
    RulePack,
    RulePackRule,
    RuleTriggerKind,
)
from lawyer_agent.domain.rule_pack_management import build_pack_rule
from lawyer_agent.domain.tenancy import TenantContext

_PACK_CREATE_OPERATION = "rule_pack.create"
_PACK_CREATE_ROUTE = "/api/v1/tenants/{tenant_id}/rule-packs"
_PACK_RESULT_TYPE = "rule_pack.pack"
_RULE_ADD_OPERATION = "rule_pack.add_rule"
_RULE_ADD_ROUTE = (
    "/api/v1/tenants/{tenant_id}/rule-packs/{pack_id}/rules"
)
_RULE_RESULT_TYPE = "rule_pack.rule"
_SET_RULE_ENABLED_OPERATION = "rule_pack.set_rule_enabled"
_SET_RULE_ENABLED_ROUTE = (
    "/api/v1/tenants/{tenant_id}/rule-packs/{pack_id}/rules/{rule_id}"
)
_ACTIVATE_PACK_OPERATION = "rule_pack.activate"
_ACTIVATE_PACK_ROUTE = (
    "/api/v1/tenants/{tenant_id}/rule-packs/{pack_id}/activate"
)


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
    idempotency: IdempotencyRepositoryPort

    async def __aenter__(self) -> RulePackAdminUnitOfWorkPort: ...

    async def __aexit__(self, *exc: object) -> None: ...


class RulePackAdminHttpService:
    """Composition facade used by the tenant HTTP endpoints."""

    def __init__(
        self,
        uow_factory: Callable[[], object],
        idempotency: IdempotencyService | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValueError("rule pack admin service requires a unit of work factory")
        self._uow_factory = uow_factory
        self._idempotency = idempotency

    async def create_pack(
        self,
        *,
        context: TenantContext,
        name: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> RulePack:
        user_id, membership_id = _actor_ids(context)
        effective_now = now or datetime.now(UTC)
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            try:
                if idempotency_key and self._idempotency is not None:
                    reservation = await self._reserve_pack_create(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        name=name,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.replay is not None:
                        return await self._find_pack_by_id(
                            uow, context, reservation.replay.result_id
                        )
                    created = await uow.rule_pack.create_pack(context, name=name)
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(
                            _PACK_RESULT_TYPE, created.id
                        ),
                        now=effective_now,
                    )
                    return created
                return await uow.rule_pack.create_pack(context, name=name)
            except ValueError as exc:
                raise RulePackAdminInvalidRequest from exc

    async def _reserve_pack_create(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        name: str,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_PACK_CREATE_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_PACK_CREATE_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={"name": name},
                    business_paths=frozenset({("name",)}),
                ),
            ),
            now=now,
        )

    async def _find_pack_by_id(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        context: TenantContext,
        pack_id: UUID,
    ) -> RulePack:
        for pack in tuple(await uow.rule_pack.list_packs(context)):
            if pack.id == pack_id:
                return pack
        raise RulePackAdminNotFound

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
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> RulePackRule:
        require_uuid7(pack_id, field="pack_id")
        user_id, membership_id = _actor_ids(context)
        effective_now = now or datetime.now(UTC)
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            await self._require_pack(uow, context, pack_id)
            try:
                if idempotency_key and self._idempotency is not None:
                    reservation = await self._reserve_rule_add(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        pack_id=pack_id,
                        trigger_kind=trigger_kind,
                        label=label,
                        pattern=pattern,
                        risk_level=risk_level,
                        suggestion=suggestion,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.replay is not None:
                        return await self._find_rule_by_id(
                            uow, context, pack_id, reservation.replay.result_id
                        )
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
                    stored = await uow.rule_pack.add_rule(context, rule)
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(_RULE_RESULT_TYPE, stored.id),
                        now=effective_now,
                    )
                    return stored
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

    async def _reserve_rule_add(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        pack_id: UUID,
        trigger_kind: RuleTriggerKind,
        label: str,
        pattern: str,
        risk_level: RiskLevel,
        suggestion: str,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_RULE_ADD_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_RULE_ADD_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "trigger_kind": trigger_kind.value,
                        "label": label,
                        "pattern": pattern,
                        "risk_level": risk_level.value,
                        "suggestion": suggestion,
                    },
                    business_paths=frozenset(
                        {
                            ("trigger_kind",),
                            ("label",),
                            ("pattern",),
                            ("risk_level",),
                            ("suggestion",),
                        }
                    ),
                ),
            ),
            now=now,
        )

    async def _find_rule_by_id(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        context: TenantContext,
        pack_id: UUID,
        rule_id: UUID,
    ) -> RulePackRule:
        rules = tuple(await uow.rule_pack.rules_for_pack(context, pack_id))
        for rule in rules:
            if rule.id == rule_id:
                return rule
        raise RulePackAdminNotFound

    async def set_rule_enabled(
        self,
        *,
        context: TenantContext,
        pack_id: UUID,
        rule_id: UUID,
        enabled: bool,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> None:
        require_uuid7(pack_id, field="pack_id")
        require_uuid7(rule_id, field="rule_id")
        membership_id = context.membership_id
        effective_now = now or datetime.now(UTC)
        reservation = None
        async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
            if idempotency_key and self._idempotency is not None:
                if membership_id is None:
                    raise RulePackAdminConflict
                reservation = await self._reserve_rule_toggle(
                    uow,
                    tenant_id=context.tenant_id,
                    membership_id=membership_id,
                    pack_id=pack_id,
                    rule_id=rule_id,
                    enabled=enabled,
                    idempotency_key=idempotency_key,
                    now=effective_now,
                )
                if reservation.is_replay:
                    return
            await self._require_pack(uow, context, pack_id)
            changed = await uow.rule_pack.set_rule_enabled(
                context, pack_id, rule_id, enabled=enabled
            )
            if not changed:
                raise RulePackAdminNotFound
            if reservation is not None:
                assert self._idempotency is not None
                await self._idempotency.complete(
                    uow.idempotency,
                    reservation,
                    IdempotencyResultReference(_RULE_RESULT_TYPE, rule_id),
                    now=effective_now,
                )

    async def _reserve_rule_toggle(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        pack_id: UUID,
        rule_id: UUID,
        enabled: bool,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_SET_RULE_ENABLED_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="PATCH",
                canonical_route=_SET_RULE_ENABLED_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={
                        "pack_id": str(pack_id),
                        "rule_id": str(rule_id),
                        "enabled": enabled,
                    },
                    business_paths=frozenset(
                        {("pack_id",), ("rule_id",), ("enabled",)}
                    ),
                ),
            ),
            now=now,
        )

    async def activate_pack(
        self,
        *,
        context: TenantContext,
        pack_id: UUID,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> RulePack:
        require_uuid7(pack_id, field="pack_id")
        membership_id = context.membership_id
        effective_now = now or datetime.now(UTC)
        reservation = None
        try:
            async with cast(RulePackAdminUnitOfWorkPort, self._uow_factory()) as uow:
                if idempotency_key and self._idempotency is not None:
                    if membership_id is None:
                        raise RulePackAdminConflict
                    reservation = await self._reserve_pack_activate(
                        uow,
                        tenant_id=context.tenant_id,
                        membership_id=membership_id,
                        pack_id=pack_id,
                        idempotency_key=idempotency_key,
                        now=effective_now,
                    )
                    if reservation.is_replay:
                        assert reservation.replay is not None
                        return await self._require_pack(uow, context, pack_id)
                await self._require_pack(uow, context, pack_id)
                activated = await uow.rule_pack.activate_pack(context, pack_id)
                if not activated:
                    raise RulePackAdminConflict
                if reservation is not None:
                    assert self._idempotency is not None
                    await self._idempotency.complete(
                        uow.idempotency,
                        reservation,
                        IdempotencyResultReference(_PACK_RESULT_TYPE, pack_id),
                        now=effective_now,
                    )
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
        except RulePackAdminNotFound as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="rule_pack.activate",
                result="denied",
                reason_code=exc.code,
                target_type="rule_pack",
                target_id=pack_id,
                trace_id=trace_id,
            )
            raise
        except RulePackAdminConflict as exc:
            await _append_rejected_audit(
                self._uow_factory,
                context=context,
                action="rule_pack.activate",
                result="denied",
                reason_code=exc.code,
                target_type="rule_pack",
                target_id=pack_id,
                trace_id=trace_id,
            )
            raise

    async def _reserve_pack_activate(
        self,
        uow: RulePackAdminUnitOfWorkPort,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        pack_id: UUID,
        idempotency_key: str,
        now: datetime,
    ) -> IdempotencyReservation:
        assert self._idempotency is not None
        return await self._idempotency.reserve(
            uow.idempotency,
            scope=IdempotencyScope(
                IdempotencyScopeType.MEMBERSHIP,
                membership_id,
                tenant_id=tenant_id,
            ),
            operation=_ACTIVATE_PACK_OPERATION,
            request=IdempotencyRequest(
                key=idempotency_key,
                method="POST",
                canonical_route=_ACTIVATE_PACK_ROUTE,
                body=IdempotencyFingerprintPayload(
                    values={"pack_id": str(pack_id)},
                    business_paths=frozenset({("pack_id",)}),
                ),
            ),
            now=now,
        )

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
    result: str = "success",
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
        result=result,
    )
    await audit.append_structured(event)


async def _append_rejected_audit(
    uow_factory: Callable[[], object],
    *,
    context: TenantContext,
    action: str,
    result: str,
    reason_code: str,
    target_type: str,
    target_id: UUID,
    trace_id: str | None,
) -> None:
    """Record a rejected/failed pack write attempt in its own committed transaction.

    The failing business UoW has already rolled back when an error handler calls
    this, so the audit append opens a fresh short-lived UoW whose clean exit
    commits only the audit row.
    """
    async with cast(RulePackAdminUnitOfWorkPort, uow_factory()) as uow:
        await _append_audit(
            uow,
            context=context,
            action=action,
            reason_code=reason_code,
            target_type=target_type,
            target_id=target_id,
            trace_id=trace_id,
            result=result,
        )


def _actor_ids(context: TenantContext) -> tuple[UUID, UUID]:
    user_id = context.membership_user_id
    membership_id = context.membership_id
    if user_id is None or membership_id is None:
        raise RulePackAdminConflict
    require_uuid7(user_id, field="actor user_id")
    require_uuid7(membership_id, field="actor membership_id")
    return user_id, membership_id
