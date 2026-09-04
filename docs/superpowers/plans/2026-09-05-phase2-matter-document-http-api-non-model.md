# 阶段 2 增补：租户 Matter/Document HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把阶段 2 已交付的 Matter/Document 服务（`SqlAlchemyMatterRepository`、`DocumentUploadService`、`SqlAlchemyDocumentRepository`）接成**受控租户 HTTP API**（`/api/v1/tenants/{tenant_id}/...`）：创建/读取 Matter、登记 Document 原件版本（校验 MIME/SHA-256/大小、`ACCEPTED`/`NEEDS_REVIEW`）、按 Matter 列表文档。这是 Rule Check HTTP API 的**上游**：没有它，产品中 Rule Check/处置/报告端点无法在真实链路上被驱动。无 Schema 变更、不引入模型/RAG/MinIO。

**Scope / 边界（明确延后）：**
- 不调用生成模型/Embedding；不做条款 AI、RAG、SSE、PDF。
- 上传仍是「受管对象引用 + 元数据」：payload 经 API 校验哈希/MIME 后只登记 object_key/sha256，字节直传 MinIO（预签名分片直传）仍为延后切片；`.doc`/ZIP/病毒扫描/宏沙箱不在本计划。
- 不新增 RBAC permission code：授权与 Rule Check API/既有切片一致——活跃成员 + 显式 tenant 上下文 + 资源 tenant 归属 + 跨租户反向；permission code 化归后续权限计划。
- 审计事件登记、Review API 端点（状态机已存在服务层）不在本计划。

**Architecture:** API（严格 Pydantic）→ 应用/服务层（`MatterQueryPort`、`DocumentUploadService` 等既有组件）→ 仓储（均 tenant-scoped）。FastAPI `TenantActorDependency` 提供已校验 `TenantContext`；端点强制 path tenant 与 actor 一致（`require_path_tenant` 复用）；跨租户读取不可达（404 语义）。组合根复用 `ApplicationServices` 惰性 UoW 入口（新加 `matter_document_http`），每请求单 session/事务，模式与 Rule Check HTTP 一致。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新第三方依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景；本切片不产生对外法律结论。
- 每请求显式租户上下文；path tenant 必须与 actor 匹配；跨租户在 API 与仓储两层都不可达（反向测试）。
- 文档文件名/对象 key：仅白名单 `[A-Za-z0-9._-]` 与租户前缀；不把不可信文件名当路径。
- MIME 白名单沿用上传服务（docx）；不合规 → `NEEDS_REVIEW`，不绕过安全语义。
- `Idempotency-Key` 支持创建类写操作；版本/ETag 语义按既有 `version`。
- 错误统一 Problem Details：稳定 `code`、`trace_id`，不泄露 SQL/堆栈/密钥。
- 使用 TDD；无新 Migration；不改已发布 Migration；不提交 Secret/.env/原件。

## File Map

```text
backend/
  src/lawyer_agent/
    api/v1/matter_documents.py        # router：POST/GET matters、POST matters/{id}/documents、GET documents/versions
    api/v1/__init__.py / router.py    # include 新 router
    api/dependencies.py               # ApplicationServices 增加惰性 matter/document HTTP 组合入口
    application/matter_document_api.py # 薄编排：actor.context + commands → 既有服务；稳定错误码
  tests/
    unit/test_matter_document_api_contract.py    # Pydantic 模型/错误码（无 DB）
    integration/mysql/test_matter_document_http_api.py  # 真实 MySQL+Redis 全栈 HTTP + 跨租户反向
```

## 里程碑 A：组合根与路由骨架

### Task A1: 组合根暴露 matter/document 服务
- `ApplicationServices` 加惰性 `matter_document_http`（UoW 每请求单 session），不改既有字段。
- 单测：组合根在无 DB 依赖下构造不破坏既有 services；缺失服务给 503 语义。
- Commit: `feat: expose matter document services to http composition`

### Task A2: 路由与严格模型
- 端点：`POST /tenants/{tenant_id}/matters`（创建，201 + Idempotency-Key）、`GET /tenants/{tenant_id}/matters/{matter_id}`（读取）、`POST /tenants/{tenant_id}/matters/{matter_id}/documents`（登记版本）、`GET /tenants/{tenant_id}/matters/{matter_id}/documents`（列出版本）。
- 严格 Pydantic（`extra="forbid"`）；path tenant 校验复用；错误码稳定（404/409/422）。
- 契约单测：body 拒绝额外字段、非法 kind/status、空标题、坏 MIME。
- Commit: `feat: add matter and document tenant http endpoints`

## 里程碑 B：行为接线与集成

### Task B1: matter create/get 端到端
- `POST matters`：`MatterKind` 白名单；创建后返回 id/ETag；幂等 Key 冲突按既有错误映射。
- `GET matters/{id}`：跨租户/不存在 → 404。
- MySQL 集成（同 Rule Check HTTP 模式）覆盖 create→get→second tenant 404。
- Commit: `feat: wire matter create and read endpoints`

### Task B2: document 登记与列表
- `POST matters/{id}/documents`：payload（受控字节）→ MIME/SHA-256/大小校验 → `ACCEPTED` 或 `NEEDS_REVIEW`；对象 key 白名单；返回版本摘要。
- `GET matters/{id}/documents`：该 matter 下版本列表（tenant 过滤），不泄漏他租户行。
- 单测（fake）+ MySQL 集成：docx 通过 → accepted；伪造 MIME → needs_review；跨租户 404。
- Commit: `feat: register and list tenant document versions`

## 里程碑 C：全栈与门禁

### Task C1: HTTP 全栈集成（真实 MySQL+Redis）
- 同 `test_rule_check_http_api.py` 基建：注册账号→建双租户→激活→switch token；租户 A 创建 matter→登记 docx→列版本→run Rule Check（验证上游闭环）；租户 B 访问 A 的 matter/document → 404（路径与资源两层）。
- Commit: `feat: verify matter document http flow over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest（真实 MySQL）、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter document http api slice`

## Plan Self-Review
- 规格 9.1 REST/Problem Details/Idempotency 收口；9.4 上传元数据先行部分（预签名直传 MinIO 延后）。
- Security：path tenant 强校验、每请求显式 context、无裸 get_by_id、跨租户反向、MIME/文件名白名单、NEEDS_REVIEW 不绕过。
- 无 Schema 变更：不加 Migration；全栈测试复用 Rule Check HTTP 基建。
- Placeholder：上传字节不持久化（MinIO 延后），登记真实元数据与哈希；不假装 AV/病毒扫描。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL+Redis 全栈 HTTP、跨租户反向、matter/document 四端点行为断言、严格模型契约测试、ruff/mypy 零错误、无 Secret、工作树干净。条款 AI/RAG/上传预签名/PDF/正式结论为明确未验证项。
