# 2026-09-10 法规清单/搜索只读 HTTP API（非模型）

## 目标

语料只读端点已覆盖：`instruments/{id}`（身份）、`instruments/{id}/version?as_of=`
（时点）、`instruments/{id}/versions`（历史）、`version-diff`、`versions/{id}`、
`versions/{id}/provisions`。但**没有任何方式列出/搜索法规身份**：盘点管理、
法规目录、后续搜索增强都必须先按 id 或逐个猜测才能浏览语料。本切片新增
`GET /api/v1/legal/instruments`（可选 title/issuing_authority 子串过滤、
jurisdiction/region_code 精确过滤、keyset 分页），响应与单身份端点同构的
`LegalInstrumentSummary` 列表 + `next_before_id`；公共语料、登录账号可读。

## 范围（只做这些）

- 仓储 `SqlAlchemyLegalCorpusRepository.list_instruments`（只读公共方法）：
  在租户无关的公共语料表上按 title 子串/issuing_authority 子串/
  jurisdiction/region_code 过滤后按 `(created_at, id)` 倒序 keyset 分页
  （limit 1–100；before_id 锚点取该行 created_at，缺失抛稳定
  `InstrumentListCursorInvalid`；MySQL 可移植写法与 audit/matter 列表一致）。
- `application/legal_corpus_read.py`：
  - 新错误 `LegalCorpusInvalidRequest`（422 `legal_corpus_invalid_request`，
    空白/超长/类型非法参数）、`LegalCorpusInstrumentCursorInvalid`（404
    `legal_corpus_instrument_cursor_invalid`，游标引用不存在的 instrument）。
  - `LegalCorpusQueryService.instruments(limit, before_id, title,
    issuing_authority, jurisdiction, region_code)`：白名单校验 → 委托
    `LegalCorpusQueryPort.list_instruments`（协议扩展）→ 仓储异常映射为稳定
    错误族。
- `api/v1/legal_corpus.py`：`GET /api/v1/legal/instruments` → 响应
  `{items: [LegalInstrumentSummary], next_before_id}`（limit 默认 20、
  参数长度约束由 Query 注解保证非法值 422）；复用 `_require_service`/
  `_map_error`；登录账号可读、公共语料非租户私有。
- 纯只读：不写审计、不改 Schema、不加依赖、不接检索/模型。

## 明确不做

- 不做版本/条文内联（继续用既有端点）；不做全文搜索/Embedding/排序相关性；
  不改单身份端点语义；不做管理写面（盘点写、导入端点仍为后续）。

## 验证

- 契约单测（扩展既有 `test_legal_corpus_http_contract.py`）：新错误码
  status/code、`LegalInstrumentPage` 往返与 `extra="forbid"`、服务装配冒烟。
- 真实 MySQL+Redis 全栈（扩展既有语料全栈或新流程）：seed 三法规
  （异 title/authority/jurisdiction）→ 全量列表按 `(created_at,id)` 倒序 →
  limit=1 两页翻页不重不漏（next_before_id 接力）→ title/authority 子串过滤
  → jurisdiction/region_code 精确过滤 → 游标不存在 404
  `legal_corpus_instrument_cursor_invalid` → 空白 title 过滤 422 → 未认证 401。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
