from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from lawyer_agent.domain.authorization import (
    Action,
    AuthorizationScope,
    ConfidentialityLevel,
    PolicyEngine,
    Principal,
    PrincipalAudience,
    ResourceAccessPath,
    ResourceAttributes,
    ResourceState,
)
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.redis.authz_cache import (
    AuthorizationCache,
    AuthorizationCacheEntry,
)
from lawyer_agent.infrastructure.redis.step_up import StepUpStore

USER_ID = UUID("01990f00-0000-7000-8000-000000000511")
OTHER_USER_ID = UUID("01990f00-0000-7000-8000-000000000512")
SESSION_ID = UUID("01990f00-0000-7000-8000-000000000513")
OTHER_SESSION_ID = UUID("01990f00-0000-7000-8000-000000000514")
TENANT_ID = UUID("01990f00-0000-7000-8000-000000000515")
OTHER_TENANT_ID = UUID("01990f00-0000-7000-8000-000000000516")
MEMBERSHIP_ID = UUID("01990f00-0000-7000-8000-000000000517")
DEPARTMENT_ID = UUID("01990f00-0000-7000-8000-000000000518")
OWNER_ID = UUID("01990f00-0000-7000-8000-000000000519")
ACTION = "tenant_application.review"
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_step_up_is_user_session_tenant_action_bound_and_single_use(
    redis_scope,
) -> None:
    redis, _, _ = redis_scope
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)

    for mismatch in (
        {"user_id": OTHER_USER_ID},
        {"session_id": OTHER_SESSION_ID},
        {"tenant_id": OTHER_TENANT_ID},
        {"action": "tenant_application.read"},
    ):
        grant = await store.issue(
            user_id=USER_ID,
            session_id=SESSION_ID,
            tenant_id=TENANT_ID,
            action=ACTION,
        )
        values = {
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "tenant_id": TENANT_ID,
            "action": ACTION,
        }
        values.update(mismatch)
        assert await store.consume(grant, **values) is False
        assert (
            await store.consume(
                grant,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_id=TENANT_ID,
                action=ACTION,
            )
            is True
        )
        assert (
            await store.consume(
                grant,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_id=TENANT_ID,
                action=ACTION,
            )
            is False
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_step_up_uses_300_second_ttl_and_never_stores_raw_grant(
    redis_scope,
) -> None:
    redis, raw, prefix = redis_scope
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)
    grant = await store.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action=ACTION,
    )

    keys = [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)]
    assert len(keys) == 1
    key = keys[0]
    assert grant.value.encode() not in key
    assert grant.value.encode() not in (await raw.get(key) or b"")
    ttl = await raw.ttl(key)
    assert 295 <= ttl <= 300


@pytest.mark.integration
@pytest.mark.asyncio
async def test_twenty_concurrent_step_up_consumers_have_exactly_one_winner(
    redis_scope,
) -> None:
    redis, _, _ = redis_scope
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)
    grant = await store.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action=ACTION,
    )

    results = await asyncio.gather(
        *(
            store.consume(
                grant,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_id=TENANT_ID,
                action=ACTION,
            )
            for _ in range(20)
        )
    )

    assert sum(results) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_step_up_grant_fails_for_every_concurrent_consumer(
    redis_scope,
) -> None:
    redis, raw, prefix = redis_scope
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)
    grant = await store.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action=ACTION,
    )
    keys = [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)]
    assert len(keys) == 1
    assert await raw.pexpire(keys[0], 50) is True
    await asyncio.wait_for(asyncio.sleep(0.1), timeout=2)
    assert await raw.exists(keys[0]) == 0

    results = await asyncio.gather(
        *(
            store.consume(
                grant,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_id=TENANT_ID,
                action=ACTION,
            )
            for _ in range(20)
        )
    )

    assert results == [False] * 20


