from __future__ import annotations

import asyncio
import hmac
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address, ip_address
from uuid import UUID

import pytest

from lawyer_agent.domain.identity import IdentityKind, normalize_identifier
from lawyer_agent.infrastructure.redis.client import RedisDependencyInvalidResponse
from lawyer_agent.infrastructure.redis.rate_limit import RateLimiter, RateLimitRule

SESSION_ID = UUID("01990f00-0000-7000-8000-0000000005aa")


def _register_key(ip: IPv4Address | IPv6Address) -> str:
    canonical = f"ipv{ip.version}:{ip.compressed}"
    digest = hmac.new(
        b"r" * 32,
        f"rate-limit:v1:register:ip:{canonical}".encode(),
        sha256,
    ).hexdigest()
    return f"rate-limit:register:ip:{digest}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_bucket_never_allows_more_than_five_concurrent_requests(
    redis_scope,
) -> None:
    redis, _, _ = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    decisions = await asyncio.gather(
        *(
            limiter.consume(
                RateLimitRule.REGISTER, {"ip": ip_address("203.0.113.10")}
            )
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
                {
                    "ip": ip_address(shared_ip),
                    "identity": normalize_identifier(
                        IdentityKind.EMAIL, "first@example.cn"
                    ),
                },
            )
            for _ in range(6)
        )
    )
    second_identity = await asyncio.gather(
        *(
            limiter.consume(
                RateLimitRule.LOGIN,
                {
                    "ip": ip_address(shared_ip),
                    "identity": normalize_identifier(
                        IdentityKind.EMAIL, "second@example.cn"
                    ),
                },
            )
            for _ in range(5)
        )
    )

    assert sum(decision.allowed for decision in first_identity) == 5
    assert all(decision.allowed for decision in second_identity)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_identity_bucket_is_shared_across_different_ips(redis_scope) -> None:
    redis, _, _ = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    identity = normalize_identifier(IdentityKind.EMAIL, "shared@example.cn")

    decisions = []
    for offset in range(6):
        decisions.append(
            await limiter.consume(
                RateLimitRule.LOGIN,
                {
                    "ip": ip_address(f"203.0.113.{20 + offset}"),
                    "identity": identity,
                },
            )
        )

    assert all(decision.allowed for decision in decisions[:5])
    assert decisions[5].allowed is False


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
                {"session": SESSION_ID},
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
        {
            "ip": ip_address("203.0.113.12"),
            "identity": normalize_identifier(IdentityKind.PHONE, identity),
        },
    )

    keys = [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)]
    assert keys
    for key in keys:
        assert identity.encode() not in key
        key_type = await raw.type(key)
        if key_type == b"hash":
            values = await raw.hgetall(key)
            assert identity.encode() not in repr(values).encode()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"tokens": "1"},
        {"updated_ms": "1"},
        {"tokens": "not-a-number", "updated_ms": "1"},
        {"tokens": "nan", "updated_ms": "1"},
        {"tokens": "inf", "updated_ms": "1"},
        {"tokens": "-1", "updated_ms": "1"},
        {"tokens": "6", "updated_ms": "1"},
        {"tokens": "1", "updated_ms": "not-a-number"},
    ],
)
async def test_malformed_bucket_state_fails_closed_without_reinitializing(
    redis_scope,
    state: dict[str, str],
) -> None:
    redis, raw, prefix = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    ip = ip_address("203.0.113.13")
    raw_key = f"{prefix}{_register_key(ip)}"
    await raw.hset(raw_key, mapping=state)

    with pytest.raises(RedisDependencyInvalidResponse):
        await limiter.consume(RateLimitRule.REGISTER, {"ip": ip})

    assert await raw.hgetall(raw_key) == {
        key.encode(): value.encode() for key, value in state.items()
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_clock_rollback_never_moves_bucket_watermark_backwards(
    redis_scope,
) -> None:
    redis, raw, prefix = redis_scope
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    ip = ip_address("203.0.113.14")
    raw_key = f"{prefix}{_register_key(ip)}"
    seconds, microseconds = await raw.time()
    future_ms = (seconds * 1000) + (microseconds // 1000) + 2000
    await raw.hset(raw_key, mapping={"tokens": "1", "updated_ms": str(future_ms)})

    first = await limiter.consume(RateLimitRule.REGISTER, {"ip": ip})
    second = await limiter.consume(RateLimitRule.REGISTER, {"ip": ip})

    assert first.allowed is True and first.remaining == 0
    assert second.allowed is False
    assert int(await raw.hget(raw_key, "updated_ms")) == future_ms
