# 检索问答 HTTP API（POST /api/v1/legal/questions）

- 日期：2026-09-10
- 类型：非模型切片（编排层已就绪；端点默认 503，装配为后续切片）
- 状态：已完成

## 动机

检索问答编排服务（legal_retrieval_qa）已就绪但无 HTTP 面。本切片新增登录
账号可用的 `POST /api/v1/legal/questions`：问题 → 有据回答（claims +
权威 citations 白名单投影）或稳定拒答，绝不伪造支持性回答；编排未装配时
稳定 503，错误映射区分 provider 超时/不可用/失败与数据集未发布。

## 范围

- `api/v1/legal_retrieval_qa.py`：
  - body 严格模型 `{question (非空 ≤4000), alias (默认 dataset_v1，
    `[a-zA-Z0-9_.-]{1,64}` 白名单), target_date? (默认今日 UTC),
    version_id?}`（`extra="forbid"`）；
  - 响应 `LegalRetrievalReply{refused, reason, text, usage?, citations[]}`；
    citation 白名单投影仅 7 字段（evidence_id/instrument_title/
    version_label/provision_no/provision_text/source_ref/dataset_version）；
  - 错误映射：服务缺失 503 `retrieval_qa_unavailable`、`LegalDatasetNotPublished`
    503 `legal_dataset_not_published`、timeout 504、unavailable 503、其余
    网关错 502 `model_provider_failure`、ValueError 422、兜底 502；
- `ApplicationServices` 增 `legal_retrieval_qa_http`（默认 None）与
  `_build_legal_retrieval_qa_http_service(settings)` 占位（返回 None，
  注明 embedding/检索配置面未就绪、不得伪造）——下一切片落真实装配；
- 编排服务微调：embed 的 model_ref/dimension 收敛为组合层默认常量
  （DEFAULT_EMBED_MODEL_REF/DEFAULT_EMBED_DIMENSION，客户端不可指定），
  claims chat 固定走 `CHAT_MODEL_REF="deepseek-chat"`，避免误把 embed
  model_ref 传给 chat 网关；
- 路由注册进 api_v1。

## 验证

- ruff + mypy（strict，162 文件）零错误；
- 既有 15 项编排单测仍绿 + **6 项真实 MySQL+Redis 全栈 HTTP**：
  未认证 401；默认装配 503 `retrieval_qa_unavailable`；fake 注入 allowed
  200（refused/reason/text/usage/citations 全字段 + 响应白名单无多键）；
  refusal no_evidence 200 稳定；provider 失败映射 504/503/503 三码；
  空白 question/坏 alias/未知键 422。

## 后续

检索问答**生产装配**（Settings 增 opensearch_url/embedding 配置面 → embed
gateway + OS 别名 + 语料仓储 + DeepSeek chat gateway 全链），SSE 流式问答，
前端问答页。
