# 证据检索问答编排服务（非流，spec 6.5 管道）

- 日期：2026-09-10
- 类型：非模型切片（编排胶水；真实模型调用发生在装配后，需用户 key）
- 状态：已完成

## 动机

检索（dataset alias → 混合检索 → EvidenceBundle）、claims 解析、
ClaimGate、CitationGate、DeepSeek chat 均已就绪，但缺把「问题 → 有据回答或
安全拒答」串起来的单一入口。本切片新增应用编排服务，一次 chat 调用产出
结构化 claims[] 并经引用门禁核验后才放行；拒答（无证据/解析失败/引用未通过
核验）是带稳定 reason 的正常结果，绝不伪造支持性回答。provider 故障不并入
拒答：以类型化 `ModelGatewayError` 上抛，供 HTTP 层区分 5xx。

## 范围

- `application/legal_retrieval_qa.py`：
  - 纯函数 `build_claims_system_prompt(items, budget_chars=12000,
    per_item_cap_chars=3000)`：按条目顺序编号（`[1]（依据编号 uuid）…`），
    超长条文显式截断加「…（条文过长已截断…）」标记并说明核验以编号对应
    权威条文为准；总预算截断；空预算/坏参数稳定 ValueError。
  - `LegalRetrievalAnswer{text, refused, reason, claims, citations, usage}`
    （citations 为 EvidenceItem 元组供 HTTP 白名单投影）。
  - `LegalRetrievalQaService(dataset_evidence, chat)` `.answer(alias,
    question, model_ref, dimension, target_date, version_id?, limit=12,
    bm25_size=80, knn_size=80)`：检索空 → 拒答 `no_evidence`（不调模型）；
    有据 → system(user) 消息 → chat → `parse_claims_text`（失败 → 拒答
    `claim_parse_failed`，保留 usage）→ `LegalClaimGate(bundle,
    CitationGate()).verify(target_date)` 不通过 → 拒答（reason 原样透传
    `claim_not_supported:<code>`）；通过 → allowed claims 按序编号成 text、
    citations 按 claims 首次引用去重保序；usage 透传。
- 默认数据集别名 `dataset_v1`、拒答文本为确定性中文（no_evidence /
  claim_parse_failed / claim_not_supported 通用措辞）。

## 验证

- 15 项离线单测：allowed 全链（编号 text/claims/citations/usage/消息
  system+user/检索 query 透传）、解析 JSON、no_evidence 不调 chat、解析
  失败安全拒答、out-of-bundle/未授权/生效前/已废止/状态未知五类拒答、
  多 claim 任一失败整体拒答、网关错误上抛、非法输入 ValueError 族、
  prompt 编号顺序/逐条截断标记/总预算截断/坏参数拒绝。
- ruff + mypy（strict）零错误；全 unit 目录 **1105 passed + 1 skipped**。
- 后端不改 Schema/DB；无真实 MySQL/OS 依赖（全 fake 端口 + 真 parse/gate）。

## 后续

检索问答 HTTP 端点与 SSE 流、装配（embedding gateway + 数据集别名 +
DeepSeek claims provider）、前端流式问答界面。
