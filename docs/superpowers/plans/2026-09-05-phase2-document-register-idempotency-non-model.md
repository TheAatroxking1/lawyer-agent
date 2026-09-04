# 阶段 2 增补：Document 登记 Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 规格 9.1 幂等收口的另一半：`POST /tenants/{tenant_id}/matters/{matter_id}/documents` 登记 Document 版本接入既有幂等框架。同 `Idempotency-Key` + 同业务指纹（matter_id/file_name/mime_type/payload SHA-256）重放 → 返回原 document/version 投影，不产生第二个 document header/version。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做 Document 登记端点（Matter 创建已接入）。
- Review/Rule Check run/dispose/Rule Pack 管理端点幂等化仍延后（run_checks 本身按内容幂等、处置有 open 条件保护）。
- 不引入新框架；复用 `IdempotencyService`/`IdempotencyRecordModel`/既有 UoW `idempotency` 仓储。
- 指纹含 `matter_id`：同一 matter 同一文件重放返回原版本；不同 matter 同 payload 视为异体 → conflict。
- 延后：permission code、审计查询、上传预签名 MinIO。

**Architecture:** 仿 Matter 创建：`register_document` 加 `idempotency_key`（无 key 走原路径）；有 key → reserve（membership scope，`operation="document.register"`，route `/api/v1/tenants/{tenant_id}/matters/{matter_id}/documents`，指纹 business_paths=file_name/mime_type/payload_sha256；matter_id 在 path 与 body 均参与语义，经 canonical_route 区分）→ replay（读回已存 document 的最高版本投影）→ 执行 `DocumentUploadService.complete` → complete（result_id=document_id）→ 返回。

payload 不进指纹明文：先算 `sha256(payload)` 作为指纹字段值；正文不落幂等表。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等作用域 membership+tenant；同 key 异体 → `idempotency_conflict`（409）；replay 投影必须与首次一致（同 document_id/version_no/upload_status）。
- payload 只用于 sha256 指纹，不入库、不日志。
- 无 key 路径行为不变（向后兼容既有调用与测试）。
- 严格 Pydantic 不变；错误映射沿用框架已有（InvalidIdempotencyKey 等 → 422/409/503）。
- 使用 TDD；无 Migration；不泄漏正文。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py   # register_document 增加幂等编排（仿 create_matter）
    api/v1/matter_documents.py           # 端点读 Idempotency-Key header 透传
  tests/
    integration/mysql/test_document_register_idempotency_http_api.py  # 真实 MySQL+Redis
```

## 里程碑 A：编排与 HTTP

### Task A1: register_document 幂等编排
- 常量：`_DOCUMENT_REGISTER_OPERATION = "document.register"`、`_DOCUMENT_RESULT_TYPE = "document.document_version"`、route 常量。
- 方法加 `idempotency_key: str | None`；`sha256(payload)` 计算指纹；reserve→replay（`find_version(tenant_id, document_id)` 取当前版本投影）→complete→原路径。
- 单测（fake/repo MySQL 集成覆盖）：同 key 同指纹重放同 document_id；同 key 异 payload 409；无 key 原路径。
- Commit: `feat: make document registration idempotent`

### Task A2: 端点接线
- `register_document` 端点读 `Idempotency-Key` header 传入编排层。
- 契约：header 透传。
- Commit: `feat: wire idempotency key to document registration`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL+Redis 全栈
- 同基建：上传 docx 两次带同 key 同 payload → 同 document_id/version_no、matter 下仅 1 个 document header；同 key 不同 payload → 409 `idempotency_conflict`；异 key 同 payload → 新 document。
- Commit: `feat: verify idempotent document registration over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete document registration idempotency slice`

## Plan Self-Review
- 规格 9.1 创建类写操作幂等闭环（matter + document 双端点）。
- 安全：payload 只入 sha256 指纹、membership scope、replay 投影一致、409 conflict。
- 无 Schema；复用既有 UoW idempotency 与框架。
- 延后：其余写端点幂等化、审计查询、上传链。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL+Redis 幂等全栈、replay 同 document_id、同 key 异 payload 409、ruff/mypy 零错误、无 Secret、工作树干净。其余端点幂等化/上传链为明确延后项。
