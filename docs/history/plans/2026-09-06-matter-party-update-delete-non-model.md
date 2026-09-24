# 阶段 2 增补：Matter 参与方更新/删除 HTTP API Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 参与方此前只能 add/list。本切片补上**更新与删除**：`PATCH /api/v1/tenants/{tenant_id}/matters/{matter_id}/parties/{party_id}` 修改 display_name/kind（必填非空、长度上限、strip、版本号递增）与 `DELETE .../parties/{party_id}`（物理删除该租户 matter 下参与方行）；仓储按 `(tenant_id, matter_id, party_id)` 归属校验；双层跨租户 404 反向测试与 404 party 不存在区分。无 Schema 变更（复用既有 `tenant_matter_parties` 与 `version` 列）。

**Scope / 边界（明确延后）：**
- 只做 party 行本身 update/delete；不做团队/角色、冲突检查、批量操作、软删除历史表。
- 删除为物理删除且当前无其它表 FK 引用 party 行；法律结论/审计留痕不在本切片（Phase 2 审计切片只覆盖 dispose/review/activate）。
- PATCH 是整体可选字段合并语义：至少提供一个字段（display_name/kind），未提供字段保持不变。
- 延后：permission code 化、团队/角色、冲突检查、参与方审计登记。

**Architecture:** 复用现有 `matter_document_api.py` 编排 + `SqlAlchemyMatterRepository`：仓储新增 `update_party`（条件 UPDATE 匹配 tenant+matter+party，返回新版本投影）/`remove_party`（条件 DELETE 匹配 tenant+matter+party，rowcount 判定）；`MatterStorePort` 协议与 UoW 不变型扩展；`MatterDocumentHttpService` 暴露 `update_party`/`remove_party`（party 不存在或不属于该 matter → `MatterDocumentNotFound` 404 `matter_document_not_found`，空字段 → `MatterDocumentInvalidRequest` 422）。API 层 body 用 StrictModel 校验（strip + 非空 + ≤256 display_name/≤64 kind）。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 每新增租户范围 Repository/API 路径都有跨租户反向测试（path 层 `tenant_resource_not_found` + 资源层 `matter_document_not_found`）。
- 使用 TDD：先写失败测试观察失败，再实现最小行为，最后聚焦+广泛验证。
- 不泄漏 Secret；测试用合成数据。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py       # update_party/remove_party 编排 + 校验映射
    infrastructure/persistence/repositories/matters.py  # update_party/remove_party
    api/v1/matter_documents.py               # PATCH/DELETE 端点 + body
  tests/
    unit/test_matter_document_api_contract.py        # body 校验与错误码 contract
    integration/mysql/test_matter_party_http_api.py  # 追加 update/delete 全栈场景
```

## 里程碑 A：仓储与编排

### Task A1: 仓储 update/remove + 协议扩展
- `update_party`：校验 uuid7；`UPDATE tenant_matter_parties SET display_name=?, kind=?, version=version+1 WHERE tenant_id AND matter_id AND id`；rowcount==1 则读回返回 `MatterParty`，否则 None。
- `remove_party`：`DELETE ... WHERE tenant_id AND matter_id AND id`；返回 rowcount==1。
- Commit: `feat: matter party update and remove repository methods`

### Task A2: HTTP 服务与端点
- `MatterDocumentHttpService.update_party/remove_party`：party 不存在→404 `matter_document_not_found`；空白/超长→422 `matter_document_invalid_request`。
- `PATCH /matters/{matter_id}/parties/{party_id}`、`DELETE /matters/{matter_id}/parties/{party_id}`；path tenant 强校验。
- Commit: `feat: matter party update and remove http endpoints`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- add 2 → PATCH 改 display_name/kind 且 version 递增 → 其余字段保留 → DELETE → list 只剩 1 → 再 PATCH/DELETE 不存在 party → 404；B 租户 path/资源两层 404。
- Commit: `feat: verify matter party update and remove over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete matter party update and remove slice`

## Plan Self-Review
- 归属校验三层：path tenant → matter 属于 tenant → party 属于 (tenant, matter)。
- update 版本乐观递增（CAS by rowcount），无跨租户越权写。
- 无 Schema：复用既有列与约束；删除无 FK 依赖。
- 延后：团队/角色/冲突检查、审计留痕、permission code。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL 更新/删除全栈断言、双层跨租户反向、ruff/mypy 零错误、无 Secret、工作树干净。团队/角色与冲突检查为明确延后项。
