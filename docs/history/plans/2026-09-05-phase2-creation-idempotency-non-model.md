# 阶段 2 增补：Matter/Document 创建写操作 Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 规格 9.1 要求「创建任务和重要写操作支持 Idempotency-Key」。AI Job/租户/邀请已接入；phase2 的 **Matter 创建** 与 **Document 版本登记** 两个创建端点仍无幂等——重复提交会创建重复资源。本切片把这两个端点接入既有幂等框架（`IdempotencyService` + membership scope + fingerprint），重复请求返回首答投影（replay），不再产生副作用。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 本切片只接入 `POST /tenants/{tenant_id}/matters`（Matter 创建）；Document 登记因每次生成新 document_id、需 content-addressed replay 语义，连同 Review/Rule Check/处置/Rule Pack 管理端点一起列为后续（run_checks 本身按内容幂等、处置有 open 条件保护，风险低于创建）。
- 不引入新框架；复用 `IdempotencyService`/`IdempotencyRecordModel`/`SqlAlchemyIdempotencyRepository` 与 AI Job 同样的 UoW 模式。
- 延后：permission code 化、审计查询、上传预签名 MinIO。

**Architecture:** 编排层方法增加 `idempotency_key` 参数；UoW 暴露 `idempotency` 仓储；执行顺序仿 AI Job：
1. 校验 key/route/body fingerprint；
2. `IdempotencyService.reserve(...)`（membership scope）；
3. 命中 replay → 返回已记录 result reference 的重放投影；
4. 未命中 → 执行业务写入 → `complete(..., result reference)`（matter_id/document 信息）→ 返回。
`canonical_route` 与指纹路径需与端点匹配（matter：`title/kind/description`；document：`matter_id/file_name/mime_type/sha256 摘要` 的稳定路径）。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等作用域 = membership（`IdempotencyScopeType.MEMBERSHIP` + tenant_id）；跨租户不可达。
- Key 合法性与 replay 语义沿用框架：同 key 不同 body → `idempotency_conflict`；进行中 → in_progress；replay 必须返回与首次完全一致的投影（matter id/document id/版本号）。
- 严格 Pydantic 不变；错误 Problem Details 稳定 code 不变（`InvalidIdempotencyKey` 等已有映射）。
- 使用 TDD；无新 Migration；不泄漏 Secret/正文。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py   # create_matter/register_document 增加 idempotency_key 参数与 reserve/complete 编排
    infrastructure/persistence/matter_document_uow.py  # 暴露 idempotency 仓储
    api/v1/matter_documents.py           # 端点读取 Idempotency-Key header 传入
    api/dependencies.py                  # _build_matter_document_http_service 注入 idempotency service
  tests/
    unit/test_matter_document_idempotency.py       # fake 仓储：重放投影/冲突/指纹不匹配
    integration/mysql/test_matter_document_idempotency_http_api.py  # 真实 MySQL+Redis：重复请求同 key → 同一资源；不同 body 同 key → 409
```

## 里程碑 A：组合根注入与编排层签名

### Task A1: UoW 暴露 idempotency + service 注入
- `SqlAlchemyMatterDocumentUnitOfWork` 暴露 `idempotency`（`SqlAlchemyIdempotencyRepository`）；`MatterDocumentHttpService` 构造接收 `idempotency: IdempotencyService`。
- `_build_matter_document_http_service(session_factory, idempotency)` 注入；既有不传的测试构造加默认 None（无 key 时仍直接执行，向后兼容）。
- 单测：service 可构造；无 idempotency 服务且无 key 路径正常。
- Commit: `feat: expose idempotency in matter document composition`

### Task A2: 编排层 create_matter/register_document 幂等编排
    async def create_matter(self, ...) 幂等编排
- 方法加 `idempotency_key: str | None`；有 key 时走 reserve→execute/complete→replay；无 key 走原路径。
- 指纹：`title/kind/description`（explicit business paths）。result reference 存 matter_id。
- 单测（fake idempotency）：同 key 同体 replay 同投影；同 key 异体 conflict；key 缺失直接执行。
- Commit: `feat: make matter creation idempotent`

## 里程碑 B：HTTP 接线

### Task B1: 端点接收 Idempotency-Key
- 两个 POST 端点加 `idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None`，传入编排层。
- 单测/契约：端点把 header 透传。
- Commit: `feat: wire idempotency key to tenant creation endpoints`

## 里程碑 C：全栈与门禁

### Task C1: 真实 MySQL+Redis 全栈
- 同 `test_matter_document_http_api` 基建：带 key POST matters 两次 → 同一 matter id + 201/200 replay；带 key 同 body 不同 key 两次 → 两个资源（合理）；同 key 异 body → 409 `idempotency_conflict`；document 登记同理（同 key 同 payload 两次 → 同一 document/version）。
- Commit: `feat: verify idempotent creation over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete matter document idempotency slice`

## Plan Self-Review
- 规格 9.1 Idempotency-Key 收口、既有框架复用。
- 安全：membership scope、指纹不含正文明文、replay 投影一致、冲突稳定码。
- 无 Schema：复用 IdempotencyRecordModel。
- 延后：其余写端点幂等化、审计查询、permission code。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL+Redis 幂等全栈、replay 投影一致、同 key 异 body 409、ruff/mypy 零错误、无 Secret、工作树干净。其余端点幂等化/审计查询/上传链为明确延后项。
