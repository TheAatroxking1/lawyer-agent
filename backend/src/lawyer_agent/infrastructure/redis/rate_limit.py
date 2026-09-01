from __future__ import annotations

import hmac
import math
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address
from uuid import RFC_4122, UUID

from lawyer_agent.domain.identity import (
    IdentityKind,
    NormalizedIdentity,
    normalize_identifier,
)
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
    if capacity == nil or capacity ~= math.floor(capacity) or capacity < 1 or
       window_ms == nil or window_ms ~= math.floor(window_ms) or window_ms < 1 then
        return redis.error_reply('invalid rate-limit policy')
    end
    local state = redis.call('HMGET', KEYS[index], 'tokens', 'updated_ms')
    local tokens_missing = state[1] == false
    local updated_missing = state[2] == false
    local tokens = nil
    local updated_ms = nil
    if tokens_missing and updated_missing then
        tokens = capacity
        updated_ms = now_ms
    elseif tokens_missing or updated_missing then
        return redis.error_reply('corrupt rate-limit state')
    else
        tokens = tonumber(state[1])
        updated_ms = tonumber(state[2])
        if tokens == nil or tokens ~= tokens or tokens == math.huge or tokens == -math.huge or
           tokens < 0 or tokens > capacity or
           updated_ms == nil or updated_ms ~= updated_ms or
           updated_ms == math.huge or updated_ms == -math.huge or
           updated_ms ~= math.floor(updated_ms) or updated_ms < 0 then
            return redis.error_reply('corrupt rate-limit state')
        end
    end
    local effective_now_ms = math.max(now_ms, updated_ms)
    local elapsed_ms = effective_now_ms - updated_ms
    local available = math.min(capacity, tokens + ((elapsed_ms * capacity) / window_ms))
    local retry_ms = 0
    if available < 1 then
        allowed = 0
        retry_ms = math.ceil(((1 - available) * window_ms) / capacity)
        maximum_retry_ms = math.max(maximum_retry_ms, retry_ms)
    end
    planned[index] = {available, window_ms, effective_now_ms}
end

for index = 1, #KEYS do
    local available = planned[index][1]
    if allowed == 1 then
        available = available - 1
    end
    redis.call(
        'HSET',
        KEYS[index],
        'tokens', tostring(available),
        'updated_ms', tostring(planned[index][3])
    )
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
        dimensions: dict[str, object],
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
            canonical = _canonical_dimension(policy.dimension, value)
            digest = hmac.new(
                self._hmac_key,
                (
                    f"rate-limit:v1:{rule.value}:{policy.dimension}:{canonical}"
                ).encode(),
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
        allowed, remaining, retry_after = _parse_decision(
            raw,
            maximum_remaining=min(policy.capacity - 1 for policy in policies),
            maximum_retry_after=max(
                math.ceil(policy.window_seconds / policy.capacity)
                for policy in policies
            ),
        )
        return RateLimitDecision(allowed, remaining, retry_after)


def _parse_decision(
    raw: object,
    *,
    maximum_remaining: int,
    maximum_retry_after: int,
) -> tuple[bool, int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise RedisDependencyInvalidResponse
    if any(type(value) is not int for value in raw):
        raise RedisDependencyInvalidResponse
    allowed_raw, remaining, retry_after = raw
    if allowed_raw not in {0, 1}:
        raise RedisDependencyInvalidResponse
    allowed = allowed_raw == 1
    if allowed and not (
        0 <= remaining <= maximum_remaining and retry_after == 0
    ):
        raise RedisDependencyInvalidResponse
    if not allowed and not (
        remaining == 0 and 1 <= retry_after <= maximum_retry_after
    ):
        raise RedisDependencyInvalidResponse
    return allowed, remaining, retry_after


def _canonical_dimension(dimension: str, value: object) -> str:
    if dimension == "ip":
        if not isinstance(value, (IPv4Address, IPv6Address)):
            raise ValueError("rate-limit IP dimension must be an ipaddress object")
        return f"ipv{value.version}:{value.compressed}"
    if dimension == "session":
        if not _is_uuid7(value):
            raise ValueError("rate-limit session dimension must be an RFC 9562 UUIDv7")
        return f"uuid:{value}"
    if dimension == "identity":
        return _canonical_identity(value)
    raise ValueError("rate-limit dimension is not supported")


def _canonical_identity(value: object) -> str:
    if not isinstance(value, NormalizedIdentity) or not isinstance(
        value.kind, IdentityKind
    ):
        raise ValueError("rate-limit identity dimension must be normalized")
    try:
        canonical = normalize_identifier(
            value.kind,
            value.subject,
            issuer=value.issuer,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("rate-limit identity dimension must be normalized") from exc
    if (
        canonical.kind is not value.kind
        or canonical.issuer != value.issuer
        or canonical.subject != value.subject
        or not _is_safe_text(value.issuer)
        or not _is_safe_text(value.subject)
    ):
        raise ValueError("rate-limit identity dimension must be normalized")
    encoded = f"{value.kind.value}\x1f{value.issuer}\x1f{value.subject}"
    if len(encoded.encode("utf-8")) > 1024:
        raise ValueError("rate-limit identity dimension is too long")
    return encoded


def _is_safe_text(value: str) -> bool:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_uuid7(value: object) -> bool:
    return (
        isinstance(value, UUID)
        and value.version == 7
        and value.variant == RFC_4122
        and value.int != 0
    )
