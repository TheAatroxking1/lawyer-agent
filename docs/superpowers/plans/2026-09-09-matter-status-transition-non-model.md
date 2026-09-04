# 阶段 2 增补：Matter 状态迁移 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Matter 创建后只有 `status=open` 且再无状态迁移路径，律师无法推进/结案/归档案件（schema 已允许 open/active/closed/archived，version 列与 GET ETag 已为条件更新铺路）。本切片交付 `POST /api/v1/tenants/{tenant_id}/matters/{matter_id}/status`：请求体 `{status}` + 可选 `If-Match`（`"<version>"` 强 ETag）；领域纯函数定义受限迁移集合；仓储条件更新 `WHERE tenant+matter+status=source AND version=expected`，返回新投影；path 租户强校验、跨租户双层 404、非法/越级迁移 409 `matter_document_conflict`、ETag 失配 409、无 key 兼容。登记 `matter.status` TENANT_USER 审计（reason=`changed`）。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做状态迁移（status 单选）；title/description/kind 编辑、批量迁移、迁移历史保留另议。
- 迁移集合（保守、可后续扩）：
  - `open → active`、`open → closed`
  - `active → closed`
  - `closed → archived`
  - `archived` 终态不可迁移；`closed → open` 等回退不允许。
- 延后：团队/角色、permission code 化、结论引用门槛。

**Architecture:** 领域新增纯函数 `require_matter_status_transition(source, target)`（仿 document_review 状态机）；仓储新增 `transition_matter_status(context, matter_id, *, expected_version, target_status, now)`：先 `get_matter(for_update)` 校验存在与当前 status，再按 domain 校验，随后 UPDATE `SET status=?, version=version+1 WHERE tenant+matter+id+version=?`（rowcount==1）；服务层 `MatterDocumentHttpService.transition_matter_status(context, matter_id, expected_version, target_status, trace_id)`：matter 不存在 → 404、迁移非法/并发失配 → 409；成功登记 `matter.status` 审计。API 层解析 If-Match 强 ETag → `expected_version`；响应 `MatterSummary` + 新 ETag。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 状态迁移不影响已受理文档/风险/审计行（facts 保留，仅案件容器状态变化）。
- 使用 TDD；跨租户反向测试；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    domain/matter_documents.py                  # 状态迁移纯函数 + 异常
    infrastructure/persistence/repositories/matters.py  # transition CAS
    application/matter_document_api.py          # 编排 + 审计
    api/v1/matter_documents.py                  # POST /matters/{id}/status + ETag
  tests/
    unit/test_matter_documents.py               # 状态机单测
    integration/mysql/test_matter_status_http_api.py  # 全栈
```

## 里程碑 A：领域与仓储

### Task A1: 状态机纯函数 + 仓储 CAS
- `require_matter_status_transition` 白名单；仓储 `transition_matter_status`（读锁→domain→CAS）。
- Commit: `feat: matter status transition domain and repository`

### Task A2: 服务与端点
- 服务编排 + `matter.status` 审计；`POST /matters/{matter_id}/status` 端点 + If-Match。
- Commit: `feat: matter status transition http endpoint`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- open→active→closed→archived 推进且 version 递增；closed→archived 后再迁 409；archived 回退 409；If-Match 旧版本 409；路径/资源双层跨租户 404；响应 ETag 更新；审计行存在；无 If-Match 时并发安全仍成立（当前版本 CAS）。
- Commit: `feat: verify matter status transitions over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter status transition slice`

## Plan Self-Review
- 状态机为纯函数、显式白名单；无隐式自由迁移。
- CAS 条件更新 + 强 ETag，防并发覆盖与跨租户越权。
- 审计仅登记标识符与 reason，无正文；facts 不受影响。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 推进/终态/回退/ETag/跨租户/审计断言、ruff/mypy 零错误、无 Secret、工作树干净。title/description 编辑、团队/角色为明确延后项。
