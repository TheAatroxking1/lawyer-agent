"""Run-bound MCP adapter for the versioned window RRF + reranker service."""

from __future__ import annotations

import math
import re
from typing import Any

import httpx
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import RetrievalCallReceipt, ReviewScope
from lawyer_agent.application.contract_review.research import LegalCandidate, LegalDocumentPage
from lawyer_agent.infrastructure.contract_review.legal_documents import RagLegalResearchTools

COLLECTION = "lawyer_windows_v2_20260920"
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("window_identity_invalid")
    return value


class _WindowDocuments:
    def __init__(self, owner: WindowRagLegalResearchTools) -> None:
        self.owner = owner

    async def read(self, document_id: str, cursor: str | None) -> LegalDocumentPage:
        owner = self.owner
        identity = owner.identities.get(document_id)
        if identity is None:
            raise ValueError("document_not_in_candidates")
        if cursor is not None:
            raise ValueError("excerpts_do_not_paginate_full_document")
        chunks = owner.selected_chunks(document_id)
        raw = await owner._post_json(
            {"document_id": document_id, "chunk_ids": chunks},
            url=owner.read_url,
        )
        page = LegalDocumentPage.model_validate({k: v for k, v in raw.items() if k != "request_id"})
        if (
            page.document_id,
            page.title,
            page.version,
            page.content_hash,
            page.dataset_version,
        ) != (document_id, *identity):
            raise ValueError("window_document_version_mismatch")
        if page.content_scope != "retrieved_excerpts" or set(chunks) != {
            key for span in page.source_ranges for key in span.chunk_ids
        }:
            raise ValueError("window_excerpt_selection_mismatch")
        offset = 0
        for span in page.source_ranges:
            selected = [owner.windows[document_id][key] for key in span.chunk_ids]
            if (span.start_char < max(0, min(item[0] for item in selected) - 1000)
                    or span.end_char > max(item[1] for item in selected) + 1000
                    or span.end_char - span.start_char > sum(
                        end - start + 2000 for start, end, _ in selected
                    )):
                raise ValueError("window_excerpt_expansion_exceeded")
            for key in span.chunk_ids:
                start, end, text = owner.windows[document_id][key]
                if not span.start_char <= start < end <= span.end_char or page.text[
                    offset + start - span.start_char:offset + end - span.start_char
                ] != text:
                    raise ValueError("window_excerpt_text_mismatch")
            offset += span.end_char - span.start_char + len("\n\n[…未选入段落…]\n\n")
        return page


