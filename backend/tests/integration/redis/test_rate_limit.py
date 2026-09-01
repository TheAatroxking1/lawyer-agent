from __future__ import annotations

import asyncio

import pytest

from lawyer_agent.infrastructure.redis.rate_limit import RateLimiter, RateLimitRule


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_bucket_never_allows_more_than_five_concurrent_requests(
    redis_scope,
) -> None:
    redis, _, _ = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    decisions = await asyncio.gather(
        *(
            limiter.consume(RateLimitRule.REGISTER, {"ip": "203.0.113.10"})
            for _ in range(25)
        )
    )

    assert sum(decision.allowed for decision in decisions) == 5
    denied = [decision for decision in decisions if not decision.allowed]
    assert denied
    assert all(1 <= decision.retry_after_seconds <= 720 for decision in denied)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_composite_denial_does_not_consume_the_other_bucket(
    redis_scope,
) -> None:
    redis, _, _ = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    shared_ip = "203.0.113.11"

    first_identity = await asyncio.gather(
        *(
            limiter.consume(
                RateLimitRule.LOGIN,
                {"ip": shared_ip, "identity": "first@example.test"},
            )
            for _ in range(6)
        )
    )
    second_identity = await asyncio.gather(
        *(
            limiter.consume(
                RateLimitRule.LOGIN,
                {"ip": shared_ip, "identity": "second@example.test"},
            )
            for _ in range(5)
        )
    )

    assert sum(decision.allowed for decision in first_identity) == 5
    assert all(decision.allowed for decision in second_identity)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "count", "allowed"),
    [
        (RateLimitRule.REFRESH, 35, 30),
        (RateLimitRule.REAUTH, 10, 5),
        (RateLimitRule.SWITCH_TENANT, 15, 10),
    ],
)
async def test_session_rules_enforce_fixed_fifteen_minute_capacities(
    redis_scope,
    rule: RateLimitRule,
    count: int,
    allowed: int,
) -> None:
    redis, _, _ = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    decisions = await asyncio.gather(
        *(
            limiter.consume(
                rule,
                {"session": "01990f00-0000-7000-8000-0000000005aa"},
            )
            for _ in range(count)
        )
    )

    assert sum(decision.allowed for decision in decisions) == allowed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_keys_and_values_do_not_contain_raw_identity(redis_scope) -> None:
    redis, raw, prefix = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    identity = "+8613800138000"
    await limiter.consume(
        RateLimitRule.LOGIN,
        {"ip": "203.0.113.12", "identity": identity},
    )

    keys = [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)]
    assert keys
    for key in keys:
        assert identity.encode() not in key
        key_type = await raw.type(key)
        if key_type == b"hash":
            values = await raw.hgetall(key)
            assert identity.encode() not in repr(values).encode()