@pytest.mark.integration
@pytest.mark.asyncio
async def test_authorization_cache_round_trip_version_miss_and_exact_invalidation(
    redis_scope,
) -> None:
    redis, raw, prefix = redis_scope
    cache = AuthorizationCache(redis=redis, ttl_seconds=60)
    entry = AuthorizationCacheEntry(
        permissions=frozenset({"tenant.read", "membership.read"}),
        scope=AuthorizationScope(
            department_ids=frozenset({DEPARTMENT_ID}),
            allow_tenant_wide=True,
            allow_owned=True,
            allow_shared=True,
            allow_matter_team=True,
            allow_class=True,
            allow_client_delegation=True,
            maximum_confidentiality=ConfidentialityLevel.CONFIDENTIAL,
        ),
    )
    await cache.set(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=7,
        entry=entry,
    )

    assert (
        await cache.get(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
        )
        == entry
    )
    assert (
        await cache.get(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=8,
        )
        is None
    )
    keys_before = [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)]
    assert len(keys_before) == 1

    await cache.invalidate(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=7,
    )

    assert [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cached_domain_scope_round_trip_drives_every_policy_abac_path(
    redis_scope,
) -> None:
    redis, _, _ = redis_scope
    cache = AuthorizationCache(redis=redis, ttl_seconds=60)
    scope = AuthorizationScope(
        department_ids=frozenset({DEPARTMENT_ID}),
        allow_tenant_wide=False,
        allow_owned=True,
        allow_shared=True,
        allow_matter_team=True,
        allow_class=True,
        allow_client_delegation=True,
        maximum_confidentiality=ConfidentialityLevel.CONFIDENTIAL,
    )
    await cache.set(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=7,
        entry=AuthorizationCacheEntry(frozenset({Action.TENANT_READ.value}), scope),
    )
    loaded = await cache.get(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=7,
    )
    assert loaded is not None and loaded.scope == scope

    principal = Principal(
        user_id=OWNER_ID,
        session_id=SESSION_ID,
        audience=PrincipalAudience.TENANT,
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        user_status="active",
        session_valid=True,
        auth_version=3,
        session_auth_version=3,
        permissions=loaded.permissions,
        role_codes=frozenset({"lawyer_or_legal"}),
        authenticated_at=NOW - timedelta(minutes=1),
    )
    context = TenantContext(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        membership_user_id=OWNER_ID,
        department_id=DEPARTMENT_ID,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        authz_version=7,
        session_authz_version=7,
        scope=loaded.scope,
    )
    policy = PolicyEngine()
    resources = (
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            access_paths=frozenset({ResourceAccessPath.DEPARTMENT}),
            department_id=DEPARTMENT_ID,
        ),
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            access_paths=frozenset({ResourceAccessPath.OWNER}),
            owner_user_id=OWNER_ID,
        ),
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            access_paths=frozenset({ResourceAccessPath.SHARED}),
            shared_with_membership_ids=frozenset({MEMBERSHIP_ID}),
        ),
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            access_paths=frozenset({ResourceAccessPath.MATTER_TEAM}),
            matter_team_membership_ids=frozenset({MEMBERSHIP_ID}),
        ),
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            access_paths=frozenset({ResourceAccessPath.CLASS}),
            class_membership_ids=frozenset({MEMBERSHIP_ID}),
        ),
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            access_paths=frozenset({ResourceAccessPath.CLIENT_DELEGATION}),
            delegated_client_membership_ids=frozenset({MEMBERSHIP_ID}),
        ),
    )
    assert all(
        policy.decide(principal, context, Action.TENANT_READ, resource, NOW).allowed
        for resource in resources
    )
    restricted = ResourceAttributes(
        tenant_id=TENANT_ID,
        state=ResourceState.ACTIVE,
        access_paths=frozenset({ResourceAccessPath.OWNER}),
        owner_user_id=OWNER_ID,
        confidentiality=ConfidentialityLevel.RESTRICTED,
    )
    decision = policy.decide(principal, context, Action.TENANT_READ, restricted, NOW)
    assert not decision.allowed and decision.reason_code == "resource_scope_denied"

    tenant_wide_scope = AuthorizationScope(
        allow_tenant_wide=True,
        maximum_confidentiality=ConfidentialityLevel.CONFIDENTIAL,
    )
    await cache.set(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=8,
        entry=AuthorizationCacheEntry(loaded.permissions, tenant_wide_scope),
    )
    tenant_wide = await cache.get(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=8,
    )
    assert tenant_wide is not None and tenant_wide.scope == tenant_wide_scope
    tenant_wide_context = replace(
        context,
        authz_version=8,
        session_authz_version=8,
        scope=tenant_wide.scope,
    )
    assert policy.decide(
        principal,
        tenant_wide_context,
        Action.TENANT_READ,
        ResourceAttributes(
            tenant_id=TENANT_ID,
            state=ResourceState.ACTIVE,
            confidentiality=ConfidentialityLevel.CONFIDENTIAL,
        ),
        NOW,
    ).allowed

    unknown_scope = replace(
        tenant_wide_scope,
        unrecognized_scope_codes=frozenset({"future.scope"}),
    )
    await cache.set(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=9,
        entry=AuthorizationCacheEntry(loaded.permissions, unknown_scope),
    )
    unknown = await cache.get(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=9,
    )
    assert unknown is not None and unknown.scope == unknown_scope
    decision = policy.decide(
        principal,
        replace(
            context,
            authz_version=9,
            session_authz_version=9,
            scope=unknown.scope,
        ),
        Action.TENANT_READ,
        ResourceAttributes(tenant_id=TENANT_ID, state=ResourceState.ACTIVE),
        NOW,
    )
    assert not decision.allowed and decision.reason_code == "unknown_scope"