class WindowRagLegalResearchTools(RagLegalResearchTools):
    """Reuse scope/query/selection limits; never fall back to v1 search or text."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.identities: dict[str, tuple[str, str, str, str]] = {}
        self.windows: dict[str, dict[str, tuple[int, int, str]]] = {}
        self.window_scores: dict[str, float] = {}
        self.bound_scope: tuple[str, str, str, str] | None = None
        self.retrieval_calls: list[dict[str, Any]] = []
        self.release: str | None = None
        self.read_url = base_url.rstrip("/") + "/documents/excerpts"
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            documents=_WindowDocuments(self),
            client=client,
            timeout_seconds=60,
            max_response_bytes=4 * 1024 * 1024,
            max_selected_documents=7,
        )

    async def search(
        self, scope: ReviewScope, query: str, *, region: str = "",
    ) -> tuple[LegalCandidate, ...]:
        key = self._scope_key(scope)
        if self.bound_scope is not None and key != self.bound_scope:
            raise ValueError("window_research_scope_mismatch")
        self.bound_scope = key
        candidates = await super().search(scope, query, region=region)
        return tuple(LegalCandidate(
            document_id=item.document_id, title=item.title,
            snippets=tuple(self.windows[item.document_id][key][2]
                           for key in self.selected_chunks(item.document_id)),
            citations=tuple(self.selected_chunks(item.document_id)),
        ) for item in candidates)

    def selected_chunks(self, document_id: str) -> list[str]:
        return sorted(self.windows[document_id], key=lambda key: -self.window_scores[key])[:7]

    def _search_body(self, query: str, region: str) -> dict[str, Any]:
        return {"query": query, "region": region, "top_k": 7}

    async def _post_json(
        self,
        body: dict[str, Any],
        *,
        url: str | None = None,
    ) -> dict[str, Any]:
        if url is not None:
            return await super()._post_json(body, url=url)
        call: dict[str, Any] = {
            "status": "started",
            "collection": COLLECTION,
            "region": body.get("region", ""),
            "release_sha256": None,
            "embedding_tokens": None,
            "rerank_tokens": None,
        }
        self.retrieval_calls.append(call)
        try:
            result = await super()._post_json(body)
            if result.get("region") != body.get("region"):
                raise ValueError("window_region_mismatch")
            call.update(
                embedding_tokens=_tokens(result.get("embedding")),
                rerank_tokens=_tokens(result.get("rerank")),
            )
            # Validate before returning even if the result is empty.
            self._candidates(result)
            for rank, hit in enumerate(result["hits"], 1):
                meta, chunk_id = hit["metadata"], hit["chunk_id"]
                document_id = meta["document_id"]
                start, end = meta["start_char"], meta["end_char"]
                text = hit["embedding_text"][len(f"法律名称：{meta['document_name']}\n正文："):]
                if type(start) is not int or type(end) is not int or not (
                    0 <= start < end and end - start == len(text)
                ):
                    raise ValueError("window_span_invalid")
                windows = self.windows.setdefault(document_id, {})
                value = (start, end, text)
                if chunk_id in windows and windows[chunk_id] != value:
                    raise ValueError("window_chunk_changed")
                windows[chunk_id] = value
                self.window_scores[chunk_id] = self.window_scores.get(chunk_id, 0) + 1 / (60 + rank)
            call["release_sha256"] = self.release
            for stage in ("dense", "bm25", "rrf"):
                if isinstance(result.get(stage), list):
                    call[stage + "_count"] = len(result[stage])
            call["hit_count"] = len(result["hits"])
            call["status"] = "success"
            RetrievalCallReceipt.model_validate(call)
            return result
        except BaseException:
            call["status"] = "error"
            raise

    def _candidates(self, payload: dict[str, Any]) -> tuple[LegalCandidate, ...]:
        if (
            payload.get("collection") != COLLECTION
            or payload.get("mode") != "rrf_then_model_rerank"
            or payload.get("llamaindex_fusion") != "reciprocal_rerank"
            or payload.get("num_queries") != 1
            or payload.get("status") not in {"ok", "empty"}
        ):
            raise ValueError("window_pipeline_mismatch")
        release = _hash(payload.get("release_sha256"))
        if self.release is not None and release != self.release:
            raise ValueError("window_release_changed")
        hits = payload.get("hits")
        if not isinstance(hits, list) or len(hits) > 7:
            raise ValueError("window_hits_invalid")
        if hits and (
            not isinstance(payload.get("rerank"), dict)
            or payload["rerank"].get("model") != "qwen3-rerank"
        ):
            raise ValueError("window_rerank_missing")
        grouped: dict[str, dict[str, Any]] = {}
        identities: dict[str, tuple[str, str, str, str]] = {}
        chunks = set()
        for hit in hits:
            meta = hit["metadata"]
            document_id, chunk_id = _hash(meta["document_id"]), _hash(hit["chunk_id"])
            if chunk_id in chunks or meta.get("release_sha256") != release:
                raise ValueError("window_hit_identity_mismatch")
            chunks.add(chunk_id)
            region = payload.get("region")
            if region and not (meta.get("scope") == "national" or meta.get("region") == region):
                raise ValueError("window_region_mismatch")
            score = hit["score"]
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("window_rerank_score_invalid")
            title = meta["document_name"]
            if not isinstance(title, str) or not title or len(title) > 4000:
                raise ValueError("window_title_invalid")
            prefix = f"法律名称：{title}\n正文："
            text = hit["embedding_text"]
            if not isinstance(text, str) or not text.startswith(prefix):
                raise ValueError("window_text_invalid")
            text = text[len(prefix) :]
            if not 0 < len(text) <= 1000:
                raise ValueError("window_text_invalid")
            identity = (
                title,
                _hash(meta["source_sha256"]),
                _hash(meta["fulltext_sha256"]),
                release,
            )
            if (
                document_id in self.identities
                and self.identities[document_id] != identity
                or document_id in identities
                and identities[document_id] != identity
            ):
                raise ValueError("window_document_identity_mismatch")
            identities[document_id] = identity
            item = grouped.setdefault(
                document_id, {"title": title, "snippets": [], "citations": []}
            )
            item["snippets"].append(text)
            item["citations"].append(chunk_id)
        candidates = tuple(
            LegalCandidate(document_id=key, **value) for key, value in grouped.items()
        )
        self.identities.update(identities)
        self.release = release
        return candidates

    async def aclose(self) -> None:
        await super().aclose()
        self.identities.clear()
        self.windows.clear()
        self.window_scores.clear()


def _tokens(value: Any) -> int | None:
    usage = value.get("usage") if isinstance(value, dict) else None
    if not isinstance(usage, dict):
        return None
    tokens = usage.get("total_tokens", usage.get("input_tokens"))
    return tokens if type(tokens) is int and tokens >= 0 else None
