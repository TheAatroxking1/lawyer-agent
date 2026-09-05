"""Retrieval-grounded legal Q&A orchestration (non-model).

spec 6.5 pipeline (single non-stream call): a question goes against the
published dataset by stable alias; retrieval returns an ``EvidenceBundle``;
the model answers with structured ``claims[]`` bound to evidence ids from that
bundle; the backend verifies every claim through the citation gate before
anything is shown. Refusals (no evidence, unparsable claims, unsupported
claims) are normal, deterministic answers with stable reason codes — the
service never fabricates a supported answer.

Provider failures are deliberately NOT folded into refusals: they surface as
typed ``ModelGatewayError`` so the HTTP layer can map 5xx states distinctly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from lawyer_agent.application.evidence import (
    CitationGate,
    EvidenceBundle,
    EvidenceItem,
)
from lawyer_agent.application.legal_claim_gate import (
    LegalClaim,
    LegalClaimGate,
)
from lawyer_agent.application.legal_claim_parse import (
    LegalClaimsParseError,
    parse_claims_text,
)
from lawyer_agent.domain.model_gateway import ChatMessage, TokenUsage

REFUSAL_NO_EVIDENCE = "no_evidence"
REFUSAL_CLAIM_PARSE_FAILED = "claim_parse_failed"
REFUSAL_CLAIM_NOT_SUPPORTED_PREFIX = "claim_not_supported:"
ALLOWED_REASON = "allowed"

DEFAULT_ALIAS = "dataset_v1"
DEFAULT_LIMIT = 12
DEFAULT_BM25_SIZE = 80
DEFAULT_KNN_SIZE = 80
# Embedding provider identity used for the query vector. It belongs to the
# composition/configuration layer; clients never choose it per request.
DEFAULT_EMBED_MODEL_REF = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBED_DIMENSION = 512
# The claims chat goes to the composed chat provider under its fixed model.
CHAT_MODEL_REF = "deepseek-chat"
EVIDENCE_BUDGET_CHARS = 12_000
PER_ITEM_CAP_CHARS = 3_000
TRUNCATION_MARK = "…（条文过长已截断，引用核验以依据编号对应的权威条文为准）"

_REFUSAL_TEXT: dict[str, str] = {
    REFUSAL_NO_EVIDENCE: "未能在当前法规数据集中找到与问题相关的有效依据，暂不作答。",
    REFUSAL_CLAIM_PARSE_FAILED: "模型未返回可核验的结构化结论，已安全拒答，请换一种问法重试。",
}

_SYSTEM_HEADER = (
    "你是中国大陆法律法规问答助手。你只能依据下方给出的条文依据回答，"
    "逐条给出结论，每个结论都必须引用至少一个依据编号；"
    "不得引用列表之外的依据，不得编造条文。"
)


class _DatasetEvidencePort(Protocol):
    async def search_evidence(
        self,
        *,
        alias: str,
        query: str,
        model_ref: str,
        dimension: int,
        version_id: UUID | None,
        limit: int,
        bm25_size: int,
        knn_size: int,
    ) -> EvidenceBundle | None: ...


class _ChatPort(Protocol):
    async def chat(
        self,
        *,
        model_ref: str,
        messages: Sequence[ChatMessage],
    ) -> tuple[str, TokenUsage]: ...


@dataclass(frozen=True, slots=True)
class LegalRetrievalAnswer:
    """Structured answer; refusals carry reason codes, never fabricated text."""

    text: str
    refused: bool
    reason: str
    claims: tuple[LegalClaim, ...] = ()
    citations: tuple[EvidenceItem, ...] = ()
    usage: TokenUsage | None = None


def build_claims_system_prompt(
    items: Sequence[EvidenceItem],
    *,
    budget_chars: int = EVIDENCE_BUDGET_CHARS,
    per_item_cap_chars: int = PER_ITEM_CAP_CHARS,
) -> str:
    """Numbers the evidence deterministically (item order) and formats it.

    Over-long items are capped with an explicit truncation marker; text inside
    the prompt is context for the model only — the citation gate re-verifies
    against the authoritative bundle ids regardless.
    """
    if isinstance(budget_chars, bool) or not isinstance(budget_chars, int):
        raise ValueError("budget_chars must be a positive integer")
    if budget_chars <= 0:
        raise ValueError("budget_chars must be a positive integer")
    if isinstance(per_item_cap_chars, bool) or not isinstance(per_item_cap_chars, int):
        raise ValueError("per_item_cap_chars must be a positive integer")
    if per_item_cap_chars <= 0:
        raise ValueError("per_item_cap_chars must be a positive integer")

    lines: list[str] = []
    used = 0
    for index, item in enumerate(items, start=1):
        body = item.provision_text
        truncated = False
        if len(body) > per_item_cap_chars:
            body = body[:per_item_cap_chars]
            truncated = True
        suffix = TRUNCATION_MARK if truncated else ""
        line = (
            f"[{index}]（依据编号 {item.evidence_id}）"
            f"《{item.instrument_title}》{item.version_label} "
            f"{item.provision_no}：{body}{suffix}"
        )
        cost = len(line)
        if used + cost > budget_chars and used > 0:
            break
        lines.append(line)
        used += cost
    if not lines:
        raise ValueError("no evidence item fits the prompt budget")
    body = "\n".join(lines)
    instruction = (
        "依据列表编号引用。回答输出 JSON："
        '{"claims":[{"text":"完整结论表述","evidence_ids":["依据编号(uuid)"]}]}，'
        "evidence_ids 只能取自上方列表中的依据编号。"
    )
    return f"{_SYSTEM_HEADER}\n\n{body}\n\n{instruction}"


class LegalRetrievalQaService:
    """Question -> evidence -> structured claims -> citation-gated answer."""

    def __init__(
        self,
        dataset_evidence: _DatasetEvidencePort,
        chat: _ChatPort,
        *,
        embed_model_ref: str = DEFAULT_EMBED_MODEL_REF,
        embed_dimension: int = DEFAULT_EMBED_DIMENSION,
    ) -> None:
        if not hasattr(dataset_evidence, "search_evidence"):
            raise ValueError("retrieval qa requires a dataset evidence service")
        if not hasattr(chat, "chat"):
            raise ValueError("retrieval qa requires a chat gateway")
        if not isinstance(embed_model_ref, str) or not embed_model_ref.strip():
            raise ValueError("embed model_ref must be non-empty text")
        if (
            isinstance(embed_dimension, bool)
            or not isinstance(embed_dimension, int)
            or embed_dimension <= 0
        ):
            raise ValueError("embed dimension must be a positive integer")
        self._dataset_evidence = dataset_evidence
        self._chat = chat
        self._embed_model_ref = embed_model_ref.strip()
        self._embed_dimension = embed_dimension

    async def answer(
        self,
        *,
        alias: str,
        question: str,
        target_date: date,
        version_id: UUID | None = None,
        limit: int = DEFAULT_LIMIT,
        bm25_size: int = DEFAULT_BM25_SIZE,
        knn_size: int = DEFAULT_KNN_SIZE,
        model_ref: str | None = None,
        dimension: int | None = None,
    ) -> LegalRetrievalAnswer:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("dataset alias must be non-empty text")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be non-empty text")
        if not isinstance(target_date, date):
            raise ValueError("target_date must be a date")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        effective_model_ref = (
            self._embed_model_ref if model_ref is None else model_ref
        )
        effective_dimension = (
            self._embed_dimension if dimension is None else dimension
        )
        if not isinstance(effective_model_ref, str) or not effective_model_ref.strip():
            raise ValueError("model_ref must be non-empty text")
        if (
            isinstance(effective_dimension, bool)
            or not isinstance(effective_dimension, int)
            or effective_dimension <= 0
        ):
            raise ValueError("dimension must be a positive integer")

        bundle = await self._dataset_evidence.search_evidence(
            alias=alias,
            query=question,
            model_ref=effective_model_ref,
            dimension=effective_dimension,
            version_id=version_id,
            limit=limit,
            bm25_size=bm25_size,
            knn_size=knn_size,
        )
        if bundle is None:
            return self._refusal(REFUSAL_NO_EVIDENCE)

        system = build_claims_system_prompt(bundle.items)
        messages = (
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=question),
        )
        text, usage = await self._chat.chat(
            model_ref=CHAT_MODEL_REF,
            messages=messages,
        )

        try:
            claims = parse_claims_text(text)
        except LegalClaimsParseError:
            return self._refusal(REFUSAL_CLAIM_PARSE_FAILED, usage=usage)

        gate = LegalClaimGate(bundle, CitationGate())
        result = await gate.verify(claims=claims, target_date=target_date)
        if not result.allowed:
            # The claim gate already returns reason="claim_not_supported:<code>".
            return self._refusal(result.reason, usage=usage)

        allowed_ids: list[UUID] = []
        seen: set[UUID] = set()
        for claim in claims:
            for evidence_id in claim.evidence_ids:
                if evidence_id not in seen:
                    seen.add(evidence_id)
                    allowed_ids.append(evidence_id)
        citations = tuple(
            bundle.item(evidence_id) for evidence_id in allowed_ids
        )

        numbered = "\n\n".join(
            f"{index}. {claim.text}" for index, claim in enumerate(claims, start=1)
        )
        return LegalRetrievalAnswer(
            text=numbered,
            refused=False,
            reason=ALLOWED_REASON,
            claims=claims,
            citations=citations,
            usage=usage,
        )

    @staticmethod
    def _refusal(
        reason: str,
        *,
        usage: TokenUsage | None = None,
    ) -> LegalRetrievalAnswer:
        base = _REFUSAL_TEXT.get(reason.split(":")[0], "无法核验的回答已安全拒答。")
        if reason.startswith(REFUSAL_CLAIM_NOT_SUPPORTED_PREFIX):
            base = "模型结论引用的依据未通过核验，已拒绝发布该结论。"
        return LegalRetrievalAnswer(
            text=base,
            refused=True,
            reason=reason,
            usage=usage,
        )
