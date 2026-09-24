# 2026-09-10 语料 OpenSearch 端到端（chunk → 索引 → BM25）

## 目标

前两切片补齐了 chunk 原料层与 OpenSearch REST 客户端（MockTransport 离线验证），
但尚未对**真实 OpenSearch** 做联调。本切片把链闭合：seed 语料 → derive chunks →
`ensure_index` → `replace_documents`（幂等重建）→ `search_bm25` 读回命中，
验证真实容器行为与映射/解析契约一致。

## 范围（只做这些）

- 新增真实 OpenSearch 全栈集成测试（MySQL seed 同既有语料基建）：
  1. `ensure_index`（测试专用唯一索引名，带时间戳后缀避免污染开发索引）；
  2. seed instrument+version+provision（两条不同内容条文）→ derive_chunks → replace；
  3. `search_bm25` 命中相关条文、非命中关键词 0 命中、version 过滤生效；
  4. 再次 replace（同 parser_version 幂等重建）后文档数不变；
  5. 结束清理（DELETE 测试索引）。
- 若 OpenSearch 不可达（非 CI 环境），按既有 Redis 可用性模式 `pytest.skip`，
  不伪造通过；本地启动容器即为真实验证证据。
- 该集成测试标记 `integration` + `opensearch`；不进全量 MySQL-only 门禁。

## 明确不做

- Embedding/Dense/RRF/Rerank（后续）；不改既有 OpenSearchRestClient 公共契约
  （只按需暴露已存在能力）；不加依赖/迁移。

## 验证

- 真实 OpenSearch + MySQL 全栈 green；既有 OS 客户端单测回归；ruff/mypy 零错误；
  无 Secret；工作树干净。
