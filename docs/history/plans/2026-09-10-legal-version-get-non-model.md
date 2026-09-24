# 2026-09-10 法规版本按 id 只读 GET（非模型）

## 目标

语料只读 API 已覆盖：`version?as_of=`（时点单点）、`versions/{id}/provisions`、
`instruments/{id}/versions`（版本历史）、`version-diff`、`instruments/{id}`
（法规身份）。但**拿到一个 version_id 后没有直接取该版本元数据的端点**：
diff 响应、历史列表、检索命中与证据组装场景都会持有 version_id，想读
label/状态/日期/文号/来源只能靠列表或 as_of 间接定位。本切片新增
`GET /api/v1/legal/versions/{version_id}` 返回单版本 `LegalVersionSummary`
（与 as_of/历史响应同构，白名单字段，`extra="forbid"`；登录账号可读）。

## 范围（只做这些）

- `SqlAlchemyLegalCorpusRepository`：复用既有 `version_with_instrument` 即可
  （无新仓储方法）。
- `application/legal_corpus_read.py`：
  - `LegalCorpusQueryService.version(version_id)`：`version_with_instrument`
    判存在，缺失 → 复用 `LegalCorpusVersionNotFound`（404
    `legal_corpus_version_not_found`）；返回 `LegalVersion`。
- `api/v1/legal_corpus.py`：
  - `GET /api/v1/legal/versions/{version_id}` → `LegalVersionSummary`
    （复用 `_version_summary`）；登录账号可读（公共语料）；错误映射沿用
    `_map_error`。
- 纯只读：不写审计、不改 Schema、不加依赖、不接检索/模型。

## 明确不做

- 不做条文/版本内容内联（调用方继续用 provisions 端点）；不做跨版本聚合；
  不改既有端点语义。

## 验证

- 契约单测：既有 `LegalCorpusVersionNotFound` 404/code 覆盖；响应复用
  `LegalVersionSummary` 往返（既有契约已含）。
- 真实 MySQL+Redis 全栈（扩展既有语料 HTTP 流程）：seed 两版本 →
  `GET versions/{id}` 读回该版本元数据（label/status/dataset_version）→
  未知 version 404 `legal_corpus_version_not_found` → 未认证 401。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
