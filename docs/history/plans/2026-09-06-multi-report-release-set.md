# 多报告发布集合实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. 用户要求自主推进；不提交、不清理共享工作树。

**Goal:** 让真实全集的多个完整导入报告共同接受一次数据库事实检查及有界发布，保留各报告来源证据。

**Architecture:** 保留单报告入口，新增严格有界的 `legal-corpus-release-set-v1` JSON 文件与 `--release-set` 互斥入口。集合仅引用原报告路径、完整字节 SHA-256 和选择数量；逐份复用现有 reader，不拼接事件或接受部分成功报告。全部选择进入现有同一 REPEATABLE READ 检查/构建路径。

**Tech Stack:** Python 3.12、Pydantic v2、现有领域 dataclass、pytest。

## 全局约束

- 承接已批准全集发布边界及 `.superpowers/sdd/multi-report-release-design-review.md`。保留原件、旧报告、DB 事实和用户脏工作树。
- 不读取外部法律原件、不加载模型、不触 GPU、不写业务库、不切正式别名。实现验证使用合成报告及端口替身；root 之后做真实只读检查。
- 集合 JSON 严格 UTF-8，拒绝重复 JSON 键、额外字段、bool 冒充 int、无效哈希、空集合及非绝对/重定向/Windows 特殊路径。集合文件最多 1 MiB、成员最多 256、累计选择最多 20,000，不能无界累计报告原字节。
- schema 固定 `legal-corpus-release-set-v1`，必填 `total` 正整数、`reports` 固定有序成员列表；成员必填 `path`、`sha256`、`selection_count`。无需附加未消费的批次标签或创建时间。
- 每个成员只能是现有 reader 接受的完整成功 import report，匹配声明摘要和数量。成员报告路径、来源文件 Windows 规范化路径、version UUID 全局去重；同源 imported/replayed 也拒绝。不能接受本轮失败恢复报告或原中断报告。
- dataset/parser/chunk parser 身份完全一致；不为混合版本归一。全集行数必须等于总数与成员计数之和。
- 集合 `selection_sha256` 使用集合原始字节 SHA-256：其字节已绑定所有成员的原报告 SHA 和数量，再逐份核验。无需额外不透明 Merkle 算法。每份成员的顺序/摘要/数量及对应选择范围保留为有类型的发布来源证据，进入质量摘要与候选快照。单报告调用保持兼容，不强迫旧调用方提供集合字段。
- 配置无效必须在构造 Settings/DB 前失败；`--check` 不初始化模型或写索引。既有证据/日期/质量门禁不弱化。

## Task 1：集合 reader 与 CLI/证据接线

Files:
- Create `backend/src/lawyer_agent/infrastructure/documents/release_set_manifest.py`。
- Modify `backend/src/lawyer_agent/cli/corpus_publish_set.py`、必要的有类型质量配置/序列化与发布 manifest 装配。
- Test `backend/tests/unit/test_release_set_manifest.py`、`test_corpus_publish_set_cli.py`、相关质量摘要/发布候选测试。

- [x] 先写行为 RED：两个不同完整报告合成选择；CLI 接受 `--release-set` 并将全部选择交给同一 check/build 调用；改变成员字节拒绝。旧 CLI 隔离快照拒绝新选项、当前入口通过，记录在实施报告；最初缺模块收集失败不作为有效行为 RED。
- [x] 实现严格 schema、安全有界读取、复用单报告 reader；缺 summary/有失败/预检拒绝；成员与全局重复/版本差异/声明计数/大小上限测试。
- [x] 接线互斥 `--import-report`/`--release-set`，保留单报告用法；报告成员证据进入质量和候选快照，成员变化使审核摘要失效；测试 `--check` 无模型写入路径。
- [x] 聚焦 pytest、Ruff、mypy；仅任务 diff 与实施记录，禁止提交。

## Task 2：独立复审与真实只读检查

- [x] 新独立 reviewer 核对规格/质量，修复阻断项；root 执行最终后端回归。首次发现候选成员范围缺校验，已用协同篡改及 falsy 输入 RED 修复；最终两项 APPROVED。2026-09-07 回归 2,597 passed / 1 skipped / 1 warning，Ruff 通过、mypy 215 个源文件通过。
- [x] root 从八份完整非重叠报告生成新集合：旧五份 11,019 + 接续 3,043 + 机关补充 215 + 结构恢复 3 = 14,280。保留报告原字节。此集合明确是已具备完整导入报告的范围，不能声称全 16,006 验收。
- [x] 真实 `--release-set --check` 在同一事实视图检查 14,280 项，保存脱敏摘要；不调用 Embedding、不切别名，失败保持显式。结果仍为 107 个 `article_sequence_invalid`、CLI 退出 1；真实核验已完成，不代表质量通过。8 份报告摘要、选择范围、阻断版本集合及任务代码摘要均复核一致。
- [x] 更新现状与交接；原中断 1,702 项及 24 未入库来源、解析纠错新版本仍须后续完成。

最终组合 diff：`.superpowers/sdd/release-set-final.diff`，SHA-256 `b19259c7b23fe2e350749e452211313092691f5fa03d210ecbdf1afd655bf4c5`；复审记录 `.superpowers/sdd/release-set-task-1-final-review.md`。真实集合 `artifacts/legal-corpus/full-release/release-set-complete-reports-14280-20260906.json`，SHA-256 `3f31caba9810f0f4e78e31c05570d075e18f6088efa1e6c38f7d7f019e88eba7`。

联合检查证据：`artifacts/legal-corpus/full-release/release-set-quality-20260906T160833Z/` 内的 `quality.jsonl`、`summary.json`、`verification.json`。质量摘要 `2c74943990ffee66ac780b80462ccdd96aecf13c539c2e7e23374aa40f8f8189`，未执行真实模型构建或索引发布。本计划的软件增量与真实只读验证已完成，全集/生产验收仍未完成。
