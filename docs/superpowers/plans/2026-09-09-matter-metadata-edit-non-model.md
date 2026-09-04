# 阶段 2 增补：Matter 元数据编辑 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Matter 创建后 title/description 无任何编辑路径，律师无法修正案件名/补充描述；kind 与归属创建后不可变。本切片交付 `PATCH /api/v1/tenants/{tenant_id}/matters/{matter_id}`：body 可选 `{title?, description?}`（至少一项；title 非空白≤512、description≤若干长度、可置空）、可选 If-Match 强 ETag；仓储先锁读再 `version` 条件 UPDATE 返回新投影；path 租户强校验、跨租户双层 404、ETag 失配/版本并发 409 `matter_document_conflict`；登记 `matter.update` TENANT_USER 审计（reason=`changed`）。kind/status/归属不可经此修改。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只编辑 title/description 文本；kind、status、owner 等通过各自专门机制（status 已有切片）；owner/团队/角色另议。
- 正文长度上限：title ≤512 字符、description ≤4000 字符（strip 后）。
- 延后：批量编辑、permission code 化。

**Architecture:** 领域新增 `validate_matter_edits(title, description)`（纯校验 + strip）；仓储新增 `update_matter_metadata(context, matter_id, *, expected_version, title?, description?)`（锁读→domain→`UPDATE ... WHERE tenant+matter+version=expected`，rowcount 判定；返回 None 表示未找到/版本失配，由服务层映射）；服务 `MatterDocumentHttpService.update_matter_metadata(...)`：不存在 → 404、失配 → 409、成功登记 `matter.update` 审计；API 解析 If-Match 强 ETag、body 至少一字段、返回 `MatterSummary` + 新 ETag。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 编辑只动本租户 matter；跨租户反向测试；不泄漏 Secret。
- 使用 TDD。

## File Map

```text
backend/
  src/lawyer_agent/
    domain/matter_documents.py                    # 编辑校验
    infrastructure/persistence/repositories/matters.py  # 元数据 CAS UPDATE
    application/matter_document_api.py            # 编排 + 审计
    api/v1/matter_documents.py                    # PATCH + body + If-Match
  tests/
    integration/mysql/test_matter_update_http_api.py  # 全栈
```

## 里程碑 A：领域与仓储

### Task A1: 校验 + 仓储 CAS
- `validate_matter_edits`；`update_matter_metadata`（锁读→校验→版本 CAS）。
- Commit: `feat: matter metadata edit domain and repository`

### Task A2: 服务与端点
- 编排 + `matter.update` 审计；`PATCH /matters/{matter_id}` 端点（body/If-Match/响应 ETag）。
- Commit: `feat: matter metadata edit http endpoint`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- PATCH title/description→200 且 version 递增；只改 description 保留 title；空 title 422；置空 description 允许（显式 null）；旧 ETag 409；无 If-Match 取当前版本成功；不存在 matter 404；跨租户双层 404；审计行 reason=changed 且 kind/status 不变。
- Commit: `feat: verify matter metadata edits over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter metadata edit slice`

## Plan Self-Review
- kind/status/归属不可编辑；版本 CAS + 强 ETag 防并发覆盖。
- 编辑审计仅登记标识符与 reason；不写正文。
- 无 Schema：复用 tenant_matters 行与 version 列。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 编辑/置空/长度/ETag/404/跨租户/审计断言、ruff/mypy 零错误、无 Secret、工作树干净。owner/团队/角色、permission code 化为明确延后项。
