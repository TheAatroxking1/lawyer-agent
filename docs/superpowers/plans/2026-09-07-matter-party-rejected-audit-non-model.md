# 阶段 2 增补：Matter 参与方拒绝路径审计 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** party 的 add/update/remove 上一切片只登记成功行；被拒尝试（目标 matter/party 不存在 404、非法请求 422、跨租户资源层 404）无审计。本切片补齐：服务层失败分支在业务 UoW 回滚后经独立短事务写 `denied`/`failure` 结构化审计（action=`matter.party.add|update|remove`、result=denied|failure、reason_code=稳定错误码 `matter_document_not_found`/`matter_document_invalid_request`、target=matter/party 引用），使「谁尝试改什么、结果如何」不变量与 dispose/review/activate 对齐。无 Schema 变更。

**Scope / 边界（明确延后）：**
- 只补 add/update/remove 的失败分支（service 层可确定目标租户与动作时）；path 层 404（租户不匹配、端点拦截）不产生审计噪音。
- 事件只写标识符与 reason_code，不写正文。
- 延后：permission code 化、团队/角色、冲突检查、IP-UA 展示、导出与保留策略。

**Architecture:** `matter_document_api.py` 三个 service 方法加 try/except 包装（与 `document_review_api`/`rule_check_api` 模式一致）：`MatterDocumentNotFound`→denied（reason=`matter_document_not_found`）、`MatterDocumentInvalidRequest`→failure（reason=`matter_document_invalid_request`）；except 内调用新 `_append_party_rejected_audit(uow_factory, ...)`（打开新短 UoW append 后提交，再 re-raise 原错误）。端点层不变。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2.0 async、asyncmy、pytest、Ruff、mypy。无新依赖、无 Migration。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景。
- 拒绝事件只在服务层能确定目标租户与动作时写；path 层 404 不写。
- 使用 TDD；跨租户反向不回归；不泄漏 Secret。

## File Map

```text
backend/
  src/lawyer_agent/
    application/matter_document_api.py        # 拒绝审计 helper + 三分支 except
  tests/
    integration/mysql/test_matter_party_http_api.py  # 追加拒绝行断言
```

## 里程碑 A：拒绝审计接线

### Task A1: service 拒绝分支
- 新增 `_append_party_rejected_audit`（fresh UoW、result=denied|failure、reason_code=错误码、target=matter_id/party_id 按可确定者）。
- add_party/update_party/remove_party 包 try/except：NotFound→denied、InvalidRequest→failure，append 后 re-raise。
- Commit: `feat: audit rejected matter party writes`

## 里程碑 B：全栈与门禁

### Task B1: 真实 MySQL 全栈
- 既有 party HTTP 全栈追加：对不存在 matter/party 的 add/update/remove → 404 且 A 或尝试方租户出现 denied `matter_document_not_found` 行；空字段（服务层可达场景）→ failure；成功行计数不变；B 资源层尝试只进 B 租户、A 看不到。
- Commit: `feat: verify rejected matter party audit rows over real mysql`

### Task B2: 全量门禁收尾
- 全量 pytest、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete rejected matter party audit slice`

## Plan Self-Review
- 成功/拒绝三态不变量与 dispose/review/activate 对齐。
- 拒绝事件独立短事务提交，不吞错误、不误提交半成品。
- 无 Schema；path 层噪音不写。

## Exit Criteria
本切片通过里程碑 B 门禁后才完成：真实 MySQL denied 行断言、成功行不回归、ruff/mypy 零错误、无 Secret、工作树干净。团队/角色/冲突检查、IP-UA、导出、保留策略为明确延后项。
