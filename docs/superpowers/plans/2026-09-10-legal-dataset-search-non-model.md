# 2026-09-10 法规数据集检索服务（数据集别名 → 混合检索，非模型编排）

## 目标

`LegalHybridSearchService` 要求调用方显式传 `index_name`（测试全用一次性临时
索引名）；`LegalDatasetAliasService` 已能解析「数据集别名 → 当前索引」。检索
链路缺一层**数据集级入口**：调用方只说「检索当前 dataset_v1」，由服务解析
别名得到物理索引后再跑 BM25+Dense→RRF 混合检索——即 spec 6.6「检索走稳定
别名」的调用方封装，后续 HTTP/QA 层直接消费。

## 范围（只做这些）

- `application/legal_dataset_search.py`：
  - `LegalDatasetNotPublished(ValueError)`（稳定语义：别名尚未指向任何索引 =
    数据集未发布，绝不把「未发布」伪装成「无结果」）；
  - `LegalDatasetSearchService(hybrid, alias)`（构造校验注入对象）：
    `search_dataset(*, alias, query, model_ref, dimension, version_id=None,
    limit=20, bm25_size=100, knn_size=100) -> tuple[LegalSearchHit, ...]`：
    1. `alias.active_dataset_index(alias)` 解析（非法名/未指向 → 稳定错误）；
    2. 解析到索引 → 委托 `LegalHybridSearchService.search`（输入校验、embed、
       BM25+kNN、RRF、截断全部沿用，调用方无感知）；
    3. 别名不存在 → `LegalDatasetNotPublished`。
  - 不改 hybrid/alias 契约；不加依赖；不接 HTTP。
- 真实链路验证（真实 OS 容器可用性 skip + 确定性 fake embed gateway）：
  seed 一版本 → chunk → 建索引发布 dataset 别名 → 经 `search_dataset` 按别名
  命中条文（语义近邻/BM25）→ 未发布别名抛稳定错误 → 清理。

## 明确不做

- 不做 HTTP/租户检索；不做证据组装/门禁（后续 QA 层）；不做跨别名多数据集
  路由与查询规划；不改 `F:\ai律师数据库`；不加依赖、不改 Schema。

## 验证

- 聚焦单测（纯离线 fake）：解析成功委托（index 透传、参数透传、返回命中）、
  别名未发布 → `LegalDatasetNotPublished`、别名解析错误上抛、非法别名校验。
- 真实 MySQL+OS e2e（可用性 skip）：发布后按别名命中、未发布报错、清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
