# 第二版向量集合与 LlamaIndex 检索实施计划

## 2026-09-21 合同上下文修正（用户已批准）

- [x] window_v2取消自动法律全文分页；受控读取只用已召回窗口ID，每文档跨查询聚合最多7个，最多选择3份法律。
- [x] 冻结产物核验、窗口与原文逐字校验、有界条文/段落补齐、重叠合并、未补齐告警；上下文显式记录范围、窗口ID、来源SHA和片段SHA。
- [x] 后端305项、工具24项、前端页面12项通过；修改代码Ruff、4源文件mypy、前端类型与构建通过。本地8001/8089已加载改动。
- [x] 复用原7次搜索的实时3份片段读取核验：153,456→12,508字符，无新增Embedding/rerank；原件及索引不变。
- [x] 用户再次授权后2026-09-21 00:11真实23页测试：7次检索/2次片段/0全文，实际Qwen请求只含相关法律段落，来源及范围核验通过；15次模型调用均stop且有usage。
- [ ] 最终合同批注验收：本次第14–15调用在第10–13页批次重复第4页旧意见，outside_batch→model_output_invalid。正常换行误判、quote错配、结构/锚点上下文过重仍需修复，详见项目现状与本地实测报告。

## 2026-09-20 合同Agent实际接入（以下为当时验证，全文策略已被上方修正覆盖）

- [x] 独立window HTTP服务复用已验收检索器，鉴权/有界同步任务/释放与关闭、固定模型、窗口及全文release链核验，11项服务专项通过。
- [x] 合同MCP适配器与runtime配置接入window_v2；摘要region传递、四阶段地域检查、最多7候选/3全文、拒绝旧集合/错误模型/串版与非候选读取，9项新接入测试通过。
- [x] 真实23页研究链验证，7次RRF＋rerank、3份第二版全文重建SHA通过，MySQL检索回执精确读回；本地8089/8001重启并验证，8002诊断服务已停止。
- [x] 后端相关323项、工具相关22项通过；修改代码Ruff和7源文件mypy通过。前端本轮未修改。
- [ ] 完整合同审阅质量与最终批注仍待解决：此次首轮审阅重复suggestion触发供应商JSON生成异常，最终failed。不得以检索接入通过代替这个验收。

用户已明确要求独立存入向量库并使用 LlamaIndex 做 RRF 和 rerank；沿用已确认的 Milvus 本地实验边界。参考 brainstorming、RAG Implementation、writing-plans 和 TDD，直接执行已授权架构，不重新讨论数据库选型。

**目标**：把核验通过的16,006份/117,976窗口导入 `blog.lawyer_windows_v2_20260920`，复用qwen3.7-text-embedding 1024维向量；提供向量/BM25各50 → LlamaIndex QueryFusionRetriever reciprocal_rerank融合50 → 云端语义模型精排Top7的可运行入口。

**边界**：旧lawyer_db和现有HTTP服务不切换；新集合只含公共法规，不能冒充多租户私有检索。来源/版本/位置/质量标记保留，法律效力保持未知。正文与embedding_text分别存储，BM25和rerank采用“法名＋正文”。来源路径中的省份用于显式地域过滤，两路同一过滤条件。7份正文覆盖警告不抹除，支持仅取无质量标记样本。

**结构**：复用tools/milvus_query项目与锁文件，增加window Extra；`legal_query/window_corpus.py`负责导入/核验/集合元数据，`window_retrieval.py`负责LlamaIndex检索与重排，`window_cli.py`为单一维护入口。配置通过文件引用现有密钥，不写入代码/日志。模型重排按已告知的推荐默认使用阿里云qwen3-rerank，已真实连通；官方SDK老接口与新模型路由不同，使用项目HTTP适配器接LlamaIndex BaseNodePostprocessor。

