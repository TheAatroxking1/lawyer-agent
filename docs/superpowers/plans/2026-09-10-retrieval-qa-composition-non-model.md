# 检索问答生产装配（全链组合 + opensearch_url 配置面）

- 日期：2026-09-10
- 类型：非模型切片（组合代码零网络/模型 I/O；真实检索需 OS + 本地模型）
- 状态：已完成

## 动机

`/api/v1/legal/questions` 端点与编排服务已就绪，但 `ApplicationServices`
装配占位返回 None（永久 503）。本切片补真实全链装配：embed gateway（本地
sentence-transformers，惰性加载）→ 混合检索（OS BM25+kNN）→ 数据集别名解析
→ 权威条文证据组装（新会话适配器）→ DeepSeek claims chat；同时给 `Settings`
补 `opensearch_url` 配置面。

## 范围

- `config.py`：`opensearch_url: str`（默认 http://127.0.0.1:9200，pattern
  `^https?://[^\s]+$`；embed model_ref/dimension 沿用编排组合层默认常量，
  不重复配置面）。
- `api/dependencies.py`：
  - `_build_legal_retrieval_qa_http_service(settings, session_factory)`：
    deepseek key 或 opensearch_url 缺失 → None（503 兜底不变）；齐全 →
    装配 `LocalSentenceTransformerEmbeddingProvider(DEFAULT_EMBED_MODEL_REF)
    → ModelGateway(embed)` + `OpenSearchRestClient(opensearch_url)` +
    `LegalDatasetAliasService` + `LegalHybridSearchService` +
    `LegalDatasetSearchService` + `LegalEvidenceAssemblyService` +
    `LegalDatasetEvidenceService` + DeepSeek chat ModelGateway → 返回
    `LegalRetrievalQaService`；全部惰性（构造零网络/零模型加载）。
  - 新 `_LegalEvidenceAssemblyQueryAdapter(session_factory)`：每次调用开
    独立会话委托 `SqlAlchemyLegalCorpusRepository` 的
    version_with_instrument/provisions_for_version（assembly 查询端口）。
- 修正协议：`LegalDatasetEvidenceService._EvidenceAssemblyPort.assemble`
  参数由 `object` 收紧为 `Sequence[LegalSearchHit]`（mypy strict 下与真实
  实现兼容）。

## 验证

- ruff + mypy strict（162 文件）零错误；
- 新增 3 项装配单测：无 key → None、无 opensearch_url → None、齐全 →
  组合服务可构造且构造零 I/O（session_factory 永不被调）；
- 全 unit + 检索问答全栈（真实 MySQL+Redis，fake 注入路径与默认 503 路径
  均不受装配影响）合计 **1114 passed + 1 skipped**。

## 后续

真实运行前提：本地 embedding 权重已下载（授权可跑）+ OpenSearch 容器在线 +
`dataset_v1` 别名已发布 + DeepSeek key。SSE 流式问答、前端问答页仍为后续。
