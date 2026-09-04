# 2026-09-10 法规版本 diff 只读 HTTP API（非模型）

## 目标

条文级版本 diff 已有应用层服务（`LegalVersionDiffService` / `version_diff`，
上一轮），但没有公开端点——调用方（半自动更新人工复核、前端对比、QA 候选
版本挑选）无法通过 API 比较同一法规的两个版本。本切片把 diff 暴露为公共
只读 HTTP 端点，错误语义稳定化，供后续 dataset 候选生成复用同一服务。

## 范围（只做这些）

- `application/legal_corpus_diff.py`：
  - 错误族加稳定码：`LegalVersionDiffError`（基类带 status/code/title）、
    `LegalVersionDiffVersionNotFound`（404 `legal_corpus_version_not_found`，
    复用语料既有 code）、`LegalVersionDiffCrossInstrument`
    （409 `legal_version_diff_cross_instrument`）；`LegalVersionDiffService`
    改抛子类错误（原 message 语义不变，既有单测回归保持）；
  - 新增 `LegalVersionDiffReadService`（UoW 工厂编排：一个 diff 全程同一个
    只读 `SqlAlchemyLegalCorpusReadUnitOfWork` 会话）。
- `api/v1/legal_corpus.py`：
  - `GET /api/v1/legal/version-diff?from_version_id=&to_version_id=`
    → `LegalVersionDiffSummary`（instrument_id + 两版本 id + added/removed/
    modified/unchanged 白名单投影：provision_no/level/structure_path/title/
    full_text，modified 携带 previous+current 全文）；`extra="forbid"`；
  - 公共语料、登录账号可读（非租户私有）；错误映射：
    404 `legal_corpus_version_not_found`、409 `legal_version_diff_cross_instrument`、
    503 服务缺失；沿用 Problem Details。
- `api/dependencies.py`：`ApplicationServices` 增 `legal_version_diff_http`
  惰性装配（复用 `SqlAlchemyLegalCorpusReadUnitOfWork`）。

## 明确不做

- 不做文件级 diff / candidate LegalInstrument 关系 / dataset_v2 生成端点；
  不做写入与审计；不改既有语料只读端点语义；不加依赖、不改 Schema。

## 验证

- 契约单测：diff 错误码族（404/409）稳定、`LegalVersionDiffReadService`
  服务装配 present。
- 真实 MySQL+Redis 全栈：seed 同法规两版本（新增第四条/删除第二条/同号异文
  第三条/未变第一条）+ 异法规一版 → diff 四组断言与 previous/current 全文 →
  跨法规 409 `legal_version_diff_cross_instrument` → 未知版本 404 →
  未认证 401。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
