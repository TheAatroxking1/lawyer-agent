# 阶段 2 增补：Matter 参与方写动作审计登记 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Matter party 的 add/update/remove 三个人工写动作目前没有任何审计留痕（party 切片明确延后）。本切片为成功路径登记 TENANT_USER 结构化审计：action=`matter.party.add|update|remove`、reason_code=`added|updated|removed`、target_type=`matter_party`、target_id=party.id、actor=发起成员；复用 `new_tenant_user_audit_event` 与既有 `matter.` action 前缀白名单（无需矩阵变更）；审计与业务写同 UoW 事务提交；跨租户写入不产生对方租户行。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只登记三个成功写动作；拒绝/失败路径（404/422）不在此切片（与「拒绝路径审计」的 dispose/review/activate 范围区分开）。
- 不改错误码/幂等/API 行为；list/get 只读不审计。
- 延后：permission code 化、团队/角色、冲突检查、party 拒绝路径审计。

**Architecture:** `MatterDocumentHttpService` 内新增私有 `_append_party_audit(...)` helper（沿用其它模块 getattr 风格取 `uow.audit`，无 audit/actor 时静默跳过）；`add_party`/`update_party`/`remove_party` 成功路径在返回前调用。`MatterDocumentUnitOfWorkPort` 无需新增（getattr 风格）。审计行 action 以 `matter.` 前缀已过白名单；不写入 IP/UA hash 与正文。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 事件只写标识符与稳定 reason_code；无正文/外部不可信内容。
- 使用 TDD；不削弱身份/租户过滤；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py        # 三个写动作审计 helper + 调用
  tests/
    integration/mysql/test_matter_party_audit_http_api.py  # 全栈审计行断言
```

## 里程碑 A：登记实现

### Task A1: 服务审计接线
- `_append_party_audit` helper（context/action/reason/target_id/trace）→ `new_tenant_user_audit_event`。
- add_party/update_party/remove_party 成功返回前各登记一次（action 分别为 `matter.party.add/update/remove`）。
- Commit: `feat: audit matter party write actions`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 真实 HTTP 流：add 2 → update 1 → remove 1；DB 断言 A 租户三行 action/reason/target 正确；租户 B 无这些行；审计查询 API 能按 `action=matter.party.` 过滤（前缀白名单含 matter.）。
- Commit: `feat: verify matter party audit rows over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter party audit registration slice`

## Plan Self-Review
- 成功写才审计；审计与业务同事务（成功才可见），失败请求不产生成功行。
- action 前缀已在白名单；查询 API 过滤沿用 action 前缀。
- 无 Schema：复用 audit 表与矩阵。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 三动作审计行断言、租户隔离不回归、ruff/mypy 零错误、无 Secret、工作树干净。party 拒绝路径审计、团队/角色、冲突检查、permission code 为明确延后项。
