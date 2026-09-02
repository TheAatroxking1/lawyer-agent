from __future__ import annotations

import hmac
import json
import re
import secrets
from hashlib import sha256
from uuid import RFC_4122, UUID

from lawyer_agent.domain.sessions import StepUpGrant
from lawyer_agent.infrastructure.redis.client import (
    RedisDependencyError,
    RedisDependencyInvalidResponse,
    RedisPort,
)

_ACTION_PATTERN = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z", re.ASCII)
_STEP_UP_TTL_SECONDS = 300
_MAX_COLLISION_RETRIES = 3

_COMPARE_AND_DELETE_SCRIPT = r"""
-- compare-and-delete-v1
local current = redis.call('GET', KEYS[1])
if current and current == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class StepUpGrantGenerationExhausted(RedisDependencyError):
    def __init__(self) -> None:
        super().__init__("could not allocate a step-up grant")


class StepUpStore:
    def __init__(self, *, redis: RedisPort, hmac_key: bytes) -> None:
        if len(hmac_key) != 32:
            raise ValueError("step-up HMAC key must contain exactly 32 bytes")
        self._redis = redis
        self._hmac_key = hmac_key

    async def issue(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        tenant_id: UUID,
        action: str,
    ) -> StepUpGrant:
        binding = _binding(user_id, session_id, tenant_id, action)
        encoded = _encode_binding(binding)
        for _ in range(_MAX_COLLISION_RETRIES):
            grant = StepUpGrant(secrets.token_urlsafe(32))
            stored = await self._redis.set(
                self._key(grant),
                encoded,
                ex=_STEP_UP_TTL_SECONDS,
                nx=True,
            )
            if stored:
                return grant
        raise StepUpGrantGenerationExhausted

    async def consume(
        self,
        grant: StepUpGrant,
        *,
        user_id: UUID,
        session_id: UUID,
        tenant_id: UUID,
        action: str,
    ) -> bool:
        if not isinstance(grant, StepUpGrant):
            raise ValueError("step-up grant must be strongly typed")
        binding = _binding(user_id, session_id, tenant_id, action)
        raw = await self._redis.eval(
            _COMPARE_AND_DELETE_SCRIPT,
            1,
            self._key(grant),
            _encode_binding(binding),
        )
        if isinstance(raw, bool) or not isinstance(raw, int) or raw not in {0, 1}:
            raise RedisDependencyInvalidResponse
        return raw == 1

    def _key(self, grant: StepUpGrant) -> str:
        digest = hmac.new(
            self._hmac_key,
            f"step-up-grant:v1:{grant.value}".encode(),
            sha256,
        ).hexdigest()
        return f"step-up:{digest}"


def _binding(
    user_id: UUID,
    session_id: UUID,
    tenant_id: UUID,
    action: str,
) -> dict[str, str | int]:
    for name, value in (
        ("user_id", user_id),
        ("session_id", session_id),
        ("tenant_id", tenant_id),
    ):
        if not _is_uuid7(value):
            raise ValueError(f"{name} must be an RFC 9562 UUIDv7")
    if not isinstance(action, str) or not _ACTION_PATTERN.fullmatch(action):
        raise ValueError("step-up action has an invalid format")
    return {
        "v": 1,
        "user_id": str(user_id),
        "session_id": str(session_id),
        "tenant_id": str(tenant_id),
        "action": action,
    }


def _encode_binding(binding: dict[str, str | int]) -> bytes:
    return json.dumps(
        binding,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _is_uuid7(value: object) -> bool:
    return (
        isinstance(value, UUID)
        and value.version == 7
        and value.variant == RFC_4122
        and value.int != 0
    )
