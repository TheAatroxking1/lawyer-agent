# 阶段 2 增补：租户 Matter 列表（keyset 分页）HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Matter 域目前只有 `POST /matters` 与 `GET /matters/{matter_id}`，律师无法列出租户下案件目录。本切片交付 `GET /api/v1/tenants/{tenant_id}/matters`：按 `created_at`（次键 id）keyset 稳定分页，`limit`（1–100）与 `before_id` 游标，返回 `{items: [...], next_before_id}`；列表只含本租户 matter，路径层租户强校验，`before_id` 越权/不存在 → 404。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做只读列表；状态过滤、搜索、按 kind/status 过滤、排序参数、团队/角色过滤均延后。
- 分页为 `created_at,id` 双向 keyset（与 audit 查询同构）。
- 延后：permission code 化、MinIO 直传、导出。

**Architecture:** 仓储新增 `list_matters(context, *, limit, before_id)`：`SELECT ... WHERE tenant_id=?`，before_id 时先解析其 `(created_at,id)` 锚点，再取 `created_at < anchor OR (created_at = anchor AND id < anchor)`，按 `created_at DESC, id DESC` 限 limit+1 判定 next_before_id（若满 limit 返回末条 id 作游标）。服务层 `MatterDocumentHttpService.list_matters(context, limit, before_id)`；API 复用 `MatterSummary` 与 `{items, next_before_id}` 响应模型。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 列表严格 tenant 范围；跨租户反向测试（B 调 A path 404、B 资源层只返回 B 数据）。
- 使用 TDD；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    infrastructure/persistence/repositories/matters.py  # list_matters keyset
    application/matter_document_api.py                  # 编排（port 扩展 + service）
    api/v1/matter_documents.py                          # GET /matters + 响应模型
  tests/
    integration/mysql/test_matter_list_http_api.py      # 全栈
```

## 里程碑 A：仓储与编排

### Task A1: 仓储 keyset 列表
- `list_matters(context, *, limit, before_id) -> tuple[Matter, ...]`（含锚点解析）。
- Commit: `feat: tenant matter keyset listing repository`

### Task A2: 服务与端点
- `MatterDocumentHttpService.list_matters`；`GET /matters` 端点（path 校验、422 非法 limit、404 越权游标）。
- Commit: `feat: tenant matter list http endpoint`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 建 3 matter → limit=2 第一页两条 + next_before_id → 第二页一条 + next 为空 → 全量不重不漏；B 租户 path 层 404、resource 层只见 B 数据；limit=0/101 → 422；随机 uuid 游标 → 404。
- Commit: `feat: verify tenant matter list over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete tenant matter list slice`

## Plan Self-Review
- keyset 稳定分页（created_at,id），插入/并发不翻页漂移。
- 越权游标 404 不泄露其它租户数据；路径层校验。
- 无 Schema：复用 tenant_matters 行。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 分页不重不漏/游标/非法 limit/跨租户断言、ruff/mypy 零错误、无 Secret、工作树干净。状态/搜索过滤、permission code 化为明确延后项。
