# Docker 中查询法律语料

当前合同调用（2026-09-21）：window_v2流程严格两次Qwen且各含全部页图；第一次总结用作一次检索查询，RRF＋rerank返回Top7，命中来源的片段直接进入第二次一次性风险JSON。没有额外模型选文或法律全文输入。23页实测7窗口、1次片段读取、0全文读取与源SHA验证通过；意见质量仍未通过，见[现状](project-status.md)。

在仓库根目录 `C:\Users\11639\Documents\ChatGPT\律师Agent` 的PowerShell执行。Docker Desktop及原Milvus需要运行。

## 第二版滑窗：LlamaIndex RRF＋模型精排

新入口为 [window_cli.py](../tools/milvus_query/legal_query/window_cli.py)，独立集合为 `blog.lawyer_windows_v2_20260920`。原 `lawyer_db` 和下方旧 Docker HTTP/CLI 保留。本节命令使用宿主机独立环境；旧 Docker 镜像不包含 `window` Extra。

合同 Agent 已接入第二版：独立本地服务 [window_service.py](../tools/milvus_query/legal_query/window_service.py) 暴露鉴权 `/search`、`/documents/excerpts`、兼容 `/documents/read`、`/readyz`；使用现有 `window-rag` 环境，运行 [start_window_rag.ps1](../scripts/start_window_rag.ps1) 启动127.0.0.1:8089。脚本读取本机忽略配置，启动前检查ready回执，端口占用时拒绝另起实例。`deploy/secrets/contract-review.json` 设 `retrieval_backend=window_v2`、`rag_base_url=http://127.0.0.1:8089`、`corpus_root=null` 后重启合同API；参见[模板](../deploy/contract-review.config.example.json)。未配置的旧部署保留显式legacy兼容，但window_v2失败不会回退旧库。容器部署须另装window Extra并挂载release/state及所有派生目录，当前仅完成本机接入，不宣称旧Docker镜像已升级。

摘要输出region（未知为空），经受控MCP传递，两路召回使用同一地域范围。2026-09-21取消合同路径默认全文读取：跨查询累计窗口名次、每份最多7个，模型选最多3份法律后，`POST /documents/excerpts`只接收document_id和chunk_ids。服务核验同release的完成回执、chunks.jsonl和fulltext.txt，再提取窗口及有界边界条文；合并重叠、标记省略与未完整补齐。响应区分来源全文SHA与实际上下文SHA，并附原文范围，正文不混入完整法律。旧`/documents/read`仅保留兼容。质量与效力未知继续保留，MySQL证据元数据保存范围和哈希，retrieval_calls保留真实usage。

片段回放核验复用原7个检索回执，仅实时调用3次片段接口，无新增Embedding/rerank：民法典110,995→5,017字符、武汉住房租赁条例7,255→4,388、民事诉讼法35,206→3,103，范围与原文逐字及SHA通过，证据`.superpowers/local-contract/qwen-excerpts-20260921/`。随后用户授权的2026-09-21 00:11真实23页测试完成7次新检索，选择民法典与住房租赁条例，输入分别6,556/1,211字符；实际Qwen请求核验只含片段、0次全文读取，MySQL回执读回一致。15次模型均正常stop；审阅却跨批重复第4页意见，最终outside_batch/model_output_invalid，无最终批注。Qwen537,936、Embedding1,894、rerank316,997 tokens，均有usage。完整证据`.superpowers/local-contract/qwen-excerpts-live-20260921-001148/`。

2026-09-20真实23页合同接入验证：7次搜索全部为第二版集合、dense50＋BM2550→RRF50→qwen3-rerank7，四阶段地域均为全国或湖北；选读民法典、武汉市住房租赁条例、民事诉讼法，3份分页全文重建SHA核验通过，未读广东条例。7条成功回执已从租户运行MySQL记录读回。检索Embedding2,083、rerank329,481 tokens；Qwen已知93,848 tokens，审阅生成另1次服务端JSON异常usage未知。审阅仍failed，不能把检索接入成功当最终批注完成。证据在`.superpowers/local-contract/qwen-window-v2-20260920-232835/`。

