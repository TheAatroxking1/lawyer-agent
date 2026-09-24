# 阶段 2 增补：租户人工写动作审计 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按审计不变量，对阶段 2 已交付 HTTP API 中**最高影响的人工写动作**登记结构化审计事件：RiskIssue 处置（accept/reject/modify）、Document Version 复核（submit/approve/reject/request_changes）、Rule Pack 激活。审计是「谁在何时对哪个租户资源做了什么」的不可变留痕，供租户审计与平台审计查询（查询 API 延后）。无 Schema 变更（审计表与结构化列已存在）。

**Scope / 边界（明确延后）：**
- 只覆盖**人工决策类写动作的成功事实**；409/404/422 拒绝路径的 denied/failure 审计、只读端点、登记类端点（上传、规则追加）与自动重跑暂不纳入本切片（可后续补）。
- 审计查询/导出 API、保留策略、跨租户审计管理延后。
- 不上新表；复用 `StructuredAuditEvent`/`AuditActorKind.TENANT_USER` 与既有结构化审计表。
- 事件只含动作事实与标识符（租户/用户/成员/target_type/target_id/action/result/reason_code/trace_id），**不含**条文正文、匹配文本、处置理由原文或规则 pattern。

## 目标动作与事件码
- `POST .../risk-issues/{id}/disposition` → action `risk_issue.dispose`，result success/failure，target_type `risk_issue`，target_id=issue_id。
- `POST .../documents/{d}/versions/{v}/review` → action `document.review`，target_type `document_version`，target_id=version id（review 对象带 version id 的事件 target 用该 id）。
- `POST .../rule-packs/{id}/activate` → action `rule_pack.activate`，target_type `rule_pack`，target_id=pack_id。

审计矩阵要求（TENANT_USER）：tenant_id + actor_user_id + actor_membership_id 非空；无 on_behalf/无 rollout；action 前缀须列入 `_TENANT_USER_PREFIXES`（`risk_issue.`、`document.`、`rule_pack.`）。

**Tech Stack:** Python 3.12、现有 audit 领域/仓储、pytest、Ruff、mypy。无新依赖、无 Migration、无新表。

## File Map

```text
backend/
  src/lawyer_agent/
    application/audit.py              # _TENANT_USER_PREFIXES 增加 risk_issue./document./rule_pack.
    application/rule_check_api.py     # dispose 成功后 append 审计
    application/document_review_api.py  # apply_review 成功后 append 审计
    application/rule_pack_admin_api.py # activate_pack 成功后 append 审计
    infrastructure/persistence/rule_check_uow.py         # 暴露 audit 仓储
    infrastructure/persistence/matter_document_uow.py    # 暴露 audit 仓储
    infrastructure/persistence/rule_pack_admin_uow.py    # 暴露 audit 仓储
  tests/
    unit/test_audit_actions.py              # 新 action 前缀矩阵通过（现有审计单测会覆盖）
    integration/mysql/test_phase2_http_audit.py  # 真实 MySQL+Redis：dispose/review/activate 后审计行存在且字段正确
```

## 里程碑 A：审计动作白名单

### Task A1: action 前缀扩展 + 事件构造 helper
- `_TENANT_USER_PREFIXES` 加入 `risk_issue.`、`document.`、`rule_pack.`。
- domain 层新增/复用小 helper 构造 TENANT_USER 审计事件（封装矩阵字段与 32 字节 hash 位置）；写 action/result/reason_code/trace_id/target 必填校验。
- 单测：三个新 action 前缀通过矩阵；缺 actor/tenant 抛错；跨前缀拒绝。
- Commit: `feat: allowlist tenant phase2 audit actions`

## 里程碑 B：UoW 暴露审计并写事件

### Task B1: 三个 UoW 注入 audit 仓储
- `rule_check_uow`/`matter_document_uow`/`rule_pack_admin_uow` 暴露 `audit`（`AuditRepository`），`__aenter__` 构造，事务结束随 session commit/rollback。
- 单测：UoW 暴露后构造不破坏既有服务。
- Commit: `feat: expose audit store in phase2 units of work`

### Task B2: 动作成功写审计
- 三个动作成功路径各自 append 一条 success 审计：dispose、review、activate；`trace_id` 由 API 层从 `request.state.trace_id` 传入编排层。
- 构造事件：`occurred_at=now`、reason_code 用稳定码（`disposed`/`reviewed`/`activated`）。
- Commit: `feat: audit tenant disposition review and pack activation`

## 里程碑 C：全栈与门禁

### Task C1: 真实 MySQL 集成
- 全栈复用既有基建：租户 A dispose/review/activate 后，MySQL 审计表各一行且 target/actor/trace_id 正确；租户 B 动作不产生 A 资源上的审计（或只产生 denied）。
- Commit: `feat: verify phase2 audit over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete phase2 audit slice`

## Plan Self-Review
- AGENTS 审计不变量：TENANT_USER 矩阵、target 事实、无正文泄漏、不可变留痕。
- 不泄漏：只写标识符与动作，不含 matched_text/reason/pattern。
- 无 Schema/无新表；复用既有 audit append 仓储。
- 延后：审计查询/导出、全量动作覆盖、保留策略。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL 审计行断言、矩阵单测、ruff/mypy 零错误、无 Secret、工作树干净。查询/导出/保留策略为明确延后项。
