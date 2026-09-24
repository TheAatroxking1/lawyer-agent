# 计划：真实逐 token 流式对话 · 第 3 步（ChatView 消费 /legal/chat/stream）

## 目标

前两步已交付 provider→gateway 的文本增量流与 `/legal/chat/stream` SSE 端点。
本轮把对话页接入流式：用户发送后立刻出现助手气泡，逐 token 追加渲染，替代
「发送中…再一次性贴全文」的体验。

## 改动（frontend/）

- `api/sse.ts`：把 askQuestionStream 的 fetch/ReadableStream/SSE 分帧逻辑提取为
  通用 `postEventStream(path, payload, signal)`（非 2xx 映射 ApiError、finally
  releaseLock 不变）；原 `askQuestionStream` 变为薄封装；新增
  `chatStream(messages, signal)` → POST `/legal/chat/stream`。
- `views/ChatView.vue`：
  - 移除一次性 `endpoints.chat`/`usage`（流式无 usage，诚实不展示）；发送改为
    先入 `{role:'assistant', content:'…'}` 占位气泡，随后消费 `chatStream`：
    `delta` 事件逐段拼接并原地替换该气泡文本（每帧滚底），`error` 事件构造
    `ApiError`（status/code/title 取事件负载，缺省稳定兜底），`done` 结束。
  - 出错处理：零增量失败 → 移除占位气泡只留错误横幅；部分增量后失败 → 保留
    已生成文本并显示错误横幅（中途失败不假装完整）；401/网络错误走既有
    ApiError 引导（重新登录 / 网络提示），503 引导 DeepSeek key 三步配置不变。
  - 「正在思考…」段落删除（占位气泡承担状态提示）。

## 验证

- `npm run typecheck` 0 错误；vitest 41/41 保持；`npm run build` 分包正常。
- 真实逐 token 渲染需用户配置 DeepSeek key 后手工体验；无 key 503 语义、SSE
  事件序、错误事件携带均已有后端真实 MySQL+Redis 全栈覆盖（见第 2 步）。
- 提交：feat（sse.ts + ChatView）+ docs（本计划 + AGENTS）。

## 完成判定（三步闭环后）

- 后端：provider chat_stream → gateway chat_stream → SSE 端点（started →
  delta* | error → done）全链具备真实测试；前端 ChatView 逐 token 渲染接入。
- 对话流式化交付完成；后续按剩余轮次评估收尾（Agent 白名单收敛 / MinIO 真实
  存储原始文件下载仍为延后项；用户唯一手工步骤：DeepSeek key 配置文件）。
