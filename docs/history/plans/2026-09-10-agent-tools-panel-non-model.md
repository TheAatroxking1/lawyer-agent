# Agent 工具面板（对接 MCP 网关 HTTP，列表 + 调用）

- 日期：2026-09-10
- 类型：非模型切片（纯前端；后端 /platform/agent 已全栈验证）
- 状态：已完成

## 动机

受控 Agent 网关 HTTP 面已就绪，前端无对应面板。本切片新增「智能工具」页：
登录账号可列出白名单工具并精确调用（当前内置 meta.list_tools），把「受控
工具分派」以可见 UI 打通，同时验证 HTTP 契约端到端。

## 范围

- `api/types`：`AgentToolInfo{name,description}`、`AgentToolCallResult{ok,
  output?, error_code?, error_message?}`、`AgentToolCallInput{tool, args?}`；
- `endpoints`：`listAgentTools`（GET /platform/agent/tools）、`callAgentTool`
  （POST /platform/agent/tools/call，args 缺省 {}）；
- `views/AgentToolsView.vue`（路由 /agent-tools 受保护，顶栏「智能工具」）：
  加载工具清单（code 名 + 描述），行内「调用」→ 展示 JSON 输出；非 ok 构造
  ApiError 走错误横幅；说明文案（白名单/Schema 校验语义）；
- 路由/导航注册。

## 验证

- `npm run typecheck` 0 错误；vitest **41/41 绿**（endpoints +1：工具列表与
  调用路径/body 含默认 args）；`npm run build` 成功。
- 真实调用已由后端 2 项 MySQL+Redis 全栈覆盖（meta 清单/成功/unknown/
  invalid）。

## 后续

把语料只读/检索注册为网关工具并授予 Agent 白名单后，本面板即可列出并调用
真实业务工具。