检索流程：**问题向量检索50＋BM25检索50 → LlamaIndex `QueryFusionRetriever` 的 RRF 融合50 → `qwen3-rerank` 精排Top7**。`num_queries=1`，不调用生成模型扩写问题。RRF使用官方实现，模型精排通过项目适配器接入 `BaseNodePostprocessor`；模型调用失败明确报错。输出分别保留 `dense`、`bm25`、`rrf`、`hits`，以及查询Embedding和rerank的真实 `usage`。各阶段分数含义不同，不能直接比较或解释为法律正确率。

语料向量直接复用已核验的 `qwen3.7-text-embedding` 1024维产物，导入不会重新调用Embedding。窗口为1000字符、重叠200字符，实际Embedding输入、BM25字段和rerank输入均为“法律名称＋完整窗口正文”。结果保留来源文件、原件SHA、文档/窗口ID、字符位置和质量标记。`--region 湖北` 将两路同时限制在来源目录标为全国的法规和湖北法规；这是目录范围过滤，未代替法律效力、具体城市及时间适用性核验。`--clean-only` 可排除带解析质量标记的文件，默认保留并显示标记。

首次准备独立环境（已有环境可以直接运行后面的查询）：

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Get-Location) '.superpowers/venvs/window-rag'
uv sync --frozen --project tools/milvus_query --extra window --python D:/Python/python.exe
$windowPython = Join-Path (Get-Location) '.superpowers/venvs/window-rag/Scripts/python.exe'
$windowState = 'F:/律师Agent派生数据/滑动窗口第二版-20260920/milvus-window-import'
$windowConfig = Join-Path (Get-Location) 'deploy/secrets/window-rag.json'
```

配置文件 `deploy/secrets/window-rag.json` 已在本机创建并被Git忽略。其他机器从 [模板](../deploy/window-rag.config.example.json) 创建：

- `embedding_config_file` 填现有 `deploy/secrets/milvus-query.json` 的绝对路径，里面的Embedding模型和维度必须一致。
- `rerank.model` 填 `qwen3-rerank`；`endpoint` 填阿里云对应业务空间的 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks`。
- `rerank.api_key_file=null` 表示沿用Embedding Key；需要独立Key时指向另一个忽略的JSON文件，内容为 `{"api_key":"你的Key"}`。不要把Key写入命令行或源码。

查询和核对集合状态（`--state` 放在子命令前）：

```powershell
Push-Location tools/milvus_query
& $windowPython -X utf8 -m legal_query.window_cli --state $windowState status
& $windowPython -X utf8 -m legal_query.window_cli --state $windowState search '武汉租赁住房，出租人维修义务、押金退还和提前解约违约金应如何约定？' --region 湖北 --config $windowConfig --output "$windowState/武汉租赁检索.json"
# 仅比较RRF，不调用rerank；仍会调用一次查询Embedding。
& $windowPython -X utf8 -m legal_query.window_cli --state $windowState search '租赁住房维修义务' --region 湖北 --config $windowConfig --rrf-only
Pop-Location
```

`--output` 不覆盖已有文件，重复测试请改文件名。不指定该参数时在终端输出全部阶段结果。查询只在导入完成并生成 `ready.json` 后开放；status和search均核对数据库目标及Milvus集合ID，防止把旧回执用于重建后的同名集合。

导入/断点续传入口（已完成的导入通常不必重跑）：

```powershell
Push-Location tools/milvus_query
& $windowPython -X utf8 -m legal_query.window_cli --state $windowState import --report 'F:/律师Agent派生数据/滑动窗口第二版-20260920/release-verification-20260920-193203/report.json'
Pop-Location
```

导入每批128窗口，进度写入 `$windowState/progress.json`；`source-artifacts.json` 冻结每份源产物的完成回执SHA，断点绑定代码、报告、来源快照、数据库和集合ID。索引采用COSINE HNSW＋中文BM25；全部数据逐条读回比较文本、元数据和float32向量，数量一致后才写 `ready.json`。同一状态目录不能同时启动两个导入进程，也不要在导入期间修改导入器代码。

文件一致性验收不代表法规有效性或检索质量通过。第一轮RAGAS小样本结果见下节；完整源文件质量问题保留在批量验收报告中，不因入库或精排而消失。

