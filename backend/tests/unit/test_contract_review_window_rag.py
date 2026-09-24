import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import ReviewScope
from lawyer_agent.infrastructure.contract_review.window_rag import WindowRagLegalResearchTools


def scope():
    return ReviewScope(
        tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(), document_version_id="contract-v1"
    )


def search_response():
    return {
        "status": "ok",
        "collection": "lawyer_windows_v2_20260920",
        "release_sha256": "a" * 64,
        "mode": "rrf_then_model_rerank",
        "llamaindex_fusion": "reciprocal_rerank",
        "num_queries": 1,
        "region": "湖北",
        "embedding": {"usage": {"total_tokens": 9}},
        "rerank": {"model": "qwen3-rerank", "usage": {"total_tokens": 99}},
        "hits": [
            {
                "chunk_id": "b" * 64,
                "score": 0.9,
                "embedding_text": "法律名称：合成租赁法\n正文：租赁原文。",
                "metadata": {
                    "document_id": "c" * 64,
                    "document_name": "合成租赁法",
                    "release_sha256": "a" * 64,
                    "fulltext_sha256": "d" * 64,
                    "source_sha256": "e" * 64,
                    "scope": "national",
                    "region": "",
                    "start_char": 100,
                    "end_char": 105,
                },
            }
        ],
    }


def page():
    return {
        "document_id": "c" * 64,
        "title": "合成租赁法",
        "version": "e" * 64,
        "content_hash": "d" * 64,
        "cursor": None,
        "next_cursor": None,
        "text": "租赁原文。",
        "complete": True,
        "quality": "verified",
        "source_ref": "synthetic-source",
        "dataset_version": "a" * 64,
        "content_scope": "retrieved_excerpts",
        "context_hash": hashlib.sha256("租赁原文。".encode()).hexdigest(),
        "source_ranges": [{"start_char": 100, "end_char": 105, "chunk_ids": ["b" * 64]}],
    }


@pytest.mark.asyncio
async def test_window_pipeline_and_version_bound_read_keep_scope_and_usage():
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        assert request.headers["authorization"] == "Bearer synthetic-key"
        if request.url.path.endswith("/search"):
            assert body == {"query": "内容总结：武汉租赁。", "region": "湖北", "top_k": 7}
            return httpx.Response(200, json=search_response())
        assert request.url.path == "/documents/excerpts"
        assert body == {"document_id": "c" * 64, "chunk_ids": ["b" * 64]}
        return httpx.Response(200, json=page())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with WindowRagLegalResearchTools(
            base_url="http://window.internal", api_key=SecretStr("synthetic-key"), client=client
        ) as tools:
            run = scope()
            candidates = await tools.search(run, "内容总结：武汉租赁。", region="湖北")
            assert candidates[0].snippets == ("租赁原文。",)
            assert (await tools.read(run, "c" * 64, None)).text == "租赁原文。"
            with pytest.raises(ValueError, match="document_not_in_candidates"):
                await tools.read(scope(), "c" * 64, None)
            assert tools.retrieval_calls[0]["collection"] == "lawyer_windows_v2_20260920"
            assert tools.retrieval_calls[0]["rerank_tokens"] == 99
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["fulltext", "unselected_chunk", "changed_text", "changed_offset", "too_large"]
)
async def test_excerpt_response_must_match_retrieved_windows(change):
    def handle(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=search_response())
        value = page()
        if change == "fulltext":
            for key in ("content_scope", "context_hash", "source_ranges"):
                value.pop(key)
        elif change == "unselected_chunk":
            value["source_ranges"][0]["chunk_ids"] = ["f" * 64]
        elif change == "changed_text":
            value["text"] = "虚构的条文"
            value["context_hash"] = hashlib.sha256(value["text"].encode()).hexdigest()
        elif change == "too_large":
            value["text"] += "无关原文" * 1000
            value["context_hash"] = hashlib.sha256(value["text"].encode()).hexdigest()
            value["source_ranges"][0]["end_char"] += 4000
        else:
            value["source_ranges"][0].update(start_char=200, end_char=205)
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with WindowRagLegalResearchTools(
            base_url="http://window.internal", api_key=SecretStr("synthetic"), client=client
        ) as tools:
            run = scope()
            await tools.search(run, "摘要", region="湖北")
            with pytest.raises(ValueError):
                await tools.read(run, "c" * 64, None)


