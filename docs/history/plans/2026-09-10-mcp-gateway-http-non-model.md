# MCP 网关 HTTP 面（GET /platform/agent/tools + POST /tools/call）

- 日期：2026-09-10
- 类型：非模型切片（受控网关的只读/调用 HTTP 暴露；仍无外部 MCP Server）
- 状态：已完成

## 动机

受控 MCP Client Gateway 已就绪但无 HTTP 面；Agent/未来前端需要“列出可用工具
并精确调用”。本切片暴露最小面：`GET /api/v1/platform/agent/tools` 列工具
（name/description），`POST /api/v1/platform/agent/tools/call` 按精确名调用，
全部账号鉴权；校验与放行权仍在网关单一权威内，HTTP 层不执行不伪造。

## 范围

- `application/mcp_gateway.py`：`MCPClientGateway` 增公开只读 `specs()`。
- `api/v1/agent_gateway.py`（prefix /platform/agent）：GET /tools →
  list[AgentToolInfo{name,description}]；POST /tools/call 严格 body
  {tool ≤64, args 对象默认 {}} → `AgentToolCallResult{ok, output|error_code,
  error_message}`（unknown_tool/tool_not_allowed/invalid_arguments/
  handler_failure 原样透出，HTTP 200 语义化返回）；服务缺失 503
  `agent_gateway_unavailable`；`extra="forbid"`。
- `dependencies.py`：`ApplicationServices.mcp_gateway_http` +
  `_build_mcp_gateway_http_service()`（空登记器 → 自动注册内置
  meta.list_tools，无外部依赖）。
- 路由注册。

## 验证

- ruff/mypy strict（164 文件）零错；全 unit 目录 **1129 passed + 1 skipped**
  保持；
- **2 项真实 MySQL+Redis 全栈**：未认证 GET/POST 401；登录后 GET 列表含
  meta.list_tools 且带描述、调用成功输出可用工具清单、未知工具 → ok=false
  unknown_tool、多余参数 → invalid_arguments、body 未知键 → 422。

## 后续

把既有合法能力（如语料只读/检索）注册为网关工具并授予 Agent 白名单；
外部 MCP Server 兼容层（明确不在首期）。
