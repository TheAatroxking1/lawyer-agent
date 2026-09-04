# 2026-09-10 法规身份只读 HTTP API（按 id 取 instrument summary，非模型）

## 目标

只读语料 API 已覆盖：`version?as_of=`、`versions/{id}/provisions`、
`instruments/{id}/versions`（版本历史）、`version-diff`。但这些端点只回
`instrument_id`，**没有任何端点返回法规身份本身**（title/issuing_authority/
jurisdiction/region_code）——客户端无法从 version/instrument id 展示「法规
名称 + 制定机关」。本切片新增 `GET /api/v1/legal/instruments/{instrument_id}`
返回公共法规身份（非租户私有，登录账号可读），并补底层读取。

## 范围（只做这些）

- `SqlAlchemyLegalCorpusRepository`（公共只读）增方法
  `instrument_by_id(instrument_id) -> LegalInstrument | None`（按 id 精确取
  一行 instrument 身份；语料为公共事实数据，非租户私有资源，按 id 显式读取
  不属于被禁的私有资源任意 get；复用 `_to_instrument` 映射）。
- `application/legal_corpus_read.py`：
  - `LegalCorpusQueryPort` 协议加 `instrument_by_id`；
  - `LegalCorpusQueryService.instrument(instrument_id)`：不存在 →
    复用既有 `LegalCorpusInstrumentNotFound`（404 `legal_corpus_instrument_not_found`）；
  - HTTP 映射沿用 `_map_error`。
- `api/v1/legal_corpus.py`：
  - `GET /api/v1/legal/instruments/{instrument_id}` → `LegalInstrumentSummary`
    （id/title/issuing_authority/jurisdiction/region_code；`extra="forbid"`）；
  - 认证 = 登录账号（公共语料）。
- 纯只读：不写审计、不改 Schema、不加依赖；不暴露版本/条文内联。

## 明确不做

- 不做 instrument 搜索/清单（按标题/机关过滤）——那是「盘点/管理 API」延后项；
  不在本端点内联版本列表（调用方继续用 `.../versions`）；不做写入/导入 API。

## 验证

- 契约单测：`LegalInstrumentSummary` 往返、404 `legal_corpus_instrument_not_found`
  稳定、服务缺失 503 语义、present 返回。
- 真实 MySQL+Redis 全栈：seed 两法规 → 按 id 读回身份字段（title/机关/法域/
  region）→ 未知 instrument 404 `legal_corpus_instrument_not_found` →
  未认证 401；与既有语料全栈共用 seed。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
