"""Real OpenSearch verification on generated, exclusively owned indices."""

from __future__ import annotations

import re
from uuid import uuid4

import httpx
import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_navigation import (
    NavigationDocument,
    NavigationDocumentKind,
    navigation_index_name,
)
from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient
from lawyer_agent.infrastructure.search.opensearch import (
    DEFAULT_OPENSEARCH_URL,
    OpenSearchError,
    OpenSearchRestClient,
    chunk_document,
)

pytestmark = pytest.mark.integration


async def test_navigation_write_search_marker_and_multi_version_scope() -> None:
    async with httpx.AsyncClient(base_url=DEFAULT_OPENSEARCH_URL, timeout=2) as probe:
        try:
            response = await probe.get("/_cluster/health")
        except httpx.HTTPError:
            pytest.skip("test OpenSearch unavailable")
        if response.status_code != 200:
            pytest.skip("test OpenSearch unavailable")
    main = f"lawyer_navigation_test_{uuid4().hex}"
    sidecar = navigation_index_name(main)
    rest = OpenSearchRestClient()
    nav = OpenSearchNavigationClient()
    versions = tuple(new_uuid7() for _ in range(3))
    try:
        await rest.ensure_index(main, vector_dimension=2)
        assert await nav.navigation_schema(main) is None
        await nav.ensure_navigation_index(sidecar)
        docs = tuple(
            NavigationDocument(
                f"{number:064x}",
                version,
                NavigationDocumentKind.INSTRUMENT,
                f"合成合同法{number}",
                f"合成合同法{number}",
                "navigation-test-v1",
            )
            for number, version in enumerate(versions, 1)
        )
        await nav.replace_navigation_documents(sidecar, docs, parser_version="navigation-test-v1")
        assert await nav.count_documents(sidecar) == 3
        hits = await nav.search_navigation(sidecar, query="合成合同法", limit=50)
        assert {hit.version_id for hit in hits} == set(versions)
        await nav.replace_navigation_documents(sidecar, docs, parser_version="navigation-test-v1")
        assert await nav.count_documents(sidecar) == 3
        async with httpx.AsyncClient(base_url=DEFAULT_OPENSEARCH_URL) as raw:
            response = await raw.put(f"/{main}/_mapping", json={"_meta": {"existing": "preserved"}})
            response.raise_for_status()
        await nav.mark_navigation_ready(main)
        assert await nav.navigation_schema(main) == 1
        async with httpx.AsyncClient(base_url=DEFAULT_OPENSEARCH_URL) as raw:
            response = await raw.get(f"/{main}/_mapping")
            assert response.json()[main]["mappings"]["_meta"]["existing"] == "preserved"

        chunks = tuple(
            {
                **chunk_document(
                    chunk_id=new_uuid7(),
                    provision_id=new_uuid7(),
                    version_id=version,
                    content="合成合同法 条文",
                    parser_version="navigation-test-v1",
                ),
                "content_vector": [1.0, 0.0],
            }
            for version in versions
        )
        await rest.replace_documents(main, chunks, parser_version="navigation-test-v1")
        bm25 = await rest.search_bm25(main, query="合成合同法", limit=10, version_ids=versions[:2])
        knn = await rest.search_knn(
            main, query_vector=(1.0, 0.0), limit=10, version_ids=versions[:2]
        )
        assert {hit.version_id for hit in bm25} == set(versions[:2])
        assert {hit.version_id for hit in knn} == set(versions[:2])
        await nav.replace_navigation_documents(sidecar, (), parser_version="navigation-test-v1")
        assert await nav.count_documents(sidecar) == 0
        _validate_owned(main, sidecar)
        await rest.delete_index(sidecar)
        with pytest.raises(OpenSearchError):
            await nav.search_navigation(sidecar, query="合成合同法", limit=5)
    finally:
        _validate_owned(main, sidecar)
        await rest.delete_index(sidecar)
        await rest.delete_index(main)


def _validate_owned(main: str, sidecar: str) -> None:
    if not re.fullmatch(r"lawyer_navigation_test_[a-f0-9]{32}", main):
        raise RuntimeError("refusing to delete an unexpected main index")
    if not re.fullmatch(r"lawyer-nav-[a-f0-9]{32}", sidecar):
        raise RuntimeError("refusing to delete an unexpected navigation index")
    if sidecar != navigation_index_name(main):
        raise RuntimeError("refusing to delete an unpaired navigation index")
