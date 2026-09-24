# 阶段 2 增补：人工写动作拒绝路径审计 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 审计切片只登记了 dispose/review/activate 的**成功**事实；被拒绝的处置尝试（非 open → 409）、不存在的目标（404）、非法请求（422）没有审计留痕，无法回答「谁试图改什么被拒」。本切片补上**拒绝/失败路径审计**：三个动作的 409/404 写 `denied` 事件、422 写 `failure` 事件（reason_code 用稳定错误码），完善「谁在何时对什么资源做什么、结果如何」的审计不变量。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只补 `dispose`/`review`/`activate` 三个已审计动作的**失败分支**；审计矩阵的 action 前缀已就绪。
- 事件仅记录：actor/tenant/membership、action、result=denied|failure、reason_code（稳定错误码：`rule_check_conflict`/`document_review_conflict`/`rule_pack_admin_not_found`/`..._invalid_request`）、target（若可确定）、trace_id。
- 404 因路径 tenant 不匹配（在端点层拦截、无资源上下文）**不写**审计（请求没进入租户资源，避免无效噪音）；只有服务层能确定目标租户与动作后才写。
- 延后：审计查询导出、IP/UA 展示、保留策略、跨租户管理。

**Architecture:** 三个编排服务（`rule_check_api`/`document_review_api`/`rule_pack_admin_api`）失败分支在抛错前追加 audit（result=denied 对 RuleCheckConflict/DocumentReviewConflict/RulePackAdminNotFound，failure 对 InvalidRequest）；404 目标不存在（`RuleCheckIssueNotFound`/`DocumentReviewNotFound`/`RulePackAdminNotFound`）统一 `denied`+`not_found`。复用 `_append_audit`/`new_tenant_user_audit_event`，仅新增 result/reason 参数。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 事件只写标识符与稳定 reason_code，不写正文/处置理由原文/pattern。
- 租户与 actor 由已校验 context 提供；跨租户请求（path 拦截层）不产生事件。
- 使用 TDD；无 Migration；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/rule_check_api.py       # dispose 失败分支写审计
    application/document_review_api.py  # apply_review 失败分支写审计
    application/rule_pack_admin_api.py  # activate_pack 失败分支写审计
  tests/
    integration/mysql/test_phase2_rejected_audit.py  # 真实 MySQL+Redis：三类拒绝均产生 denied/failure 行
```

## 里程碑 A：拒绝路径审计

### Task A1: dispose 失败分支
- dispose 的 `RuleCheckConflict`（非 open）→ denied `rule_check_conflict`；`RuleCheckInvalidRequest`（空理由等）→ failure `rule_check_invalid_request`。
- Commit: `feat: audit rejected dispositions`

### Task A2: review/activate 失败分支
- review 的 `DocumentReviewConflict` → denied `document_review_conflict`；InvalidRequest → failure；NotFound → denied `document_review_not_found`。
- activate 的 `RulePackAdminConflict` → denied `rule_pack_admin_conflict`；NotFound → denied `rule_pack_admin_not_found`。
- Commit: `feat: audit rejected reviews and activations`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 复用基建：对已处置 issue 再 dispose → 409 且审计行 denied `rule_check_conflict`；对不存在/越权 issue dispose → 404 且 denied `rule_check_issue_not_found`；review 空理由 request_changes → 422 且 failure 行 `document_review_invalid_request`（dispose 空理由在 body 层被拒，无法到达服务层，故 failure 行由 review 提供）；重复 approve → 409 且 denied `document_review_conflict`；对不存在 version review → 404 且 denied `document_review_not_found`；对不存在/越权 pack activate → 404 且 denied `rule_pack_admin_not_found`；越权跨租户资源层尝试在**尝试方**租户产生对应 denied 行，路径层 404 不产生行。
- Commit: `feat: verify rejected audit rows over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete rejected audit slice`

## Plan Self-Review
- 审计不变量完整化：success/denied/failure 三态都有留痕。
- 安全：不记录 path 拦截层噪音、只写稳定 reason_code、无正文。
- 无 Schema：复用 audit 表与矩阵（result 已允许 denied/failure）。
- 延后：导出/保留/查询增强/跨租户。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 拒绝审计行断言、矩阵不破坏、ruff/mypy 零错误、无 Secret、工作树干净。导出/保留/跨租户为明确延后项。