- [x] 合成测试先失败：数据/向量错配拒绝、质量字段保留、批次续传绑定、新旧集合隔离、RRF真实融合/去重、rerank结果索引校验和不可用拒绝。补充嵌套JSON键序导致RRF少算一路的反例、产物快照与集合重建反例。
- [x] 有界导入：逐文档读回已验证产物，每批128窗口；新集合schema绑定报告SHA，幂等upsert、完成进度在数据库确认后落盘。已有非本批集合拒绝接管；代码不提供drop/delete旧集合路径。源产物快照绑定本次冻结时每份completed摘要，不能反向冒充历史报告当时的字节证明。
- [x] 索引：COSINE dense HNSW和中文分析器BM25 sparse；全量按ID/内容/元数据/float32向量读回核对，117,976行且无额外ID，2026-09-20 20:17:41核验通过并生成ready回执。
- [x] LlamaIndex：两个BaseRetriever接Milvus，QueryFusionRetriever(num_queries=1)关闭额外LLM查询生成，显式禁用生成模型，RRF采用官方实现；BaseNodePostprocessor接语义rerank。完整保存dense/BM25/RRF/rerank阶段分数与usage，不能伪装服务失败为成功重排。真实qwen3-rerank小样本已成功，返回67tokens。
- [x] 实际导入新集合；3题真实检索验证全国民法典与湖北/武汉问题，并保存RRF和rerank对照输出。质量实测只记录结果，不冒充RAGAS通过。已更新操作说明和project-status对应行。真实运行发现SDK以chunk_id而非id返回命名主键，已按实际响应修复并回归；未改动已完成导入的代码快照。

验证：在独立 `.superpowers/venvs/window-rag` 执行 tools/milvus_query 的合成回归、Ruff与真实Milvus读回；依赖变更维护uv.lock。全量import中不累计向量，未知网络提交通过同批幂等续跑，不修改已冻结语料。

官方参考：[LlamaIndex RRF](https://developers.llamaindex.ai/python/framework/integrations/retrievers/reciprocal_rerank_fusion/)、[阿里云rerank API](https://help.aliyun.com/zh/model-studio/text-rerank-api)。

## 用户追加：RAGAS第一轮评分

按writing-plans与TDD组织本次已授权评测，沿用独立实验集合，不变更检索参数来追分。

- [x] 在检索前固定12题及来自已核验全文的法条参考；覆盖租赁、违约、格式条款、劳动、保证。问题不是从命中结果倒推，数据集冻结后再检索。
- [x] 新增单一 `tools/milvus_query/evaluate_ragas.py` 入口，独立evaluation Extra及虚拟环境；小测试先验证条文边界、跨窗口覆盖、漏评不能算完成。
- [x] 使用官方RAGAS collections `ContextPrecision`、`ContextRecall`，Qwen3.6-flash为judge、temperature=0；同题同一召回结果比较RRF Top7与精排Top7。保留包版本、数据集/代码/集合摘要、逐项评分与供应商usage，失败和未知用量不吞掉。
- [x] 完成24组上下文、48个指标后汇总；另给确定性参考条文字符覆盖率，避免只相信LLM评分。不生成答案、不报告答案忠实度；样本未由律师标注，不代表全库法律准确率或正式基准。
- [x] 更新检索说明及现状文档，交付完整逐题报告与可复跑命令。

指标依据：[ContextPrecision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)、[ContextRecall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)。

## 用户追加：四项完整评测

- 使用同一冻结12题和RRF/精排各Top7，核对原输入摘要并复用48个检索指标；不重新检索或更换参考答案。
- `evaluate_ragas_answers.py` 独立生成24份答案，不向生成模型提供reference；官方AnswerRelevancy（strictness=3、1024维embedding）及Faithfulness真实调用，输出至独立派生目录。
- 保留答案、公开评判输入输出、逐题分数、真实usage和失败记录；相同提示的三次问题生成独立采样，断点续跑复用各自缓存。
- 生成和评判均采用当前Qwen3.6-flash、temperature=0、不设置API输出token上限；四项完整矩阵核验后交付。明确同模型评判偏差、小样本和非完整合同工作流边界。

已完成：24答案、48新指标与48复用指标完整交付；独立从向量和裁判缓存复算一致，293条陈述逐一核对、verdict均0/1。报告在主批次`ragas-four-12-20260920/`；运行快照冻结保存，未为提高分数重跑或改检索。Windows57项通过，Docker56通过/1平台跳过，Ruff通过。
