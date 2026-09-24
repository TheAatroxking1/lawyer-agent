# 2026-09-10 OpenSearch Dense/k-NN 与 RRF 融合（非模型先行）

## 背景

已授权 embedding。BM25 端到端已通（真实 OS 容器）。要让后续真实向量可用，
索引 mapping 需要 `knn_vector` 字段、检索需要 k-NN 查询、BM25 与 Dense 需要
RRF 融合。本切片先补齐**确定性部分**（mapping/查询构建/RRF 纯函数，
MockTransport 离线验证），真实模型向量化在后续切片经 ModelGateway 接入。

## 范围（只做这些）

- OS 客户端扩展：
  - `ensure_index(index_name, vector_dimension: int | None)`：有维度时 mapping 加
    `content_vector: {"type": "knn_vector", "dimension": N}`（并记录该索引含 dense）。
  - `search_knn(index_name, query_vector, limit, version_id=None)`：POST `/_search`
    `knn: {field: content_vector, query_vector: [...], k: limit}` + 可选 version filter，
    命中复用 `parse_search_hit`（源仍需 chunk/provision/version_id）。
  - MockTransport 单测：带维度 mapping 请求、knn 请求体、版本过滤、503 映射。
- 域纯函数 `reciprocal_rank_fusion(bm25: tuple[LegalSearchHit,...], dense: tuple[LegalSearchHit,...], *, k=60)`
  → 稳定排序的 `LegalSearchHit` 元组（同一 chunk 只出现一次，分值为两个 RRF 分求和）；
  输入空集安全；非法 k 拒绝。
- 单测（纯离线，含 mock transport 断言请求/响应）。

## 明确不做

- 不运行真实 embedding 模型/不接 gateway（后续切片）；不改既有 BM25 契约；
  不加依赖/迁移。

## 验证

- 聚焦单测 green；全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff/mypy 零错误；无 Secret；树干净。
