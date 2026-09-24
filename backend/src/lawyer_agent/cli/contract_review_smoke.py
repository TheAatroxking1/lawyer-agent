"""Explicit synthetic demo: real Workflow/MCP SDK, no real model or legal advice.

Run: python -X utf8 -m lawyer_agent.cli.contract_review_smoke
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from mcp import Client

from lawyer_agent.application.contract_review.contracts import (
    Anchor,
    BlocksResult,
    DocumentBlock,
    EvidenceInput,
    JobInput,
    JobResult,
    LegalEvidence,
    ReadBlocksInput,
    RegionInput,
    ReviewCandidate,
    ReviewError,
    ReviewRequest,
    ReviewResult,
    ReviewScope,
    RiskIssueDraft,
    SearchInput,
    SearchResult,
    StructureInput,
    StructureResult,
    VersionInput,
)
from lawyer_agent.application.contract_review.gate import AnchoredReviewGate
from lawyer_agent.domain.model_gateway import ChatMessage
from lawyer_agent.infrastructure.contract_review.mcp_servers import (
    build_document_mcp,
    build_legal_mcp,
)
from lawyer_agent.infrastructure.contract_review.tools import ControlledMCPTools
from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow


class SyntheticServices:
    """Synthetic-only fixtures; never wired into the HTTP application."""

    def __init__(self) -> None:
        self.scope = ReviewScope(
            tenant_id=uuid4(),
            actor_id=uuid4(),
            run_id=uuid4(),
            document_version_id="synthetic-doc-1",
        )
        self.saved: dict[tuple[UUID, UUID], ReviewResult] = {}
        self.block = DocumentBlock(
            block_id="b1",
            kind="text",
            text="及时付款",
            quality="verified",
            anchors=(
                Anchor(
                    anchor_id="a1", page=1, start=0, end=4, quad=(10, 10, 50, 10, 50, 20, 10, 20)
                ),
            ),
        )

    async def current(self) -> ReviewScope:
        return self.scope

    async def require(self, scope: ReviewScope, action: str, resource_id: str) -> None:
        if scope != self.scope or resource_id not in {
            self.scope.document_version_id,
            "synthetic-job-1",
            "synthetic-law-1",
            "public-law",
        }:
            raise ReviewError("resource_unavailable")

    async def parse(self, scope: ReviewScope, request: VersionInput) -> JobResult:
        return JobResult(
            document_version_id=scope.document_version_id,
            job_id="synthetic-job-1",
            status="ready",
            result_block_ids=("b1",),
        )

    async def job_status(self, scope: ReviewScope, request: JobInput) -> JobResult:
        return await self.parse(scope, VersionInput(document_version_id=scope.document_version_id))

    async def structure(self, scope: ReviewScope, request: StructureInput) -> StructureResult:
        if request.cursor is not None:
            raise ReviewError("resource_unavailable")
        return StructureResult(
            document_version_id=scope.document_version_id,
            block_ids=("b1",),
            recognition_complete=True,
        )

    async def read_blocks(self, scope: ReviewScope, request: ReadBlocksInput) -> BlocksResult:
        if request.block_ids != ("b1",):
            raise ReviewError("resource_unavailable")
        return BlocksResult(document_version_id=scope.document_version_id, blocks=(self.block,))

    async def inspect_region(self, scope: ReviewScope, request: RegionInput) -> JobResult:
        if request.region_id != "b1":
            raise ReviewError("resource_unavailable")
        return await self.parse(scope, VersionInput(document_version_id=scope.document_version_id))

    async def search(self, scope: ReviewScope, request: SearchInput) -> SearchResult:
        return SearchResult(
            items=(await self.read(scope, EvidenceInput(evidence_id="synthetic-law-1")),)
        )

    async def read(self, scope: ReviewScope, request: EvidenceInput) -> LegalEvidence:
        if request.evidence_id != "synthetic-law-1":
            raise ReviewError("resource_unavailable")
        return LegalEvidence(
            evidence_id="synthetic-law-1",
            title="合成检索材料（非法律）",
            text="仅用于协议验证，不能作为法律依据。",
            source_ref="synthetic-fixture",
            version="v1",
            jurisdiction="中国大陆",
            effective_from=None,
            validity="unknown",
            is_complete=True,
        )

    async def save(self, scope: ReviewScope, result: ReviewResult) -> None:
        await self.require(scope, "review_save", result.document_version_id)
        key = (scope.tenant_id, scope.run_id)
        if key in self.saved and self.saved[key] != result:
            raise ReviewError("idempotency_conflict")
        self.saved[key] = result

    async def require_allowed(
        self, scope: ReviewScope, request: ReviewRequest, issue: RiskIssueDraft
    ) -> None:
        """Exact synthetic fixture allowlist; not a production content policy."""
        if (
            issue.category != "wording"
            or issue.evidence_ids
            or issue.problem != "未明确付款期限（合成示例）"
            or issue.suggestion != "由双方明确付款起算条件及期限（合成示例）"
        ):
            raise ReviewError("content_rejected")


class SyntheticModel:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Sequence[ChatMessage]) -> str:
        self.calls += 1
        if self.calls == 1:
            return (
                "Thought: 检查可用材料。\nAction: legal_read\n"
                'Action Input: {"request":{"evidence_id":"synthetic-law-1"}}'
            )
        candidate = ReviewCandidate(
            reviewed_block_ids=("b1",),
            issues=(
                RiskIssueDraft(
                    block_id="b1",
                    anchor_id="a1",
                    start=0,
                    end=4,
                    quote="及时付款",
                    category="wording",
                    severity="medium",
                    problem="未明确付款期限（合成示例）",
                    suggestion="由双方明确付款起算条件及期限（合成示例）",
                ),
            ),
        )
        return "Thought: 仅提供表述提示。\nAnswer: " + candidate.model_dump_json()


async def run_smoke() -> dict[str, Any]:
    services = SyntheticServices()
    document = build_document_mcp(service=services, scopes=services, authorizer=services)
    legal = build_legal_mcp(service=services, scopes=services, authorizer=services)
    async with Client(document) as document_client, Client(legal) as legal_client:
        tools = ControlledMCPTools(
            scope=services.scope, authorizer=services, document=document_client, legal=legal_client
        )
        await tools.discover()
        workflow = ContractReviewWorkflow(
            scope=services.scope,
            model=SyntheticModel(),
            tools=tools,
            gate=AnchoredReviewGate(
                documents=services, authorizer=services, content_policy=services
            ),
            store=services,
        )
        result = await workflow.review(
            ReviewRequest(
                instruction="检查合成合同表述",
                as_of=date(2026, 9, 16),
            )
        )
        count = len((await document_client.list_tools()).tools) + len(
            (await legal_client.list_tools()).tools
        )
    return {
        "synthetic": True,
        "mcp_tools": count,
        "saved": bool(services.saved),
        "progress": workflow.progress,
        "review": result.model_dump(mode="json"),
    }


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
