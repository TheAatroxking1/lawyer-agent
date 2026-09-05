# 法规对话页面（多轮会话接入 POST /api/v1/legal/chat）

- 日期：2026-09-10
- 类型：非模型切片（界面接入既有 DeepSeek chat HTTP；真实调用需用户 key）
- 状态：已完成

## 动机

Vue 骨架的对话页只是占位说明。后端 `POST /api/v1/legal/chat`（经
ModelGateway + DeepSeek provider）已就绪，本切片把 /chat 做成**真实多轮
会话界面**：用户提问 → 带历史调用 chat → 展示回复与 token 用量；未配置
DeepSeek key（503 model_provider_unavailable）、登录失效（401）、输入/网络
错误均有明确 UI 语义，绝不静默吞错。

## 范围

- `src/api/types.ts`：`ChatMessageInput`（role system/user/assistant +
  content）、`ChatUsage`、`ChatReply{text, usage}`（镜像后端
  LegalChatReply）。
- `src/api/endpoints.ts`：`chat(client, messages)` → POST `/legal/chat`。
- `src/chat/messages.ts` 纯函数 `composeChatMessages(turns, maxTurns=12)`：
  保留最近 N 轮（按轮计数保证 user/assistant 配对完整）、滤空白；用户文本
  不改写/不截断（超长交后端稳定 422，前端 textarea 以
  `MAX_CONTENT_CHARS=4000` 前置约束）。
- `ChatView` 重写：滚动会话区（自动滚底、aria-live）、气泡（我/律师
  Agent）、Enter 发送/Shift+Enter 换行、发送中禁用与「正在思考…」、
  错误横幅显示 code+title 并提供引导（503 → JSON key 配置三步；401 → 清除
  token 跳登录）、最近回答 token 用量、清空对话、空态快捷提问示例；免责
  声明提示以现行有效法规为准并自行核验。
- HomeView 对话卡片文案同步更新。

## 验证

- `npm run typecheck` 0 错误；
- vitest 新增 5 项（messages 裁剪/顺序/空/空白 + chat 端点 body），合计
  **30/30 绿**；
- `npm run build` 成功（ChatView 分包 gzip 2.0 kB）；
- 真实 DeepSeek 回复需用户配置 `deploy/deepseek.config.json` 后手工验证
  （后端无 key 503 语义此前已由真实 MySQL+Redis 全栈覆盖）。

## 后续

SSE 流式证据检索问答编排（后端 + 前端流式渲染）、上传下载页面与端点、
MCP 白名单化工具、Nginx 托管入 compose。
