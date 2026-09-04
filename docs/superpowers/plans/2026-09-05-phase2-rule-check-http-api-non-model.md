# 阶段 2 增补：Rule Check 租户 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已交付的 Rule Check 应用服务（`RuleCheckService`/`RuleEngine`、RiskIssue 处置、`RiskReportService` DOCX 导出）接成 **受控租户 HTTP API**（`/api/v1/tenants/{tenant_id}/...`），使律师可在真实 FastAPI 端点：对某 Document 提交条文文本运行确定性检查、读取/处置 RiskIssue、下载 DOCX 工作产物报告。本切片只做接线与授权收口，**无 Schema 变更**、不引入模型/RAG/SSE/MinIO。

**Scope / 边界（明确延后）：**
- 不调用生成模型/Embedding；不做条款 AI、RAG、OpenSearch、SSE、PDF。
- 不新增 RBAC permission code/feature rollout：Rule Check 授权与既有 Matter/Document 切片一致——「已认证的活跃租户成员 + 显式 tenant 上下文 + 资源 tenant 归属 + 跨租户反向测试」；把 Rule Check 端点并入 ai_job 式 permission code 体系（如 `rule_check.run/read/dispose/report.export`）与审计事件写入列为后续权限计划。
- Matter/Document 上传/登记端点（原 9.4 预签名直传链）不在本计划；测试通过既有仓储/服务直接建立 document 上下文。
- `provisions` 输入由调用方提供（受控条文文本，白名单规则只做确定性匹配；不可信文本不当指令），本切片不把上传原件当输入源。

**Architecture:** 保持三层：API（Pydantic 严格模型 + Problem Details）→ 应用服务（`RuleCheckService`、`RiskReportService`，均已 tenant-scoped）→ 仓储（`SqlAlchemyRulePackRepository`/`SqlAlchemyDocumentRepository`）。FastAPI 依赖 `TenantActorDependency` 提供已校验 `TenantContext`（成员/租户状态/版本），端点强制 path `tenant_id == actor.context.tenant_id`（`require_path_tenant` 复用）；不引入裸 `get_by_id`。组合根在 `ApplicationServices` 增加惰性构建的 phase2 服务入口（session_factory → 单请求 session → 仓储 → 服务），并在 `application_services` 生命周期内暴露。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新第三方依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景；本切片不产生对外法律结论。
- 每个请求显式租户上下文；`tenant_id` 路径必须与 actor 匹配；任何跨租户读取在 API 与仓储两层都不可达（反向测试）。
- 免责/人工核验语义保持：open 状态输出「待人工核验」；处置必须有理由；报告固定 `rule_based` 措辞。
- Idempotency：运行检查对相同 `(document, rule, provision_no, matched_text)` 幂等（`RuleCheckService.run_checks` 已保证）；处置以 `status=open` 条件更新防并发双处。
- 错误统一 Problem Details：稳定 `code`、`trace_id`，不泄露 SQL/堆栈/密钥。
- 使用 TDD；无新 Migration；不改已发布 Migration；不提交 Secret/.env/原件。

## File Map

```text
backend/
  src/lawyer_agent/
    api/v1/rule_checks.py            # router：POST risk-checks / GET risk-issues / POST dispose / GET report
    api/v1/__init__.py / router.py   # include 新 router（仅按需小改）
    api/dependencies.py              # ApplicationServices 增加惰性 phase2 服务组合入口（少量扩展）
    application/rule_check_api.py    # 薄编排：把 actor.context + commands 转成既有服务调用（如需）
  tests/
    unit/test_rule_check_api_contract.py     # Pydantic body/响应模型、错误码映射（无 DB）
    integration/mysql/test_rule_check_http_api.py  # 真实 MySQL+Redis 全栈 HTTP：登录→上传上下文→run→dispose→report+跨租户反向
```

## 里程碑 A：API 组合根与路由骨架

