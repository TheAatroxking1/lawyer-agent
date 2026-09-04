# 阶段 2 增补：Matter 参与方利益冲突只读检查 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** spec 8.4「Conflict Check：在分派律师前由授权人员按当事方执行利益冲突检查；结果只回答是否存在冲突及处置状态，不向无权用户泄露其他保密 Matter」。本切片交付其**只读核心**：租户内授权成员提交一个当事方标识（display_name，可选 kind），系统只回答该当事方是否出现在**其它 Matter** 中及命中数；**不返回其它 Matter 的名称/ID/任何内容**，避免向无权者泄露保密 Matter。返回稳定 Problem Details（matter 不存在 404、非法输入 422）。无 Schema 变更（复用 `tenant_matter_parties`）。

**Scope / 边界（明确延后）：**
- 只做存在性只读检查；处置状态记录、冲突登记表、分派前强制门槛、权限 code 化均延后。
- 检查按 display_name（+kind 可选，均精确匹配、strip）在同租户内查询；排除调用方指定的当前 matter（可选参数 `exclude_matter_id`）。
- 响应不含其它 Matter 的 id/名称/文件等任何标识。
- 延后：跨租户冲突（不应存在）、处置工作流、正式 Conflict Register。

**Architecture:** `SqlAlchemyMatterRepository` 新增租户范围只读查询：按 tenant_id + display_name（+可选 kind + 可选排除 matter）`SELECT COUNT(DISTINCT matter_id)` 于 `tenant_matter_parties`；服务层 `MatterDocumentHttpService` 暴露 `check_party_conflicts(context, display_name, kind, exclude_matter_id)` → 领域投影 `PartyConflictCheck(conflict, other_matter_count)`；API `POST /api/v1/tenants/{tenant_id}/matter-party-conflict-checks`（严格模型，path tenant 强校验）。仅返回布尔与计数。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 不泄漏其它 Matter 内容/标识；租户隔离为硬边界（B 租户查询绝不含 A 数据）。
- 使用 TDD；跨租户反向测试；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py          # check_party_conflicts 编排
    infrastructure/persistence/repositories/matters.py  # count 查询
    domain/matter_documents.py                  # PartyConflictCheck 领域投影
    api/v1/matter_documents.py                  # POST 端点 + body
  tests/
    unit/test_matter_document_api_contract.py   # body/错误契约
    integration/mysql/test_matter_conflict_check_http_api.py  # 全栈
```

## 里程碑 A：领域与仓储

### Task A1: 领域投影 + 仓储计数
- `PartyConflictCheck` dataclass；`SqlAlchemyMatterRepository.count_party_other_matters(...)`（tenant 范围、精确匹配、排除可选 matter，无内容回读）。
- Commit: `feat: party conflict existence query`

### Task A2: 服务编排 + 端点
- service 校验输入并映射 404/422；`POST /tenants/{tenant_id}/matter-party-conflict-checks` 返回 `{conflict, other_matter_count}`。
- Commit: `feat: matter party conflict check http endpoint`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 同租户 A：matter1 有 甲公司/乙公司，matter2 有 甲公司 → 查 甲公司 count=1/conflict；查 乙公司 conflict=false；排除 matter2 查 甲公司 → conflict=false；非法空 display_name 422；租户 B 带 A 的 id/path 双层 404；响应不含其它 matter 标识。
- Commit: `feat: verify matter party conflict check over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter party conflict check slice`

## Plan Self-Review
- 最小信息返回（仅 boolean+count），无保密 Matter 内容/标识外泄。
- 租户范围 COUNT 查询 + path 层强校验 + 跨租户反向测试。
- 无 Schema；无模型依赖。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 冲突/非冲突/排除/非法/跨租户断言、ruff/mypy 零错误、无 Secret、工作树干净。处置工作流、Conflict Register、强制门槛与 permission code 化为明确延后项。
