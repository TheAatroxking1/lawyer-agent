"""LlamaIndex RRF + Alibaba语义重排；不调用生成LLM、不降级伪装rerank。"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from llama_index.core.llms import CustomLLM, LLMMetadata
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from pydantic import PrivateAttr

from legal_query.config import MODEL, EmbeddingConfig, QueryError, require
from legal_query.window_corpus import COLLECTION, FIELDS, REGIONS, load, validate_ready


class RetrievalOnlyLLM(CustomLLM):
    """满足框架类型要求；若意外启用查询生成则直接拒绝，绝不生成模拟文本。"""

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name="generation-disabled")

    def complete(self, *args, **kwargs):
        raise QueryError("query_generation_disabled")

    def stream_complete(self, *args, **kwargs):
        raise QueryError("query_generation_disabled")


@dataclass(frozen=True)
class Options:
    recall_k: int = 50
    fusion_k: int = 50
    top_k: int = 7
    region: str = ""
    clean_only: bool = False

    def expression(self, release: str) -> str:
        require(
            all(type(x) is int for x in [self.top_k, self.fusion_k, self.recall_k])
            and 1 <= self.top_k <= self.fusion_k <= self.recall_k <= 100,
            "invalid_window_limits",
        )
        require(not self.region or self.region in REGIONS, "invalid_window_region")
        expr = (
            "release_sha256 == "
            + json.dumps(release)
            + " and model == "
            + json.dumps(MODEL)
        )
        if self.region:
            expr += (
                ' and (scope == "national" or region == '
                + json.dumps(self.region, ensure_ascii=False)
                + ")"
            )
        if self.clean_only:
            expr += " and needs_review == false"
        return expr


class MilvusWindowRetriever(BaseRetriever):
    def __init__(self, client, embed, mode, options, release):
        self.client = client
        self.embed = embed
        self.mode = mode
        self.options = options
        self.release = release
        self.last_hits = []
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle):
        dense = self.mode == "dense"
        query = query_bundle.query_str
        result = self.client.search(
            collection_name=COLLECTION,
            data=[self.embed(query)] if dense else [query],
            anns_field="vector" if dense else "sparse_vector",
            search_params={
                "metric_type": "COSINE" if dense else "BM25",
                "params": {"ef": 128} if dense else {},
            },
            filter=self.options.expression(self.release),
            limit=self.options.recall_k,
            output_fields=FIELDS,
            consistency_level="Strong",
            timeout=30,
        )
        require(len(result) == 1, "window_search_batch_invalid")
        nodes = []
        seen = set()
        for hit in result[0]:
            e = hit["entity"]
            ident = e["chunk_id"]
            require(
                ident == hit["chunk_id"] and ident not in seen,
                "window_search_identity_invalid",
            )
            require(
                e["release_sha256"] == self.release and e["model"] == MODEL,
                "window_search_release_mismatch",
            )
            score = hit["distance"]
            require(
                type(score) in (int, float) and math.isfinite(score),
                "window_search_score_invalid",
            )
            seen.add(ident)
            metadata = {k: e[k] for k in FIELDS if k not in ("text", "embedding_text")}
            # Milvus JSON对象键序不固定；TextNode.hash必须在两路保持一致。
            metadata = json.loads(
                json.dumps(metadata, sort_keys=True, ensure_ascii=False)
            )
            nodes.append(
                NodeWithScore(
                    node=TextNode(
                        id_=ident,
                        text=e["embedding_text"],
                        metadata=metadata,
                        excluded_embed_metadata_keys=list(metadata),
                        excluded_llm_metadata_keys=list(metadata),
                    ),
                    score=float(score),
                )
            )
        self.last_hits = copy.deepcopy(nodes)
        return nodes


class FixedRetriever(BaseRetriever):
    def __init__(self, nodes):
        self.nodes = nodes
        super().__init__()

    def _retrieve(self, query_bundle):
        return copy.deepcopy(self.nodes)


def fusion(retrievers, top_k):
    return QueryFusionRetriever(
        retrievers,
        llm=RetrievalOnlyLLM(),
        num_queries=1,
        mode="reciprocal_rerank",
        similarity_top_k=top_k,
        use_async=False,
    )


def fuse_nodes(dense, sparse, top_k):
    return fusion([FixedRetriever(dense), FixedRetriever(sparse)], top_k).retrieve(
        "fusion test"
    )


def validate_rerank(results, count: int, top_n: int):
    require(
        isinstance(results, list) and len(results) == min(count, top_n),
        "rerank_result_count_invalid",
    )
    output = []
    seen = set()
    for row in results:
        index = row.get("index")
        score = row.get("relevance_score")
        require(
            type(index) is int and 0 <= index < count and index not in seen,
            "rerank_result_index_invalid",
        )
        require(
            type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
            "rerank_result_score_invalid",
        )
        seen.add(index)
        output.append((index, float(score)))
    return sorted(output, key=lambda x: x[1], reverse=True)


class AlibabaReranker(BaseNodePostprocessor):
    model: str = "qwen3-rerank"
    endpoint: str
    top_n: int = 7
    _api_key: str = PrivateAttr()
    _transport: Any = PrivateAttr(default=None)
    _last_call: dict = PrivateAttr(default_factory=dict)

    def __init__(self, *, api_key, transport=None, **kwargs):
        super().__init__(**kwargs)
        self._api_key = api_key
        self._transport = transport
        parsed = urlsplit(self.endpoint)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment,
            "rerank_endpoint_invalid",
        )
        require(
            self.model in ("qwen3-rerank", "qwen3.7-text-rerank"),
            "rerank_model_invalid",
        )
        require(bool(api_key.strip()), "rerank_key_missing")

    @property
    def call_info(self):
        return dict(self._last_call)

    def _postprocess_nodes(self, nodes, query_bundle=None):
        require(query_bundle is not None, "rerank_query_missing")
        self._last_call = {}
        if not nodes:
            return []
        documents = [n.node.text for n in nodes]
        query = query_bundle.query_str
        if self.model == "qwen3-rerank":
            body = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": min(self.top_n, len(nodes)),
            }
        else:
            body = {
                "model": self.model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": min(self.top_n, len(nodes))},
            }
        try:
            with httpx.Client(
                timeout=60, follow_redirects=False, transport=self._transport
            ) as client:
                response = client.post(
                    self.endpoint,
                    json=body,
                    headers={"Authorization": "Bearer " + self._api_key},
                )
            if response.status_code != 200:
                raise QueryError("rerank_http_" + str(response.status_code))
            payload = response.json()
            result = (
                payload.get("results")
                if self.model == "qwen3-rerank"
                else payload.get("output", {}).get("results")
            )
            ranked = validate_rerank(result, len(nodes), self.top_n)
            self._last_call = {
                "model": self.model,
                "usage": payload.get("usage"),
                "request_id": payload.get("request_id", payload.get("id")),
            }
            return [
                NodeWithScore(node=nodes[i].node, score=score) for i, score in ranked
            ]
        except httpx.HTTPError:
            raise QueryError("rerank_unavailable") from None
        except (ValueError, KeyError, TypeError):
            raise QueryError("rerank_response_invalid") from None


def configured_models(path: Path):
    require(path.stat().st_size <= 65536, "window_config_too_large")
    config = load(path)
    credentials = Path(config["embedding_config_file"])
    require(credentials.stat().st_size <= 65536, "embedding_config_too_large")
    secret = load(credentials)
    embedding = EmbeddingConfig(**secret)
    embedding.validate()
    rerank_config = config["rerank"]
    key = embedding.api_key
    if rerank_config.get("api_key_file"):
        keypath = Path(rerank_config["api_key_file"])
        require(keypath.stat().st_size <= 65536, "rerank_config_too_large")
        key = load(keypath)["api_key"]
    return embedding, rerank_config, key


def result_rows(nodes):
    return [
        {
            "chunk_id": n.node.node_id,
            "score": n.score,
            "embedding_text": n.node.text,
            "metadata": n.node.metadata,
        }
        for n in nodes
    ]


def search_windows(
    client,
    query,
    ready_path: Path,
    options: Options,
    config_path: Path,
    rrf_only=False,
    *,
    target=None,
):
    require(isinstance(query, str) and 0 < len(query.strip()) <= 8000, "invalid_query")
    receipt = validate_ready(client, ready_path, target=target)
    options.expression(receipt["release_sha256"])
    embedding, rc, key = configured_models(config_path)
    embedding_call = {}

    def embed(query):
        vector, info = embedding.embed_with_usage(query)
        embedding_call.update(info)
        return vector

    dense = MilvusWindowRetriever(
        client, embed, "dense", options, receipt["release_sha256"]
    )
    sparse = MilvusWindowRetriever(
        client, None, "bm25", options, receipt["release_sha256"]
    )
    fused = fusion([dense, sparse], options.fusion_k).retrieve(query.strip())
    rrf = result_rows(fused)
    reranker = AlibabaReranker(
        api_key=key, model=rc["model"], endpoint=rc["endpoint"], top_n=options.top_k
    )
    ranked = (
        fused[: options.top_k]
        if rrf_only
        else reranker.postprocess_nodes(fused, query_str=query.strip())
    )
    return {
        "status": "ok" if ranked else "empty",
        "collection": COLLECTION,
        "release_sha256": receipt["release_sha256"],
        "mode": "rrf_only" if rrf_only else "rrf_then_model_rerank",
        "llamaindex_fusion": "reciprocal_rerank",
        "num_queries": 1,
        "region": options.region,
        "clean_only": options.clean_only,
        "dense": result_rows(dense.last_hits),
        "bm25": result_rows(sparse.last_hits),
        "rrf": rrf,
        "hits": result_rows(ranked),
        "rerank": None if rrf_only else reranker.call_info,
        "embedding": embedding_call,
    }
