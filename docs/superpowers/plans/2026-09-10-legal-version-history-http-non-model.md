# 2026-09-10 法规版本历史只读 HTTP API（列表某 instrument 全部版本，非模型）

## 目标

语料只读 HTTP API 目前只有两个端点：`version?as_of=`（取某时点现行单版本）
与 `versions/{version_id}/provisions`（按版本取条文）。缺**版本清单**：调用方
（版本 diff、证据检索、前端浏览、半自动更新）无法获知某法规有哪些版本、各自
的公布/施行/废止状态与文号。本切片新增公共只读端点：按 `instrument_id` 列出
全部版本元数据（与 `version?as_of` 响应同构的 `LegalVersionSummary` 列表），
按版本公布/生效时间倒序（稳定排序），供上层挑选 diff 对或展示历史。

## 范围（只做这些）

- `SqlAlchemyLegalCorpusRepository`（公共只读，无 tenant）增两个只读方法：
  - `instrument_exists(instrument_id) -> bool`；
  - `versions_for_instrument(instrument_id) -> tuple[LegalVersion, ...]`
    （按 `published_on` 倒序、`effective_on` 倒序、`id` 决胜稳定排序；
    NULL 日期视为最旧；沿用版本唯一 (instrument_id, version_label) 无重复）。
- `application/legal_corpus_read.py`：
  - `LegalCorpusQueryPort` 协议与方法同步扩展；
  - `LegalCorpusQueryService.versions_for_instrument(instrument_id)`：
    instrument 不存在 → `LegalCorpusInstrumentNotFound`（404、
    `legal_corpus_instrument_not_found`）；存在但无版本 → 返回空元组；
- `api/v1/legal_corpus.py`：
  - `GET /api/v1/legal/instruments/{instrument_id}/versions` → `list[LegalVersionSummary]`；
    认证 = 登录账号（公共语料，非租户私有，同既有 read API）；
    错误映射沿用 Problem Details（404 instrument_not_found / 503 服务缺失）。
- 纯只读：不写审计、不改 Schema、不加依赖、不做分页（版本数受唯一约束有界；
  如需大批量由后续 keyset 切片补）。

## 明确不做

- 不做条文级/文件级 diff 端点（已有应用层 `LegalVersionDiffService`，其 HTTP
  暴露留后续）；不做全文/语义检索；不做 instrument 清单/搜索端点；不做写入/
  导入/盘点管理 API；不改既有 `version?as_of` 语义。

## 验证

- 契约单测（纯离线）：`LegalCorpusInstrumentNotFound` 稳定 404/code；
  响应模型复用 `LegalVersionSummary` 往返；服务缺失 503 语义；present 返回。
- 真实 MySQL 全栈：seed 一个 instrument + 三个版本（不同公布/生效日，含
  一 historical）→ 列出按日期倒序、字段完整（status/law_number/source_ref/
  dataset_version）→ 未知 instrument 404 `legal_corpus_instrument_not_found`
  → 存在但无版本的 instrument 返回 `[]` → 未认证 401；与 `version?as_of`
  返回同构。
- 全量 pytest（Redis 抖动按已知模式隔离重跑）；ruff/mypy 零错误；无 Secret；
  树干净。
