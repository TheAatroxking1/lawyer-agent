# 2026-09-10 DeepSeek chat provider 适配器（经 ModelGateway，非模型选型）

## 目标

Model Gateway 与本地 embedding provider 已就绪；key 配置底座已交付
（`Settings.deepseek_api_key`/`deepseek_api_key_file`，用户填 JSON）。下一个
全栈闭环是**对话**：新增 DeepSeek chat provider 适配器实现项目自有
`ModelProviderPort.chat`（OpenAI 兼容 chat/completions REST，httpx 无 SDK；
注入 transport 离线可测），使 `ModelGateway.chat` 能真正调用 DeepSeek。
供应商载荷/凭据只留在此适配器（spec 7.2：模型配置记录版本/延迟/Token/
费用；不配置备用模型时供应商故障明确失败、绝不伪造 fallback——已有网关
语义）。

## 范围（只做这些）

- 新 `infrastructure/providers/deepseek.py` `DeepSeekChatProvider`：
  - 构造：`api_key`（非空，缺失/空抛 ValueError——与 embedding provider
    同风格）、可选 `base_url`（默认 `https://api.deepseek.com`）、可选
    `model_name`（默认 `deepseek-chat`）、可选 `client:
    httpx.AsyncClient`（测试注入 MockTransport；缺省按需新建并关闭）；
  - `chat(messages, timeout_seconds)`：校验 messages 非空且全为
    `ChatMessage`（非法抛 `ModelInputInvalid`）；POST
    `{base_url}/chat/completions`，Authorization Bearer api_key，body
    `{"model": model_name, "messages": [{"role","content"}...]}`；
    解析 `choices[0].message.content` 与 `usage`（prompt/completion/total，
    缺字段按 0），返回 `(text, TokenUsage)`；
  - 错误映射（稳定 code，**错误文本永不含 api_key/请求体**）：
    httpx `TimeoutException` → `ModelProviderTimeout`；非 2xx → 稳定
    `ModelProviderUnavailable`（带 HTTP 状态但不回显供应商正文）；
    JSON/结构损坏/空 content → `ModelProviderInvalidResponse`；
    其它网络/异常 → `ModelProviderUnavailable`；异常原始消息不回显。
  - `embed`/`rerank` → `ModelProviderUnavailable`（本适配器只提供 chat）。
- 不改 ModelGateway/domain/embedding provider。

## 明确不做

- 不做对话 HTTP 端点/SSE（下一片）；不做流式 chat（DeepSeek `stream` 参数
  留待 SSE 切片）；不改 key 配置底座。

## 验证

- 离线单测（新 `tests/unit/test_deepseek_provider.py`，httpx MockTransport，
  无网络）：请求形态（URL/method/headers/Bearer/body 含 model 与逐条
  messages）→ 成功解析 content+usage；空消息/非 ChatMessage 抛
  `ModelInputInvalid` 且不发请求；401/5xx → `ModelProviderUnavailable`
  （断言错误不含 key）；超时 → `ModelProviderTimeout`；坏 JSON/空
  choices/空 content → `ModelProviderInvalidResponse`；embed/rerank →
  unavailable；空 api_key 构造拒绝；错误信息不回显 key。
- 与 `ModelGateway.chat` 组合离线测试：成功经 MemoryRecorder 记 usage、
  失败记 error（复用既有 gateway 语义，扩展
  `tests/unit/test_model_gateway.py` 或用组合单测）。
- ruff/mypy 全量；全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；
  无 Secret；树干净。

## 计划自检

- spec 7.2 满足：模型调用经项目自有接口、失败稳定、无伪造 fallback；
  凭据隔离在适配器且错误不泄露。
