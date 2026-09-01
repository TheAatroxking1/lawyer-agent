from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from lawyer_agent.infrastructure.redis.client import RedisAsyncioAdapter


@pytest_asyncio.fixture
async def redis_scope() -> AsyncIterator[tuple[RedisAsyncioAdapter, Redis, str]]:
    url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    prefix = f"lawyer-test:{uuid4().hex}:"
    raw = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        retry_on_timeout=False,
    )
    try:
        if not await raw.ping():
            pytest.skip("test Redis did not answer PING")
    except Exception as exc:
        await raw.aclose()
        pytest.skip(f"test Redis unavailable: {type(exc).__name__}")

    adapter = RedisAsyncioAdapter(raw, key_prefix=prefix)
    try:
        yield adapter, raw, prefix
    finally:
        keys = [key async for key in raw.scan_iter(match=f"{prefix}*", count=100)]
        if keys:
            await raw.delete(*keys)
        await adapter.aclose()
