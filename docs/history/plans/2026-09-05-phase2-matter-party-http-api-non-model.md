# 阶段 2 增补：Matter 参与方 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Matter 已能创建/读取/挂文档，但**参与方（Party）**只有仓储 `add_party` 无 HTTP、无列表。本切片交付 Matter 参与方管理：向 Matter 添加参与方（`POST /tenants/{t}/matters/{matter_id}/parties`）、列出参与方（`GET .../parties`）。对应规格 8.2-6 MatterManagement「参与方」非模型部分。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做参与方 add/list；不接团队/角色/冲突检查（外部客户/教育方参与共享为延后）。
- `kind` 用自由短文本（角色/类型标签），不做枚举强约束（延后到权限/团队模型）；display_name 必填非空、kind 必填非空。
- 不带幂等（`POST` 每次新增一条参与方是有意行为；若需幂等后续补）、不带审计（后续统一）。
- 延后：删除/更新参与方、参与方冲突检查、permission code、上传链。

**Architecture:** 仓储补 `list_parties(context, matter_id)`（按 tenant+matter 过滤、created_at 升序）；`MatterDocumentHttpService` 加 `add_party/list_parties` 透传（复用既有 UoW 的 `matters`）；两个端点 path tenant 强校验、404（matter 不存在/跨租户）。`kind` 长度上限 64、display_name 上限 256（与 DB 列一致）。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 每请求显式租户上下文；path tenant 强校验；跨租户在 API 与仓储两层不可达（反向测试）。
- 严格 Pydantic（`extra="forbid"`）；错误 Problem Details 稳定 code。
- 使用 TDD；无 Migration；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    infrastructure/persistence/repositories/matters.py  # list_parties
    application/matter_document_api.py                   # add_party/list_parties
    api/v1/matter_documents.py                           # 两个端点
  tests/
    unit/test_matter_party_contract.py                   # body 模型/长度校验
    integration/mysql/test_matter_party_http_api.py      # 真实 MySQL+Redis：add/list/跨租户 404
```

## 里程碑 A：仓储与服务

### Task A1: list_parties + service 透传
- 仓储 `list_parties(context, matter_id)`；`MatterDocumentHttpService.add_party/list_parties`（校验 matter 存在）。
- 单测（MySQL 集成/单测）：add 后 list 返回、顺序稳定、跨租户空。
- Commit: `feat: add matter party reads and writes`

## 里程碑 B：HTTP 与全栈

### Task B1: 端点 + 全栈
- `POST/GET /tenants/{tenant_id}/matters/{matter_id}/parties`；契约单测；真实 MySQL+Redis：A 加 2 参与方 → list 2；B 访问 A 的 matter 404（path/资源两层）。
- Commit: `feat: verify matter party http flow over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete matter party http api slice`

## Plan Self-Review
- 规格 8.2-6 MatterManagement 参与方后端非模型部分。
- Security：租户硬隔离、path 校验、反向测试。
- 无 Schema：复用 tenant_matter_parties 表。
- 延后：团队/角色/冲突/更新删除/审计/幂等。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL+Redis 参与方全栈、add/list 行为、跨租户反向、契约测试、ruff/mypy 零错误、无 Secret、工作树干净。团队/冲突检查/审计为明确延后项。
