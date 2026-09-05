# 计划：真实逐 token 流式对话 · 第 2 步（/legal/chat/stream SSE 端点）

## 目标

第 1 步已交付 `ModelGateway.chat_stream`/`DeepSeekChatProvider.chat_stream`
（文本增量流）。本轮加 HTTP SSE 面，使对话页能逐 token 收到回答：

- `POST /api/v1/legal/chat/stream`（登录账号、公共问答），body 与 `/legal/chat`
  同一 `LegalChatBody` 严格契约；鉴权与输入校验仍在开流前普通 Problem Details。
- 事件序与检索问答 SSE 一致：`started → (delta*) | error → done`；每个 `delta`
  事件 `data: {"text": <增量>}`；运行期网关故障（timeout/provider）转 `error`
  事件携带与 HTTP 层一致 `status/code/title`；配置缺失（无 key）开流前 503
  `model_provider_unavailable`（与 `/chat` 相同语义，不伪造流）。
- 已发出的部分增量不可撤回：中途失败在部分 `delta` 之后补 `error` 事件宣告不完整。

## 改动

- `application/legal_chat.py`：`LegalChatHttpService` 增 `gateway_available`
  只读属性与 `chat_stream(messages) -> AsyncIterator[str]`——先 `_validate_messages`，
  无 gateway → `LegalChatUnavailable`；能力探测 `gateway.chat_stream`（无 →
  `LegalChatProviderFailure`）；中途错误同 `chat` 映射（timeout → 504
  `model_provider_timeout`、unavailable/invalid → 502 `model_provider_failure`）。
- `api/v1/legal_chat.py`：`_event` 序列化 + `_chat_delta_stream` 生成器
  （started → delta* → done，异常段转 error 事件）+ `/chat/stream` 端点
  （service 缺失或 `gateway_available=False` → 开流前 503）。

## 验证

- ruff + mypy strict 零错；全 unit 保持 green。
- 服务单测扩展：chat_stream 委托透传 model_ref/messages、无 gateway 503、
  无流能力网关 → `LegalChatProviderFailure`（零 yield）、中途 timeout/provider
  稳定映射、消息校验族零调用。
- 真实 MySQL+Redis 全栈扩展：stream 未认证 401、无 key 开流前 503 Problem、
  坏 role/空 messages 422、注入 FakeChatStreamService → 事件序
  started/delta/delta/done 且 delta 文本正确、注入中途抛 `LegalChatTimeout` →
  started/delta/error(504 `model_provider_timeout`)/done。
- 提交：feat + test + docs（本计划 + AGENTS）。
