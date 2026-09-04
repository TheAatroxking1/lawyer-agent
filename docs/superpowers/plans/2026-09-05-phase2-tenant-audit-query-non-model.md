# 阶段 2 增补：租户审计查询 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 人工写动作审计切片已能**写入**审计，但租户无法读取自己的审计留痕。本切片交付**租户范围审计查询 API**：律师/法务按租户列出审计事件（可按 action/target_type/trace_id 过滤、时间倒序、分页），仅返回本租户行。平台级跨租户审计查询/导出延后。

**Scope / 边界（明确延后）：**
- 只读端点；只返回**本租户**审计行（`tenant_id` 过滤是唯一硬边界；actor/on_behalf FK 已保证行租户归属）。
- 不暴露 `client_ip_hash`/`user_agent_hash` 原文（返回是否存在/脱敏布尔？本切片不返回该两字段，避免敏感）；不返回 `metadata_json`（可能含敏感内容，延后）。
- 过滤白名单：`action`（前缀匹配，如 `risk_issue.`/`document.`/`rule_pack.`/`matter.`）、`target_type`、`trace_id` 精确；`limit ≤ 100` + `before_id` 游标（按 occurred_at 倒序稳定分页）。
- 审计查询不鉴权到单个事件（租户内所有 active member 可读本租户审计）——permission code 化延后。
- 延后：跨租户管理端、导出、保留策略、IP/UA hash 展示。

**Architecture:** 仓储 `list_audit_events(context, *, action_prefix?, target_type?, trace_id?, limit, before_occurred_at/before_id)` 以 `tenant_id` 过滤并按 `occurred_at desc, id desc` 返回；应用薄层透传 + 校验；`GET /api/v1/tenants/{tenant_id}/audit`。响应模型字段：id/actor_kind/action/result/reason_code/target_type/target_id/trace_id/occurred_at。返回游标 `next_before_id` 供翻页。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- `tenant_id` 过滤为安全硬边界；path tenant 强校验；跨租户行不可见（反向测试：B 请求 A 的审计不可见、B 只能看到自己）。
- 过滤参数严格校验（白名单前缀、trace_id 长度、limit 范围）；错误统一 Problem Details。
- 不返回 IP/UA hash 与 metadata_json；正文/处置理由本身从不入审计（写入侧已保证）。
- 使用 TDD；无 Migration；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    infrastructure/persistence/repositories/audit.py  # 新增 list_for_tenant（Query）
    api/v1/audit.py                # router：GET /tenants/{tenant_id}/audit
    api/v1/router.py               # include
    api/dependencies.py            # ApplicationServices 暴露审计查询（或直接经 UoW/仓储 + 每请求 session）
  tests/
    unit/test_tenant_audit_query_contract.py   # body/参数模型、游标校验（无 DB）
    integration/mysql/test_tenant_audit_http_api.py  # 真实 MySQL+Redis：写 3 类事件 → 查询过滤/翻页/跨租户反向
```

## 里程碑 A：仓储查询

### Task A1: list_for_tenant
- `AuditRepository.list_for_tenant(tenant_id, *, action_prefix, target_type, trace_id, limit, before_occurred_at, before_id)`：`tenant_id` 过滤 + 可选条件 + `occurred_at desc, id desc`，LIMIT 上限 100；仅返回列白名单（不含 hash/metadata）。
- 单测（MySQL 集成）：同租户多条按时间倒序；action 前缀过滤；跨租户 0 行。
- Commit: `feat: add tenant audit query`

## 里程碑 B：HTTP API

### Task B1: 查询端点 + 严格模型
- `GET /tenants/{tenant_id}/audit`：query 参数 `action`（白名单前缀校验）、`target_type`、`trace_id`（16-64 ASCII）、`limit`（1-100 默认 20）、`before_id`（uuid）；响应 `{events:[...], next_before_id?}`。
- path tenant 强校验；无匹配返回空列表。
- 契约单测：非法 action/超长 trace_id/越界 limit 422。
- Commit: `feat: add tenant audit read endpoint`

## 里程碑 C：全栈与门禁

### Task C1: MySQL+Redis 全栈
- 复用审计写入全栈基建：建租户 A/B → A 上 activate pack + review + dispose 3 事件 → GET audit 倒序返回 3 条且 action 白名单可见；`action=risk_issue.` 过滤 1 条；`before_id` 翻页稳定；B token 在 A 路径 404、B 路径空列表。
- Commit: `feat: verify tenant audit query over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete tenant audit query slice`

## Plan Self-Review
- 审计不变量：写入已覆盖人工动作；查询只读、租户硬隔离、敏感字段不返回。
- 无 Schema：索引 `(tenant_id, occurred_at)` 已存在。
- 延后：跨租户管理/导出/IP-UA 展示/审计保留策略。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL+Redis 审计查询全栈、过滤/翻页正确、跨租户反向、参数契约测试、ruff/mypy 零错误、无 Secret、工作树干净。跨租户查询/导出为明确延后项。
