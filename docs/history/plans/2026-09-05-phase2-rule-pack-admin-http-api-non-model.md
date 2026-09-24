# 阶段 2 增补：Rule Pack 管理 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 规则引擎的「喂食端」目前是空的：Rule Pack/规则只能靠 SQL seed 建库，没有任何管理入口。本切片把 **Rule Pack 管理**交付为受控租户 HTTP API：创建/查询租户 Rule Pack（版本化、唯一激活）、向 Pack 添加规则（正则白名单、`rule_based` 证据）、启停规则、激活某版本（唯一 active，停用自动只读）。规则引擎检查/处置/报告端点已存在，本切片补齐管理闭环。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 不调用生成模型/Embedding；不做条款 AI、RAG、模板规则库。
- 不改动 RuleEngine/RiskIssue/处置/报告（读侧已交付）；本切片只做 Rule Pack 与规则的**管理写入与查询**。
- 不做规则版本的差异比对/回滚（新版本即新 Pack 行）；不做 Pack 删除（保留历史，停用即只读）。
- 上传预签名 MinIO、Idempotency-Key 化、permission code 化、审计登记均为延后项。
- 规则触发语义保持：`trigger_kind ∈ {clause_type, risk_phrase}`、`risk_level ∈ {low, medium, high}`、`pattern` 必须是合法正则（白名单，非代码执行）。

**Architecture:** Domain 提供管理纯函数与校验（`new_rule_pack_version`/`next_pack_version`/`new_pack_rule` 校验正则/枚举/evidence 语义）。仓储扩展租户范围写方法：
- `create_pack(name)` → 版本号 = 该租户同名 Pack 最大版本 +1；新 Pack `active=False`。
- `list_packs()` → 租户全部 Pack（含 active 标识）。
- `add_rule(pack_id, rule)` → 写入 Pack 下规则。
- `set_rule_enabled(pack_id, rule_id, enabled)` → 条件更新。
- `activate_pack(pack_id)` → 事务内把租户其它 active Pack 置 0、目标 Pack 置 1（同一租户最多一个 active）。
所有写按 `tenant_id` 复合键作用域；不做裸 `get_by_id`。组合根沿用既有 UoW 模式。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新第三方依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景；本切片不产生对外法律结论。
- 每请求显式租户上下文；path tenant 强校验；跨租户在 API 与仓储两层不可达（反向测试）。
- 规则 pattern 必须可编译且属白名单语义；`evidence_level` 一律 `rule_based`；不执行文档/规则内容作为代码。
- 严格 Pydantic（`extra="forbid"`）；错误统一 Problem Details（稳定 code、trace_id）。
- 使用 TDD；无新 Migration；不改已发布 Migration；不提交 Secret/.env。

## File Map

```text
backend/
  src/lawyer_agent/
    domain/rule_pack_management.py  # 管理纯函数：版本推进/新建规则校验（正则、枚举、长度）
    application/rule_pack_admin_api.py # 薄编排：UoW + 租户命令 + 稳定错误
    infrastructure/persistence/repositories/rule_pack.py  # create/list/add_rule/enable/activate
    infrastructure/persistence/rule_pack_admin_uow.py     # 每请求单 session
    api/v1/rule_packs.py             # router：POST/GET packs、POST rules、PATCH rules、POST activate
    api/v1/router.py / dependencies.py
  tests/
    unit/test_rule_pack_management.py           # 管理纯函数（版本推进/正则拒绝/证据固定）
    unit/test_rule_pack_admin_api_contract.py   # body 模型与错误码
    integration/mysql/test_rule_pack_admin_repositories.py # 仓储 MySQL 行为+激活唯一性
    integration/mysql/test_rule_pack_admin_http_api.py     # 真实 MySQL+Redis 全栈 + 跨租户反向
```

## 里程碑 A：Domain 管理纯函数与仓储写方法

### Task A1: 管理纯函数
- `next_pack_version(existing_versions) -> int`（同租户同名 Pack 最大版本 +1，空则 1）。
- `new_pack_rule(...)`：复用 `RulePackRule` 校验（合法正则、枚举、pattern ≤4KB）；证据固定 `rule_based` 不在此对象上（规则无 evidence 字段，由 RiskIssue 固定）。
- 单测：版本推进（空/1/多版本）、坏正则拒绝、未知枚举拒绝、pattern 超长拒绝。
- Commit: `feat: model rule pack management functions`

### Task A2: 仓储写方法
- `SqlAlchemyRulePackRepository` 增加租户范围写方法：`list_packs(context)`、`create_pack(context, name)`（自动版本号、inactive）、`pack_rules(context, pack_id)`、`add_rule(context, rule)`、`set_rule_enabled(context, pack_id, rule_id, enabled)`、`activate_pack(context, pack_id)`（唯一 active，事务内清理其它）。
- MySQL 集成（先失败）：同租户多次 create 版本递增；激活后旧 active 归零、目标唯一 active；跨租户互不可见；对不存在 Pack/规则操作返回 None/False。
- Commit: `feat: add rule pack admin writes`

## 里程碑 B：HTTP API

### Task B1: 路由与严格模型
- 端点：
  - `POST /tenants/{tenant_id}/rule-packs`（创建，body `{name}` → 新版本 inactive）
  - `GET /tenants/{tenant_id}/rule-packs`（列表，含 active/version）
  - `POST /tenants/{tenant_id}/rule-packs/{pack_id}/rules`（body 规则）
  - `PATCH /tenants/{tenant_id}/rule-packs/{pack_id}/rules/{rule_id}`（body `{enabled}`）
  - `POST /tenants/{tenant_id}/rule-packs/{pack_id}/activate`
- path tenant 强校验；严格模型（未知枚举/坏正则由域层在服务内校验 → 422；Pack/规则不存在/跨租户 → 404；停用状态规则不可追加等 → 409）。
- 契约单测：body 拒绝额外字段、未知 trigger/risk、空白 name、坏 JSON。
- Commit: `feat: add rule pack admin tenant http endpoints`

### Task B2: 组合根接线
- `ApplicationServices` 加惰性 `rule_pack_admin_http`（复用既有 UoW session 生命周期）；薄编排转发；缺失服务 503。
- 单测：缺失服务 503 语义；present 返回。
- Commit: `feat: expose rule pack admin to http composition`

## 里程碑 C：全栈与门禁

### Task C1: MySQL+Redis 全栈
- 真实全栈：上传 docx 之前——创建 pack（v1）→ 加规则（命中「违约金」）→ 再建同名新版本（v2，active=False）→ 激活 v2 → 列表唯一 active → 用激活包跑 Rule Check 命中 → 停用规则后 run 不再产生该规则命中（幂等/empty）→ 租户 B 访问 A 的 pack/规则 404（路径/资源两层）。
- Commit: `feat: verify rule pack admin http flow over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest（真实 MySQL）、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete rule pack admin http api slice`

## Plan Self-Review
- 规格 8.1 Rule Pack 版本化发布/规则启停后端管理部分；引擎读侧不动。
- Security：租户作用域写、正则白名单非代码执行、唯一 active 事务、跨租户反向、path 校验。
- 无 Schema：不加 Migration（表已含 name/version/active/enabled 等列与唯一约束）。
- Placeholder：不假装规则库/模板/条款 AI。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL+Redis 全栈 HTTP、跨租户反向、管理写行为（版本推进/唯一激活/启停）断言、严格模型契约、ruff/mypy 零错误、无 Secret、工作树干净。模板规则库/条款 AI/审计登记为明确未验证项。
