# 阶段 2 增补：Document Review Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /tenants/{tenant_id}/documents/{document_id}/versions/{version_no}/review` 目前无 Idempotency-Key：网络重试可能产生同一决策的重复事务记录或 409 抖动。本切片接入既有幂等框架：同 membership scope + key，指纹 = decision + reason；首次成功 complete（result_type=`document_review.version`、result_id=version.id），同 key 同指纹 replay 返回原版本投影（不再校验状态机、不重复转移）；同 key 异指纹 → 409 `idempotency_conflict`；异 key 仍走状态机（终态 409 `document_review_conflict`）。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做 review 端点幂等；dispose/rule-pack 启停/激活等其它状态保护写仍延后。
- replay 成功于 complete 之后；失败/被拒请求不 complete（业务 UoW 回滚时保留事务撤销，同 key 重试可重新执行）。
- 不改变状态机、审计、错误码语义；无 key 请求完全向后兼容。
- 延后：permission code 化、结论引用门槛、Review 结果快照版本化。

**Architecture:** `DocumentReviewHttpService` 注入可选 `IdempotencyService`；`SqlAlchemyMatterDocumentUnitOfWork` 已暴露 `idempotency` 仓储（直接复用）。reserve→执行→complete 同一 UoW 事务；replay 分支用 `documents.find_version` 读取原版本（找不到→`DocumentReviewConflict`）。端点新增 `Idempotency-Key` header（Annotated Header），service 方法新增 `idempotency_key`/`now` 参数。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等 scope 必须 membership+tenant；指纹只含决策与理由文本，不写正文外泄；无 key 路径行为不变。
- 使用 TDD；跨租户反向测试不回归（replay 404 路径仍需 404 而非错误 complete）。
- 不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/document_review_api.py       # idempotency 接线
    api/v1/reviews.py                        # Idempotency-Key header + 参数
    api/dependencies.py                      # review builder 注入 IdempotencyService
  tests/
    integration/mysql/test_document_review_idempotency_http_api.py  # 全栈
```

## 里程碑 A：服务与依赖

### Task A1: service 幂等接线
- `DocumentReviewHttpService.__init__` 增加 `idempotency: IdempotencyService | None = None`。
- `apply_review` 增加 `idempotency_key`/`now`：key+service 存在时 reserve（scope=MEMBERSHIP、op=`document.review`、canonical route 模板、指纹 decision/reason）；replay → `find_version` 读取原版本返回；成功 → complete。
- Commit: `feat: document review idempotency reservation`

### Task A2: 端点与依赖注入
- reviews.py 端点接收 `Idempotency-Key` header 并传递；dependencies `_build_document_review_http_service` 传入共享 IdempotencyService。
- Commit: `feat: wire document review idempotency http`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 同 key 同指纹 submit→replay 返回同一版本/状态且不产生第二次转移；同 key 异 decision → 409 `idempotency_conflict`；终态 approve 后同 key replay 仍 200（绕过状态机）；异 key 新决策按状态机 409；无 key 兼容；跨租户 key 不串（replay 404）。
- Commit: `feat: verify document review idempotency over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete document review idempotency slice`

## Plan Self-Review
- 幂等不绕过授权与租户校验：reserve 前仍走 require_uuid7/path tenant；replay 读取仍 tenant 范围仓储。
- 状态机仍由仓储原子更新；幂等只防重复"同请求"。
- 无 Schema：复用 idempotency_records 与版本行。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL replay/conflict/终态兼容全栈断言、跨租户不回归、ruff/mypy 零错误、无 Secret、工作树干净。dispose/启停/激活幂等化为明确延后项。
