# 阶段 2 增补：Rule Pack 与确定性合同风险规则 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在既有 Matter/Document 基础上交付**确定性（非模型）**的规则包与合同风险检查先行切片：版本化 Rule Pack、条款/风险规则定义、对已解析 Document Version 条文运行引擎生成 `RiskIssue`、人工处置状态机（接受/驳回/修改）。条款识别 AI、法规 RAG、起草/导出、生成模型一律不在本计划。

**Scope / 边界（明确延后）：**
- 不调用生成模型/Embedding；不做 LLM 条款识别；不做 RAG/OpenSearch。
- 规则只匹配已入库的结构化条文文本（来自阶段 1/2 解析结果或人工录入），不做全文语义推断。
- 规则产生“候选风险提示”而非法律结论：任何高风险建议必须人工确认；引擎输出固定 `evidence_level=rule_based`，绝不自称官方/法规结论。
- 运行时序结论（诉讼时效等）一律禁止生成，仅允许提示“需人工核实时限”。

**Architecture:** MySQL 是 Rule Pack/规则/RiskIssue 处置的事实源。`RulePack` 租户可版本化发布；规则包括：id/kind(条款类型|风险规则)/触发模式（正则，白名单）/风险级别（低中高）/建议文本占位/evidence 说明/启停状态。引擎对某版本文档的全部 Provision 文本确定性执行规则，命中生成 `RiskIssue`（含位置、规则 id、pack 版本、级别、evidence_level）；律师处置：`OPEN → ACCEPTED / REJECTED / MODIFIED`，处置仅允许在 Pack 为当前激活版本时创建新 Issue，Pack 停用后只读。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.0、Alembic、asyncmy、MySQL 8.4、pytest、Ruff、mypy、Docker Compose。

## Global Constraints

- 全部代码/迁移/测试/配置/文档 UTF-8；仅中国大陆法律/法律服务场景；本切片不产生对外法律结论。
- MySQL 8.4/InnoDB/`utf8mb4`/UTC `DATETIME(6)`；UUIDv7 `BINARY(16)`；Tenant 强制 `tenant_id` 复合键/FK。
- 每个租户 Rule Pack 与文档检查绑定租户；仓库显式 TenantContext；必须有跨租户反向测试。
- 规则触发文本来自受管 Document Version/Provision（经只读 Loader/Parser 或人工录入）；禁止直接执行文档内容。
- `RiskIssue.evidence_level` 固定 `rule_based`；生成模型/法规引用缺口时不得声称已引用法规。
- 正则规则仅限明确的条款标记/常见风险短语白名单；规则文件为代码内数据或租户库行，禁止注入任意代码。
- 高风险建议的人工确认是必须步骤；未处置/被驳回的 Issue 不进入任何对外结论。
- 使用 TDD；不改已发布 Migration；新 Revision 从 `20260905_08` 向前。
- 不提交 Secret/.env/原件；日志脱敏。

## File Map

```text
backend/
  alembic/versions/20260905_09_phase2_rule_pack.py
  src/lawyer_agent/
    domain/rule_pack.py             # RulePack/Rule/RiskIssue/RuleTrigger/RiskLevel/处置状态
    application/rules.py            # RuleEnginePort/RuleEngine(确定性执行)/处置服务
    infrastructure/persistence/models/rule_pack.py
    infrastructure/persistence/repositories/rule_pack.py
  tests/
    unit/test_rule_engine.py
    integration/mysql/test_rule_pack_migration.py  test_rule_pack_repositories.py
    integration/mysql/test_rule_engine_flow.py
```

## 里程碑 A：Rule Pack 与 RiskIssue 模型/Migration/Repository

### Task A1: Rule Pack Schema
- domain + 3–4 张表（rule_packs/rule_pack_rules/contract_risk_issues + 处置状态），迁移往返 + `alembic check`
- Commit: `feat: model versioned rule packs and risk issues`

### Task A2: 跨租户反向 + 激活版本只读
- Repository：按 tenant 读取 pack/规则/issue；停用 pack 后禁止新建 issue
- 跨租户反向测试（A 无法读 B 的 issue）
- Commit: `feat: scope rule packs per tenant`

## 里程碑 B：确定性引擎与处置

### Task B1: RuleEngine（正则白名单）
- 输入 Provision 文本 + 激活 RulePack → 命中生成 RiskIssue（rule_based、位置、级别）
- 单元测试：条款类型/风险短语命中、多规则命中、无命中
- Commit: `feat: run deterministic rule pack checks`

### Task B2: 处置状态机与门禁
- `OPEN→ACCEPTED/REJECTED/MODIFIED`（绑定若匹配文本变化则失效 MODIFIED→需重跑）；高风险未确认不得进入结论
- MySQL 集成：docx 解析 → 引擎命中 → 律师处置 → 重跑
- Commit: `feat: track human disposition of rule based risks`

## 里程碑 C：门禁收尾
- 全量 pytest（真实 MySQL）、ruff、mypy、迁移往返、secret 扫描、AGENTS.md
- Commit: `docs: complete phase2 rule pack slice`

## Plan Self-Review
- Spec 8.1 Rule Pack / 8.2-2,5 / 8.3 Risk Issue 后端部分；条款 AI、RAG、Drafting/导出、正式时限结论全部延后标注。
- Security：Tenant 隔离、规则非代码注入、evidence_level=rule_based、人工确认门槛。
- Placeholder：正则引擎真实运行于已解析文本样本；无模型调用点。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL、迁移往返、跨租户反向、确定性引擎命中/处置测试、ruff/mypy 零错误、无 Secret、工作树干净。生成模型/法规 RAG/时限结论/DOCX 导出为明确未验证项。
