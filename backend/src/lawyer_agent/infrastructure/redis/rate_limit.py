from __future__ import annotations

import hmac
import math
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from lawyer_agent.infrastructure.redis.client import (
    RedisDependencyInvalidResponse,
    RedisPort,
)


class RateLimitRule(StrEnum):
    REGISTER = "register"
    LOGIN = "login"
    REFRESH = "refresh"
    REAUTH = "reauth"
    SWITCH_TENANT = "switch"


REGISTER_RULE = RateLimitRule.REGISTER
LOGIN_RULE = RateLimitRule.LOGIN
REFRESH_RULE = RateLimitRule.REFRESH
REAUTH_RULE = RateLimitRule.REAUTH
SWITCH_RULE = RateLimitRule.SWITCH_TENANT


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int

    def __post_init__(self) -> None:
        if self.remaining < 0 or self.retry_after_seconds < 0:
            raise ValueError("rate-limit counters cannot be negative")
        if self.allowed and self.retry_after_seconds != 0:
            raise ValueError("an allowed request cannot have Retry-After")
        if not self.allowed and self.retry_after_seconds < 1:
            raise ValueError("a denied request requires Retry-After")


@dataclass(frozen=True, slots=True)
class _BucketPolicy:
    dimension: str
    capacity: int
    window_seconds: int


_POLICIES: dict[RateLimitRule, tuple[_BucketPolicy, ...]] = {
    RateLimitRule.REGISTER: (_BucketPolicy("ip", 5, 3600),),
    RateLimitRule.LOGIN: (
        _BucketPolicy("ip", 20, 900),
        _BucketPolicy("identity", 5, 900),
    ),
    RateLimitRule.REFRESH: (_BucketPolicy("session", 30, 900),),
    RateLimitRule.REAUTH: (_BucketPolicy("session", 5, 900),),
    RateLimitRule.SWITCH_TENANT: (_BucketPolicy("session", 10, 900),),
}

# All bucket calculations, all-or-nothing composite consumption, TTL, remaining and
# retry-after are decided inside this single Redis command.
_RATE_LIMIT_LUA = r"""
local marker = 'token-bucket-v1'
local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local planned = {}
local allowed = 1
local minimum_remaining = nil
local maximum_retry_ms = 0

for index = 1, #KEYS do
    local capacity = tonumber(ARGV[((index - 1) * 2) + 1])
    local window_ms = tonumber(ARGV[((index - 1) * 2) + 2])
    local state = redis.call('HMGET', KEYS[index], 'tokens', 'updated_ms')
    local tokens = tonumber(state[1]) or capacity
    local updated_ms = tonumber(state[2]) or now_ms
    local elapsed_ms = math.max(0, now_ms - updated_ms)
    local available = math.min(capacity, tokens + ((elapsed_ms * capacity) / window_ms))
    local retry_ms = 0
    if available < 1 then
        allowed = 0
        retry_ms = math.ceil(((1 - available) * window_ms) / capacity)
        maximum_retry_ms = math.max(maximum_retry_ms, retry_ms)
    end
    planned[index] = {available, window_ms}
end

for index = 1, #KEYS do
    local available = planned[index][1]
    if allowed == 1 then
        available = available - 1
    end
    redis.call('HSET', KEYS[index], 'tokens', tostring(available), 'updated_ms', tostring(now_ms))
    redis.call('PEXPIRE', KEYS[index], planned[index][2])
    local remaining = math.max(0, math.floor(available))
    if minimum_remaining == nil or remaining < minimum_remaining then
        minimum_remaining = remaining
    end
end

return {allowed, minimum_remaining or 0, math.ceil(maximum_retry_ms / 1000)}
"""


class RateLimiter:
    def __init__(self, *, redis: RedisPort, hmac_key: bytes) -> None:
        if len(hmac_key) != 32:
            raise ValueError("rate-limit HMAC key must contain exactly 32 bytes")
        self._redis = redis
        self._hmac_key = hmac_key

    async def consume(
        self,
        rule: RateLimitRule,
        dimensions: dict[str, str],
    ) -> RateLimitDecision:
        if not isinstance(rule, RateLimitRule):
            raise ValueError("rate-limit rule must be strongly typed")
        policies = _POLICIES[rule]
        required = {policy.dimension for policy in policies}
        if set(dimensions) != required:
            raise ValueError("rate-limit dimensions do not match the selected rule")

        keys: list[str] = []
        arguments: list[int] = []
        for policy in policies:
            value = dimensions[policy.dimension]
            if not _is_safe_dimension(value):
                raise ValueError("rate-limit dimension has an invalid value")
            digest = hmac.new(
                self._hmac_key,
                f"rate-limit:v1:{rule.value}:{policy.dimension}:{value}".encode(),
                sha256,
            ).hexdigest()
            keys.append(f"rate-limit:{rule.value}:{policy.dimension}:{digest}")
            arguments.extend((policy.capacity, policy.window_seconds * 1000))

        raw = await self._redis.eval(
            _RATE_LIMIT_LUA,
            len(keys),
            *keys,
            *arguments,
        )
        allowed, remaining, retry_after = _parse_decision(raw)
        return RateLimitDecision(allowed, remaining, retry_after)


def _parse_decision(raw: object) -> tuple[bool, int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise RedisDependencyInvalidResponse
    values: list[int] = []
    for value in raw:
        if isinstance(value, bool):
            raise RedisDependencyInvalidResponse
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RedisDependencyInvalidResponse from exc
        values.append(parsed)
    allowed_raw, remaining, retry_after = values
    if allowed_raw not in {0, 1} or remaining < 0 or retry_after < 0:
        raise RedisDependencyInvalidResponse
    allowed = allowed_raw == 1
    if allowed:
        retry_after = 0
    elif retry_after < 1:
        retry_after = 1
    return allowed, remaining, math.ceil(retry_after)


def _is_safe_dimension(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 1024
    except UnicodeEncodeError:
        return False
