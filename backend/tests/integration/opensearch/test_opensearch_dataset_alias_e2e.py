from __future__ import annotations

import asyncio
import re
from uuid import uuid4

import httpx
import pytest

from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
from lawyer_agent.infrastructure.search.opensearch import (
    OpenSearchRestClient,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


async def _opensearch_available(base_url: str = "http://127.0.0.1:9200") -> bool:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.get("/_cluster/health")
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - availability probe
        return False


def test_legal_dataset_alias_atomic_switch() -> None:
    base_url = "http://127.0.0.1:9200"
    if not asyncio.run(_opensearch_available(base_url)):
        pytest.skip("test OpenSearch unavailable")
    alias = f"lawyer_dataset_alias_test_{uuid4().hex}"
    index_a = f"lawyer_dataset_alias_a_{uuid4().hex}"
    index_b = f"lawyer_dataset_alias_b_{uuid4().hex}"
    pattern = re.compile(
        r"^lawyer_(dataset_alias_test|dataset_alias_[ab])_[a-f0-9]{32}$"
    )
    if not all(pattern.fullmatch(name) for name in (alias, index_a, index_b)):
        raise RuntimeError("refusing to manage an unexpected index/alias name")

    client = OpenSearchRestClient(base_url=base_url)
    service = LegalDatasetAliasService(client)

    async def scenario() -> None:
        await client.ensure_index(index_a)
        await client.ensure_index(index_b)
        try:
            # First publish points at A and returns no previous target.
            previous = await service.publish_dataset(alias, index_a)
            assert previous is None
            assert await service.active_dataset_index(alias) == index_a

            # Re-pointing to B returns A and switches atomically.
            previous = await service.publish_dataset(alias, index_b)
            assert previous == index_a
            assert await service.active_dataset_index(alias) == index_b

            # Same-target publish is a no-op that keeps B.
            previous = await service.publish_dataset(alias, index_b)
            assert previous == index_b
            assert await service.active_dataset_index(alias) == index_b

            # Both concrete indexes still exist for history and rollback.
            assert await client.index_exists(index_a)
            assert await client.index_exists(index_b)
        finally:
            await client.drop_alias(alias)
            assert await client.resolve_alias(alias) is None
            await client.delete_index(index_a)
            await client.delete_index(index_b)
            assert not await client.index_exists(index_a)
            assert not await client.index_exists(index_b)

    asyncio.run(scenario())
