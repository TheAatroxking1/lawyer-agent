from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol, Self, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

RedisValue = str | bytes | int | float
_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9:_-]{0,160}\Z", re.ASCII)


class RedisFailureKind(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"


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

    def __init__(self, client: _RedisSdkPort, *, key_prefix: str = "") -> None:
        if not _PREFIX_PATTERN.fullmatch(key_prefix):
            raise ValueError("Redis key prefix contains unsupported characters")
        self._client = client
        self._key_prefix = key_prefix

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        key_prefix: str = "",
        connect_timeout_seconds: float = 1.0,
        socket_timeout_seconds: float = 1.0,
    ) -> Self:
        if connect_timeout_seconds <= 0 or socket_timeout_seconds <= 0:
            raise ValueError("Redis timeouts must be positive")
        client = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=connect_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(cast(_RedisSdkPort, client), key_prefix=key_prefix)

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
        try:
            return await self._client.eval(script, numkeys, *prefixed)
        except RedisTimeoutError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.TIMEOUT) from exc
        except RedisConnectionError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION) from exc

    async def get(self, key: str) -> bytes | None:
        try:
            result = await self._client.get(self._key(key))
        except RedisTimeoutError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.TIMEOUT) from exc
        except RedisConnectionError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION) from exc
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
        try:
            result = await self._client.set(self._key(key), value, ex=ex, nx=nx)
        except RedisTimeoutError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.TIMEOUT) from exc
        except RedisConnectionError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION) from exc
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
        try:
            result = await self._client.delete(*(self._key(key) for key in keys))
        except RedisTimeoutError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.TIMEOUT) from exc
        except RedisConnectionError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION) from exc
        if isinstance(result, bool) or not isinstance(result, int):
            raise RedisDependencyInvalidResponse
        return result

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except RedisTimeoutError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.TIMEOUT) from exc
        except RedisConnectionError as exc:
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION) from exc

    def _key(self, value: RedisValue) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Redis keys must be non-empty strings")
        return f"{self._key_prefix}{value}"