2026-09-20实际验收：16,006份、117,976窗口已全部导入并逐条核验，回执为 `$windowState/ready.json`。Windows测试49项通过，Docker测试48项通过、1项Windows专用文件锁测试跳过；Ruff及独立复核通过。3题真实检索均完成50＋50→50→7，两路地域范围一致，精排改变了Top7顺序；旧集合读回仍为921,117条。详细输出在 `$windowState/retrieval-check-20260920-201920/`，包含3份 `case-*.json` 和 `summary.json`。维修问题首位命中民法典维修义务窗口；违约金问题含第五百八十五条的窗口由RRF第1变为精排第2，不能据此泛称重排总能提升质量。成功三题的查询Embedding108tokens、rerank98,937tokens；探针另67tokens，首次调试失败的一次Embedding usage未留存。

实现维护：[导入及核验](../tools/milvus_query/legal_query/window_corpus.py)、[两路检索/RRF/重排](../tools/milvus_query/legal_query/window_retrieval.py)。官方依据：[LlamaIndex RRF](https://developers.llamaindex.ai/python/framework/integrations/retrievers/reciprocal_rerank_fusion/)、[阿里云rerank API](https://help.aliyun.com/zh/model-studio/text-rerank-api)。

## RAGAS第一轮：12题检索对照

2026-09-20已完成24组Top7、48项评分。官方RAGAS 0.4.3，judge为Qwen3.6-flash、temperature=0；参考在检索前从本地民法典/劳动合同法全文固定，同题共享召回与相同judge判断缓存。

| 指标 | RRF Top7 | 精排 Top7 |
| --- | ---: | ---: |
| Context Precision（排序AP） | 82.64% | 95.42% |
| Context Recall（参考陈述覆盖） | 100% | 100% |
| 指定原文字符/完整法条覆盖（非RAGAS） | 100% | 100% |

排序AP提高12.78个百分点，但不是95.42%的窗口均相关；本轮每题平均相关窗口为1.25→1.33条。4题AP提升、7题不变、1题下降；下降题核心相关条文仍排第一，新增相关窗口排第五触发AP下降。题目为11题民法典、1题劳动合同法，未由律师独立标注，不能代表全库、湖北地方规范或法律答案准确率；本轮不生成答案，未测Faithfulness等答案指标。

完整报告和可核对数据：`F:/律师Agent派生数据/滑动窗口第二版-20260920/ragas-12-20260920/评测报告.md`；同目录有 `dataset.json`、`summary.json`、`comparison.json`、`retrieval/`、`scores/`、`judge-cache/` 和usage账本。已返回usage为Embedding507、rerank406,619、judge372,343 tokens；另一次重排网络失败计费未知，未计为零。

新增[评测入口](../tools/milvus_query/evaluate_ragas.py)使用独立evaluation Extra。固定langchain-community 0.3.31以兼容RAGAS 0.4.3的导入；仅评测环境安装，不变更现有服务。以下在项目根目录执行，完整重新评测会调用付费模型：

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Get-Location) '.superpowers/venvs/ragas-eval'
uv sync --frozen --project tools/milvus_query --extra window --extra evaluation --python D:/Python/python.exe
$ragasPython = Join-Path (Get-Location) '.superpowers/venvs/ragas-eval/Scripts/python.exe'
$ragasRetrievalConfig = Join-Path (Get-Location) 'deploy/secrets/window-rag.json'
$ragasJudgeConfig = Join-Path (Get-Location) 'deploy/secrets/dashscope.json'
Push-Location tools/milvus_query
& $ragasPython -X utf8 evaluate_ragas.py --state 'F:/律师Agent派生数据/滑动窗口第二版-20260920/milvus-window-import' --retrieval-config $ragasRetrievalConfig --judge-config $ragasJudgeConfig --output 'F:/律师Agent派生数据/滑动窗口第二版-20260920/ragas-next-run'
Pop-Location
```

同一输出目录有跨进程锁，只有代码、依赖锁、非秘密模型配置、数据集及集合身份相同才能续跑；模型Key轮换不进入身份摘要。参考全文与冻结manifest再核对，失败评分不能被静默丢弃。第一轮真实评分脚本保存在输出目录的 `evaluate_ragas.snapshot.py`；评分结束后已补强当前入口的缓存身份、互斥和请求开始/未知账本，因此当前入口需使用新的输出目录，不能直接覆盖第一轮。Docker test目标验证辅助函数，实际RAGAS模型调用在宿主机独立环境完成。

### 四项完整评分（2026-09-20）

同一12题、两组Top7分别真实生成答案，共24份；复用原48个检索指标，新增48个答案指标。生成和裁判均为Qwen3.6-flash、temperature=0，未发送输出token上限；生成输入不含参考答案。RAGAS 0.4.3官方AnswerRelevancy采用strictness=3及1024维Embedding，Faithfulness逐条判定上下文支持。

| 指标 | RRF Top7 | 精排Top7 |
| --- | ---: | ---: |
| Context Precision（AP） | 0.826389 | 0.954167 |
| Answer Relevancy | 0.828173 | 0.857572 |
| Faithfulness | 0.850133 | 0.880037 |
| Context Recall | 1.000000 | 1.000000 |

完整报告、24份答案、实际生成输入、裁判结构化输出、逐题分数与用量：`F:/律师Agent派生数据/滑动窗口第二版-20260920/ragas-four-12-20260920/`。独立离线复算48个答案指标一致；72次反向问题生成未合并，293条抽取陈述与判定陈述逐一核对、verdict均0/1。官方Faithfulness自身不严格校验漏评，本次另由输出目录中的`verify-ragas-four.snapshot.py`复核通过，不能仅凭summary状态推断这项核验。

本轮新增生成24次/99,592 tokens、裁判120次/246,069 tokens、Embedding48次/1,908 tokens；全部返回usage，无未知请求。这是API用量，不是货币账单；此前检索评测的用量及未知请求仍单独保留。Windows57项通过，Docker56项通过/1项平台跳过，Ruff通过。

精排后平均分提高，但违约金题Faithfulness仅0.60、格式条款题0.5833；部分回答扩展了上下文没有支持的结论，不等于每一条已被证明法律上错误。11题民法典、1题劳动合同法的小样本、同模型生成与评判不能代替律师标注、法律准确性或完整合同Agent验收。

新入口[ evaluate_ragas_answers.py](../tools/milvus_query/evaluate_ragas_answers.py)复用上节环境与配置，在`tools/milvus_query`下执行：

```powershell
& $ragasPython -X utf8 evaluate_ragas_answers.py --baseline 'F:/律师Agent派生数据/滑动窗口第二版-20260920/ragas-12-20260920' --retrieval-config $ragasRetrievalConfig --judge-config $ragasJudgeConfig --output 'F:/律师Agent派生数据/滑动窗口第二版-20260920/ragas-four-next-run'
```

## 简版：同时查看三组结果

想阅读官方示例风格的代码，打开 [simple_search.py](../tools/milvus_query/simple_search.py)。向量查询、BM25、AnnSearchRequest和RRF调用集中在这一个文件里；仅复用已有密钥读取和Embedding适配器，不导入原全集批处理脚本。

```powershell
docker compose -f deploy/milvus-query.compose.yaml run --rm --entrypoint python query simple_search.py "北京租房的时候，有什么需要注意的"
```

输出三组：`dense`（向量检索）、`bm25`（关键词检索）、`rrf`（融合排序）。每组默认展示5条，代码顶部`SHOW_TOP`可改成20或50，修改后执行下方build命令。`RECALL=100`、`CANDIDATES=50`分别控制每路召回数与融合候选数；RRF平滑参数仍为60。所有调用共用同一个问题向量，只调用一次Embedding；hybrid_search会重新执行两路检索，并非直接接收前两组Python结果列表。

返回保留SDK结构：正文和文书名称在`entity`里；`distance`在三组中分别代表COSINE、BM25、RRF分数，不能跨组直接比较数值。

2026-09-15实际复现：向量第一名是“北京市住房租赁条例”的标题块；BM25第一名是“非机动船舶海上安全航行暂行规则”第四条；RRF让两者以约1/61的分数进入前两名。说明无关船舶条文来自BM25，向量路也仍有标题块和异地法规问题。**这是拆分诊断入口，不是检索质量修复**。结果保存在`artifacts/legal-corpus/pymilvus-query/simple-beijing-rental.json`。本次19项容器单测通过。

## 直接查询

### BM25查询清理（2026-09-15）

默认对BM25输入做保守的口语套话清理，向量Embedding仍输入完整问题。例如“北京租房的时候，有什么需要注意的”变为“北京租房 住房租赁”。只移除已识别的完整套话，不全局删除“不/未/不得/需要/注意”；引号、书名号查询整句保留，公租房等复合词不补同义词。实现见[带注释的规则](../tools/milvus_query/legal_query/keywords.py)，没有LLM重写或额外模型费用。输出新增`bm25_query`，可查看实际BM25输入；纯dense为null。

```powershell
# 默认启用；在项目根目录运行
docker compose -f deploy/milvus-query.compose.yaml run --rm query hybrid "北京租房的时候，有什么需要注意的" --top-k 5
# 回到原问题BM25，保留相同RRF和候选参数
docker compose -f deploy/milvus-query.compose.yaml run --rm query hybrid "北京租房的时候，有什么需要注意的" --top-k 5 --raw-bm25
```

24题真实对照位于`artifacts/legal-corpus/pymilvus-query/bm25-cleanup-24/report.md`及`comparison.json`；两组共享每题的同一个向量，共24次Embedding。12题触发清理，9题RRF前5身份/顺序变化，另12题原查询控制的BM25/RRF前5身份和顺序均一致。北京租房的无关船舶/刑诉/议事规则退出前5，但仍有标题和外地法规；第12题未成年人保护清理后全国法退出前5，是明确的退步案例。不能把本轮视为检索质量达标。

可复现评测（首次最多24次Embedding，重复运行复用模型/维度/问题已核验的本地查询向量；结果重新生成）：

```powershell
docker compose -f deploy/milvus-query.compose.yaml run --rm -T --volume "${PWD}/artifacts/legal-corpus/pymilvus-query:/evidence" --entrypoint python query -m tests.evaluate_keywords /evidence/bm25-cleanup-24
```

标题导航、法名条号精确路由、权威地域元数据与宽泛问题拆分尚未接入本轮，后续独立实施和评测；不靠过滤掉所有无条号或短块解决标题问题。

按用户选择，当前默认使用官方Python类`RRFRanker(k=60)`；中文常称RRF Reranker，但SDK中没有`RRFReranker`这个类名。`simple_search.py`和下方hybrid命令均已接入。它与此前`Function(..., params={"reranker": "rrf", "k": 60})`采用同一种名次融合算法，不会额外调用语义精排模型；问题Embedding仍调用百炼。`k=60`是平滑参数，返回条数使用`--top-k`控制。

本次真实查询“北京租房的时候，有什么需要注意的”前5仍为船舶规则、北京住房租赁条例标题、深圳租赁安全决定、刑诉法、陕西议事规则。官方RRF接口已验证可用，质量问题仍在；结果文件`artifacts/legal-corpus/pymilvus-query/official-rrf-beijing-rental.json`保留dense/BM25/RRF三组用于对照。

```powershell
# 自然语言混合检索：一次问题Embedding + 稠密/BM25 + RRF
docker compose -f deploy/milvus-query.compose.yaml run --rm query hybrid "用人单位违法解除劳动合同，应如何支付赔偿金？" --top-k 5

