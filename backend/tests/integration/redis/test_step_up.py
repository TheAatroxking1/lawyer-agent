from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from lawyer_agent.infrastructure.redis.authz_cache import (
    AuthorizationCache,
    AuthorizationCacheEntry,
    AuthorizationScope,
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
ACTION = "tenant_application.review"


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
            allow_owned=True,
            allow_shared=True,
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
