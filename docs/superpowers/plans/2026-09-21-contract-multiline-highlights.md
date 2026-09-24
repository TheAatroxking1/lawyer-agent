# Contract Multiline Highlights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为一条合同意见生成多个精确行级标红矩形，并在点击或触摸时只显示修改建议。

**Architecture:** 后端定位器返回有序span集合，结果模型以新增`highlights`传给前端；前端按anchor展开多个overlay并共享同一issue。指定历史run从已保存模型响应离线恢复quote和highlights，不调用外部模型。

**Tech Stack:** Python 3.12、Pydantic v2、FastAPI、Vue 3、TypeScript、Vitest、pytest。

## Global Constraints

- UTF-8；不改PDF原件，不新增Qwen/OCR/RAG调用。
- 只用`quality=verified`的原生anchor，不猜坐标。
- 保留`anchor_id`兼容旧记录；`highlights`最多200项且anchor去重有序。
- 气泡仅显示修改建议；完整依据继续保留在左侧审阅结果。
- 共享脏工作树不自动提交。

---

### Task 1: 多行定位契约

**Files:**
- Modify: `backend/src/lawyer_agent/application/contract_review/contracts.py`
- Modify: `backend/src/lawyer_agent/infrastructure/contract_review/two_pass.py`
- Modify: `backend/src/lawyer_agent/application/contract_review/gate.py`
- Test: `backend/tests/unit/test_contract_review_two_pass.py`
- Test: `backend/tests/unit/test_contract_review_gate.py`

**Interfaces:**
- Produces: `RiskIssueDraft.highlights: tuple[IssueHighlight, ...]`；每项含`block_id/anchor_id/start/end`，定位器返回覆盖完整唯一引用的有序span集合。

- [x] **Step 1: 写跨三个相邻行、单行兼容和重复文本拒绝的失败测试**
- [x] **Step 2: 运行聚焦pytest，确认因仅返回单anchor而失败**
- [x] **Step 3: 最小实现有界多anchor定位，保留完整quote并更新Gate校验**
- [x] **Step 4: 运行聚焦及合同工作流回归，确认通过**

### Task 2: 多矩形和精简建议气泡

**Files:**
- Modify: `frontend/src/api/contractReview.ts`
- Modify: `frontend/src/components/PdfContractReview.vue`
- Create: `frontend/src/components/PdfContractReview.spec.ts`

**Interfaces:**
- Consumes: `ReviewIssue.highlights?: IssueHighlight[]`，缺失时回退非空`anchor_id`。
- Produces: 每个有效anchor一个`.issue-mark`；任一标记设置同一`selected` issue。

- [x] **Step 1: 写一条意见生成三个按钮且气泡只含suggestion的失败组件测试**
- [x] **Step 2: 运行Vitest确认失败原因是单anchor展开及旧气泡内容**
- [x] **Step 3: 最小实现多overlay及“修改建议”精简气泡**
- [x] **Step 4: 运行组件、API和视图回归，执行typecheck/build**

### Task 3: 指定历史合同离线修复

**Files:**
- Create: `.superpowers/local-contract/repair-review-highlights.py`
- Create: `.superpowers/local-contract/review-highlight-repair-proof.json`
- Modify: `docs/project-status.md`

**Interfaces:**
- Consumes: 指定run、冻结模型响应、数据库旧结果、parsed_document和原件SHA。
- Produces: 事务内更新后的完整quote/highlights，以及不含秘密的前后摘要与零模型调用证明。

- [x] **Step 1: 以dry-run验证7条模型意见与数据库意见逐项身份一致并报告可定位数量**
- [x] **Step 2: 对目标run加行锁，复核租户、run、SHA和旧摘要后写入修复结果**
- [x] **Step 3: HTTP读回并确认问题/建议/法律引用不变，只有quote和定位字段按证明变化**
- [x] **Step 4: 重启本地API，在浏览器验证多行标红、点击建议及刷新恢复**

### Task 4: 收口验证

**Files:**
- Modify: `docs/project-status.md`

**Interfaces:**
- Consumes: Tasks 1–3最终源码和证明。
- Produces: 可复核的测试、静态检查和浏览器验收记录。

- [x] **Step 1: 运行相关pytest、Ruff和mypy**
- [x] **Step 2: 运行前端全部测试、typecheck和build**
- [x] **Step 3: 执行`git -c core.safecrlf=false diff --check`并检查秘密未被跟踪**
- [x] **Step 4: 更新项目现状，明确历史不可恢复记录仍显示待核对**