# 纯关键词检索：不调用Embedding
docker compose -f deploy/milvus-query.compose.yaml run --rm query bm25 "违法解除劳动合同 赔偿金" --top-k 5

# 检查SDK连接、字段、索引、加载状态与记录数：不调用Embedding
docker compose -f deploy/milvus-query.compose.yaml run --rm query status

# 按已知文书ID与条号精确查询：不调用Embedding
docker compose -f deploy/milvus-query.compose.yaml run --rm query exact 711aa125aecd3505ddda997e1ab0ee762a34ab2084e4d0263c5f50b49e08da22 "第四条"
```

输出JSON包含文书名称、条号、正文、document_id、chunk_id、父块ID；混合结果提供`fusion_score`，单路结果提供`retrieval_score`。分数用于本次排序，不能解释为法律答案正确率。`status=empty`表示没有候选；错误返回非零退出码和稳定code，不自动切换另一种检索方式。

## 参数

默认：两路各召回100，RRF k=60，融合保留50，返回10条。`--top-k 50`可查看全部50个融合候选；参数必须满足`1 <= top-k <= candidate-k <= recall-k <= 1000`。

```powershell
# 在指定文书范围内检索；--document-id可重复，两路同时过滤
docker compose -f deploy/milvus-query.compose.yaml run --rm query hybrid "检察机关如何开展法律监督" --document-id 711aa125aecd3505ddda997e1ab0ee762a34ab2084e4d0263c5f50b49e08da22

