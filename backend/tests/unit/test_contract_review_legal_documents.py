from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

pytest.importorskip("mcp", reason="requires contract-review extra")
from mcp import Client  # noqa: E402

from lawyer_agent.application.contract_review.contracts import ReviewScope
from lawyer_agent.application.contract_review.research import LegalDocumentPage
from lawyer_agent.infrastructure.contract_review.legal_documents import (
    LocalLegalDocumentStore,
    RagLegalResearchTools,
    build_legal_research_mcp,
)


def _line(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _corpus(root: Path) -> tuple[str, str]:
    document_id = "a" * 64
    folder = "b" * 24
    target = root / folder
    target.mkdir(parents=True)
    paragraphs = b"".join(
        _line(
            {
                "paragraph_id": str(index),
                "ordinal": index,
                "text": text,
                "part": "word/document.xml",
                "location": "body",
                "table_position": None,
            }
        )
        for index, text in enumerate(("甲。", "乙。", "丙。"))
    )
    (target / "paragraphs.jsonl").write_bytes(paragraphs)
    (target / "document.json").write_text(
        json.dumps(
            {
                "schema_version": "legal-corpus-export-v2",
                "id_namespace": "offline-export-v1",
                "document_id": document_id,
                "source_relative_path": "法律/合成法_20260916.docx",
                "source_sha256": "c" * 64,
                "input_sha256": "c" * 64,
                "loader_version": "test",
                "parser_version": "test",
                "export_version": "legal-corpus-export-v2",
                "legal_metadata": {"status": "unknown"},
                "paragraph_count": 3,
                "chunk_count": 4,
                "parent_count": 3,
                "child_count": 1,
                "covered_paragraph_count": 3,
                "quality_flags": ["auxiliary_parts_separate"],
                "output_hashes": {
                    "paragraphs.jsonl": hashlib.sha256(paragraphs).hexdigest(),
                    "chunks.jsonl": "d" * 64,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = _line(
        {
            "code": "completed",
            "status": "completed",
            "document_id": document_id,
            "id_namespace": "offline-export-v1",
            "output_directory": folder,
            "source_relative_path": "法律/合成法_20260916.docx",
            "source_sha256": "c" * 64,
            "quality_flags": ["auxiliary_parts_separate"],
            "schema_version": "legal-corpus-export-v2",
        }
    )
    (root / "manifest.jsonl").write_bytes(manifest)
    return document_id, hashlib.sha256(manifest).hexdigest()


@pytest.mark.asyncio
async def test_local_store_pages_paragraphs_with_bound_cursor(tmp_path: Path) -> None:
    document_id, manifest_hash = _corpus(tmp_path)
    store = LocalLegalDocumentStore(
        root=tmp_path,
        cursor_secret=SecretStr("cursor-secret-with-enough-entropy"),
        expected_manifest_sha256=manifest_hash,
        max_paragraphs=2,
        max_chars=20,
    )

    first = await store.read(document_id, None)
    assert first.document_id == document_id
    assert first.title == "合成法_20260916"
    assert first.cursor is None
    assert first.next_cursor is not None
    assert first.text == "甲。\n乙。\n"
    assert first.complete is False
    assert first.quality == "verified"
    assert first.quality_flags == ("auxiliary_parts_separate",)
    assert first.source_ref == f"offline-export-v1:{document_id}"
    assert first.dataset_version == manifest_hash
    assert first.parser_version == "test"

    second = await store.read(document_id, first.next_cursor)
    assert second.cursor == first.next_cursor
    assert second.next_cursor is None
    assert second.text == "丙。\n"
    assert second.complete is True
    assert second.version == first.version
    assert second.content_hash == first.content_hash


@pytest.mark.asyncio
async def test_cancelled_read_retains_capacity_until_disk_work_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id, _ = _corpus(tmp_path)
    store = LocalLegalDocumentStore(
        root=tmp_path, cursor_secret=SecretStr("cursor-secret-with-enough-entropy"),
        max_concurrency=1,
    )
    entered, release = threading.Event(), threading.Event()
    original = store._read_sync

    def slow_read(document_id: str, cursor: str | None) -> LegalDocumentPage:
        entered.set()
        assert release.wait(3)
        return original(document_id, cursor)

    monkeypatch.setattr(store, "_read_sync", slow_read)
    first = asyncio.create_task(store.read(document_id, None))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(store.read(document_id, None), .02)
        with pytest.raises(TimeoutError, match="legal_document_close_timeout"):
            await store.aclose(timeout_seconds=.02)
    finally:
        release.set()
        await store.aclose(timeout_seconds=2)
    with pytest.raises(ValueError, match="legal_document_store_closed"):
        await store.read(document_id, None)


@pytest.mark.asyncio
async def test_local_store_rejects_tampering_and_cursor_replay(tmp_path: Path) -> None:
    document_id, manifest_hash = _corpus(tmp_path)
    store = LocalLegalDocumentStore(
        root=tmp_path,
        cursor_secret=SecretStr("cursor-secret-with-enough-entropy"),
        expected_manifest_sha256=manifest_hash,
        max_paragraphs=1,
    )
    first = await store.read(document_id, None)

    assert first.next_cursor is not None
    tampered = first.next_cursor[:-1] + ("0" if first.next_cursor[-1] != "0" else "1")
    with pytest.raises(ValueError, match="invalid_cursor"):
        await store.read(document_id, tampered)

    paragraph_file = tmp_path / ("b" * 24) / "paragraphs.jsonl"
    paragraph_file.write_bytes(paragraph_file.read_bytes() + b"{}\n")
    with pytest.raises(ValueError, match="paragraphs_hash_mismatch"):
        await store.read(document_id, None)


class _Store:
    async def read(self, document_id: str, cursor: str | None) -> LegalDocumentPage:
        return LegalDocumentPage(
            document_id=document_id,
            title="合成法",
            version="f" * 64,
            content_hash="e" * 64,
            cursor=cursor,
            text="合成全文",
            complete=True,
            quality="verified",
            source_ref=f"offline-export-v1:{document_id}",
        )


def _scope() -> ReviewScope:
    return ReviewScope(
        tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(), document_version_id="contract-v1"
    )


@pytest.mark.asyncio
async def test_rag_adapter_requests_rrf_and_groups_unique_documents() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "mode": "hybrid",
                "ranker": "rrf",
                "evidence": [
                    {
                        "evidence_id": "ev-1",
                        "document_id": "a" * 64,
                        "document_name": "合成法",
                        "article_no": "第一条",
                        "text": "片段一",
                    },
                    {
                        "evidence_id": "ev-2",
                        "document_id": "a" * 64,
                        "document_name": "合成法",
                        "article_no": "第二条",
                        "text": "片段二",
                    },
                    {
                        "evidence_id": "ev-3",
                        "document_id": "b" * 64,
                        "document_name": "其他法",
                        "article_no": None,
                        "text": "片段三",
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = RagLegalResearchTools(
            base_url="http://rag.internal/api/v1/rag",
            api_key=SecretStr("rag-secret"),
            documents=_Store(),
            client=client,
        )
        candidates = await tools.search(_scope(), "合同违约责任")

    assert [item.document_id for item in candidates] == ["a" * 64, "b" * 64]
    assert candidates[0].snippets == ("片段一", "片段二")
    assert candidates[0].citations == ("ev-1", "ev-2")
    assert requests[0].url == httpx.URL("http://rag.internal/api/v1/rag/search")
    assert requests[0].headers["authorization"] == "Bearer rag-secret"
    assert json.loads(requests[0].content) == {
        "query": "合同违约责任",
        "mode": "hybrid",
        "top_k": 20,
        "document_ids": [],
        "raw_bm25": False,
        "max_chars": 40000,
    }


@pytest.mark.asyncio
async def test_rag_adapter_freezes_candidate_scope_and_three_reads() -> None:
    ids = [f"{index:064x}" for index in range(1, 5)]

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "mode": "hybrid",
                "ranker": "rrf",
                "evidence": [
                    {
                        "evidence_id": f"ev-{index}",
                        "document_id": document_id,
                        "document_name": "合成法",
                        "article_no": None,
                        "text": f"片段{index}",
                    }
                    for index, document_id in enumerate(ids)
                ],
            },
        )

    scope = _scope()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = RagLegalResearchTools(
            base_url="http://rag.internal/api/v1/rag",
            api_key=SecretStr("rag-secret"),
            documents=_Store(),
            client=client,
        )
        await tools.search(scope, "问题")
        for document_id in ids[:3]:
            assert (await tools.read(scope, document_id, None)).document_id == document_id
        with pytest.raises(ValueError, match="document_read_limit_exceeded"):
            await tools.read(scope, ids[3], None)
        with pytest.raises(ValueError, match="document_not_in_candidates"):
            await tools.read(scope, "f" * 64, None)


@pytest.mark.asyncio
async def test_rag_adapter_freezes_cross_query_rrf_top_seven_on_first_read() -> None:
    initial = [f"{index:064x}" for index in range(1, 8)]
    later = f"{8:064x}"
    batches = [initial, [later]]

    def handle(_: httpx.Request) -> httpx.Response:
        document_ids = batches.pop(0)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "mode": "hybrid",
                "ranker": "rrf",
                "evidence": [
                    {
                        "evidence_id": f"ev-{rank}",
                        "document_id": document_id,
                        "document_name": "合成法",
                        "article_no": None,
                        "text": f"摘录{rank}",
                    }
                    for rank, document_id in enumerate(document_ids, start=1)
                ],
            },
        )

    scope = _scope()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = RagLegalResearchTools(
            base_url="http://rag.internal/api/v1/rag",
            api_key=SecretStr("rag-secret"),
            documents=_Store(),
            client=client,
        )
        assert len(await tools.search(scope, "第一问")) == 7
        assert [item.document_id for item in await tools.search(scope, "第二问")] == [later]
        assert (await tools.read(scope, later, None)).document_id == later
        with pytest.raises(ValueError, match="document_not_in_candidates"):
            await tools.read(scope, initial[-1], None)
        with pytest.raises(ValueError, match="research_scope_frozen"):
            await tools.search(scope, "冻结后搜索")


@pytest.mark.asyncio
async def test_rag_adapter_limits_each_scope_to_seven_queries() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "mode": "hybrid",
                "ranker": "rrf",
                "evidence": [
                    {
                        "evidence_id": "ev",
                        "document_id": "a" * 64,
                        "document_name": "合成法",
                        "article_no": None,
                        "text": "摘录",
                    }
                ],
            },
        )

    scope = _scope()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tools = RagLegalResearchTools(
            base_url="http://rag.internal/api/v1/rag",
            api_key=SecretStr("rag-secret"),
            documents=_Store(),
            client=client,
        )
        for index in range(7):
            await tools.search(scope, f"问题{index}")
        with pytest.raises(ValueError, match="research_query_limit_exceeded"):
            await tools.search(scope, "第八问")


@pytest.mark.asyncio
async def test_mcp_uses_request_envelope_trusted_scope_and_authorizer() -> None:
    scope = _scope()

    class Access:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def current(self) -> ReviewScope:
            return scope

        async def require(self, actual: ReviewScope, action: str, resource: str) -> None:
            assert actual == scope
            self.calls.append((action, resource))

    class Tools:
        async def search(self, actual: ReviewScope, query: str):
            assert actual == scope
            return [
                {
                    "document_id": "a" * 64,
                    "title": "合成法",
                    "snippets": [query],
                    "citations": ["ev-1"],
                }
            ]

        async def read(self, actual: ReviewScope, document_id: str, cursor: str | None):
            assert actual == scope
            return await _Store().read(document_id, cursor)

    access = Access()
    server = build_legal_research_mcp(service=Tools(), scopes=access, authorizer=access)
    async with Client(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "legal_search_documents",
            "legal_read_document",
        }
        catalog = {tool.name: tool for tool in listed.tools}
        assert catalog["legal_search_documents"].meta == {
            "lawyer_agent/capability": "legal.search_documents",
            "lawyer_agent/agent_visible": True,
            "lawyer_agent/resource_constant": "public-law",
        }
        assert catalog["legal_read_document"].meta == {
            "lawyer_agent/capability": "legal.read_document",
            "lawyer_agent/agent_visible": True,
            "lawyer_agent/resource_path": "/request/document_id",
        }
        assert "tenant_id" not in str([tool.input_schema for tool in listed.tools])
        searched = await client.call_tool(
            "legal_search_documents", {"request": {"query": "合成问题"}}
        )
        assert not searched.is_error
        read = await client.call_tool(
            "legal_read_document",
            {"request": {"document_id": "a" * 64, "cursor": None}},
        )
        assert not read.is_error
        forged = await client.call_tool(
            "legal_read_document",
            {"request": {"document_id": "a" * 64}, "tenant_id": "forged"},
        )
        assert forged.is_error
    assert access.calls == [
        ("legal_search_documents", "public-law"),
        ("legal_read_document", "a" * 64),
    ]
