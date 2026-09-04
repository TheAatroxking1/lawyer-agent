# 阶段 2 增补：Document Review 状态机 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把阶段 2 已建模但未落地的 **Document Version Review 状态机**交付为受控租户 HTTP API：律师/法务对某个上传完成的 Document Version 提交复核（`PENDING_REVIEW`）、批准（`APPROVED`）、驳回（`REJECTED`）或要求修改（`CHANGES_REQUESTED`）。驳回/要求修改必须记录人工理由（新 Migration 为 `tenant_document_versions` 增加 `review_reason` 列）。Review 状态是后续「审查报告/正式结论可否引用该版本」的人工闸门。不加新表、不引入模型。

**Scope / 边界（明确延后）：**
- 不调用生成模型/Embedding；不做条款 AI、RAG、PDF。
- 不做 Review 与 RiskIssue/报告导出的自动联动（发布门槛接入报告/正式结论为后续切片）；本切片只让状态机真实可写可查。
- 上传/预签名 MinIO、Idempotency-Key 化、permission code 化、审计事件登记均为延后项。
- 只允许有意义的转换（见 Domain 转换表）；`DRAFT→PENDING_REVIEW→APPROVED/CHANGES_REQUESTED/REJECTED`，`PENDING_REVIEW→CHANGES_REQUESTED→PENDING_REVIEW`，终态 `APPROVED/REJECTED` 不可再转换。
- **Schema**：新增唯一一个向前 Migration（`20260905_10`）为 `tenant_document_versions` 增加可空 `review_reason`（Text）列；不修改已发布 Migration。

**Architecture:** Domain 层提供纯函数状态机（frozen value 语义，非法转换抛错）；仓储提供乐观条件更新（按 `tenant_id+document_id+version_id`，`upload_status` 门禁只允许已上传完成的版本进入 Review，`version` 乐观锁）；API 层薄封装（TenantActorDependency + require_path_tenant + 严格 Pydantic + Problem Details）。组合根沿用 `matter_document_http` 的 UoW 模式。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新第三方依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景；本切片不产生对外法律结论。
- Review 是人工动作：无自动通过；`REJECTED`/`CHANGES_REQUESTED` 需要理由；批准不自动代表可对外发布。
- 每请求显式租户上下文；path tenant 强校验；跨租户在 API 与仓储两层不可达（反向测试）。
- 严格 Pydantic（`extra="forbid"`）；错误统一 Problem Details（稳定 code、trace_id）。
- 使用 TDD；无新 Migration；不改已发布 Migration；不提交 Secret/.env。

## File Map

```text
backend/
  alembic/versions/20260905_10_document_review_reason.py  # add nullable review_reason to tenant_document_versions
  src/lawyer_agent/
    domain/document_review.py       # ReviewDecision + document_review_transition 纯函数（已完成 A1）
    domain/matter_documents.py      # DocumentVersion.review_reason 字段
    infrastructure/persistence/models/matter_documents.py  # 同步 review_reason 列
    infrastructure/persistence/repositories/documents.py  # review_version 原子更新
    api/v1/reviews.py               # router：POST documents/{document_id}/versions/{version_no}/review
    api/v1/router.py / dependencies.py  # include + 组合根
  tests/
    unit/test_document_review.py           # 状态机纯函数（合法/非法转换、理由必填）——已完成 A1
    unit/test_document_review_api_contract.py  # body 模型与错误码
    integration/mysql/test_document_review_migration.py   # 迁移往返 + alembic check
    integration/mysql/test_document_review_http_api.py  # 真实 MySQL+Redis 全栈 + 跨租户反向
```

## 里程碑 A：Domain 状态机与仓储

### Task A1: Review 转换纯函数
- `ReviewDecision(APPROVE/REJECT/REQUEST_CHANGES/SUBMIT)`；纯函数对 `DocumentVersion.review_status`（`None` 视为未复核初始态）执行合法转换，非法转换抛 `InvalidReviewTransition`；`SUBMIT` 只允许 `None|DRAFT|CHANGES_REQUESTED → PENDING_REVIEW`；`APPROVE/REJECT` 只允许 `PENDING_REVIEW → APPROVED/REJECTED`；`REQUEST_CHANGES` 只允许 `PENDING_REVIEW → CHANGES_REQUESTED`；终态 `APPROVED/REJECTED` 不可再转换。
- 理由要求：`REJECT`/`REQUEST_CHANGES` 必须给非空理由；`APPROVE` 可带可不带。
- 单测：全部合法转换 + 非法转换拒绝 + 空理由拒绝。
- Commit: `feat: model document review transitions`

### Task A2: Migration + 模型 + 仓储 Review 更新
- Migration `20260905_10`：`tenant_document_versions` 增加可空 `review_reason`（Text）；往返 + `alembic check`（对照模型）。
- `DocumentVersion.review_reason`（str | None）与 ORM 列、仓储映射同步；`_document_version`/`save` 读写该列。
- `review_document_version(...)`：按唯一键 `tenant_id+document_id+version_no` 原子条件更新 `review_status` + `review_reason`（`rowcount==1` 判定成功）；加载时校验 `upload_status in ('accepted','ready')`（只有完成上传的版本可进 Review），域转换通过后才落库。
- 单测/迁移测试：迁移往返 + `alembic check`；未上传完成的版本拒绝进入 Review；不存在的版本返回 None。
- Commit: `feat: persist document review decisions`

## 里程碑 B：HTTP API

### Task B1: 路由与严格模型
- `POST /tenants/{tenant_id}/documents/{document_id}/versions/{version_no}/review`，body `{decision, reason?}`；path tenant 强校验；非法 decision/空理由 422；版本不存在/跨租户 404；不可转换 409。
- 契约单测：body 拒绝未知 decision、空 reason 语义、额外字段。
- Commit: `feat: add document review tenant http endpoint`

### Task B2: 组合根接线
- `ApplicationServices` 加惰性 `review_http`（复用 matter_document UoW session 生命周期）或扩展现有 service；薄编排转发到仓储。
- 单测：缺失服务 503 语义；present 返回。
- Commit: `feat: expose document review to http composition`

## 里程碑 C：全栈与门禁

### Task C1: MySQL+Redis 全栈
- 真实全栈：注册/租户/激活/switch → 上传 docx（accepted）→ 提交 PENDING_REVIEW → REQUEST_CHANGES(带理由) → 再 SUBMIT → APPROVE → GET 读回状态；另一租户访问 404（路径/资源两层）。
- Commit: `feat: verify document review http flow over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest（真实 MySQL）、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete document review http api slice`

## Plan Self-Review
- 规格 8.2-2 Review 后端落地、非模型；发布门槛联动（报告/正式结论需 APPROVED 版本）标为后续。
- Security：Review 仅人工、理由必填（驳回/修改）、乐观并发、跨租户反向、path 校验。
- 无 Schema：upload_status/review_status 列与 CHECK 已存在，只加领域与服务逻辑。
- Placeholder：不把 Review 批准自动当对外发布。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL+Redis 全栈 HTTP、跨租户反向、状态机合法/非法转换单测、理由必填、ruff/mypy 零错误、无 Secret、工作树干净。条款 AI/RAG/上传预签名/自动发布联动为明确未验证项。
