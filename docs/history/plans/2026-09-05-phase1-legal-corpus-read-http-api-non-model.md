# 阶段 1 增补：法规语料只读 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 阶段 1 已交付 `LegalCorpusQueryPort`（`version_at` 时点查询、`provisions_for_version` 条文读取）但没有任何 HTTP 出口，后续“有据问答/证据引用”无法消费语料。本切片把**平台公共法规语料只读查询**暴露为 HTTP API：按法规标识+日期取现行版本元数据、按版本取条文列表。非模型、无 Schema 变更、无新依赖。

**Scope / 边界（明确延后）：**
- 只做**只读公共语料查询**；法规数据为平台公共事实（非租户私有），不要求 tenant 隔离；认证 = 已登录账号（`AccountSession`）可读，无需租户成员。
- 不做全文/语义检索（Embedding/OpenSearch 延后）；不做搜索词/模糊匹配；不做导出。
- 不暴露 `content_hash` 之外的内部列；条文返回 `full_text`（语料即正文，但响应大时由调用方按需用分页？本切片一次返回整版本条文，语料正文属公开数据）。
- 只返回**有出处**的版本/条文（version 元数据含 published/effective/repealed、law_number、dataset_version、source_ref——调用方可继续 Evidence Bundle 链路）。
- 延后：清单/盘点管理 API、写入/导入 API、Citation Gate 触发、租户绑定引用。

**Architecture:** 复用 `SqlAlchemyLegalCorpusRepository`（读库，无 tenant 概念，公开只读）；应用薄层透传并校验 `as_of` 为合法日期；`GET` 端点挂 `AccountSession` 认证。组合根暴露惰性 `legal_corpus_http`。路径建议：
- `GET /api/v1/legal/instruments/{instrument_id}/version?as_of=YYYY-MM-DD` → 版本元数据（无则 404）
- `GET /api/v1/legal/versions/{version_id}/provisions` → 条文数组（按 char_start）

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律数据；查询结果本身不是法律结论（无时效/效力断言之外的解释——版本元数据 `status` 如实呈现，`status_unknown` 不冒充现行）。
- 认证账号即可读（公共语料）；问题/Prompt/正文不落审计（只读查询不写审计，如需可在后续切片加只读查询审计）。
- `as_of` 参数严格解析为 ISO 日期；非法 → 422；无版本 → 404。
- 错误统一 Problem Details（稳定 code、trace_id）。
- 使用 TDD；无 Migration；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    api/v1/legal_corpus.py          # router：两个 GET
    api/v1/router.py                # include
    api/dependencies.py             # ApplicationServices 惰性 legal_corpus_http（每请求单 session 只读）
  tests/
    unit/test_legal_corpus_http_contract.py    # 参数/响应模型、as_of 校验
    integration/mysql/test_legal_corpus_http_api.py  # seed 小样本 → 时点查询/条文读取/404/422
```

## 里程碑 A：路由与组合根

### Task A1: 只读端点
- `version?as_of`：解析 ISO 日期 → `version_at` → 404 无版本 → 200 版本元数据。
- `versions/{version_id}/provisions`：`provisions_for_version` → 200 条文数组。
- 契约单测：as_of 非法 422；响应字段齐全；无版本 404。
- Commit: `feat: add legal corpus read endpoints`

### Task A2: 组合根注入
- `ApplicationServices` 加 `legal_corpus_http`（惰性、每请求单 session 只读）；端点用 `AccountSession`。
- 单测：缺失服务 503 语义；present 返回。
- Commit: `feat: expose legal corpus query to http composition`

## 里程碑 B：全栈与门禁

### Task B1: MySQL 全栈
- seed 一个 instrument + 两个不同 effective_on 的版本（含条文）→ `as_of` 前取旧版、后取新版、更早无版本 404；`provisions` 按序返回全文；认证账号可读，未认证 401。
- Commit: `feat: verify legal corpus read api over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 更新。
- Commit: `docs: complete legal corpus read api slice`

## Plan Self-Review
- 规格 8.1 法规数据读取后端先行（检索/问答延后）；9.1 REST 基础路径一致。
- 安全：公开语料无需 tenant；只读；无写入/导入路径暴露；版本状态如实呈现。
- 无 Schema：不加 Migration；复用既有仓储与领域对象。
- Placeholder：不假装检索；查询只按 instrument+as_of 或 version_id。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 全栈、as_of 时点语义、条文按序读取、404/422、未认证 401、ruff/mypy 零错误、无 Secret、工作树干净。全文检索/Embedding/问答/导出为明确延后项。
