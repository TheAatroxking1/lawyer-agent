# 阶段 2 增补：Rule Pack 创建/加规则 Idempotency-Key Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 规格 9.1 幂等收口继续：把 Rule Pack 管理的两个**创建类**端点接入既有幂等框架——`POST /tenants/{tenant_id}/rule-packs`（建包）与 `POST /tenants/{tenant_id}/rule-packs/{pack_id}/rules`（加规则）。重复提交同 key 同指纹 → 返回首答投影（同一 pack/rule），不再创建重复行。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只做两个创建端点；`PATCH rules`（启停）与 `POST activate` 是幂等状态写（重复同样结果），不接入。
- Review/Rule Check dispose 已有状态机/条件保护（重复 409），不接入。
- 不引入新框架；复用 `IdempotencyService`/既有 UoW 的 `idempotency` 仓储。
- 延后：permission code、审计查询导出、上传预签名 MinIO。

**Architecture:** 仿 Matter/Document 创建幂等：
- `create_pack`：指纹 `name`；route `/api/v1/tenants/{tenant_id}/rule-packs`；result 存 pack_id；replay 经 `list_packs` 定位同 id 返回。
- `add_rule`：指纹 `pack_id/trigger_kind/label/pattern/risk_level/suggestion`（pattern 是规则唯一性核心，指纹含 pattern 避免同 key 异规则冲突）；route `/api/v1/tenants/{tenant_id}/rule-packs/{pack_id}/rules`；result 存 rule_id；replay 经 `rules_for_pack` 定位同 id 返回。
- 无 key 路径不变（向后兼容）。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 幂等作用域 membership+tenant；同 key 异指纹 → `idempotency_conflict`（409）；replay 投影与首答一致。
- pattern/建议为受控规则数据，指纹含其文本（pattern 属规则定义非正文敏感内容；仍不做正则执行）。
- 无 key 路径行为不变。
- 严格 Pydantic 不变；错误映射沿用框架。
- 使用 TDD；无 Migration；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/rule_pack_admin_api.py  # create_pack/add_rule 幂等编排（仿 matter_document_api）
    api/v1/rule_packs.py                # 两个端点读 Idempotency-Key header 透传
  tests/
    integration/mysql/test_rule_pack_admin_idempotency_http_api.py  # 真实 MySQL+Redis
```

## 里程碑 A：编排与 HTTP

### Task A1: create_pack/add_rule 幂等编排
- 常量 operation/result_type/route；方法加 `idempotency_key`；reserve→replay（list 定位）→execute→complete；无 key 原路径。
- 单测（MySQL 集成覆盖）：同 key 同指纹建包两次 → 同 pack_id（同版本号）；同 key 同指纹加规则两次 → 同 rule_id；异指纹 → 409。
- Commit: `feat: make rule pack creation idempotent`

### Task A2: 端点接线
- 两个 POST 端点读 `Idempotency-Key` header 透传。
- Commit: `feat: wire idempotency key to rule pack creation`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL+Redis 全栈
- 同基建：建包同 key 两次 → 同 pack_id/版本 1 仅一行；加规则同 key 两次 → 同 rule_id 仅一行；同 key 异指纹 → 409；异 key 建独立包/规则。
- Commit: `feat: verify rule pack admin idempotency over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete rule pack admin idempotency slice`

## Plan Self-Review
- 规格 9.1 创建类写操作幂等闭环扩展。
- 安全：membership scope、指纹含规则定义、replay 一致、409 冲突。
- 无 Schema；复用既有 UoW idempotency。
- 延后：Review/dispose/启停/激活幂等化（有状态保护）、上传链。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL+Redis 幂等全栈、replay 同 pack/rule id、同 key 异指纹 409、ruff/mypy 零错误、无 Secret、工作树干净。其余写端点幂等化/上传链为明确延后项。
