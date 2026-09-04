# 2026-09-10 语料 OpenSearch 检索客户端（BM25 先行，非模型）

## 背景

Embedding/OpenSearch 检索已获授权。compose 已定义 OpenSearch 3（127.0.0.1:9200、
security disabled），`LegalChunk` 原料层已落库（上一切片），但后端**没有任何**
OpenSearch 代码或依赖。本切片用既有 `httpx`（REST，零新依赖）建检索客户端与
BM25 查询编排，为后续 Dense Vector/RRF/Embedding 提供可插拔索引底座。

## 范围（只做这些）

- 域纯值 `legal_search.py`：`LegalSearchHit(chunk_id, provision_id, version_id, score)`
  与查询参数（query、limit、filter_version_id/dataset_version 可选）。
- `infrastructure/search/opensearch.py`：`OpenSearchRestClient`（httpx.AsyncClient 注入，
  base_url 默认 `http://127.0.0.1:9200`）：
  - `ensure_index(index_name)`：PUT 索引（含 text 字段 BM25 所需 mapping + 版本过滤字段）；
  - `replace_documents(index_name, docs)`：POST `/_bulk`（delete-by-query 旧版本 + 批量 upsert，
    派生可重建、幂等）；
  - `search_bm25(index_name, query, limit, filters)`：POST `/_search`（match on content +
    term filters），返回 hit（id/_score）。
  - 所有 HTTP 走注入 transport（测试用 MockTransport，不联真实 OS）。
- 纯映射 `chunk_document(chunk, version_id)` → 索引 doc；`parse_search_hit(raw)`。
- 单测（httpx MockTransport）：索引创建请求体、bulk 幂等重建、BM25 查询请求/命中解析、
  OS 错误（非 2xx）映射为稳定异常。

## 明确不做

- 不接 Embedding/Dense/RRF/Rerank（后续切片）；不启动/依赖真实 OpenSearch 容器跑单测
  （真实联调切片后续执行并如实标注）；不加 pyproject 依赖；不加租户语义。

## 验证

- 聚焦单测（纯离线）；全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff check src tests / mypy src 零错误；无 Secret；工作树干净。
