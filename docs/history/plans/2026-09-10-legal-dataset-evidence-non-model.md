# 2026-09-10 法规数据集检索证据编排（数据集别名检索 → EvidenceBundle，非模型）

## 目标

`LegalDatasetSearchService` 返回命中（LegalSearchHit）；`LegalEvidenceAssemblyService`
把命中恢复为权威条文 `EvidenceBundle`。检索问答链路（后续 QA/证据门禁）需要
一个**单一入口**：按数据集别名提问 → 自动检索 → 权威条文证据组装，无命中/
不可追溯则得到 `None`（由上层拒答）。本切片提供 `LegalDatasetEvidenceService`
编排，隐藏「解析别名 → 混合检索 → 按版本恢复权威条文」的全部细节。

## 范围（只做这些）

- `application/legal_dataset_evidence.py`：
  - `LegalDatasetEvidenceService(dataset_search, evidence_assembly)`（构造校验
    注入对象）：
    `search_evidence(*, alias, query, model_ref, dimension, version_id=None,
    limit=20, bm25_size=100, knn_size=100) -> EvidenceBundle | None`：
    1. `LegalDatasetSearchService.search_dataset` 检索当前数据集（未发布 →
       `LegalDatasetNotPublished` 上抛，不吞）；
    2. 命中空 → 返回 `None`（无证据，由上层拒答/降级）；
    3. 命中 → `LegalEvidenceAssemblyService.assemble` 恢复权威条文并组装
       `EvidenceBundle`（去重/来源缺失拒绝/版本归属语义沿用）。
  - 只读编排、不改两服务契约、不加依赖、不接 HTTP/门禁（CitationGate 由
    上层对 claims 执行）。

## 明确不做

- 不做 claims 生成 / CitationGate 触发（QA 层后续）；不做 HTTP/SSE；不做
  租户私有知识检索；不改 `F:\ai律师数据库`；不改 Schema。

## 验证

- 聚焦单测（纯离线 fake dataset_search/evidence_assembly）：
  命中 → 委托检索+组装并返回 Bundle；空命中 → None 且不调组装；未发布别名
  错误上抛不吞；参数透传；构造校验。
- 真实 MySQL 集成（复用既有证据组装全栈 scaffold 风格）：seed 一版本
  （source_ref/dataset_version 齐全 + 条文）→ fake dataset_search 返回该条文
  命中 → 编排返回含该 EvidenceItem 的 Bundle；空命中 → None。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