# 对照：纯稠密检索（会调用一次Embedding）
docker compose -f deploy/milvus-query.compose.yaml run --rm query dense "检察机关如何开展法律监督" --top-k 5

# 对照：加权融合，固定稠密0.6 / BM25 0.4
docker compose -f deploy/milvus-query.compose.yaml run --rm query hybrid "检察机关如何开展法律监督" --ranker weighted --top-k 5
```

文书ID可从结果中取得，也可用[现有名称映射](document-name-map.md)查找。名称相同不代表文书版本相同。精确查询可能返回父块和子块，`truncated=true`说明达到条数限制；它不会自动拼接完整条文。

## 已安装环境与配置

- 镜像：`lawyer-milvus-query:local`，Python3.12、PyMilvus3.0.1；直接与传递依赖固定在`tools/milvus_query/uv.lock`。
- 配置：[独立Compose](../deploy/milvus-query.compose.yaml)。加入已有`milvus`网络，通过`standalone:19530`连接`blog.lawyer_db`，更换Wi-Fi通常无需更改。
- 每条命令临时运行查询容器，完成后自动删除容器；镜像保留。限制2 CPU、1 GiB内存、128进程、非root、只读根文件系统、数值库单线程，不配置GPU。
- 查询配置文件为`deploy/secrets/milvus-query.json`。本次已从现有embedding.py提取相同端点与密钥保存，不执行或import原全集脚本。后续修改API密钥/端点时更新此文件。参考[配置模板](../deploy/milvus-query.config.example.json)，模型和维度必须与现有库一致。
- 凭据仅运行时只读挂载到`/run/secrets/embedding.json`，不进入镜像或Git；无需在命令行传API Key。
- Embedding请求使用`qwen3.7-text-embedding`、1024维，HTTP等待上限30秒；Milvus查询RPC15秒。两者不等于整个命令15秒硬截止。远端拒绝访问、限流和超时均明确报错。

代码入口：[CLI](../tools/milvus_query/legal_query/__main__.py)、[检索与重排](../tools/milvus_query/legal_query/search.py)、[Embedding配置与调用](../tools/milvus_query/legal_query/config.py)。

## 修改代码后的构建与验证

```powershell
docker compose -f deploy/milvus-query.compose.yaml build query
docker compose -f deploy/milvus-query.compose.yaml config --quiet
docker build --target test -t lawyer-milvus-query:test tools/milvus_query
docker run --rm --entrypoint python lawyer-milvus-query:test -m unittest discover -s tests -q
# 只读真实SDK探针，重用现有向量，不调用Embedding
docker compose -f deploy/milvus-query.compose.yaml run --rm --entrypoint python query -m tests.smoke_live
```

本次真实SDK完成前/中/后三个样本的dense/BM25/RRF/weighted共12次检索、3次文书范围、3次精确查询、1次空范围检查；前后Strong count均921,117。另实际调用1次查询Embedding，中文问题得到50候选并展示5条；结果包含劳动合同法第四十八条和第八十七条等。证据在`artifacts/legal-corpus/pymilvus-query/`，这些是运行验证，不是法律正确率评估。

上述旧集合入口为本地只读检索CLI，本身未接语义重排模型；第二版滑窗的LlamaIndex＋模型精排见本页顶部。自然问题标注集评测、完整条文恢复、回答生成和公网应用装配仍需后续实施。

现已另提供[内网RAG HTTP服务与同事对接说明](rag-http-handoff.md)，常驻服务由`deploy/rag-http.compose.yaml`启动；CLI照常可用。正式镜像不安装TestClient依赖，完整测试使用上方test目标。
