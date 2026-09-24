# 面试演示版合同 Agent 实施计划

> **For the agent executing this plan:** Use the `executing-plans` skill and complete the tasks in order. Keep the existing dirty worktree intact; do not reset or clean unrelated changes.

**Goal:** 将现有合同审阅整理成可重复的面试演示主线，并把动态 MCP 工具发现结果安全地呈现在审阅结果中。

**Architecture:** runtime 从动态 MCP Client 生成有界工具目录并保存到 evidence manifest；应用层通过 `ContractRunView` 投影给前端；前端在合同页提供演示模式、阶段时间线、证据卡片和工具目录。演示模式使用独立固定 fixture，不接触真实模型、RAG 或客户文件。

**Tech stack:** Python 3.12、FastAPI/Pydantic、SQLAlchemy repository、Vue 3、TypeScript、Vitest。

### Task 1: 固化工具目录公共契约

**Files:** `backend/src/lawyer_agent/application/contract_review/web.py`, `backend/src/lawyer_agent/infrastructure/contract_review/runtime.py`, `backend/src/lawyer_agent/infrastructure/contract_review/storage.py`

- [x] 先新增单元测试，验证目录字段有界、旧 manifest 缺字段可读、无 schema/凭据泄露。
- [x] 增加 `McpToolCatalogItem` 公共模型和 `mcp_tools` 字段；在 `run_view`、最终 SSE 结果和 evidence manifest 中投影。
- [x] 从动态合同 MCP 和研究 MCP 的发现结果生成稳定排序的目录快照，限制数量和字段长度。
- [x] 扩展 storage manifest 校验，拒绝未知结构与过大目录，同时保留既有 manifest 兼容性。

**Verification:** `uv run python -X utf8 -m pytest tests/unit/test_contract_review_runtime.py tests/unit/test_contract_review_storage.py tests/api/test_contract_review_web.py -q`。

### Task 2: 增加独立演示 fixture 服务

**Files:** `backend/src/lawyer_agent/application/contract_review/demo.py`, `backend/src/lawyer_agent/api/v1/contract_reviews.py`, `backend/src/lawyer_agent/api/dependencies.py`（按实际装配位置）

- [x] 先写服务测试，确认演示结果固定、`is_demo` 明确、不会调用模型/检索依赖，并返回可展示的证据和工具目录。
- [x] 实现最小的演示响应/阶段流，沿用租户鉴权和公共合同结果投影；不写入客户审阅表，不调用外部服务。
- [x] 暴露稳定的演示 HTTP/SSE 入口，错误返回 `demo_fixture_unavailable`。

**Verification:** `uv run python -X utf8 -m pytest tests/unit/test_contract_review_demo.py tests/api/test_contract_review_web.py -q`。

### Task 3: 前端演示入口和工具目录

**Files:** `frontend/src/api/contractReview.ts`, `frontend/src/views/ContractReviewView.vue`, `frontend/src/views/ContractReviewView.spec.ts`

- [x] 先写组件测试，覆盖演示按钮、阶段时间线、工具目录面板、`is_demo` 标签和证据片段渲染。
- [x] 增加 API 类型和演示 SSE 客户端；把工具目录渲染成按能力分组的安全摘要。
- [x] 在欢迎页加入演示入口，在结果页加入“查看 MCP 工具链”面板，并保留 PDF 批注和普通追问输入框。
- [x] 将演示状态与真实上传状态隔离，演示失败不清空会话历史。

**Verification:** `npm test -- --run frontend/src/views/ContractReviewView.spec.ts`（按项目 Vitest 配置调整）、`npm run typecheck`。

### Task 4: 文档、回归与浏览器验收

**Files:** `README.md`, `docs/project-status.md`, `docs/contract-review-core.md`

- [x] 补充面试演示启动步骤、演示与真实模式边界和 MCP 工具目录解释。
- [x] 运行后端合同聚焦测试、Ruff/mypy；运行前端测试、typecheck、build。
- [x] 启动/确认本地服务，并通过本地鉴权 HTTP 验收演示阶段、结果和 PDF fixture；浏览器控制连接当前不可用。
- [x] 用 `git diff --check` 检查最终差异，确认没有新增密钥、客户原件或缓存产物。

**Verification:** `uv run python -X utf8 -m pytest tests/unit/test_contract_review_*.py tests/api/test_contract_review_web.py -q`; `uv run ruff check`（修改范围）；`uv run mypy`（修改源码）；`npm test`; `npm run typecheck`; `npm run build`。
