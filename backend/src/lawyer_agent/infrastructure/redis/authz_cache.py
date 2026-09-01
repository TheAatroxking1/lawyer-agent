from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from uuid import RFC_4122, UUID

from lawyer_agent.infrastructure.redis.client import (
    RedisDependencyError,
    RedisDependencyInvalidResponse,
    RedisPort,
)

_PERMISSION_PATTERN = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z", re.ASCII)
_MAX_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    department_ids: frozenset[UUID] = field(default_factory=frozenset)
    allow_owned: bool = False
    allow_shared: bool = False

    def __post_init__(self) -> None:
        if any(not _is_uuid7(value) for value in self.department_ids):
            raise ValueError("authorization department IDs must be RFC 9562 UUIDv7")
        if not isinstance(self.allow_owned, bool) or not isinstance(
            self.allow_shared, bool
        ):
            raise ValueError("authorization scope flags must be booleans")


@dataclass(frozen=True, slots=True)
class AuthorizationCacheEntry:
    permissions: frozenset[str]
    scope: AuthorizationScope

    def __post_init__(self) -> None:
        if not isinstance(self.permissions, frozenset) or any(
            not isinstance(permission, str)
            or not _PERMISSION_PATTERN.fullmatch(permission)
            for permission in self.permissions
        ):
            raise ValueError("authorization permissions must be stable codes")
        if not isinstance(self.scope, AuthorizationScope):
            raise ValueError("authorization scope must be strongly typed")


class AuthorizationCache:
    def __init__(self, *, redis: RedisPort, ttl_seconds: int = 60) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS
        ):
            raise ValueError("authorization cache TTL must be between 1 and 300 seconds")
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        authz_version: int,
    ) -> AuthorizationCacheEntry | None:
        key = _key(tenant_id, membership_id, authz_version)
        try:
            encoded = await self._redis.get(key)
        except RedisDependencyError:
            return None
        if encoded is None:
            return None
        return _decode(encoded, expected_authz_version=authz_version)

    async def set(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        authz_version: int,
        entry: AuthorizationCacheEntry,
    ) -> None:
        key = _key(tenant_id, membership_id, authz_version)
        if not isinstance(entry, AuthorizationCacheEntry):
            raise ValueError("authorization cache entry must be strongly typed")
        payload = {
            "v": 1,
            "authz_version": authz_version,
            "permissions": sorted(entry.permissions),
            "scope": {
                "department_ids": sorted(str(value) for value in entry.scope.department_ids),
                "allow_owned": entry.scope.allow_owned,
                "allow_shared": entry.scope.allow_shared,
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if not await self._redis.set(key, encoded, ex=self._ttl_seconds):
            raise RedisDependencyInvalidResponse

    async def invalidate(
        self,
        *,
        tenant_id: UUID,
        membership_id: UUID,
        authz_version: int,
    ) -> None:
        await self._redis.delete(_key(tenant_id, membership_id, authz_version))


def _key(tenant_id: UUID, membership_id: UUID, authz_version: int) -> str:
    if not _is_uuid7(tenant_id) or not _is_uuid7(membership_id):
        raise ValueError("authorization cache IDs must be RFC 9562 UUIDv7")
    if (
        isinstance(authz_version, bool)
        or not isinstance(authz_version, int)
        or authz_version < 1
    ):
        raise ValueError("authz_version must be a positive integer")
    return f"authz:{tenant_id}:{membership_id}:{authz_version}"


def _decode(
    encoded: bytes,
    *,
    expected_authz_version: int,
) -> AuthorizationCacheEntry | None:
    try:
        payload = json.loads(encoded)
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "authz_version",
            "permissions",
            "scope",
        }:
            return None
        if (
            type(payload["v"]) is not int
            or payload["v"] != 1
            or type(payload["authz_version"]) is not int
            or payload["authz_version"] != expected_authz_version
        ):
            return None
        permissions = payload["permissions"]
        if (
            not isinstance(permissions, list)
            or not all(isinstance(value, str) for value in permissions)
            or permissions != sorted(set(permissions))
        ):
            return None
        scope = payload["scope"]
        if not isinstance(scope, dict) or set(scope) != {
            "department_ids",
            "allow_owned",
            "allow_shared",
        }:
            return None
        department_values = scope["department_ids"]
        if (
            not isinstance(department_values, list)
            or not all(isinstance(value, str) for value in department_values)
            or department_values != sorted(set(department_values))
            or not isinstance(scope["allow_owned"], bool)
            or not isinstance(scope["allow_shared"], bool)
        ):
            return None
        departments = frozenset(UUID(value) for value in department_values)
        return AuthorizationCacheEntry(
            permissions=frozenset(permissions),
            scope=AuthorizationScope(
                department_ids=departments,
                allow_owned=scope["allow_owned"],
                allow_shared=scope["allow_shared"],
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None


def _is_uuid7(value: object) -> bool:
    return (
        isinstance(value, UUID)
        and value.version == 7
        and value.variant == RFC_4122
        and value.int != 0
    )
