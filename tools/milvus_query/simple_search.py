"""官方示例风格：一次Embedding，分别查看向量、BM25、RRF结果。"""

import json
import logging
import os
import sys
from typing import Any

# 仅复用密钥读取和Embedding调用，下面的检索直接使用PyMilvus。
from legal_query.config import MODEL, EmbeddingConfig, QueryError, require
from legal_query.keywords import bm25_query
from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker

QUESTION = "北京租房的时候，有什么需要注意的"
RECALL = 100  # 每一路召回数量
CANDIDATES = 50  # RRF融合后保留数量
SHOW_TOP = 5  # 每组打印多少条；想多看可改成20或50，再重建镜像
FIELDS = ["document_name", "article_no", "text", "document_id", "chunk_id", "chunk_type"]


def compare(client: Any, question: str, vector: list[float]) -> dict[str, Any]:
    # 两路使用相同模型过滤，与原查询命令保持一致。
    expr = "model == " + json.dumps(MODEL)
    keywords = bm25_query(question)  # 仅BM25清理，向量仍由完整问题生成。
    dense_params = {"metric_type": "COSINE", "params": {}}
    bm25_params = {"metric_type": "BM25", "params": {}}
    common = dict(
        collection_name="lawyer_db", output_fields=FIELDS, consistency_level="Strong", timeout=15
    )

    # 1. 单独看语义向量检索：输入1024维问题向量。
    dense = client.search(
        data=[vector],
        anns_field="vector",
        search_params=dense_params,
        filter=expr,
        limit=RECALL,
        **common,
    )[0]

    # 2. 单独看BM25：输入问题原文，由已有BM25 Function处理。
    bm25 = client.search(
        data=[keywords],
        anns_field="sparse_vector",
        search_params=bm25_params,
        filter=expr,
        limit=RECALL,
        **common,
    )[0]

    # 3. 官方多向量搜索写法：两条ANN请求 + 查询级RRF排序器。
    requests = [
        AnnSearchRequest(
            data=[vector], anns_field="vector", param=dense_params, limit=RECALL, expr=expr
        ),
        AnnSearchRequest(
            data=[keywords], anns_field="sparse_vector", param=bm25_params, limit=RECALL, expr=expr
        ),
    ]
    # 官方RRF排序器按两路名次融合；不调用语义精排模型。
    # k是名次平滑参数，不是返回条数；返回数量由下面的limit控制。
    ranker = RRFRanker(k=60)
    rrf = client.hybrid_search(reqs=requests, ranker=ranker, limit=CANDIDATES, **common)[0]

    # 三组保留SDK原始结构；distance在三组中的含义分别为COSINE、BM25、RRF分数。
    # hybrid_search内部会再做两路搜索，不是直接把上面两个Python列表传进去。
    return {
        "bm25_query": keywords,
        "dense": dense[:SHOW_TOP],
        "bm25": bm25[:SHOW_TOP],
        "rrf": rrf[:SHOW_TOP],
    }


def main() -> int:
    try:
        require(len(sys.argv) <= 2, "usage: simple_search.py [question]")
        question = sys.argv[1].strip() if len(sys.argv) == 2 else QUESTION
        require(bool(question), "empty_question")
        logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
        client = MilvusClient(
            uri=os.getenv("MILVUS_URI", "http://standalone:19530"),
            db_name=os.getenv("MILVUS_DB", "blog"),
            token=os.getenv("MILVUS_TOKEN", ""),
            timeout=15,
        )
        try:
            vector = EmbeddingConfig.load().embed(question)  # 三组共用，仅调用一次百炼
            results = compare(client, question, vector)
        finally:
            client.close()
        print(json.dumps({"question": question, **results}, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        # 失败时不打印供应商原始响应或密钥。
        code = str(error) if isinstance(error, QueryError) else "query_failed"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
