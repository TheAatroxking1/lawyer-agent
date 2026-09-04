# 阶段 2 增补：RiskIssue 处置 Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /tenants/{tenant_id}/risk-issues/{issue_id}/disposition` 无 Idempotency-Key，网络重试可能重复提交处置意图。接入既有幂等框架：membership scope；指纹 = issue_id/status/reason；首次成功 complete（result_type=`risk_issue`、result_id=issue_id），同 key 同指纹 replay 返回原 issue 投影（不再触发处置、不受状态机 409 干扰）；同 key 异指纹 → 409 `idempotency_conflict`；异 key 仍受状态机约束（非 open → 409 `rule_check_conflict`）；无 key 路径完全向后兼容。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做 dispose 端点幂等；rule-pack 启停/激活、`rule_checks` 其余写不在此切片。
- replay 读取经仓储 tenant 范围 `find_issue`；找不到 → `RuleCheckConflict`（不可复现原答）。
- 状态机、审计（成功/拒绝路径）、错误码语义不变。
- 延后：permission code 化、报告派生落库、启停/激活幂等化。

**Architecture:** `SqlAlchemyRuleCheckUnitOfWork` 补 `idempotency` 仓储（复用 `SqlAlchemyIdempotencyRepository`）；`RuleCheckUnitOfWorkPort` 协议加 `idempotency`；`RuleCheckHttpService` 注入可选 `IdempotencyService`，`dispose` 增加 `idempotency_key`/`now` 参数：reserve→执行→complete 同一 UoW；replay 直接返回读取的 issue。`_build_rule_check_http_service` 组合根传入共享 IdempotencyService；端点接收 `Idempotency-Key` header。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等 scope=membership+tenant；指纹只含 issue_id/status/reason；无 key 行为不变。
- 使用 TDD；跨租户反向与拒绝审计不回归。
- 不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    infrastructure/persistence/rule_check_uow.py   # 暴露 idempotency
    application/rule_check_api.py                  # dispose 幂等接线
    api/v1/rule_checks.py                          # Idempotency-Key header
    api/dependencies.py                            # builder 注入 IdempotencyService
  tests/
    integration/mysql/test_rule_check_dispose_idempotency_http_api.py  # 全栈
```

## 里程碑 A：UoW 与服务

### Task A1: rule_check UoW 暴露 idempotency + 服务接线
- UoW `__aenter__` 建 `self.idempotency = SqlAlchemyIdempotencyRepository(session)`；协议加字段。
- `RuleCheckHttpService.__init__` 加 `idempotency: IdempotencyService | None`；`dispose` 支持 reserve/replay/complete。
- Commit: `feat: rule check dispose idempotency reservation`

### Task A2: 端点与组合根
- rule_checks.py dispose 端点传 `Idempotency-Key`；dependencies builder 传共享实例。
- Commit: `feat: wire rule check dispose idempotency http`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 同 key 同指纹 dispose→replay 返回同 issue 投影且只产生一次处置；同 key 异 body → 409 `idempotency_conflict`；处置后再用异 key → 409 `rule_check_conflict`；无 key 兼容；对不存在 issue 带 key → 404（不 complete，可重试）。
- Commit: `feat: verify rule check dispose idempotency over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete rule check dispose idempotency slice`

## Plan Self-Review
- 幂等不绕过租户/授权与状态机：replay 读取 tenant 范围；非 replay 仍走条件更新与 domain 校验。
- 失败/被拒请求不 complete（回滚撤销保留行，可重试）。
- 无 Schema：复用 idempotency_records 与 risk issue 行。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL replay/conflict/状态机/无 key 全栈断言、拒绝审计不回归、ruff/mypy 零错误、无 Secret、工作树干净。启停/激活幂等化为明确延后项。
