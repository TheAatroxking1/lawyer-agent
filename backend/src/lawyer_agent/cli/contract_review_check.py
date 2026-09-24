"""Explicit local PDF/research integration check; not a public upload endpoint.

Runs the real parser, two in-process MCP servers, configured Qwen and HTTP RAG.
Only identifiers, counts, hashes and usage are written to the JSON report.
No legal opinion is generated or represented as an approved contract review.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from mcp import Client
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import (
    BlocksResult,
    JobResult,
    ReviewError,
    ReviewRequest,
    ReviewScope,
    RunLimits,
    StructureResult,
)
from lawyer_agent.application.contract_review.research import (
    BoundedContractResearch,
    ResearchBudget,
    ResearchResult,
)
from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.model_gateway import CallLimits, ModelCallRecord
from lawyer_agent.infrastructure.contract_review.llm import GatewayReviewModel
from lawyer_agent.infrastructure.contract_review.mcp_servers import build_document_mcp
from lawyer_agent.infrastructure.contract_review.research import MCPResearchTools
from lawyer_agent.infrastructure.providers.dashscope import (
    DashScopeChatProvider,
    DashScopeSettings,
)


def research_report(result: ResearchResult) -> dict[str, Any]:
    """A safe diagnostic summary, deliberately omitting all document bodies."""
    return {
        "status": result.status,
        "review_completed": False,
        "contract_type": result.brief.contract_type,
        "candidate_count": len(result.candidates),
        "full_documents_read": len(result.documents),
        "candidates": [
            {"document_id": item.document_id, "title": item.title}
            for item in result.candidates
        ],
        "documents": [
            {
                "document_id": item.document_id, "title": item.title,
                "version": item.version, "content_hash": item.content_hash,
                "characters_read": len(item.text), "quality_flags": item.quality_flags,
            }
            for item in result.documents
        ],
    }


class _LocalScope:
    """Trusted single-user CLI scope, never installed in the HTTP application."""

    def __init__(self, scope: ReviewScope) -> None:
        self.scope = scope

    async def current(self) -> ReviewScope:
        return self.scope

    async def require(self, scope: ReviewScope, action: str, resource_id: str) -> None:
        if scope != self.scope:
            raise ReviewError("resource_unavailable")
        if action.startswith("document_") and resource_id != scope.document_version_id:
            raise ReviewError("resource_unavailable")
        if action not in {
            "document_parse", "document_get_structure", "document_structure",
            "document_read_blocks",
            "legal_search_documents", "legal_read_document",
        }:
            raise ReviewError("tool_not_allowed")


class _UsageRecorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


async def _document_context(
    client: Client, scope: ReviewScope, budget: ResearchBudget,
) -> tuple[str, int]:
    async def call(name: str, request: dict[str, Any]) -> dict[str, Any]:
        budget.consume_tool()
        result = await client.call_tool(name, {"request": request})
        if result.is_error or not isinstance(result.structured_content, dict):
            raise ReviewError("document_unavailable")
        if len(json.dumps(result.structured_content).encode()) > budget.limits.max_response_bytes:
            raise ReviewError("tool_result_too_large")
        return result.structured_content

    version = {"document_version_id": scope.document_version_id}
    job = JobResult.model_validate(await call("document_parse", version))
    if job.status != "ready" or job.document_version_id != scope.document_version_id:
        raise ReviewError("document_incomplete")
    ids: list[str] = []
    cursors: set[str] = set()
    cursor = None
    for _ in range(10):
        page = StructureResult.model_validate(
            await call("document_get_structure", {**version, "cursor": cursor})
        )
        if page.document_version_id != scope.document_version_id or not page.recognition_complete:
            raise ReviewError("document_incomplete")
        ids.extend(page.block_ids)
        if len(ids) > 2000 or len(ids) != len(set(ids)):
            raise ReviewError("invalid_document_structure")
        cursor = page.next_cursor
        if cursor is None:
            break
        if cursor in cursors:
            raise ReviewError("invalid_document_structure")
        cursors.add(cursor)
    else:
        raise ReviewError("document_too_large")
    if not ids:
        raise ReviewError("document_incomplete")
    blocks: list[dict[str, Any]] = []
    for offset in range(0, len(ids), 20):
        selected = ids[offset : offset + 20]
        response = BlocksResult.model_validate(
            await call("document_read_blocks", {**version, "block_ids": selected})
        )
        if (
            response.document_version_id != scope.document_version_id or response.truncated
            or len(response.blocks) != len(selected)
            or {block.block_id for block in response.blocks} != set(selected)
            or any(block.quality != "verified" for block in response.blocks)
        ):
            raise ReviewError("document_incomplete")
        blocks.extend({"block_id": b.block_id, "text": b.text,
                       "table_position": b.table_position.model_dump()
                       if b.table_position is not None else None}
                      for b in response.blocks)
    context = json.dumps({**version, "blocks": blocks}, ensure_ascii=False)
    if len(context.encode("utf-8")) > budget.limits.max_context_bytes:
        raise ReviewError("context_limit_exceeded")
    return context, len(ids)


async def run_check(args: argparse.Namespace) -> dict[str, Any]:
    # Optional runtime dependencies load only for this explicit check command.
    from lawyer_agent.infrastructure.contract_review.legal_documents import (
        LocalLegalDocumentStore,
        RagLegalResearchTools,
        build_legal_research_mcp,
    )
    from lawyer_agent.infrastructure.contract_review.pdf import PdfDocumentService

    if not args.live:
        raise ReviewError("live_check_requires_explicit_flag")
    if not args.pdf.is_file() or args.pdf.stat().st_size > 10 * 1024 * 1024:
        raise ReviewError("invalid_pdf")
    payload = args.pdf.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    scope = ReviewScope(tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(),
                        document_version_id=digest)
    authority = _LocalScope(scope)
    settings = DashScopeSettings.load(args.key_file.resolve())
    recorder = _UsageRecorder()
    limits = RunLimits(max_tool_calls=100, max_steps=8, max_context_bytes=2 * 1024 * 1024,
                       max_response_bytes=256 * 1024, timeout_seconds=600)
    budget = ResearchBudget(limits=limits)
    store = await asyncio.to_thread(
        LocalLegalDocumentStore, root=args.corpus,
        cursor_secret=SecretStr(secrets.token_hex(32)), max_paragraphs=1000,
    )
    pdf = PdfDocumentService(trusted_scope=scope, pdf_bytes=payload, authorizer=authority)
    report: dict[str, Any] = {
        "mode": "local_live_research_check", "review_completed": False,
        "pdf_sha256": digest, "model": settings.model_name, "ranker": "rrf",
    }
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as http:
        gateway = ModelGateway(
            DashScopeChatProvider(settings=settings, client=http), recorder,
            limits=CallLimits(timeout_seconds=90.0),
        )
        model = GatewayReviewModel(gateway, model_ref=settings.model_name)
        async with RagLegalResearchTools(
            base_url=args.rag_url,
            api_key=SecretStr(args.rag_key_file.read_text(encoding="utf-8").strip()),
            documents=store,
        ) as rag:
            document_server = build_document_mcp(
                service=pdf, scopes=authority, authorizer=authority,
            )
            legal_server = build_legal_research_mcp(
                service=rag, scopes=authority, authorizer=authority,
            )
            try:
                async with Client(document_server) as docs, Client(legal_server) as legal:
                    async with asyncio.timeout(limits.timeout_seconds):
                        context, count = await _document_context(docs, scope, budget)
                        report.update(pdf_pages=len(pdf.page_dimensions), pdf_blocks=count)
                        research_tools = MCPResearchTools(
                            client=legal,
                            authorizer=authority,
                        )
                        await research_tools.discover()
                        research = BoundedContractResearch(
                            model=model,
                            tools=research_tools,
                        )
                        result = await research.run(
                            scope=scope,
                            request=ReviewRequest(instruction="请中立审阅这份合同有哪些问题。",
                                                  as_of=date.today()),
                            document=context, budget=budget,
                        )
                        report.update(research_report(result))
            except ReviewError as exc:
                report.update(status="failed", code=exc.code)
            except TimeoutError:
                report.update(status="failed", code="check_timeout")
            finally:
                try:
                    await pdf.aclose()
                finally:
                    await store.aclose()
    report["model_calls"] = [asdict(record) for record in recorder.records]
    report["tool_calls"] = budget.tool_calls
    return report


def main() -> None:
    root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--corpus", type=Path, default=Path("F:/ai律师数据库/切块第一版/chunks"))
    parser.add_argument("--key-file", type=Path, default=root / "deploy/secrets/dashscope.json")
    parser.add_argument(
        "--rag-key-file", type=Path, default=root / "deploy/secrets/rag-api-key.txt",
    )
    parser.add_argument("--rag-url", default="http://192.168.31.14:8088/api/v1/rag")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_check(args))
    except Exception as exc:  # noqa: BLE001 - never print credentials or provider bodies
        report = {"status": "failed", "code": getattr(exc, "code", "check_failed")}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if report.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