### Task A1: 组合根暴露 phase2 服务
- `ApplicationServices` 增加惰性 phase2 session 入口（不影响既有字段）；构造规则：每个请求级操作打开单一 `AsyncSession`，用完后关闭。
- 单测：组合根在无 DB 依赖下构造不破坏既有 services（保持 `ai_jobs` 等字段语义）；错误路径给 503。
- Commit: `feat: expose phase2 rule check services to http composition`

### Task A2: 路由与严格模型
- `api/v1/rule_checks.py`：`POST /tenants/{tenant_id}/documents/{document_id}/risk-checks`、`GET /tenants/{tenant_id}/documents/{document_id}/risk-issues`、`POST /tenants/{tenant_id}/risk-issues/{issue_id}/disposition`、`GET /tenants/{tenant_id}/documents/{document_id}/report.docx`。
- 严格 Pydantic（`extra="forbid"`）；path tenant 校验复用；错误码稳定（404 资源/403 权限语义按现有 ApiProblem）。
- 单元契约测试：body 拒绝额外字段、非法 `status` 值、空 `reason`；路径 tenant 不匹配返回 404。
- Commit: `feat: add rule check tenant http endpoints`

## 里程碑 B：行为接线与集成

### Task B1: run/dispose 端到端接线
- `risk-checks`：接受 `provisions: [{provision_no, text}]`，经 `RuleCheckService.run_checks(context, document_id, provisions)` 落库并返回新 open issues 摘要；无激活 pack / 无启用的规则 → 明确 409/422 风格 Problem 而非伪装成功。
- `disposition`：`status ∈ {accepted, rejected, modified}` + 非空 `reason` → `dispose`（领域层校验仅 open→目标态）；非 open/已处置返回冲突语义（409）；理由空白 422。
- 单测：fake store 覆盖 run 幂等/空 provisions 422/处置字段校验。
- Commit: `feat: wire rule check run and disposition endpoints`

### Task B2: 列表与 DOCX 报告端点
- `risk-issues`：返回文档全部 RiskIssue（tenant 过滤）+ 稳定排序（provision_no/rule），不返回跨租户数据。
- `report.docx`：调用 `RiskReportService.export` 返回 `application/vnd.openxmlformats-officedocument.wordprocessingml.document` 字节流；Content-Disposition 安全文件名。
- 单测：fake store 断言列表排序/字段、报告字节可由 `ZipDocxLoader` 读回。
- Commit: `feat: serve risk issue list and docx report`

## 里程碑 C：真实 MySQL 全栈与门禁

### Task C1: HTTP 全栈集成（真实 MySQL+Redis）
- `test_rule_check_http_api.py`：沿用 `test_http_api.py` 的数据库/seed/TestClient 基建；真实注册账号→建租户→拿 tenant token；用既有仓储/上传服务建立 matter/document + 激活 pack/规则；POST run → GET issues → POST dispose → GET report.docx（读回断言免责/命中/处置）；再用第二个租户 token 请求 A 租户 document → 404，确认跨租户反向。
- Commit: `feat: verify rule check http flow over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest（真实 MySQL）、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete rule check http api slice`

## Plan Self-Review
- 规格 9.1 REST/Problem Details/Idempotency 收口；8.3 合同审查流程的 HTTP 后端先行部分；MinIO/上传/SSE/PDF/权限码体系明确延后。
- Security：path tenant 强校验、每请求显式 context、无裸 get_by_id、跨租户反向、处置条件更新、免责与人工核验措辞。
- 无 Schema 变更：不加 Migration；全栈测试复用现有上传/仓储建立上下文。
- Placeholder：不假装「上传→解析→检查」闭环（上传链为 MinIO 延后项）；本切片暴露「对已登记 Document 提交受控条文运行检查」的真实端点。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL+Redis 全栈 HTTP、跨租户反向、run/dispose/report 三端点行为断言、严格模型契约测试、ruff/mypy 零错误、无 Secret、工作树干净。条款 AI/RAG/上传预签名/PDF/正式结论为明确未验证项。
