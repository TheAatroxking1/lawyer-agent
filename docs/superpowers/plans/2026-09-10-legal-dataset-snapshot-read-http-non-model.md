# 2026-09-10 数据集快照只读 HTTP API（盘点管理，非模型）

## 目标

语料只读 HTTP 已覆盖 instrument/version/provision（盘点管理读面基本齐），
但 **dataset snapshot（dataset_v1/v2 发布记录、质量指标、发布时间）没有任何
公开读端点**：盘点/运维只能看 DB 或别名层，无法经 API 回答「现在发布了哪些
数据集、各是什么状态/质量/何时发布」。本切片新增只读快照浏览：
`GET /api/v1/legal/datasets`（全量，按 released_at 有值在前→倒序、id 决胜）
与 `GET /api/v1/legal/datasets/{dataset_name}`（按名读单条，缺失 404），
响应为白名单 `DatasetSnapshotSummary`（dataset_name/state/parser_version/
released_at + 质量指标白名单标量；**不暴露 manifest 全文**——内含来源
清单路径，非读面所需）；公共语料、登录账号可读。

## 范围（只做这些）

- `SqlAlchemyLegalCorpusRepository` 只读方法：
  - `dataset_snapshots()`（select snapshot，order released_at 非空在前 →
    released_at desc → id desc，映射稳定值对象）；
  - `dataset_snapshot_by_name(dataset_name)`（复用 find_dataset 语义的只读
    形态；公共事实数据按名显式读取）。
- `application/legal_corpus_read.py`：
  - 错误 `LegalCorpusDatasetSnapshotNotFound`（404
    `legal_dataset_snapshot_not_found`，继承 LegalCorpusQueryError 错误族）；
  - `LegalCorpusQueryPort` 补两条方法 + `LegalCorpusQueryService.datasets() /
    dataset(name)`（name 空白/超长 → 422 `legal_corpus_invalid_request`；
    不存在 → 404）。
- `api/v1/legal_corpus.py`：`DatasetSnapshotSummary`（白名单字段 + 质量指标
  白名单子集，`extra="forbid"`）+ 两个 GET 端点；复用 `_require_service`/
  `_map_error`；登录账号可读、公共语料非租户私有。
- 纯只读：不写审计、不改 Schema、不加依赖、不接检索/模型。

## 明确不做

- 不做 dataset 发布/重发写面（发布编排仍是后续）；不暴露 manifest 原始内容；
  不做分页（快照行数量级小，与 versions_for_instrument 同无分页约定）。

## 验证

- 契约单测（扩展 `test_legal_corpus_http_contract.py`）：summary 往返与
  `extra="forbid"`、新 404 错误码、服务装配沿用。
- 服务单测（扩展 `test_legal_corpus_read_service.py`）：datasets 透传、
  dataset(name) 透传、空白/超长 name 422、缺失映射 404。
- 真实 MySQL+Redis 全栈（扩展既有语料 HTTP 全栈文件）：seed 两条快照
  （published 有 released_at + 一条 pending 无）→ 列表 published 在前 →
  按名读回字段（质量指标白名单）→ 未知名 404
  `legal_dataset_snapshot_not_found` → 未认证 401 → 清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
