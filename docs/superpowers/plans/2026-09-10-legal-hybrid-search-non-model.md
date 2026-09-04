# 2026-09-10 法规混合检索服务（BM25 + Dense → RRF）

## 目标

OpenSearch BM25/k-NN 客户端、真实 Embedding（CPU 模型）、RRF 融合与
向量索引编排均已单独验证。缺一个**编排服务**把查询串起来：
query 文本 → 经 gateway embed 得向量 → BM25 检索 + k-NN 检索 →
`reciprocal_rank_fusion` 融合 → 返回统一 LegalSearchHit 列表。
对调用方隐藏「何时 embed、两个后端、如何融合」的细节，只给查询语义。

## 范围（只做这些）

- `application/legal_hybrid_search.py`：
  - `LegalHybridSearchService`（依赖注入：`ModelGateway`（embed）、
    `OpenSearchRestClient`、`LegalChunkReadPort` 可选用于 version 归属校验/过滤）：
    - `search(query, *, model_ref, dimension, index_name, version_id=None,
      limit=20, bm25_size=100, knn_size=100)`
      → gateway.embed(query) → search_bm25 + search_knn（同 version 过滤）
      → RRF(k=60) → 截断到 limit；
    - 输入校验：query 非空、limit/bm25_size/knn_size 正整数、版本过滤与索引名匹配；
    - 空结果返回空元组（不抛错）。
  - 仅做检索编排：不执行证据门禁/条文恢复（后续切片接 EvidenceBundle）。

## 明确不做

- 不改 gateway/OS/RRF 契约；不做租户知识/联合检索/Query Planner 法域解析；
  不接 Evidence/问答/SSE；不加依赖。

## 验证

- 聚焦单测：用确定性 fake provider + MockTransport 断言
  embed→两个 `_search` 请求→RRF 后顺序/截断；version 过滤透传；空结果；
  坏输入拒绝；单测为纯离线。全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff/mypy 零错误；无 Secret；树干净。
