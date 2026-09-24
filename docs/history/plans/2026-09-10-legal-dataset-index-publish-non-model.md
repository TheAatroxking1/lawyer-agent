# 2026-09-10 法规数据集索引发布编排（建索引 → 原子切别名，非模型）

## 目标

切片已具备：导入编排（import→chunk 落库）、`LegalVectorIndexingService`
（chunk→OS k-NN 索引）、`LegalDatasetAliasService`（数据集别名原子切换）。
缺一个**发布编排**把这些串成 spec 6.6 的完整动作：对某版本构建/更新一个
物理索引 → 原子地把「数据集别名」切到该索引（返回旧目标，历史索引仍保留
可回滚）。本切片提供 `LegalDatasetIndexPublishService`，供后续 dataset_v1/v2
发布流程调用；只做「索引+别名指向」，不搬数据、不动 MySQL。

## 范围（只做这些）

- `application/legal_index_publish.py`：
  - `LegalDatasetIndexPublishService(indexer, alias)`：
    `publish_version(*, version_id, index_name, alias, model_ref, dimension,
    batch_size=64) -> DatasetPublishResult(index_name, previous_target,
    indexed_documents)`：
    1. 校验 index_name/alias 名（复用 `LegalDatasetAliasService` 同款白名单
       校验，稳定错误）；
    2. `LegalVectorIndexingService.index_version` 批量 embed 并重建索引文档
       （幂等）；
    3. `indexed_documents == 0` → 抛 `LegalDatasetPublishError`（不发布空
       数据集到别名，宁缺毋滥）；
    4. `LegalDatasetAliasService.publish_dataset` 原子指向新索引，返回切换
       前目标；
    5. 返回 `indexed_documents` + `previous_target`。
  - 幂等：同 version/同索引重复发布 → index 文档数不变、别名同目标 no-op。
- 真实链路验证（MySQL+真实 OS 容器，OS 不可达按可用性 skip）：
  seed 两版本条文 → onboarding 生成 chunk → 用注入 fake embedding provider
  的 gateway 建索引 → 发布别名 dataset_v1 → resolve 指向新索引 → 旧索引仍
  在且可删 → 二次发布到同一索引幂等 → 清理索引与别名。

## 明确不做

- 不做 dataset snapshot 落库/质量门禁联动（后续切片）；不做 HTTP/审计；
  不改 vector-indexing/alias 契约；不加依赖；不改 Schema。
- 真实 embedding 推理（CPU 本地模型）由既有真实模型 e2e 覆盖，本切片发布
  编排用确定性 fake provider 验证编排语义。

## 验证

- 聚焦单测（纯离线 fake indexer/alias）：编排顺序（index 成功→切别名）、
  返回旧目标与文档数、0 文档拒绝发布（不切别名）、名字非法拒绝、
  indexer/alias 错误上抛、幂等重发。
- 真实 MySQL+OS e2e（可用性 skip）：onboarding→index→alias 原子切换→
  旧索引保留→重复发布幂等→清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
