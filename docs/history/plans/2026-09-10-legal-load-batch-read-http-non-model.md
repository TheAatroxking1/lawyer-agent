# 2026-09-10 语料盘点批次只读 HTTP API（非模型）

## 目标

盘点管理读面已覆盖 instrument（身份/清单/搜索）与 dataset snapshot
（发布记录），但 **LoadBatch（盘点批次）没有任何公开读端点**：批次由
`LegalCorpusInventoryService`（inventoried）与 `LegalCorpusPublishService`
（completed）写入，目录/审计只能查 DB，无法经 API 回答「跑了哪些批次、
各是什么状态/来源/计数、何时完成」。本切片新增只读批次浏览：
`GET /api/v1/legal/load-batches`（keyset 分页 newest-first）与
`GET /api/v1/legal/load-batches/{batch_id}`（按 id 读单条，缺失 404），
响应为白名单 `LoadBatchSummary`（id/batch_no/source_ref/parser_version/
status/item_counts/started_at/completed_at/error_message；不含 manifest——
快照 manifest 属 dataset 面）；公共语料、登录账号可读。

## 范围（只做这些）

- `SqlAlchemyLegalCorpusRepository` 只读方法：
  - `load_batches(limit, before_id=None)`：按 `(created_at,id)` 倒序 keyset，
    before_id 锚点取 created_at（缺失抛仓储级稳定异常，参照
    `LegalCorpusInstrumentListCursorInvalid` 模式命名为
    `LegalCorpusLoadBatchCursorInvalid`）；
  - `load_batch_by_id(batch_id)`（公共事实数据按 id 显式读取）。
  - 复用 inventory 仓储同款 `_load_batch` 映射（迁移到本模块或直接映射；
    released/时间字段按 UTC）。
- `application/legal_corpus_read.py`：
  - 新错误 `LegalCorpusLoadBatchNotFound`（404 `legal_corpus_load_batch_not_found`）、
    `LegalCorpusLoadBatchCursorInvalid` 映射（404
    `legal_corpus_load_batch_cursor_invalid`）；
  - `LegalCorpusQueryPort` 补两条方法 + `LegalCorpusQueryService.load_batches()/
    load_batch(batch_id)`（limit 1–100、before_id uuid7、id uuid7 校验；
    非法 → 422 `legal_corpus_invalid_request`）。
- `api/v1/legal_corpus.py`：`LoadBatchSummary`（白名单字段 `extra="forbid"`）+ 两个
  GET 端点（列表响应 `{items, next_before_id}` 同 instrument list 约定）；
  复用 `_require_service`/`_map_error`。
- 纯只读：不写审计、不改 Schema、不加依赖。

## 明确不做

- 不做批次删除/重跑/质量明细展开（质量 issue 行读面仍为后续）；不做
  LoadBatch 与 instrument 聚合视图。

## 验证

- 契约单测（扩展 `test_legal_corpus_http_contract.py`）：summary 往返与
  `extra="forbid"`、新 404 错误码。
- 服务单测（扩展 `test_legal_corpus_read_service.py`）：load_batches 透传/
  before_id 透传、load_batch 透传与缺失映射、limit/游标/id 非法 422。
- 真实 MySQL+Redis 全栈（扩展既有语料 HTTP 全栈文件）：seed 三批（不同
  created_at、completed/inventoried 各状态）→ 列表 newest-first 分页
  （limit=2 两页不重不漏）→ 按 id 读回字段（含 item_counts）→ 未知名 404 →
  非法游标 404 → 未认证 401 → 清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
