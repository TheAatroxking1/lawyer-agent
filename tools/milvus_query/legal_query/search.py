"""PyMilvus只读检索；排序与引用字段独立于模型供应商。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, RRFRanker

from legal_query.config import DIMENSIONS, MODEL, require, validate_vector
from legal_query.keywords import bm25_query

COLLECTION = "lawyer_db"
FIELDS = [
    "chunk_id",
    "document_id",
    "document_name",
    "article_no",
    "text",
    "chunk_type",
    "parent_chunk_id",
    "model",
]


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class SearchOptions:
    mode: str = "hybrid"
    top_k: int = 10
    candidate_k: int = 50
    recall_k: int = 100
    document_ids: tuple[str, ...] = ()
    ranker: str = "rrf"
    raw_bm25: bool = False

    def validate(self) -> None:
        require(self.mode in ("hybrid", "dense", "bm25"), "invalid_search_mode")
        require(self.ranker in ("rrf", "weighted"), "invalid_ranker")
        require(
            all(type(x) is int for x in (self.top_k, self.candidate_k, self.recall_k)),
            "invalid_limits",
        )
        require(1 <= self.top_k <= self.candidate_k <= self.recall_k <= 1000, "invalid_limits")
        require(
            len(self.document_ids) <= 100 and all(valid_id(x) for x in self.document_ids),
            "invalid_document_ids",
        )

    def expression(self) -> str:
        self.validate()
        expr = "model == " + json.dumps(MODEL)
        if self.document_ids:
            expr += " and document_id in " + json.dumps(list(self.document_ids))
        return expr


def build_requests(
    query: str, vector: list[float], options: SearchOptions
) -> list[AnnSearchRequest]:
    expr = options.expression()
    return [
        AnnSearchRequest(
            data=[validate_vector(vector)],
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=options.recall_k,
            expr=expr,
        ),
        AnnSearchRequest(
            data=[query if options.raw_bm25 else bm25_query(query)],
            anns_field="sparse_vector",
            param={"metric_type": "BM25", "params": {}},
            limit=options.recall_k,
            expr=expr,
        ),
    ]


def format_hits(hits: list[dict[str, Any]], top_k: int, mode: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen = set()
    for hit in hits:
        entity = hit["entity"]
        identity = entity.get("chunk_id", hit.get("id"))
        require(valid_id(identity) and identity not in seen, "invalid_result_ids")
        require(hit.get("id", identity) == identity, "result_identity_mismatch")
        require(valid_id(entity.get("document_id")), "result_document_missing")
        require(all(key in entity for key in FIELDS), "result_metadata_missing")
        require(
            isinstance(entity["document_name"], str) and bool(entity["document_name"]),
            "result_name_missing",
        )
        require(entity["model"] == MODEL, "result_model_mismatch")
        score = hit["distance"]
        require(type(score) in (int, float) and math.isfinite(score), "invalid_result_score")
        seen.add(identity)
        output.append(
            {
                **{key: entity[key] for key in FIELDS},
                "rank": len(output) + 1,
                "fusion_score" if mode == "hybrid" else "retrieval_score": float(score),
            }
        )
    return output[:top_k]


def search(
    client: Any, query: str, options: SearchOptions, embed: Callable[[str], list[float]]
) -> dict[str, Any]:
    options.validate()
    require(isinstance(query, str) and 0 < len(query.strip()) <= 8000, "invalid_query")
    query = query.strip()
    common = {
        "collection_name": COLLECTION,
        "limit": options.candidate_k,
        "output_fields": FIELDS,
        "consistency_level": "Strong",
        "timeout": 15,
    }
    if options.mode == "hybrid":
        requests = build_requests(query, embed(query), options)
        # RRF直接使用官方排序器；这是名次融合，不是语义模型精排。
        ranker: RRFRanker | Function = RRFRanker(k=60)
        if options.ranker == "weighted":
            ranker = Function(
                name="legal_weighted",
                input_field_names=[],
                function_type=FunctionType.RERANK,
                params={"reranker": "weighted", "weights": [0.6, 0.4], "norm_score": True},
            )
        response = client.hybrid_search(reqs=requests, ranker=ranker, **common)
    else:
        dense = options.mode == "dense"
        data = (
            [validate_vector(embed(query))]
            if dense
            else [query if options.raw_bm25 else bm25_query(query)]
        )
        response = client.search(
            data=data,
            anns_field="vector" if dense else "sparse_vector",
            search_params={"metric_type": "COSINE" if dense else "BM25", "params": {}},
            filter=options.expression(),
            **common,
        )
    require(len(response) == 1, "unexpected_result_batch")
    hits = format_hits(response[0], options.top_k, options.mode)
    return {
        "status": "ok" if hits else "empty",
        "mode": options.mode,
        "ranker": options.ranker if options.mode == "hybrid" else None,
        "candidate_count": len(response[0]),
        "bm25_query": (query if options.raw_bm25 else bm25_query(query))
        if options.mode != "dense"
        else None,
        "hits": hits,
    }


def exact(client: Any, document_id: str, article_no: str, limit: int) -> dict[str, Any]:
    require(valid_id(document_id), "invalid_document_id")
    require(isinstance(article_no, str) and 0 < len(article_no.strip()) <= 256, "invalid_article")
    require(type(limit) is int and 1 <= limit <= 1000, "invalid_limits")
    expr = (
        "document_id == "
        + json.dumps(document_id)
        + " and article_no == "
        + json.dumps(article_no.strip(), ensure_ascii=False)
        + " and model == "
        + json.dumps(MODEL)
    )
    rows = client.query(
        collection_name=COLLECTION,
        filter=expr,
        output_fields=FIELDS,
        limit=limit + 1,
        consistency_level="Strong",
        timeout=15,
    )
    # 精确查询可能同时返回父块和子块，不暗示向量分数或完整性。
    rows.sort(key=lambda r: r["chunk_id"])
    return {
        "status": "ok" if rows else "empty",
        "mode": "exact",
        "truncated": len(rows) > limit,
        "hits": rows[:limit],
    }


def preflight(client: Any) -> dict[str, Any]:
    schema = client.describe_collection(COLLECTION, timeout=15)
    fields = {f["name"]: f for f in schema["fields"]}
    require(set(FIELDS) <= set(fields) and "sparse_vector" in fields, "collection_fields_mismatch")
    require(fields["chunk_id"].get("is_primary") is True, "collection_primary_mismatch")
    require(
        fields["vector"]["type"] == DataType.FLOAT_VECTOR
        and int(fields["vector"]["params"]["dim"]) == DIMENSIONS,
        "collection_vector_mismatch",
    )
    require(
        fields["sparse_vector"]["type"] == DataType.SPARSE_FLOAT_VECTOR,
        "collection_sparse_mismatch",
    )
    functions = schema.get("functions", [])
    require(
        any(
            f.get("input_field_names") == ["text"]
            and f.get("output_field_names") == ["sparse_vector"]
            and f.get("type") == FunctionType.BM25
            for f in functions
        ),
        "collection_bm25_mismatch",
    )
    indexes = [
        client.describe_index(COLLECTION, name, timeout=15)
        for name in client.list_indexes(COLLECTION, timeout=15)
    ]
    require(
        any(i.get("field_name") == "vector" and i.get("metric_type") == "COSINE" for i in indexes),
        "dense_index_mismatch",
    )
    require(
        any(
            i.get("field_name") == "sparse_vector" and i.get("metric_type") == "BM25"
            for i in indexes
        ),
        "sparse_index_mismatch",
    )
    return {
        "collection": COLLECTION,
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "load": str(client.get_load_state(COLLECTION, timeout=15)["state"]),
    }
