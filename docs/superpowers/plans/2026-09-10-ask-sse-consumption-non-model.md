# 前端消费检索问答 SSE（fetch 流解析 + 阶段渲染）

- 日期：2026-09-10
- 类型：非模型切片（前端流消费；后端 `/legal/questions/stream` 已就绪）
- 状态：已完成

## 动机

后端 SSE 端点已推送 `started → answer|error → done` 稳定事件序，但前端
AskView 仍走一次性 POST。本切片让 AskView 改经 fetch ReadableStream 消费
SSE：真正“检索中 → 出文”的分阶段体验，answer/error 载荷按事件处理。

## 范围

- `lib/sse.ts`：框架无关 SSE 解析 `parseSseBlock`（event/多条 data 行以 \n
  拼接、忽略注释/未知行）与 `parseSseStream`（按空行分帧，兼容 \r\n）。
- `api/sse.ts`：`askQuestionStream(input, signal?)` → AsyncIterable
  {event,data}：POST `/legal/questions/stream`（同源 `/api/v1`、Bearer 注入、
  JSON body），`response.body` reader + TextDecoder 增量缓冲按 `\n\n` 边界
  解析 JSON；非 2xx 解析 Problem Details → 抛 ApiError；finally releaseLock。
- `AskView`：提交改为流式消费——answer 事件直接渲染（与旧一次性渲染共用
  卡片/拒答 UI），error 事件构造 ApiError 走既有引导，done 结束；
  phase=searching 期间显示“正在检索权威条文并生成回答…”。

## 验证

- `npm run typecheck` 0 错误；vitest **38/38 绿**（新增 sse 解析 7 项：
  单/多 data、注释忽略、空帧、多帧分块、\r\n、纯空白零帧）；`npm run
  build` 成功（AskView gzip 3.1 kB）。
- 真实逐 token 流（provider chat_stream 后置）时只需在流内把 answer 换成
  delta 序列即可平滑演进。

## 后续

上传下载页面、Nginx 托管入 compose；MCP 网关对 Agent 暴露与 HTTP 化。
