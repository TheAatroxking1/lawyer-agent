from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol, Self, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import OutOfMemoryError as RedisOutOfMemoryError
from redis.exceptions import ReadOnlyError as RedisReadOnlyError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

RedisValue = str | bytes | int | float
_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]{0,160}\Z", re.ASCII)


class RedisFailureKind(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    SERVER = "server"


class SecurityRedisTopology(StrEnum):
    """Non-sharded Redis topologies that preserve multi-key Lua atomicity."""

    STANDALONE = "standalone"
    SENTINEL_PRIMARY = "sentinel-primary"


class RedisDependencyError(Exception):
    code = "security_dependency_unavailable"


class RedisDependencyUnavailable(RedisDependencyError):

    def __init__(self, kind: RedisFailureKind) -> None:
        self.kind = kind
        super().__init__("security dependency is unavailable")


class RedisDependencyInvalidResponse(RedisDependencyError):
    def __init__(self) -> None:
        super().__init__("security dependency returned an invalid response")


class RedisPort(Protocol):
    async def ping(self) -> None: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisValue,
    ) -> object: ...

    async def get(self, key: str) -> bytes | None: ...

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool: ...

    async def delete(self, *keys: str) -> int: ...

    async def aclose(self) -> None: ...


class _RedisSdkPort(Protocol):
    async def ping(self) -> object: ...

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisValue,
    ) -> object: ...

    async def get(self, key: str) -> object: ...

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> object: ...

    async def delete(self, *keys: str) -> object: ...

    async def aclose(self) -> None: ...


class RedisAsyncioAdapter:
    """The only redis-py boundary used by security services."""

    def __init__(
        self,
        client: _RedisSdkPort,
        *,
        key_prefix: str = "",
        topology: SecurityRedisTopology = SecurityRedisTopology.STANDALONE,
    ) -> None:
        if not _PREFIX_PATTERN.fullmatch(key_prefix):
            raise ValueError("Redis key prefix contains unsupported characters")
        _require_security_topology(topology)
        self._client = client
        self._key_prefix = key_prefix
        self.topology = topology

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_prefix: str = "",
        topology: SecurityRedisTopology = SecurityRedisTopology.STANDALONE,
        connect_timeout_seconds: float = 1.0,
        socket_timeout_seconds: float = 1.0,
    ) -> Self:
        _require_security_topology(topology)
        if connect_timeout_seconds <= 0 or socket_timeout_seconds <= 0:
            raise ValueError("Redis timeouts must be positive")
        client = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=connect_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(
            cast(_RedisSdkPort, client),
            key_prefix=key_prefix,
            topology=topology,
        )

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: RedisValue,
    ) -> object:
        if isinstance(numkeys, bool) or not 0 <= numkeys <= len(keys_and_args):
            raise ValueError("invalid Redis script key count")
        prefixed = tuple(
            self._key(value) if index < numkeys else value
            for index, value in enumerate(keys_and_args)
        )
        translated_error: RedisDependencyError | None = None
        try:
            result = await self._client.eval(script, numkeys, *prefixed)
        except RedisError as exc:
            translated_error = _translate_sdk_error(exc)
        if translated_error is not None:
            raise translated_error
        return result

    async def ping(self) -> None:
        translated_error: RedisDependencyError | None = None
        try:
            result = await self._client.ping()
        except RedisError as exc:
            translated_error = _translate_sdk_error(exc)
        if translated_error is not None:
            raise translated_error
        if result is not True:
            raise RedisDependencyInvalidResponse

    async def get(self, key: str) -> bytes | None:
        translated_error: RedisDependencyError | None = None
        try:
            result = await self._client.get(self._key(key))
        except RedisError as exc:
            translated_error = _translate_sdk_error(exc)
        if translated_error is not None:
            raise translated_error
        if result is None:
            return None
        if isinstance(result, bytes):
            return result
        raise RedisDependencyInvalidResponse

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        translated_error: RedisDependencyError | None = None
        try:
            result = await self._client.set(self._key(key), value, ex=ex, nx=nx)
        except RedisError as exc:
            translated_error = _translate_sdk_error(exc)
        if translated_error is not None:
            raise translated_error
        if result is True:
            return True
        if result is False or result is None:
            if nx:
                return False
            raise RedisDependencyInvalidResponse
        raise RedisDependencyInvalidResponse

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        translated_error: RedisDependencyError | None = None
        try:
            result = await self._client.delete(*(self._key(key) for key in keys))
        except RedisError as exc:
            translated_error = _translate_sdk_error(exc)
        if translated_error is not None:
            raise translated_error
        if type(result) is not int or not 0 <= result <= len(keys):
            raise RedisDependencyInvalidResponse
        return result

    async def aclose(self) -> None:
        translated_error: RedisDependencyError | None = None
        try:
            await self._client.aclose()
        except RedisError as exc:
            translated_error = _translate_sdk_error(exc)
        if translated_error is not None:
            raise translated_error

    def _key(self, value: RedisValue) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Redis keys must be non-empty strings")
        return f"{self._key_prefix}{value}"


def _translate_sdk_error(error: RedisError) -> RedisDependencyError:
    if isinstance(error, RedisTimeoutError):
        return RedisDependencyUnavailable(RedisFailureKind.TIMEOUT)
    if isinstance(error, RedisConnectionError):
        return RedisDependencyUnavailable(RedisFailureKind.CONNECTION)
    if isinstance(error, (RedisReadOnlyError, RedisOutOfMemoryError)):
        return RedisDependencyUnavailable(RedisFailureKind.SERVER)
    return RedisDependencyInvalidResponse()


def _require_security_topology(topology: object) -> None:
    if not isinstance(topology, SecurityRedisTopology):
        raise ValueError("security Redis topology must be strongly typed")
