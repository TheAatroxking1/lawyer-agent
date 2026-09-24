"""内网只读服务的执行边界；同步外部工作实际结束后才归还并发名额。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pymilvus import MilvusClient

from legal_query.config import MODEL, EmbeddingConfig, QueryError, require
from legal_query.search import COLLECTION, FIELDS, SearchOptions, exact, preflight, search

T = TypeVar("T")


class WorkGate:
    def __init__(self, capacity: int = 2) -> None:
        self.slots = threading.BoundedSemaphore(capacity)

    def run(self, work: Callable[[], T]) -> T:
        if not self.slots.acquire(blocking=False):
            raise QueryError("service_busy")
        try:
            return work()
        finally:
            self.slots.release()


def evidence_bundle(rows: list[dict[str, Any]], request_id: str, max_chars: int) -> dict[str, Any]:
    """只提供库中原文；字符预算不是token数，原始分块完整性尚无权威证明。"""
    evidence: list[dict[str, Any]] = []
    remaining = max_chars
    truncated = False
    for row in rows:
        text = row.get("text") or ""
        if not remaining:
            truncated = True
            break
        clipped = len(text) > remaining
        selected = text[:remaining]
        remaining -= len(selected)
        truncated |= clipped
        evidence.append(
            {
                **{key: row.get(key) for key in FIELDS if key != "text"},
                "text": selected,
                "evidence_id": "ev_"
                + hashlib.sha256((request_id + ":" + row["chunk_id"]).encode()).hexdigest()[:24],
                "rank": len(evidence) + 1,
                "fusion_score": row.get("fusion_score"),
                "retrieval_score": row.get("retrieval_score"),
                "is_complete": False if clipped else None,
                "text_truncated": clipped,
            }
        )
    warnings = ["source_completeness_unverified", "jurisdiction_not_verified"] if evidence else []
    if truncated:
        warnings.append("evidence_character_budget_reached")
    return {"evidence": evidence, "truncated": truncated, "warnings": warnings}


class Runtime:
    def __init__(self, key: str, embedding: EmbeddingConfig) -> None:
        require(
            32 <= len(key) <= 256 and key.isascii() and not any(c.isspace() for c in key),
            "rag_key_invalid",
        )
        self.key = key
        self.embedding = embedding
        self.gate = WorkGate()

    @classmethod
    def from_env(cls) -> Runtime:
        try:
            with Path(os.getenv("RAG_API_KEY_FILE", "/run/secrets/rag-api-key")).open(
                "rb"
            ) as stream:
                key = stream.read(258).decode("ascii").strip()
        except (OSError, UnicodeError):
            raise QueryError("rag_key_invalid") from None
        return cls(key, EmbeddingConfig.load())

    def run(self, operation: str, payload: Any = None, *, request_id: str = "") -> dict[str, Any]:
        return self.gate.run(lambda: self._execute(operation, payload, request_id))

    def _execute(self, operation: str, payload: Any, request_id: str) -> dict[str, Any]:
        # 每个工作独立拥有连接，跨线程不共享Session，finally总会关闭。
        client = MilvusClient(
            uri=os.getenv("MILVUS_URI", "http://standalone:19530"),
            db_name=os.getenv("MILVUS_DB", "blog"),
            token=os.getenv("MILVUS_TOKEN", ""),
            timeout=15,
        )
        try:
            if operation == "ready":
                info = preflight(client)
                require(info["load"] == "Loaded", "collection_not_loaded")
                return {"status": "ready", **info}
            if operation == "search":
                result = search(
                    client,
                    payload.query,
                    SearchOptions(
                        mode=payload.mode,
                        top_k=payload.top_k,
                        candidate_k=50,
                        recall_k=100,
                        document_ids=tuple(payload.document_ids),
                        raw_bm25=payload.raw_bm25,
                    ),
                    self.embedding.embed,
                )
                bundle = evidence_bundle(result["hits"], request_id, payload.max_chars)
                return {
                    **bundle,
                    "status": result["status"],
                    "bm25_query": result["bm25_query"],
                    "candidate_count": result["candidate_count"],
                    "mode": result["mode"],
                    "ranker": result["ranker"],
                }
            require(operation == "read", "invalid_operation")
            if payload.chunk_id:
                expr = (
                    "document_id == "
                    + json.dumps(payload.document_id)
                    + " and chunk_id == "
                    + json.dumps(payload.chunk_id)
                    + " and model == "
                    + json.dumps(MODEL)
                )
                rows = client.query(
                    COLLECTION,
                    filter=expr,
                    output_fields=FIELDS,
                    limit=1,
                    consistency_level="Strong",
                    timeout=15,
                )
                more = False
            else:
                result = exact(client, payload.document_id, payload.article_no, 20)
                rows, more = result["hits"], result["truncated"]
            bundle = evidence_bundle(rows, request_id, payload.max_chars)
            if more:
                bundle["truncated"] = True
                bundle["warnings"].append("article_chunk_limit_reached")
            return {**bundle, "status": "ok" if rows else "empty", "mode": "read"}
        finally:
            client.close()
