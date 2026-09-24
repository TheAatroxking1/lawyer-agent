from __future__ import annotations

import asyncio
import re
from uuid import uuid4

import httpx
import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.search.opensearch import (
    DEFAULT_OPENSEARCH_URL,
    OpenSearchRestClient,
    chunk_document,
)

pytestmark = pytest.mark.integration


async def _available() -> bool:
    try:
        async with httpx.AsyncClient(
            base_url=DEFAULT_OPENSEARCH_URL, timeout=2.0
        ) as client:
            return (await client.get("/_cluster/health")).status_code == 200
    except Exception:  # noqa: BLE001 - bounded availability probe
        return False


async def _exercise(index_name: str) -> None:
    client = OpenSearchRestClient(base_url=DEFAULT_OPENSEARCH_URL)
    await client.ensure_index(index_name)
    version_id = new_uuid7()
    provision_id = new_uuid7()
    first = chunk_document(
        chunk_id=new_uuid7(),
        provision_id=provision_id,
        version_id=version_id,
        content="第一条 临时索引旧文本。",
        parser_version="write-ack-test-v1",
    )
    second = chunk_document(
        chunk_id=new_uuid7(),
        provision_id=provision_id,
        version_id=version_id,
        content="第一条 临时索引第二块。",
        parser_version="write-ack-test-v1",
    )
    await client.replace_documents(
        index_name, (first, second), parser_version="write-ack-test-v1"
    )
    assert len(await client.search_bm25(index_name, query="临时索引", limit=10)) == 2

    replacement = chunk_document(
        chunk_id=new_uuid7(),
        provision_id=provision_id,
        version_id=version_id,
        content="第一条 临时索引替换文本。",
        parser_version="write-ack-test-v1",
    )
    await client.replace_documents(
        index_name, (replacement,), parser_version="write-ack-test-v1"
    )
    hits = await client.search_bm25(index_name, query="临时索引", limit=10)
    assert [str(hit.chunk_id) for hit in hits] == [replacement["chunk_id"]]

    await client.replace_documents(
        index_name, (), parser_version="write-ack-test-v1"
    )
    assert await client.search_bm25(index_name, query="临时索引", limit=10) == ()


def test_real_opensearch_write_acknowledgements_and_empty_replace() -> None:
    if not asyncio.run(_available()):
        pytest.skip("test OpenSearch unavailable")
    index_name = f"lawyer_write_ack_test_{uuid4().hex}"
    if re.fullmatch(r"lawyer_write_ack_test_[a-f0-9]{32}", index_name) is None:
        raise RuntimeError("refusing to manage an unexpected index name")
    client = OpenSearchRestClient(base_url=DEFAULT_OPENSEARCH_URL)
    try:
        asyncio.run(_exercise(index_name))
    finally:
        asyncio.run(client.delete_index(index_name))
