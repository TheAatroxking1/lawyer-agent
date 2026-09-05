# 2026-09-10 索引发布编排联动 dataset snapshot 落库（非模型）

## 目标

`LegalDatasetIndexPublishService.publish_version` 已经能建 OS 新索引并原子切
dataset 别名，但发布动作**不留任何 MySQL 记录**：上一轮新增的
`GET /legal/datasets` / `GET /legal/datasets/{dataset_name}` 读面只能看到
批次发布（`LegalCorpusPublishService`，按 LoadBatch+质量门禁写 dataset_v1）
产生的快照行，OS 索引发布与快照目录是割裂的。spec 6.6 半自动更新要求
「人工发布后形成 dataset_v2 等新版本」——本切片让**索引发布编排在成功发布后
记录/更新一条 PUBLISHED dataset snapshot 行**（dataset_name=别名，manifest
记 index_name/alias/version_id/model_ref/dimension/indexed_documents，
released_at=当前 UTC，重复发布幂等 upsert 只留最新），使发布动作立刻可被
快照读面与盘点审计看见，形成「发布→登记→可读」闭环。

## 范围（只做这些）

- `application/legal_index_publish.py`：
  - 新 `DatasetSnapshotWritePort`（find_dataset/upsert_dataset 两方法；
    `SqlAlchemyLegalCorpusInventoryRepository` 结构上满足）；
  - `LegalDatasetIndexPublishService.__init__` 增加**可选**
    `snapshot: DatasetSnapshotWritePort | None = None`（旧调用完全不变）；
  - `publish_version` 在 `alias.publish_dataset` 成功后、仅当
    `indexed_documents > 0` 时登记快照：复用既有行 id（不存在则新 uuid7），
    dataset_name=alias、parser_version=model_ref 显式传入的
    `dataset_parser_version`（默认 "docx-zip-v1"，校验非空）、state=PUBLISHED、
    manifest 白名单字段、quality_metrics={indexed_documents, dimension}、
    released_at=now（可注入 clock 便于测试）；`indexed==0`/前置校验失败/
    别名未切换时**绝不写快照**。
- `api/v1/legal_corpus.py` 读面 `_METRIC_ALLOWLIST` 增补
  `indexed_documents`/`dimension`，使发布登记的质量指标可在
  `DatasetSnapshotSummary` 中读回。
- 不改 OS 客户端/别名/既有批次发布流程；不加新表/迁移。

## 明确不做

- 不做「质量门禁联动发布前 gate」写面（批次质量门禁已在
  `LegalCorpusPublishService`，OS 面发布即质量达标后的动作）；不把旧快照
  标 SUPERSEDED（历史别名/索引仍保留供历史问答与回滚，快照行记最新一次）；
  不做 candidate 关系/dataset_v2 自动生成。

## 验证

- 离线单测（扩展 `tests/unit/test_legal_index_publish.py`）：
  成功发布→快照记录（dataset_name=alias、state=PUBLISHED、manifest
  index_name/model_ref/dimension/indexed_documents、released_at=now）；
  无 recorder 注入时行为不变；`indexed==0` 不写、名称校验失败不写、
  别名失败上抛不写；重复发布复用同一 id（upsert）只留一行。
- 真实 MySQL 集成（新 `tests/integration/mysql/test_legal_index_publish_snapshot_repositories.py`）：
  真 `SqlAlchemyLegalCorpusInventoryRepository` + fake indexer/alias → 发布
  dataset_v2 → snapshot 行读回（state/released_at/manifest/metrics）→ 再发
  布 dataset_v3 同 dataset_v2? （改用 dataset_v2 二次发布新 index → 同
  dataset_name 行被更新且仍一行、released_at 前进）→ 清理；读方法与
  round-176 读面同 repo。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。

## 计划自检

- 覆盖 AGENTS index-publish 尾注「dataset snapshot 落库…仍为后续」；行为
  向后兼容；manifest 只写结构化标识符（无来源路径泄漏）；快照记录幂等。
