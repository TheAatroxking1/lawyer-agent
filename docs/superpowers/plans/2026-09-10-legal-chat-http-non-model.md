# 2026-09-10 法规对话 HTTP（chat 端点，经 ModelGateway，非 SSE）

## 目标

DeepSeek chat provider 与 key 配置底座已就绪，但对话没有任何 HTTP 入口：
用户无法把问题交给 ModelGateway→DeepSeek。本切片新增
`POST /api/v1/legal/chat`（登录账号、公共法规问答语境；非 SSE）：
请求严格模型 `{messages: [{role, content}...]}`（白名单 role、条数与长度
上限），服务经 ModelGateway.chat 调 DeepSeek provider，响应
`{text, usage}`。**key 未配置时 503 `model_provider_unavailable`**（绝不
伪造回复、绝不用假 key 跑通）；装配由 Settings.deepseek_api_key 决定。

## 范围（只做这些）

- 应用 `application/legal_chat.py`：
  - `LegalChatHttpService(gateway: ModelGateway | None)`：gateway 为 None →
    抛稳定 `LegalChatUnavailable`（503 `model_provider_unavailable`，带
    title）；否则 `chat(messages)` 委托 gateway（输入校验/计时/记录沿用，
    错误族稳定映射：unavailable/timeout/invalid_response/invalid_input）。
  - 输入校验（服务层）：messages 非空 ≤32 条、role 属
    system/user/assistant、content 非空 ≤4000 字符（超限 422
    `legal_chat_invalid_request`，与 gateway 的 ModelInputInvalid 区分并转稳定
    code）。模型名固定由装配端 provider 决定，不接受调用方任意 model_ref。
- `api/v1/legal_chat.py`：`POST /legal/chat`，Pydantic 严格 body
  （`extra="forbid"`，白名单），响应 `LegalChatReply {text, usage}`；
  AccountSession 登录可读（公共问答、非租户私有）；错误映射
  `LegalChatUnavailable → 503 model_provider_unavailable`、
  unavailable/timeout/invalid_response → 稳定 502/503/422 code。
- 装配：`ApplicationServices` 增 `legal_chat_http`；
  `application_services` 依 `settings.deepseek_api_key`：
  非空 → `ModelGateway(DeepSeekChatProvider(api_key=…), recorder=日志/内存
  recorder)`；为空 → `LegalChatHttpService(None)`（启动可用、调用 503）。
  需要 ModelCallRecorderPort 实现：新增轻量 `infrastructure/providers/
  recorder.py` `LoggingModelCallRecorder`（append → logger.info 结构化脱敏：
  model_ref/operation/status/latency_ms/usage，不含 prompt 内容）供装配与
  测试注入。
- router.py include；不改既有端点。

## 明确不做

- 不做 SSE/流式、不做证据检索/引用门禁问答编排、不做前端（后续切片）；
  真实 DeepSeek 出网调用只在用户配置 key 后运行时发生，测试用假
  gateway/provider 离线验证。

## 验证

- 离线单测：
  - `test_legal_chat_service.py`：gateway None → 503 stable；成功透传 text/
    usage；messages 空/超条/非法 role/空 content/超长 → 422 stable；
    gateway 抛 unavailable/timeout/invalid_response → 映射稳定 code；
  - `test_legal_chat_http_contract.py`：body extra=forbid、reply 往返、
    装配占位。
  - recorder：append 记录脱敏字段、不含 prompt/key。
- 真实 MySQL+Redis 全栈（无 key 路径）：登录后 POST chat → 503
  `model_provider_unavailable`；未认证 401；非法 body 422。
- 装配单测：settings 无 key → service.gateway is None（不崩溃）；
  有 key（注入 MockTransport client）→ 可通过假 transport 成功（可选）。
- ruff/mypy 全量；全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；
  无 Secret；树干净。