@pytest.mark.asyncio
async def test_multiple_queries_accumulate_windows_without_cross_run_leak():
    calls = 0

    def handle(request):
        nonlocal calls
        value = search_response()
        if calls:
            value["hits"][0]["chunk_id"] = "f" * 64
        calls += 1
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with WindowRagLegalResearchTools(
            base_url="http://window.internal", api_key=SecretStr("synthetic"), client=client
        ) as tools:
            run = scope()
            await tools.search(run, "维修", region="湖北")
            result = await tools.search(run, "押金", region="湖北")
            assert set(result[0].citations) == {"b" * 64, "f" * 64}
            with pytest.raises(ValueError, match="scope_mismatch"):
                await tools.search(scope(), "另一个租户", region="湖北")
            with pytest.raises(ValueError, match="document_not_in_candidates"):
                await tools.read(scope(), "c" * 64, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["old_collection", "no_rerank", "changed_fulltext", "wrong_region", "wrong_model"]
)
async def test_window_adapter_never_falls_back_or_mixes_versions(change):
    def handle(request):
        if request.url.path.endswith("/search"):
            body = search_response()
            if change == "old_collection":
                body["collection"] = "lawyer_db"
            if change == "no_rerank":
                body["mode"] = "rrf_only"
            if change == "wrong_region":
                body["hits"][0]["metadata"].update(scope="local", region="广东")
            if change == "wrong_model":
                body["rerank"]["model"] = "qwen3.7-text-rerank"
        else:
            body = page()
            body["content_hash"] = "f" * 64
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with WindowRagLegalResearchTools(
            base_url="http://window.internal", api_key=SecretStr("synthetic-key"), client=client
        ) as tools:
            with pytest.raises(ValueError):
                run = scope()
                await tools.search(run, "武汉租赁", region="湖北")
                if change == "changed_fulltext":
                    await tools.read(run, "c" * 64, None)


@pytest.mark.asyncio
async def test_summary_region_reaches_mcp_and_both_retrieval_routes():
    from mcp import Client

    from lawyer_agent.application.contract_review.contracts import RunLimits
    from lawyer_agent.application.contract_review.research import (
        BoundedContractResearch,
        ContractBrief,
        ResearchBudget,
    )
    from lawyer_agent.infrastructure.contract_review.legal_documents import build_legal_research_mcp
    from lawyer_agent.infrastructure.contract_review.research import MCPResearchTools

    run = scope()
    queries = []

    class Authority:
        async def current(self):
            return run

        async def require(self, actual, action, resource):
            assert actual == run

    def handle(request):
        body = json.loads(request.content)
        assert body["region"] == "湖北"
        queries.append(body["query"])
        return httpx.Response(200, json=search_response())

    authority = Authority()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with WindowRagLegalResearchTools(
            base_url="http://window.internal", api_key=SecretStr("synthetic-key"), client=client
        ) as service:
            async with Client(
                build_legal_research_mcp(service=service, scopes=authority, authorizer=authority)
            ) as mcp:
                tools = MCPResearchTools(client=mcp, authorizer=authority)
                await tools.discover()
                research = BoundedContractResearch(model=None, tools=tools)
                brief = ContractBrief(
                    is_contract=True,
                    contract_type="租赁",
                    fact_summary="房屋位于武汉，提前退房扣押金。",
                    region="湖北",
                    queries=("押金退还",),
                )
                candidates = await research._search(run, brief, ResearchBudget(limits=RunLimits()))
                assert candidates[0].document_id == "c" * 64
    assert len(queries) == 1 and "内容总结：房屋位于武汉" in queries[0]


def test_window_runtime_configuration_does_not_require_old_corpus():
    from pathlib import Path

    from lawyer_agent.infrastructure.contract_review.runtime import ContractRuntimeConfig

    config = ContractRuntimeConfig(
        retrieval_backend="window_v2",
        dashscope_config_file=Path("C:/test/qwen.json"),
        rag_base_url="http://127.0.0.1:8089",
        rag_api_key_file=Path("C:/test/rag.key"),
        s3_endpoint="http://s3.internal",
        s3_bucket="contracts",
        s3_access_key=SecretStr("key"),
        s3_secret_key=SecretStr("secret"),
    )
    assert config.corpus_root is None and config.retrieval_backend == "window_v2"


def test_failed_run_retains_bounded_retrieval_receipt_without_query_text():
    from lawyer_agent.infrastructure.contract_review.storage import _validate_failure_manifest

    manifest = {
        "documents": [],
        "model_calls": [],
        "retrieval_calls": [
            {
                "status": "error",
                "collection": "lawyer_windows_v2_20260920",
                "region": "湖北",
                "release_sha256": None,
                "embedding_tokens": None,
                "rerank_tokens": None,
            }
        ],
    }
    _validate_failure_manifest(manifest)
    manifest["retrieval_calls"][0]["query"] = "PRIVATE_CONTRACT"
    with pytest.raises(ValueError):
        _validate_failure_manifest(manifest)
