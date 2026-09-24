# 阶段 2 增补：Rule Pack 启停/激活 Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `PATCH .../rule-packs/{pack_id}/rules/{rule_id}`（启停规则）与 `POST .../rule-packs/{pack_id}/activate`（激活唯一 active）无 Idempotency-Key。接入既有幂等框架（AGENTS 记录「Review/启停/激活幂等化仍有状态保护未接」）：membership scope；activate 指纹 = pack_id；set_rule_enabled 指纹 = pack_id/rule_id/enabled。首次成功 complete（activate result_type=`rule_pack.pack`/result_id=pack_id；set_rule_enabled result_type=`rule_pack.rule`/result_id=rule_id）；同 key 同指纹 replay 返回原投影/204 且不重复状态写入、同 key 异指纹 → 409 `idempotency_conflict`；异 key 仍走业务校验（404/409 不变）；无 key 完全向后兼容。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做 activate 与 set_rule_enabled 两个端点幂等；不扩展其它 admin 写。
- replay 不追加第二条成功审计（activate 成功审计只在首次执行写）；失败/被拒请求不 complete（业务 UoW 回滚撤销保留行，同 key 可重试）。
- 状态机与错误码语义不变。
- 延后：模板规则库、permission code 化。

**Architecture:** `RulePackAdminHttpService` 已持有 `IdempotencyService` 与 `idempotency` 仓储（create/add_rule 已用）；`set_rule_enabled`/`activate_pack` 增加 `idempotency_key`/`now` 参数：reserve→执行→complete 同一 UoW；replay 分支 activate 经 `_require_pack` 读回 pack 返回、set_rule_enabled 直接 204 返回。端点加 `Idempotency-Key` header。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等 scope=membership+tenant；指纹只含标识符与开关值；无 key 行为不变。
- 使用 TDD；跨租户反向与拒绝审计不回归。
- 不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/rule_pack_admin_api.py       # set_rule_enabled/activate_pack 幂等接线
    api/v1/rule_packs.py                     # Idempotency-Key headers
  tests/
    integration/mysql/test_rule_pack_activate_idempotency_http_api.py  # 全栈
```

## 里程碑 A：服务与端点

### Task A1: set_rule_enabled 幂等接线
- reserve（指纹 pack_id/rule_id/enabled；result_type=`rule_pack.rule`、result_id=rule_id）；replay → 204 直接返回；成功 → complete。
- Commit: `feat: rule pack set-rule-enabled idempotency reservation`

### Task A2: activate_pack 幂等接线 + 端点 headers
- activate reserve（指纹 pack_id；result_type=`rule_pack.pack`、result_id=pack_id）；replay → `_require_pack` 读回返回；成功 → complete；两个端点加 header。
- Commit: `feat: rule pack activate idempotency and http headers`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- activate：建包→带 key 激活→replay 同 pack/200→同 key 异 pack → 409 `idempotency_conflict`；异 key 重复激活返回 200（已 active 幂等）→不存在 pack 带 key 404 且不 complete（修复后同 key 可重试）。
- set_rule_enabled：PATCH 带 key 204→replay 204 且 idempotency_records 仅一行→同 key 异 enabled 409→不存在 rule 带 key 404→无 key 兼容。
- Commit: `feat: verify rule pack activate and set-rule-enabled idempotency over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete rule pack activate and set-rule-enabled idempotency slice`

## Plan Self-Review
- 幂等不绕过租户/授权与业务校验；replay 读取 tenant 范围。
- 失败/被拒不 complete；成功只 complete 一次；无 key 不变。
- 无 Schema：复用 idempotency_records。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL replay/conflict/状态机/无 key 全栈断言、拒绝审计与跨租户不回归、ruff/mypy 零错误、无 Secret、工作树干净。模板规则库与 permission code 化为明确延后项。
