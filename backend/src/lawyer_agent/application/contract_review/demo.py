"""Deterministic, non-model contract review fixture for interview demonstrations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from lawyer_agent.application.contract_review.contracts import (
    Anchor,
    DocumentBlock,
    IssueHighlight,
    LegalPassageCitation,
    RiskIssueDraft,
)
from lawyer_agent.application.contract_review.web import (
    ContractRunView,
    EvidenceDocumentLabel,
    McpToolCatalogItem,
)


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 40 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


DEMO_PDF = _pdf("DEMO CONTRACT  |  Apartment service agreement")
_VERSION = "demo-contract-v1"
_LAW_ID = "civil-code-699"
_CONTRACT_TEXT = "Tenant shall pay a service fee of 100% of the monthly rent as a penalty."


def _catalog() -> tuple[McpToolCatalogItem, ...]:
    return (
        McpToolCatalogItem(
            name="document_read_blocks", capability="document.read_blocks",
            description="读取合同原生文字、表格和定位坐标", status="discovered",
        ),
        McpToolCatalogItem(
            name="legal_search_documents", capability="legal.search_documents",
            description="用合同摘要查询第二版法律滑窗索引", status="discovered",
        ),
        McpToolCatalogItem(
            name="legal_read_document", capability="legal.read_document",
            description="读取命中法律来源的完整相关窗口", status="discovered",
        ),
    )


def demo_run() -> ContractRunView:
    block = DocumentBlock(
        block_id="demo-rent-block",
        kind="text",
        text=_CONTRACT_TEXT,
        anchors=(Anchor(
            anchor_id="demo-rent-anchor", page=1, start=0, end=len(_CONTRACT_TEXT),
            quad=(40.0, 680.0, 540.0, 680.0, 540.0, 700.0, 40.0, 700.0),
        ),),
        quality="verified",
    )
    first = RiskIssueDraft(
        block_id=block.block_id, anchor_id="demo-rent-anchor", start=0, end=len(_CONTRACT_TEXT),
        quote=block.text, category="legal", severity="high",
        problem="违约金按月租金的100%计算，可能显著高于实际损失。",
        suggestion="建议改为与实际损失相匹配的固定金额或合理比例，并补充调整上限。",
        evidence_ids=(_LAW_ID,),
        evidence_passages=(LegalPassageCitation(
            passage_id="civil-code-699.window-1", document_id=_LAW_ID,
            quote="约定的违约金过分高于造成的损失的，当事人可以请求人民法院或者仲裁机构予以适当减少。",
        ),),
        highlights=(IssueHighlight(
            block_id=block.block_id, anchor_id="demo-rent-anchor", start=0, end=len(_CONTRACT_TEXT),
        ),),
    )
    second = RiskIssueDraft(
        block_id=block.block_id, anchor_id="demo-rent-anchor", start=0, end=len(_CONTRACT_TEXT),
        quote=block.text, category="commercial", severity="medium",
        problem="服务费与违约金表述混用，触发条件和计算基数不够清楚。",
        suggestion="建议拆分服务费与违约责任条款，明确触发条件、计算基数和支付期限。",
        evidence_ids=(_LAW_ID,),
        evidence_passages=(LegalPassageCitation(
            passage_id="civil-code-699.window-1", document_id=_LAW_ID,
            quote="当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金。",
        ),),
        highlights=(IssueHighlight(
            block_id=block.block_id, anchor_id="demo-rent-anchor", start=0, end=len(_CONTRACT_TEXT),
        ),),
    )
    return ContractRunView(
        id=uuid4(), status="draft", file_name="演示合同-房屋租赁.pdf",
        document_version_id=_VERSION, page_dimensions=((612.0, 792.0),),
        blocks=(block,), issues=(first, second),
        evidence_documents=(EvidenceDocumentLabel(
            document_id=_LAW_ID, title="中华人民共和国民法典",
        ),),
        mcp_tools=_catalog(), is_demo=True,
        message="演示模式：固定样例结果，仅用于展示 Agent、MCP、RAG 和网页批注链路。",
    )


async def demo_events() -> AsyncIterator[dict[str, object]]:
    for stage in (
        "parsing", "contract_understanding", "preparing", "researching",
        "legal_search", "legal_selection", "legal_read", "reviewing", "saved",
    ):
        await asyncio.sleep(0.08)
        yield {"type": "progress", "stage": stage}
    yield {"type": "result", "run": demo_run().model_dump(mode="json")}
    yield {"type": "done", "status": "draft"}
