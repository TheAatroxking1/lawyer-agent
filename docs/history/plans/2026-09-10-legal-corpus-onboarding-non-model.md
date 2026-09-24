# 2026-09-10 法规导入编排：导入即自动分块落库（非模型）

## 目标

受控导入服务只写 instrument/version/provisions；chunk 原料行在生产代码里
从未由导入自动产生（既有测试全是手工 seed→`derive_chunks`→
`replace_chunks_for_version`），向量索引编排消费的 chunk 层在真实导入后并不
存在。本切片提供 **导入编排服务**：一次调用完成「受控导入 → 按权威条文派生
PROVISION chunk → 幂等落库」，为后续 OpenSearch 向量索引提供现成原料。

## 范围（只做这些）

- `application/legal_corpus_onboarding.py`：
  - `LegalCorpusOnboardingService(import_version, provisions, chunks)`：
    `onboard_version(command) -> OnboardingResult(instrument_id, version_id,
    chunk_count)`：
    1. 复用既有 `LegalCorpusImportService.import_version`（命令校验/身份归属/
       幂等 replay/冲突语义原样保留）；
    2. `provisions_for_version(version_id)` 读回权威条文；
    3. `derive_chunks(version_id, provisions, parser_version=command.parser_version)`
       派生 PROVISION chunk（一版多条文 → 多条）；
    4. `replace_chunks_for_version(version_id, chunks)` 原子删旧插新、幂等
       （重复导入同版本不会积累重复 chunk 行）；
    5. 返回 chunk 数。replay 时同样重建（旧 chunk 先删后插 → 结果稳定）。
  - 输入输出强类型；错误（`LegalCorpusImportError/Conflict`）原样上抛；
    不改变既有导入/chunk 契约。
- 不动 Schema、不加依赖、不接 HTTP/OS 索引；不动 `F:\ai律师数据库`。

## 明确不做

- 不做 OS 索引/别名联动（向量索引消费本切片产物是后续切片）；
  不做 DOCX 自动导入编排；不做导入 HTTP 端点；不做租户知识检索。

## 验证

- 聚焦单测（纯离线 fake）：导入两条文版本 → chunk_count=2 且 replace 收到
  两 chunk、字段正确；重复 onboard 同命令 → chunk 仍 2（无重复行）；
  版本级冲突/未知版本错误上抛；provisions 读空 → chunk_count=0 不写。
- 真实 MySQL 集成：同一 session 组合真实 import+corpus+chunk 仓储：
  导入版本 A（2 条文）→ chunks 2 行内容/哈希/parser_version 正确 →
  重复 onboard 同命令仍 2 行（幂等）→ 版本 B（1 条文）独立 1 行无串 →
  清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
