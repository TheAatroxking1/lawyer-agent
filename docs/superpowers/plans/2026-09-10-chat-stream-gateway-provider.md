# 计划：真实逐 token 流式对话 · 第 1 步（provider chat_stream + ModelGateway 流式委托）

## 目标

对话/问答目前都一次性拿全量文本；用户侧聊天界面「正在思考…」期间无逐字输出。
spec 9.3 要求 SSE 流式。分三步推进，本轮只做完全离线可测的第一步：

1. **本轮**：`DeepSeekChatProvider.chat_stream`（OpenAI 兼容 `stream:true` SSE 逐行解析，
   产出文本增量）+ `ModelGateway.chat_stream`（与 chat 同白名单/错误映射/调用记录，
   流结束才记 success，中途失败记 error）。
2. 后续：`POST /api/v1/legal/chat/stream` SSE 端点（started → delta… → done）。
3. 后续：`ChatView` 消费流逐 token 渲染。

## 契约设计

- `domain/model_gateway.py`：`ModelOperation` 增 `CHAT_STREAM = "chat_stream"`。
- provider 契约采用**能力探测**而非强加方法到主 Protocol：`ModelGateway.chat_stream`
  在运行期检查 `provider.chat_stream` 可调用，否则抛
  `ModelProviderUnavailable("chat streaming is not supported ...")`——
  既有 embed/rerank provider 与测试 fake 无需改动即可继续实现主协议。
- provider `chat_stream(*, messages, timeout_seconds) -> AsyncIterator[str]`：
  - body 与 `chat` 同构，仅 `stream:true`；非 2xx → `ModelProviderUnavailable`（只带
    HTTP 状态，不回显 key/正文）；httpx 超时 → `ModelProviderTimeout`；坏 data 行
    JSON/缺 choices → `ModelProviderInvalidResponse`（绝不静默吞帧）。
  - 只认 `data: ` 行（忽略事件/注释/空行），`[DONE]` 正常收尾；取
    `choices[0].delta.content`，空/缺省/role-only 块产出空串（跳过），非空才 yield。
- gateway `chat_stream(*, model_ref, messages) -> AsyncIterator[str]`：
  - 与 `chat` 相同输入校验（空消息/非强类型 → `ModelInputInvalid` 且不发请求）。
  - 每段增量校验非空 str；流正常结束但零增量 → `ModelProviderInvalidResponse`
    （与 chat 空文本同语义，不伪造空回复）。
  - 中途 provider 故障 → 记 error 并稳定映射（typed error 原样、TimeoutError →
    `ModelProviderTimeout`、其余 → `ModelProviderUnavailable`）；正常结束 → 记
    success（usage 未知，记 None——流式 token 计量不在本轮承诺范围，如实缺省）。
  - 全程结构化脱敏日志沿用 recorder，无 prompt/key/正文。
- 流式 usage：DeepSeek 流式尾部 usage 字段**不依赖**（各家行为不一致），success 记录
  usage=None，诚实缺省；后续如有需要再扩展 done 事件带 usage。

## 验证

- ruff + mypy strict 零错；全 unit 保持 green。
- 新增单测：
  - provider：请求形态（stream true/Bearer/body）、多增量拼接、role-only/空 delta
    跳过、`[DONE]` 收尾、401 不泄 key、超时映射、坏 data 行 JSON → invalid、空/非
    强类型消息不发请求。
  - gateway：成功转发增量且记录 CHAT_STREAM success（usage None）、无流能力 provider
    → unavailable、空流 → invalid response、首段前失败与中途失败均记 error 并稳定
    映射、输入校验族零调用。
- 提交：feat（domain+gateway+provider）+ test + docs（本计划 + AGENTS）。
