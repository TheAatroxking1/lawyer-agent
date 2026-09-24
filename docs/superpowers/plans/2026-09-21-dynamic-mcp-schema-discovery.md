# MCP 动态 Schema 发现实施计划

> **目标：** MCP Server 成为工具名称、说明、输入 Schema、输出 Schema 与能力元数据的唯一事实源；客户端不再保存工具专用 Schema 注册表。

**范围：** 合同文档 MCP、旧法律 MCP、第二版法律研究 MCP 及其客户端适配器。租户授权、可信 Server 装配、调用预算、缓存、结果大小限制和审计继续独立于 Schema。

**技术路线：** Server 继续用带类型的 `@server.tool()` 生成 JSON Schema，并通过 MCP `meta` 声明能力和是否对 Agent 可见。Client 在连接时调用 `list_tools`，动态构建 LlamaIndex `FunctionTool`，以发现到的 JSON Schema 校验输入输出；业务层只保留对消费结果的领域模型校验。

---

## 任务 1：用测试定义动态发现契约

**文件：**
- 修改：`backend/tests/unit/test_contract_review_tools.py`
- 修改：`backend/tests/unit/test_contract_review_mcp.py`
- 修改：`backend/tests/unit/test_contract_review_research.py`

1. 增加“Server 新增工具后客户端无需注册即可发现”的失败测试。
2. 增加 Server 描述、输入/输出 Schema 和能力元数据被客户端采用的测试。
3. 增加内部工具不暴露、未知工具不可调用、输出不符合动态 Schema 时拒绝的测试。
4. 增加第二版法律研究工具按 capability 发现，即使工具名称变化也可调用的测试。

## 任务 2：让 Server 完整声明工具目录

**文件：**
- 修改：`backend/src/lawyer_agent/infrastructure/contract_review/mcp_servers.py`
- 修改：`backend/src/lawyer_agent/infrastructure/contract_review/legal_documents.py`

1. 为每个 MCP 工具增加命名空间化 `meta`：能力标识和 Agent 可见性。
2. 移除依赖客户端共享 `INPUTS` 的参数守卫；只保留通用信封/字节上限，具体输入由 MCP SDK 根据 Server 函数类型校验。
3. 验证 `list_tools` 返回完整描述、输入 Schema、输出 Schema 和元数据。

## 任务 3：实现通用动态 MCP Client Gateway

**文件：**
- 修改：`backend/src/lawyer_agent/infrastructure/contract_review/tools.py`
- 删除：`backend/src/lawyer_agent/infrastructure/contract_review/tool_contracts.py`
- 可能修改：`backend/pyproject.toml`、`backend/uv.lock`

1. `discover()` 从固定可信 Client 调用 `list_tools`，拒绝重复名称和缺失 Schema。
2. 使用 Server Schema 动态创建 LlamaIndex 工具，不再比较本地 Pydantic Schema。
3. 调用时只允许本次发现且可见的工具；输入、输出按发现到的 JSON Schema 校验。
4. 保留预算、缓存、上下文上限和 Server 端权威授权。
5. 删除 `INPUTS`、`OUTPUTS`、`AGENT_TOOLS` 以及所有引用。

## 任务 4：第二版法律研究按能力发现

**文件：**
- 修改：`backend/src/lawyer_agent/infrastructure/contract_review/research.py`
- 修改：`backend/src/lawyer_agent/infrastructure/contract_review/runtime.py`

1. `MCPResearchTools.discover()` 从 Server 元数据解析 `legal.search_documents` 与 `legal.read_document` 能力。
2. 启动时先发现后使用，工具名不再写死为客户端默认值。
3. 调用输入输出仍按 Server Schema 校验；领域模型只负责把结果转换为合同研究对象。

## 任务 5：验证和文档

**文件：**
- 修改：`docs/contract-review-core.md`
- 修改：`docs/project-status.md`

1. 运行合同 MCP、研究、runtime 聚焦测试并确认先红后绿。
2. 运行修改文件 Ruff、mypy；再运行合同审阅单元测试集合。
3. 更新维护文档，明确新增工具只修改可信 MCP Server；Client 权限和预算仍独立执行。
4. 检查 `git diff --check`、最终 diff 和秘密扫描，不提交或打印密钥。
