# 检索问答 SSE 流式端点（POST /api/v1/legal/questions/stream）

- 日期：2026-09-10
- 类型：非模型切片（编排端单发、SSE 传输分阶段事件；真实逐 token 流需
  provider chat_stream，后置）
- 状态：已完成

## 动机

`POST /legal/questions` 已可用但一次性返回。spec 9.3 以 SSE 承载实时体验。
本切片新增同参数 SSE 端点，把一次检索问答编排以稳定事件序
`started → answer|error → done` 推送；鉴权/输入校验仍在开流前按普通
Problem Details 失败，运行期 provider/检索失败转为 `error` SSE 事件（携带
与 HTTP 层一致的 status/code/title），传输层保持到 `done`，绝不伪造流内容。

## 范围

- `api/v1/legal_retrieval_qa.py`：
  - `_event(name, payload)`：`event:`+`data:`(JSON, ensure_ascii=False) 序列化；
  - `_answer_event_stream(service, body)` 异步生成器（started 携带问题原文 →
    answer 事件 payload 与 `LegalRetrievalReply` 同构（refused/reason/text/
    usage/citations 白名单七字段）或 error 事件（status/code/title）→ done）；
  - `POST /legal/questions/stream`：`StreamingResponse(text/event-stream)` +
    `Cache-Control: no-cache` / `X-Accel-Buffering: no`；服务缺失仍 503
    `retrieval_qa_unavailable`（开流前）。
- 复用既有 body 严格校验、target_date 默认今日、错误映射。

## 验证

- ruff/mypy strict 零错；**10 项真实 MySQL+Redis 全栈**（原 6 + 新增 4）：
  stream 未认证 401；fake allowed → media_type `text/event-stream` 且事件序
  `started/answer/done`（answer 含 refused false、usage、citation 字段）；
  refusal no_evidence → answer 事件 refused true + citations []；fake
  ModelProviderTimeout → `error` 事件 status 504/code model_provider_timeout，
  事件序 started/error/done。
- 事件解析按 SSE 空行分块逐事件断言。

## 后续

前端消费 SSE（fetch ReadableStream 解析事件）渲染问答与引用；真实逐 token
流式体验需 `ModelProviderPort` 增 chat_stream 能力后把 `answer` 拆成
`delta` 序列；随后 Nginx 缓冲关闭已在 header 预留。
