# 阶段 2 增补：Matter 参与方 Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** party 的 `add/update/remove` 无 Idempotency-Key，网络重试可能重复添加/重复写。接入既有幂等框架（AGENTS 记录「其余写端点幂等化仍为延后项」；与 review/dispose/rule-pack 状态幂等切片同构）：
- add：指纹 = matter_id/display_name/kind；result_type=`matter_party`、result_id=party.id；同 key 同体 replay 返回原 party 投影、不重复插入。
- update：指纹 = matter_id/party_id/display_name/kind；result_type=`matter_party`、result_id=party.id；replay 返回当前 party 投影（update 幂等语义：同 key 同体不再应用第二次版本递增）。
- remove：指纹 = matter_id/party_id；result_type=`matter_party`、result_id=party_id；同 key replay 返回 204 且不重复删除。
- 同 key 异指纹 → 409 `idempotency_conflict`；404/422 不 complete（业务 UoW 回滚撤销保留行，可重试）；无 key 路径完全向后兼容。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做三个 party 写端点幂等；不扩展 list/get 与审计语义。
- update 为合并语义（至少一个字段）；remove 幂等 replay 需原 party 已删也返回 204（同 key 已完成即视作成功，不再触碰资源）。
- 延后：permission code 化、团队/角色、冲突检查。

**Architecture:** `MatterDocumentHttpService` 已持有 `IdempotencyService` 与 `idempotency` 仓储（matter create/document register 已用）；`add_party/update_party/remove_party` 增加 `idempotency_key`/`now`：reserve→执行→complete 同一 UoW；replay 分支 add/update 经 `list_parties` 读回投影（找不到→`MatterDocumentConflict`）、remove 直接 204 返回。端点加 `Idempotency-Key` header。新增 `matter.party` 拒绝审计与成功审计保持不变（replay 不重复登记）。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等 scope=membership+tenant；指纹只含标识符与文本字段；无 key 行为不变。
- 使用 TDD；跨租户反向与审计不回归。
- 不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py       # 三方法幂等接线
    api/v1/matter_documents.py               # Idempotency-Key headers
  tests/
    integration/mysql/test_matter_party_idempotency_http_api.py  # 全栈
```

## 里程碑 A：服务与端点

### Task A1: add/update/remove 幂等接线
- add：reserve 指纹（matter_id/display_name/kind）→ replay `list_parties` 读回 → 成功 complete。
- update：reserve 指纹（matter_id/party_id/display_name/kind）→ replay 读回 → 成功 complete。
- remove：reserve 指纹（matter_id/party_id）→ replay 204 → 成功 complete。
- Commit: `feat: matter party idempotency reservations`

### Task A2: 端点 headers
- matter_documents.py 三个 party 端点接收 `Idempotency-Key` 并传递。
- Commit: `feat: wire matter party idempotency http`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- add 带 key→replay 同 id 且 party 行数不变；同 key 异 body 409 `idempotency_conflict`；update 同 key replay 版本不重复递增；remove 同 key replay 204 且行数不变；404/422 不 complete（修复后同 key 可重试）；无 key 兼容；跨租户 key 不串（replay 404）。
- Commit: `feat: verify matter party idempotency over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter party idempotency slice`

## Plan Self-Review
- 幂等不绕过租户/授权校验；replay 读取仍 tenant 范围仓储。
- 成功只 complete 一次；replay 不重复审计/版本递增/删除。
- 无 Schema：复用 idempotency_records。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL replay/conflict/无 key 全栈断言、审计与跨租户不回归、ruff/mypy 零错误、无 Secret、工作树干净。团队/角色/冲突检查、permission code 化为明确延后项。
