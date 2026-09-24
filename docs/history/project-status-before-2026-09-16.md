# 项目现状与续做入口

## 如何使用

这是当前实现与交接入口，不是新的产品规格。后续每次交付更新对应能力、验证结果及下一步；长期规则只维护在 [AGENTS.md](../../AGENTS.md)。

初始核验基线：`master`，`33064c2`（结构化层级分块单测）。开始时仅 `AGENTS.md` 有未提交修改，新增内容是集合发布试点和 S1 记录，已完整保留在 [历史阶段归档](agents-stage-history.md)。初始盘点仅整理文档。

随后用户明确要求持续完成项目；当前分支为 `codex/project-completion`，以下新增实现均保留在工作区，未提交 Git。数据库集成验证使用合成数据与隔离测试库，真实语料只读转换与文件导出单独记录；没有重建现有业务库或切换 `dataset_v1`，没有调用付费模型。

历史计划的日期标签包含 `2026-09-10`；判断先后与完成度应依据实际内容、提交关系和下述证据，不按文件名日期推断当前状态。

## 当前结论

2026-09-16 API Key 配置设计：按用户指定教程路径找到实际 `course_core/config/load_key.py` 并仅静态查看加载结构。新增[集中加载设计与填写步骤](../superpowers/specs/2026-09-16-agent-api-key-design.md)：阿里云 Key 拟存 `deploy/secrets/dashscope.json`，字段 `DASHSCOPE_API_KEY`；文件模式与环境变量模式互斥，Provider 显式注入，保留现有 DeepSeek 配置。已说明手工创建和填写方式；本次为设计文档，尚未实现 DashScope 加载器/Provider 或配置检查 CLI，未创建/复制真实 Key、执行教程或调用模型。下一步按该契约实现加载器及离线校验，再完成模型适配装配。

2026-09-16 合同审查核心代码框架已实现：自定义 LlamaIndex 四步 ReAct Workflow，文档/法律两个 MCP SDK 服务共七工具，固定 Schema、可信身份注入、逐调用授权及预算；强制原文/位置/全篇覆盖校验、每条意见内容策略及保存阶段。模型复用既有 ModelGateway，独立 `contract-review` Extra 与锁文件安装；无默认供应商。代码入口和装配见[核心框架说明](../contract-review-core.md)，计划见[实施计划](../superpowers/plans/2026-09-16-contract-workflow-core.md)。最终35项新增测试、真实 SDK 合成闭环、Ruff、mypy236源码通过，独立复审关闭五项问题。全后端回归3116通过/3失败/1平台skip；三项既有缓存Junction路径测试失败在原环境复现，详见运行说明，未放宽路径保护。尚未接入真实解析/法规服务/生产内容策略/数据库/网页，未调用付费模型或提交Git；下一切片为DocumentService的版面与可靠锚点适配。以下同日设计记录为该实现前历史状态。

2026-09-16 合同审查工作流骨架已按用户附件固定为自定义 `ReActAgent(Workflow)`，保留 `new_user_msg → prepare_chat_history → handle_llm_input → handle_tool_calls` 的 ReAct 循环，见[设计第 5.3 节](../superpowers/specs/2026-09-16-contract-web-annotations-design.md#53-用户指定-workflow-的逐步接入)。文档明确 MCP 异步接入位置及新增结果校验/持久化阶段，记录缺失事件定义、空流、原始流与 reasoning 暴露、同步工具、sources 嵌套和循环预算的修正要求。已读取附件并核对官方 Workflow 示例及现有网关；未执行附件、安装依赖或实现 Agent。下一可执行切片为该自定义工作流的隔离 TDD 骨架验证，再接真实 MCP/文档解析。

2026-09-16 合同审查技术方向更新：用户明确“react”指 **ReAct Agent**，框架采用 **LlamaIndex**，加入版面分析并通过 **MCP** 接入 Agent。已修订[网页合同批注草案](../superpowers/specs/2026-09-16-contract-web-annotations-design.md)、总体规格场景覆盖说明及 AGENTS 长期选型规则；合同场景不再沿原 LangChain Chain 方案推进。设计区分版面分析、区域文字识别和精细坐标对齐，提出 PaddleOCR-VL 与 Docling 对照评测、有界 ReAct、文档 MCP 工具、可信上下文注入与强制输出门禁。仅核对官方资料和现有代码；未安装 LlamaIndex、运行识别模型、接通真实 MCP 或改变前端框架。下一步评测中文合同的结构/文字/定位质量并细化适配器及实施计划，不能把选型方向当作已完成接入。

2026-09-16 用户明确合同 PDF 上传后在网页预览，标红与气泡均在网页查看。已形成[网页合同批注设计草案](../superpowers/specs/2026-09-16-contract-web-annotations-design.md)：固定版本原文定位、文字/扫描/混合页识别、风险气泡及人工处置；先完成 PDF，原需求中的 Word 作为相邻转换切片保留。当前仅设计草案待审阅，尚未新增实现；现有 DOCX 登记、规则风险项和报告导出不代表该能力已交付。下一步确认具体交互设计后编写实施计划；本次只核对代码/文档和官方渲染资料，未运行应用测试或调用模型。

2026-09-15 第一版离线产物已按用户授权迁移到 F 盘：`切块第一版/chunks`、`向量化第一版/api-embeddings`、`向量化第一版/vectors`、`导入前清理第一版/attu-import`。共 **380,848 个文件、49,000,436,279 字节（约 45.64 GiB）**；四组均完成源/目标逐文件 SHA-256 比对后才清理 C 盘副本，原项目路径保留 Junction。旧缓存首次慢校验的中断记录保留，随后有界并发重新完整核对全部 316,307 个缓存文件。最终独立复查全部目标文件集合/大小、摘要清单身份、12 个旧路径读回样本及四处备份不存在均通过，RAG `/healthz` 返回 200；C 盘可用空间由初始盘点约 43.83 GB 增至约 93.74 GB（前后差约 49.91 GB，包含运行期间其他进程的磁盘变化）。迁移及复查报告在 `artifacts/legal-corpus/storage-migration/`，目录和后续输出限制见[存储位置说明](../corpus-storage-locations.md)。本次不重算向量、不写 Milvus、不移动 Docker 数据卷；旧清洗/名称映射脚本的 F 盘原件根目录写保护保留。UTF-8/文档路径及 `git diff --check` 通过，未运行无关应用测试，未提交 Git。

2026-09-15 内网RAG HTTP服务已封装并实际启动：独立`deploy/rag-http.compose.yaml`绑定`192.168.31.14:8088`，同事访问`/docs`；实现健康/认证ready、`POST /api/v1/rag/search`与`POST /api/v1/rag/read`，复用现有BM25/向量/RRF和按文书+条号/分块读取。独立Bearer秘密文件、64KiB请求上限、最多2个实际外部工作、单进程每分钟30认证请求；同步SDK在线程内执行，取消不提前释放容量。证据带request_id/evidence_id、原文身份、字符预算截断标记，corpus_version/is_complete未知保留null，不冒充完整条文或引用门禁。供共享法律语料内网联调，未接入公网多租户业务或Agent回答。

最终37项容器单测、8源码严格mypy、Ruff、Compose/镜像通过；TestClient存在1项httpx弃用提示，测试未失败。真实HTTP无Key/错Key401、无效参数422、ready、BM25、hybrid、chunk/article读取及错文书空结果通过；同事标准库客户端示例已从宿主LAN地址调用成功。共2次真实Embedding，最终验证前后Strong行数均921117，容器采样约92MiB，无GPU。证据`artifacts/legal-corpus/rag-http/verification.json`；无秘密交接包`rag-http-handoff.zip`，说明[rag-http-handoff.md](../rag-http-handoff.md)。防火墙Allow规则已验证目标IP/8088与远端192.168.31.0/24，但系统Public/Private原本关闭，未更改全机开关，不能宣称网段隔离生效；后续用户已确认同事电脑连通（用户反馈；尚未单独报告同事端带认证的search/read调用结果）。下一步按接口接Agent，继续标题导航/地域元数据/完整条文及引用门禁，当前不是RAG法律质量验收。未运行无关前后端全套测试，未提交Git。

2026-09-15 BM25查询侧清理第一步已实施：`tools/milvus_query/legal_query/keywords.py`纯Python保守规则，默认接入hybrid/bm25及simple_search；向量路原问题不变，官方RRFRanker60/双路100/候选50不变，新增`--raw-bm25`对照开关与`bm25_query`输出。24题真实对照共24次Embedding，两组共享问题向量；12题触发规则、9题RRF前5变化，12题未触发控制前5身份/顺序保持一致。北京租房前5无关船舶/刑诉/议事规则消失，但仍有标题/外地法；第12题未成年人保护全国法退出前5，明确保留退步记录。报告`artifacts/legal-corpus/pymilvus-query/bm25-cleanup-24/report.md`及comparison.json，921,117行前后不变，无GPU/语义精排调用/集合写入。26项容器单测、Ruff、6源码严格mypy、Compose校验、真实SDK既有12探针/3范围/3精确/负范围通过；无需重算语料向量。下一步为标题转文书内条文检索、显式法名条号精确路由，地域需核验元数据后实现；本轮非RAG质量验收，未运行无关前后端全套测试、未提交Git。

2026-09-15 用户改为只使用官方RRF，不接语义精排API。`simple_search.py`及CLI默认hybrid现直接使用PyMilvus3.0.1的`RRFRanker(k=60)`（Python类名不是RRFReranker），保留两路各100、融合50及原过滤；weighted入口不变。两项契约先RED后GREEN，19项容器单测通过，严格mypy通过。真实北京租房问题调用1次Embedding，官方RRF前5与之前相同，仍含船舶/刑诉/陕西议事规则，检索质量未改善；三路结果见`artifacts/legal-corpus/pymilvus-query/official-rrf-beijing-rental.json`。未调用语义精排API、未使用GPU、未写集合。下一步应评测召回与BM25噪声，不能把更换等价SDK写法当作质量修复。

2026-09-15 按用户确认新增官方示例风格的[simple_search.py](../../tools/milvus_query/simple_search.py)：直接调用两次search和一次hybrid_search，分别展示dense/BM25/RRF前5条，只生成一次问题向量；密钥读取复用既有适配器。已加入Docker白名单并重建查询镜像，19项容器单测、Ruff、单文件严格mypy通过。用户“北京租房”的问题已真实复现：dense第一名为北京市住房租赁条例标题块，BM25第一名为非机动船舶航行规则，RRF前5与用户提供结果一致。证据`artifacts/legal-corpus/pymilvus-query/simple-beijing-rental.json`；本次实际Embedding调用1次。**简化完成，检索质量问题未修复**；下一步针对BM25自然语言噪声及向量标题/地域问题分别评测。运行命令见[查询说明](../milvus-query.md)。原查询工具、集合和向量保留，未提交Git。

2026-09-15 Docker PyMilvus查询已实施：用户确认在Docker安装并实现设计，新增`tools/milvus_query/legal_query/`（配置/Embedding、检索、CLI）、固定PyMilvus3.0.1及传递依赖的uv.lock、Dockerfile与[独立Compose](../../deploy/milvus-query.compose.yaml)。镜像`lawyer-milvus-query:local`已实际构建，接入现有milvus网络，非root、只读、2 CPU/1 GiB、数值库单线程，无GPU。提供status、hybrid、dense、bm25、exact命令；默认RRF60/两路100/候选50/返回10，另支持加权0.6/0.4和双路同文书过滤。复制命令见[查询说明](../milvus-query.md)。

本次**18项容器单测、Ruff、独立源码严格mypy（外部SDK导入忽略）、Compose校验与独立复审通过**；复审发现的argparse输入回显和关闭异常已补RED→GREEN回归。真实SDK3.0.1连接Milvus3.0.0，完成12次dense/BM25/RRF/weighted探针、3次文书范围、3次精确查询和1次空范围检查；最终CLI BM25/精确/status均成功，Strong count前后保持921,117。实际调用**1次**既有查询Embedding，问题“用人单位违法解除劳动合同，应如何支付赔偿金？”得到50候选，前5项含劳动合同法第四十八条、第八十七条；不据此宣称法律质量达标。

证据在`artifacts/legal-corpus/pymilvus-query/`，final-verification记录最终镜像`sha256:edfffed4ebfc476990e5d0327c001bd229685504de78cc19298fc53409cf7a57`、代码摘要和验证结果。现有API配置安全提取到Git忽略的deploy/secrets，只读挂载，未import或执行原全集脚本，未写数据库、未改变原Milvus栈或公网OpenSearch装配。下一步为自然问题人工标注质量评测与回答层接入；语义模型重排、完整条文恢复与生产证据门禁装配未因此完成。未运行无关前后端全套测试，未提交Git。

2026-09-15 PyMilvus重排设计：用户指定参考Milvus多向量搜索文档，已形成 [本地集合查询设计](../superpowers/specs/2026-09-15-milvus-pymilvus-rerank-design.md)：稠密/BM25各召回100，RRF k=60融合取50，默认展示10；采用PyMilvus的AnnSearchRequest、查询级RERANK Function和hybrid_search，返回文书名称与引用身份，文书过滤同步作用于两路。加权融合用于评测对照，语义模型重排为后续独立步骤。当前为设计建议，未安装PyMilvus、未写查询实现或调用百炼；现有REST验收不能冒充新SDK或自然问题质量验收。下一实施切片为独立本地查询模块、固定兼容SDK依赖及真实查询验证，保留现有公网OpenSearch装配。

2026-09-15 Attu名称列：按用户要求在现有`blog.lawyer_db`新增`document_name`（VarChar、nullable、1024字节，无Analyzer/分区键/聚簇键），集合ID保持`469076060698085606`。已使用 [局部回填脚本](../../scripts/backfill_milvus_document_names.py) 完成**921,117条名称回填、1,231组逐条读回，空名称0，总记录数921,117**。每组首尾原8字段与1024维向量均在更新前后核对，首批样本原字段精确不变；仅提交chunk_id和document_name，未调用Embedding或GPU。说明见 [文书名称映射](../document-name-map.md)，方案见 [名称列回填计划](plans/2026-09-15-milvus-document-name-column.md)。名称取自已核验映射的原文件名，16,006文书，不冒充封面正式标题。

相关25项单测、Ruff（backend配置）、严格mypy通过。证据目录为`artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/milvus-blog-lawyer_db/document-name-backfill/`，含Schema前后、首批样本、断点、内存监测和`final-verification.json`。写入02:58:50 UTC完成；03:05:31 UTC最终验收通过：**两类索引Finished且pendingRows=0、集合加载100%、前/中/后三份样本共9次稠密/BM25/RRF检索均命中来源且全部返回名称正确**。1,231组首尾共2,462项原字段/向量样本检查通过。索引物理计数1,642,494含upsert旧版本，不作为逻辑记录数；Strong count仍921,117。内存监测最低可用约2.50 GiB，无STOP触发。原始导入器固定旧Schema，新增列后不可绕过校验强行整行重导；名称操作使用独立回填脚本。下一步在实际RAG入口请求document_name并传给rerank/回答上下文，补自然问题质量评测；公网问答装配尚未因此完成，不把名称回填当作RAG准确率验收。未运行无关前后端全套测试，未提交Git。

2026-09-15 文书名称映射完成：用户要求先添加document_id与文书名称映射，已新增 [build_document_name_map.py](../../scripts/build_document_name_map.py)、[使用说明](../document-name-map.md)。实际输出 `artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/document-names/document_names.json`，**16,006个ID全部逐项对照通过，覆盖来源921,117块；16,004个不同名称、2组同名保留独立ID**。名称按原文件名去掉扩展名生成，保留日期/版本，不冒充封面正式标题。映射2,034,113字节，SHA-256 `426c2ad7067d521400f6c7a6df576c62bfbbeeeeb25056f95e767dc65f8d083e`；同级report记录源报告、manifest、provenance和导入目标绑定。DocumentNameMap提供lookup/find/enrich；未知ID和冲突名称拒绝，检索原记录不被修改。

本切片15项合成测试、Ruff（backend配置）、严格mypy通过；重复构建未改输出字节/mtime，Milvus前/中/后三个真实命中成功补名，数据库仍921,117条。证据 `milvus-blog-lawyer_db/document-name-map-verification.json`。本次仅生成本地派生映射和查询入口，未修改Milvus Schema或向量，Attu原集合不会自动多出名称列，问答/rerank生产装配仍待后续接入。下一步在用户选定的实际RAG查询入口使用该映射做名称回填、指定文书过滤，并补自然问题的召回与引用质量评测；不把本次映射验证当作RAG准确率验收。

2026-09-15 Milvus 全集导入完成：用户明确授权把现有 API 向量导入其 Attu 集合，本次实际目标为本机 Milvus 3.0 `blog.lawyer_db`（collectionID `469076060698085606`），初始 Strong count=0。用户启动的全集转换 `artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/` 已完成文件读回及输入一致性检查：**full ready，16,006份文档、921,117条、411文件，错误0，总20,512,181,911字节**，正式文件位于该目录 `import/`。报告SHA-256 `82e07a678678f69db2a2a9d1f7cfdf0079bf1bf169cbce960b755d72117f19ca`。

新增 [import_attu_jsonl.py](../../scripts/import_attu_jsonl.py) 及 [操作说明](../milvus-jsonl-import.md)：固定目标、集合共享锁、每256条upsert、来源SHA核对、pending批次登记和不确定写入回读、确认主键/数量及每文件首尾核验。**16项合成测试、Ruff（backend配置）、严格mypy及差异检查通过**，独立复审的两项接续/锁阻断已关闭。先写768条并回读3个请求共6个首尾样本，再接续全集，**共确认3,693个写入请求；411个文件首尾822项回读通过；最终Strong count=921,117**。北京时间01:11启动首批、01:46:45结束写入，状态data_verified。Flush成功后两索引均Finished、indexedRows=totalRows=921,117、pendingRows=0，集合加载100%；前/中/后三个文件额外回读及稠密、BM25、RRF混合检索共9个真实探针全部命中来源。

证据保存在上述目录的 `milvus-blog-lawyer_db/`：`state.json`、`smoke-verification.json`、`flush.json`、`final-verification.json`、导入前后Schema及资源采样。保留原8个输入字段，中文BM25 Function自动生成sparse_vector；未调用百炼、未使用GPU、未更改Schema、未修改外部原件。**本次完成Milvus存储/索引/检索入口验证；原项目MySQL/OpenSearch生产发布、问答入口接入此集合、追溯元数据回填及法律质量评测仍未因此完成。** 下一步按用户选定检索方案处理应用接入与端到端问答验收；不把离线ID冒充旧事实库ID。未提交Git，未运行无关后端/前端全套测试。

2026-09-14 Attu 导入准备：用户当前选择百炼 `qwen3.7-text-embedding` / 1024 维，已在 Attu 创建 `lawyer_db`（8 个输入字段，另有 BM25 Function 输出 `sparse_vector`）。新增带中文注释的独立标准库脚本 [prepare_attu_import.py](../../scripts/prepare_attu_import.py) 及 [运行说明](../attu-import-preparation.md)，按用户批准方案展开向量文件、补文档身份、原文/摘要/父子关系核对、全局 SQLite ID 去重、有界 JSONL 分批及独立追溯文件。保留页码块，不重切块、不改变正文/ID/向量，不写外部原件、不调用 API、不操作数据库。

本次实际目录盘点 **16,006/16,006 份 API 向量文件，缺失0/额外0，总约20.237 GiB，最大38.36 MiB**；这只证明文件清单齐全，未验证全集向量内容。最终脚本真实前3份/1,129块 check-only 与 JSONL 转换、原文件摘要核对及输出独立逐条比较均通过；1 MB测试分批共26文件，最大999,636字节。报告 `artifacts/legal-corpus/attu-import/20260914T142217Z-474c4c15/report.json`（sample_checked）与 `20260914T142219Z-f4937171/report.json`（sample_ready），均明确为样本。默认正式分批上限50 MB，不采用测试的1 MB。

本切片 **30项合成测试、Ruff（backend配置）、独立脚本严格mypy、git diff --check通过**。复审发现的关闭失败、最终报告失败/中断发布、额外字段覆盖追溯身份问题已补RED→GREEN回归，独立故障复查通过、三项阻断关闭。没有运行后端/前端全套或真实 Milvus 导入，因为本次只改独立文件转换工具；项目生产发布状态仍以前序事实为准。下一步由用户运行 `python -X utf8 scripts/prepare_attu_import.py` 做全集校验和分批；只有报告ready且import目录存在时上传该目录内JSONL，失败不上传暂存目录。未替用户执行全集转换、导入或重新embedding，未提交Git。

2026-09-07 15:37 更新：**当前 v3/v4/v5 管线已入库 15,995/16,006，剩 11 份；全集向量化及发布仍未完成。** 四川省消毒管理条例的重复“第”字款项引用通过新 `corpus-docx-v5` 正确保留在第六条，真实原件解析由31条恢复为30条。新增身份接通精确导入、分块和质量摘要；v4及旧内容摘要不变。批次 `artifacts/legal-corpus/full-release/v5-sichuan-20260907T072625Z/` 实际 import=1、replay=1，30条/30块/30叶；真实 MySQL 全文与原件准备、叶块逐字符覆盖、重放事实及ID均通过，原件SHA未改。`verified-import.json` 为交付证据；含旧profile的历史去重计数是15,996，不能与当前管线计数混用。

固定 v5 运行时 `.superpowers/frozen/corpus-v5-20260907T072712Z/runtime.json`，SHA `f684c34c5c7ebb3de5eaccbe22746a02bd22cb6476c781600131590405a366ef`。新14完整报告/75显式替换集合 `full-release/release-set-mixed-15995-20260907.json`（SHA `e7943ffb0d21ad151b77de2cdea55abeecc9ae16838f5ffbad24f09dd8814914`）已实际统一质量核验：**15,960无机器阻断、35仍仅article_sequence_invalid**，`v5-sichuan-20260907T072625Z/mixed-quality.jsonl` 质量摘要 `f01b162e2f934829b988717d45702d41ebc8c18ed7d7f5310fcab5119566f7c3`，父报告仍passed=false；`mixed-quality-comparison.json`证明旧15,994项summary和facts摘要全部不变。空日期保留null，既有用户现行审核/日期政策继续适用，不重复询问。

合格补算范围扩大为 **96版本（86 v4 + 9 v3 + 1 v5）、3,627叶**；`full-release/mixed-vector-coverage-20260907T073528Z.json`真实缓存回读命中2,018行，缺1,609行/1,607个不同缓存键，为时点数据。执行清单 `full-release/mixed-gpu-supplement-20260907T073607Z/selection.json`，SHA `d9aacb52a406e734d627e39eb55a79df5fcaa1a72e30f86f0646c9dd57d1514c`；新同快照预检 `full-release/mixed-cache-preflight-20260907T073625Z/summary.json` verified_sources=96 / verified_leaf_rows=3627 / model_loaded=false，通过实际质量、身份及叶摘要核验。64文件新重建存档 `.superpowers/frozen/mixed-gpu-supplement-20260907T073719Z/runtime.json`，SHA `7979bfefedba659cd16cef5a803f7ec3b4472c47b491f67af31a26c506de3ff5`。旧95选择与63归档保留，但v5管线变化后不能继续按旧工作区摘要启动；后续用本段96版本清单。

活动GPU仍只有 PID51824，15:40第二批 **1,286/9,803**（首批6,127完成），整卡约3GiB、59°C、新增相关事件和缓存错误0，原51项运行依赖及新增64项归档与工作区摘要均未变；证据 `full-release/v5-vectorization-progress-20260907T074005Z.json`。96补算尚未执行：当前任务正常完成、排空退出后再串行600秒试跑，成功干净暂停后用新报告resume/seconds=0接续；不自动重试failed/stopped，不启动第二模型或OpenSearch。剩余11份清单 `.superpowers/sdd/remaining11-after-v5-20260907.json`已与真实库source proofs对账，沿用旧只读结构分类，不冒充重新解析通过。后续继续处理11份来源结构与35份条序问题，再完整构建索引、检索和正式别名切换。

本切片最终后端 **3087 passed / 1平台skip / 1既有warning**、Ruff通过、mypy225源文件通过；新隔离MySQL实际CLI链路1通过，最终聚焦含集成44通过、独立复审182通过；两项词法P2补14项RED→GREEN后关闭。v5变更后补算/启动/热恢复组合重新 **111 passed**。无前端/Schema/部署改动，未重跑相关检查，未提交Git。详细TDD、实源边界、固定运行时及交接见 `.superpowers/sdd/v5-reference/verification.md` 与[本轮计划](plans/2026-09-07-v5-repeated-item-reference.md)。以下日期段落为历史证据。

2026-09-07 15:03 混合版本补算入口已实现并完成真实预检：新增独立mixed选择Reader、逐版本实际facts/身份/叶摘要核验、单模型启动门禁、每批8叶缓存循环与断点回读；复用68°C冷却/62°C恢复/80°C硬停。新选择`artifacts/legal-corpus/full-release/mixed-gpu-supplement-20260907T065416Z/selection.json`，SHA `c80351b696397657cdd2e2d498f1b7d3c44e9642e7b4bab32797ec057b0e2e48`。真实MySQL只读同快照**95版本/3,597叶预检通过**，报告`full-release/mixed-cache-preflight-20260907T070116Z/summary.json`，未加载模型、未写数据库/缓存；父全集quality仍有35阻断，未改为通过。

组合测试**111 passed**、相应范围Ruff及Reader scoped mypy通过；独立审查发现前驱事件时间未严格校验，新增解析/拒未来/UTC规范化后复审关闭。最终63文件源码重建证据`.superpowers/frozen/mixed-gpu-supplement-20260907T070251Z/runtime.json`，SHA `8259cfa922c460d17144339c0f500764b276ae7349ed5bf556b6fd1be4160347`；命令、TDD及边界见`.superpowers/sdd/mixed-gpu-supplement-verification.md`。实际补算尚未执行，当前活跃前驱在真实启动门禁被拒绝，未启动第二模型；没有新增自动等待器。

15:02 GPU PID51824存活，第二批**1,005/9,803**、62°C/整卡约3GiB、缓存错误与新增事件0、原51哈希一致。保持当前计算推进；正常完成排空退出后核对新63项SHA并串行600秒补算试跑；代码变化须重新预检。剩35质量/12入库、全集索引/检索及正式发布要求不变。

2026-09-07 14:42 补充向量范围已实核：`artifacts/legal-corpus/full-release/mixed-vector-coverage-20260907T064210Z.json`按当前15,994个发布版本、首批6,127与当前9,803选择逐ID对账。两批选择无交集，其中15,882个版本仍在当前发布集合，另48个已不在当前集合；当前集合有112个版本不在两批选择中，其中**95个质量通过（86 v4 + 9 v3）、17个质量阻断**。不能将两批总数直接当当前全集覆盖率。

95个合格补充版本已在同一MySQL只读一致事务重新检查质量、版本/数据集/来源证明身份及叶块，逐版本质量摘要与真实全集报告一致；共3,597叶块，当前真实缓存回读命中2,016行，缺1,581行，对应**1,579个不同缓存键**。完整缓存文件按1792维、L2、文件摘要检验，无读取或损坏错误。缓存仍可能被活动任务补齐，这只是当时缺口，不冻结缺失状态。核验工具`.superpowers/sdd/audit_mixed_vector_coverage_20260907.py`不导入模型、不写数据库或缓存，活动GPU由848推进到849，51项运行依赖未变。

此产物明确为audit_only，不是可执行GPU选择或发布批准。当前selected runner硬绑定v3，不能把v4冒称v3送入；下一向量增量应新增独立混合版本补充入口/严格选择验证，保留当前51文件和模型身份，测试清单篡改、逐成员parser/来源/叶摘要及断点恢复，再于现有worker排空退出后串行补齐95版本。现有任务继续运行，不启动第二模型。另17个集合外质量阻断加两批中18个质量阻断仍共35，需先修复后再纳入完整发布。

2026-09-07 14:34 向量化续做核验：用户要求继续推进，现有计算PID51824存活且报告持续更新，第二批已完成786/9,803来源（较上次653增加133），首批6,127完成。当前采样57°C、整卡约3GiB、约5.65向量/秒，缓存错误和新增相关事件均0，51项运行依赖SHA全部一致。保持已运行的无时限单模型任务和68°C冷却/62°C恢复/80°C硬停配置，不重复启动模型；实际状态以`artifacts/legal-corpus/full-release/v3-cache-gpu-selected-20260907T055059Z/summary.json`为准。86个v4和9个新v3后续缓存补齐仍未完成，向量化尚未全部完成。

2026-09-07 14:21 验证收尾：**全集大JSON持久化与读取容量已通过，全集发布仍未完成**。真实15,994来源的完整候选格式JSON为58,989,921字节，在Windows Proactor/asyncmy/MySQL实际写入后，经`json_documents.py`单次SELECT、每字段最多128KiB读取，完整摘要一致；证据`artifacts/legal-corpus/full-release/mixed-release-capacity-20260907T060841Z.json`。该载荷仍携带35份阻断的真实质量报告，只证明容量，不是批准发布。随机隔离库已清理，业务库写入0。

读取修复已接入journal、inventory及`legal_corpus_read_uow`的数据集列表/详情，避免旧入口直接读取或排序大JSON。元数据与JSON摘要在同一查询中取得，读取校验摘要且刷新ORM身份映射，拒绝并发变化；原GPU冻结的`repositories/legal_corpus.py`未修改。17MiB实际查询/恢复、并发修改拒绝及旧ORM元数据刷新均通过。最终后端**3,044 passed / 1平台skip / 1既有Starlette警告**，Ruff通过，mypy224源码通过；隔离MySQL新旧journal与JSON读取共**14 passed**，独立复审阻断项已关闭。前端与部署未改，本轮未重复其验证。

最终发布源码入口`.superpowers/frozen/mixed-release-20260907T062115Z/runtime.json`，SHA `fa321a4b982b8995073ffa1260416c2af75337e813ac0356429c72494758d136`，224源码加pyproject/lock共226项。下文真实全集质量检查使用较早054433Z存档；新的存档补齐传输与查询修复，不能说已用新存档重跑全集质量。14:19 GPU报告仍为running、计算PID51824、第二批653/9,803，采样59°C，首批6,127已完成；模型缓存完成不等于索引发布。继续保持51文件运行依赖不变。接续主线：12份未入库、35份条序质量问题、当前向量计算与新版缓存补齐，然后完整质量、OpenSearch构建/检索及正式别名切换验收。

2026-09-07 14:02 更新：**release-set-v3已接入typed来源证明、完整报告Reader、逐成员质量检查、CLI及候选恢复门禁**。真实新清单`artifacts/legal-corpus/full-release/release-set-mixed-15994-20260907.json`（SHA `129be1c05a3afae0918840aac6ca8f89d70b2d4a49d9d1405197c0c0dee265cb`）绑定13份完整报告/16,069行，经75对显式替代得到15,994唯一来源，保留3份编号审核。真实同一数据库快照统一质量检查已完成：**15,959无阻断、35份仅article_sequence_invalid**，质量摘要`d14d91685868a20d78e4a706d2f520e5b816f025c3d260a5402a838b99f5dd04`，报告`full-release/mixed-release-quality-20260907T054500Z/quality.jsonl`。剩35的逐源清单`full-release/mixed-release-remaining35-20260907.json`；另12份尚未进入当前导入流程。不能把35份移出清单以宣称全集发布。

本次全套后端3,026通过/1平台skip，Ruff通过、mypy222；隔离MySQL真实v3/v4导入→混合CLI质量→journal JSON恢复及旧journal验证7通过，独立复审无P1/P2。实际质量使用源码存档`.superpowers/frozen/mixed-release-20260907T054433Z/runtime.json`，SHA `e91450a03139793eedd4e3fd25c32b77f077675b517b702c100b32e9349dbe89`（222源码+pyproject/lock）；不包含后续大JSON传输修复。容量实测完整JSON约59MB虽低于64MiB，但asyncmy大包读取失败，因此**完整容量及正式发布仍未验收**。诊断与修复设计见`.sdd/asyncmy-large-json/reviewer-diagnosis.md`和同日mixed-parser-release设计；当前正实现单次SELECT、128KiB分行的JSON传输，保留原JSON列及完整来源证明，需完成真实大文档往返后更新存档。全部容量测试在随机lawyer_test库执行并清理，未写业务库、未切正式别名。

GPU状态更正：旧PID25876于13:22触发80°C保护后已正常排空退出，286/9,803、缓存无错误、无新增显示/WHEA事件。新独立降载入口`.superpowers/sdd/warm_v3_gpu_thermal_recovery.py`保留原49文件，完整运行依赖扩为51项；68°C暂停、62°C恢复、80°C硬停，微批间隔至少0.25s。600秒实际试跑新增3,437向量、约5.73向量/秒、最高68°C/整卡3.1GiB，完整前缀缓存核验并正常排空；试跑报告`full-release/v3-cache-gpu-selected-20260907T053834Z/summary.json`。已持续续跑：**计算PID51824、启动器60028**，报告`full-release/v3-cache-gpu-selected-20260907T055059Z/summary.json`，13:57实际426/9,803，最高66°C，无新事件，51运行哈希一致。运行期间禁止编辑这51项、启动第二模型或OpenSearch。首批6,127已完成，86个v4及9个新v3后续仍需缓存补充核对。实际进展证据`full-release/mixed-release-verified-progress-20260907T055755Z.json`。

2026-09-07 13:13 更新：本轮已将**86个v4版本实际导入并严格replay，两个正式批次均0失败**，新增3,133条文、3,364分块；真实MySQL发布质量检查86/86通过，摘要 `e4937deb747a2f43513557b4160fa27e297a2d925fd41f4d0fdf86920cd4bdf5`。本批对应旧127版本、4,660条文、4,973分块的ID/关系/内容摘要前后完全一致，新v4三类事实在replay前后亦完全一致。批次入口 `artifacts/legal-corpus/full-release/v4-verified-candidates-20260907T050559Z/`，内含passed-manifest、frozen-preflight、import、quality、replay及前后事实快照。整合验收 `full-release/v4-import-verified-progress-20260907T051250Z.json`。

**当前v3/v4导入线覆盖15,994/16,006来源，剩12份**。须区分口径：本轮86份中84份已有v3，另2份前导标题解释从旧v2补齐到当前流程；并非86份全新来源。包含全部历史parser时MySQL已有15,995个不同来源，本轮前后不变，另1份仅旧版本不能计当前导入线完成。过去15,992数字实际是v3范围，不是包含历史版本的MySQL总覆盖。本批解决9份旧source-proof冲突的新版报告缺口，并为原110个质量阻断来源中的75份建立了通过完整质量检查的v4替代版本；旧事实及旧110报告保留，另35份仍无通过替代。剩12份尚未完成当前导入与这35份已入库质量问题分别续做。

GPU首批**6,127/6,127正常完成、provider已排空**，旧PID51652已退出，报告 `v3-cache-gpu-adaptive-20260907T034110Z/summary.json`。串行等待器已自动接续下一批9,803份/426,506叶：活动计算PID **25876**、启动器PID **58724**，报告 `full-release/v3-cache-gpu-selected-20260907T050940Z/summary.json`；13:12实际72/9,803，已真实CUDA编码，约17向量/秒，整卡显存约3.0GiB，新增相关事件及缓存错误0。49项冻结运行依赖仍全部一致，继续单模型、window2/outer8、温度冷却和内存检查。handoff旧报告中的worker_pid为启动器，实际计算PID以新summary为准；不得再启动一个模型。本次86个v4版本和之前9个新增v3版本尚不在这个固定下一批选择内，后续补充缓存须核对，不能把旧版已缓存直接当新版验证完成。OpenSearch继续停止，正式dataset_v1未切换。

Parser方面：条号数字内部水平空白与U+200B前缀识别仅精确v4启用，原文/坐标保留。独立复审发现的款项引用伪锚、NEL跨行问题已按TDD修复，混合重复条号由实际CLI硬拒绝。乌鲁木齐实源67→68条、68块逐字符覆盖及内存replay通过；福建虽可识别零宽候选，但沿革行全角左括号/半角右括号错配导致全局边界不清，仍阻断，没有计作恢复成功。另8份解释/批复经76段、3,908字符、23标题跨度核对后显式改用non_article_document，旧审核元数据保留，新审核证据 `.superpowers/sdd/leading-eight-content-mode-review-20260907.json`；哈尔滨原metadata标题与原件的既有差异已留证，未在模式修改中静默改名。

实际候选接线验证还发现v4导入采用结构摘要、发布门禁却沿用拼接正文摘要，导致所有候选被误拒；已修复为从权威Provision重建完整字段并复用精确v4规范算法，旧profile及其固定质量摘要不变。真实import→quality正反例和独立复审通过。最终后端**2,923 passed / 1平台skip / 1既有Starlette警告**，Ruff全部通过，mypy219通过；未改前端、Schema、部署，本轮未运行这些检查。最终可重建生产源码存档 `.superpowers/frozen/corpus-v4-20260907T050545Z/runtime.json`，SHA `59cc234c5e84c1901e0ce4c962a34c477ac1669952e0ec05069269d479968f91`；219源码+pyproject/lock共221项，解释器和已安装发行包版本清单受检。冻结CLI拒绝重复/缩写parser参数及未登记可导入产物，使用-I/-B，凭据仅环境注入。第三方安装字节未归档，不冒称完整环境字节冻结。旧045500Z/045835Z档未用于生产且不含最终质量修复，不能作为接续入口；v4重放使用固定执行工具和最终存档，后续行为变更使用新Parser身份。

**统一全集发布仍未完成**：当前release-set-v1/v2要求全部report同parser身份，也不支持显式排除已被替代来源；所以不能把新v4报告直接拼接到旧15,983 v3清单，更不能删除同版本检查或改写报告。下一明确切片是显式release-set-v3，绑定完整报告、逐成员原parser/chunk身份、来源from/to替代、最终选择摘要及快照恢复证明。代码定位 `.superpowers/sdd/mixed-parser-release-design-audit-20260907.md`；表格引用4源及其它复合材料设计 `.superpowers/sdd/reference-table-code-design-20260907.md`，均尚未实现。三新缺号来源官方核对证据 `artifacts/legal-corpus/official-gap-evidence-20260907/`：西藏全文规范化后仅缺第十八条四字，尚未应用修复；湖南有修改后编号及第五条文字缺失，不能直接补号；长沙同版官方全文未取得。用户的current及日期政策继续保留，不重问、不补造日期。

全部16,006份离线切块文件仍在 `C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\chunks`，逐源清单为同目录`文件清单.csv`。离线导出完成、当前数据库派生版本验收和全集索引发布仍分别计数，已有导出不重跑。后续优先保持GPU运行，完成混合发布清单与结构/源修复、补充缓存，再统一质量、主/导航完整计数和检索验收后切正式别名。

2026-09-07 12:22 更新：本轮实际新增机场规定1份、机关补充3份及普通条文6份，**MySQL已入库15,992/16,006份，剩14份**；只读盘点 `full-release/v3-cache-remaining-inventory-20260907T042149Z.json`。新增10份的正式导入均0失败，三个批次分别通过实际发布质量检查。机关证据及角色局限见 `.superpowers/sdd/remaining3-issuer-followup-20260907.md`：海缆原通知扫描另有正常HTTPS正文交叉核对；福利费为官方执行文件旁证；出版为主管机关官方沿革，不冒称后两份原颁发件已取得。日期继续null，原件未改。

原中断批次的1,702份中，**1,693份严格replay全部成功**，新完整报告 `full-release/interrupted-replay-20260907T035130Z/replay.jsonl`；0新增导入、0失败。前后1,693版本、65,599条文、69,423分块的ID/关系/内容摘要完全一致；原清单、旧报告及当次生产runtime摘要未变。9份既有source-proof冲突保留未纳入，未改写中断报告为完整。当前12份完整报告共覆盖**15,983份**，可执行集合 `full-release/release-set-complete-15983-20260907.json`（SHA `87d55cd09fdeab38a58f2f107220d4ead5de667802cb29bffa8cbe47012fdf02`）；15,977旧集合保留，不作最新入口。

扩大到15,974份的同一只读事实视图质量检查已完成：**15,864份无机器阻断，110份article_sequence_invalid**，报告 `full-release/expanded-release-quality-20260907T035528Z/quality.json`，质量摘要 `8de3c3077ba04660b1a676b71ecd8383ce916b5f6aa91dc2b4a6ac15d266a3d0`。较早14,280范围新增6份条号阻断不是旧问题突然增加；新增6份实际v4 CLI复查1份条序通过、5份仍阻断，见 `new-six-v4-sequence-audit-20260907.json`，该复查不等于完整正文或入库验收。随后新增3+6份分批质量通过，尚未重跑15,983统一质量；正式别名尚未切换。

GPU仍为单一 **PID51652 / PTY14926**，当前报告 `full-release/v3-cache-gpu-adaptive-20260907T034110Z/summary.json`；本轮观察约16行/秒、整卡显存约2.8GiB，缓存错误及新显示/WHEA事件0。已启动**串行接续等待器PID2820 / PTY42502**，状态 `waiting`，报告 `full-release/gpu-next-handoff-20260907T0414/summary.json`。它最多等待6小时；只在当前6,127份全部正常完成、provider排空且进程退出后，启动下一批**9,803份/426,506叶块**，遇停止、失败、事件或缓存错误不自动重试。

最终下一批选择 `full-release/next-gpu-selection-20260907T040738Z/selection.json`（SHA `00eefa09c7f327e01f4aad6745b166b24a25f22478bc75e8b01b2080302c5c5c`）：9,803份质量检查与proof/叶子摘要获取在**同一只读一致事务**完成；完整逐版本结果等于父15,974报告中的对应合格子集，质量报告passed=true、版本集合与选择严格相等。父报告成员仍表示15,974来源的导入报告出处，不冒称本次核对全部父范围。旧040037候选跨事务缺口已发现、保留NOT-APPROVED并由新校验器明确拒绝，未启动；040341尝试因Python tuple与JSON list表示差异断言失败，实际单源probe证明规范JSON一致后重跑，未放宽内容断言。当前6,127选择本身含66份已知机器质量阻断，因此不能把6,127+9,803当作全部已具发布资格。

接续工具沿用window2/outer8、FP32/1792/512-token均值L2、25%分配器及74°C冷却/68°C恢复/80°C停止；共享单模型锁，逐源回读真实缓存后推进游标，resume先核对已完成前缀缓存并检查STOP。AST确定的49项真实向量运行依赖及锁文件已冻结，**等待/运行期间不要修改此集合**；不冻结未参与执行的Parser/API，可继续结构修复。生产Provider与当前GPU runner未改。15项聚焦测试和实际MySQL6叶/已有缓存解码probe通过；最终独立复审 `.superpowers/sdd/next-gpu-queue-review-20260907.md` 已通过。下一批GPU尚未实际启动，不把等待器或无模型验证算作CUDA端到端成功；OpenSearch仍停止，不并发模型或搜索容器。

本次先识别**8份v4候选**并完成正式CLI预检及内存导入/replay/218块逐字符覆盖，随后确认其中6份普通条文无需v4能力：它们实际v3 CLI/条号/内存replay/205父块覆盖全部通过，并已生产导入与质量核对通过，清单 `.superpowers/sdd/remaining6-v3-ready-20260907.jsonl`。历史现行性冲突保留在审计记录，current仍来自用户全集审核，不冒充重新外部核验。旧8行v4清单保留，不能再把已入库6份计作缺口。

**剩14份：2份前导引用标题解释已具v4候选条件，12份为重复条号9、段内歧义2、补充条号歧义1**。审计报告保留当时23份范围，其中机关3份和普通条文6份现已入库。v4仍未全局冻结，2份解释待新解析身份导入；继续闭合剩余结构问题、保留旧事实重导，并解决9份proof漂移。新增3+6份尚不在已冻结GPU下一批9,803选择内，后续补充缓存及全集质量时须计入。切块交付仍在 `C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\chunks`，无需重跑已完成16,006份文件导出。本轮未改生产后端、前端或Schema，未重复整套后端/前端/部署测试；全集向量化及正式发布仍未完成。

2026-09-07 11:42 更新：用户明确允许适量GPU并要求优先提高向量速度，已正常结束原CPU任务并切换为单GPU、温度自适应接续。CPU最终4030/6127来源，173484行缓存访问统计，provider已排空且进程退出；原脚本的failed状态由operator_stop产生，不是新掉卡。当前活动 **PID51652 / PTY14926**，报告 `artifacts/legal-corpus/full-release/v3-cache-gpu-adaptive-20260907T034110Z/summary.json`，从4166/6127接续；只有一个模型任务，不重算已命中的向量。

最终300.312秒实际GPU验证新增 **4328行、14.412行/秒**；CPU切换前约600秒新增2550行、4.253行/秒，观察约快 **3.39倍**。两段为不同真实输入，不是同样本严格基准，也不能据此保证全集耗时。第二轮最高77°C、NVML显存峰值2760.36MiB（含驱动保留），累计冷却24秒，新相关事件和缓存错误均0，正常暂停/排空。验证汇总 `adaptive-gpu-validation-20260907T034051Z.json`，实际生成向量已缓存，没有只做空跑。

本机GPU运行参数：固定FP32/Yuan/1792/512-token窗口均值L2，window2、外层8；PyTorch分配器25%约3GiB（不是整卡硬上限），整卡已用5GiB/剩余6GiB检查；逐微批74°C暂停、68°C恢复、80°C停止，并检查OEM均衡零偏移、显存时钟、主机内存和新增显示/WHEA事件。NVML直接只读采样减少子进程开销；原4项Provider实现未改。运行期间不得修改GPU runner、policy、checkpoint、NVML、旧guard与Provider文件。13项操作测试、实际缓存写失败/空叶子probe通过；独立复审P1/P2已修复关闭，见 `.superpowers/sdd/adaptive-gpu-review.md`。完整项目、全集向量化、正式索引发布仍未完成；下面CPU进度为历史记录。

2026-09-07 11:12 更新：继续以CPU运行向量接续，11:11同一PID58004实际进度 **3929/6127来源、169253行缓存统计**，CUDA未初始化、缓存错误0；私有内存约2862MiB，整卡显存917MiB/61°C。当前6127是固定子集，不能作为16006全集向量化进度。11:12实际只读MySQL盘点仍 **15982/16006份入库，24份未入库**，9855个已入库版本在活动缓存选择之外；报告 `artifacts/legal-corpus/full-release/v3-cache-remaining-inventory-20260907T031238Z.json`。全集向量化与正式发布仍未完成。

[空括号条头前缀切片](plans/2026-09-07-empty-parenthesis-article-prefix.md)已完成，独立任务与集成复审通过，报告 `.superpowers/sdd/empty-parenthesis-review.md`。仅精确v4恢复 `（）` 加水平空白且前后条号闭合的条头，原文不改。双鸭山真实CLI由17条恢复为18条，18块/18叶和逐字符覆盖通过；旧7源前缀回归通过。残余32源经实际CLI预检重新核对，新增1源通过、31源仍有条序问题；原107证据汇总为46历史v3、27新v4、3编号审核、31残余，索引 `publication-structure-resolution-evidence-index-20260907T031022Z.json`。此结果不是生产库104项质量阻断已解除，v4修复仍待冻结后重新入库。前文多余“发”保留为来源局限，结构通过不是全文法律质量验收。

本次还确认原始ZIP Loader的早期样本不能替代实际导入：乌鲁木齐源在Word条号中有制表符，ExportReader正确保留后仍漏识别；山东来源涉及零空白私用前缀、章节与同段右锚，另行处理。三源格式证据 `residual-format-context-20260906T205426Z.json`，最终32源预检 `residual32-v4-parser-audit-20260906T205719Z.json`。最终后端 **2787 passed / 1平台skip / 1既有Starlette警告**，Ruff全部通过、mypy218通过；未改前端、Schema及部署，本轮未运行这些检查。

11:14收尾采样：同一CPU任务 **3948/6127份、170188行缓存统计**，私有内存2861MiB、CUDA未初始化，5项冻结runtime摘要均未变；整卡显存938MiB/62°C，证据 `.superpowers/sdd/cpu-persistent-observation-20260907T111459.json`。最终diff检查通过，251项脏树保留，未提交，真实语料和指定秘密文件没有被跟踪。下一切片先处理数字内部制表符，再处理章节/段内组合；已有切块交付不重复执行。

2026-09-07 04:34 更新：[有证据的延续条号发布检查](plans/2026-09-07-reviewed-article-numbering.md)本切片已完成，任务复审与最终集成审查均通过。新增 v2 集合清单，将明确的连续起止条号、审核引用、官方证据原始字节摘要绑定到已入库版本及 source/input/structure 三个摘要；审核记录进入质量摘要和持久发布报告。默认缺号检查及历史审核摘要保持不变。复审补齐了审核模式前导零拒绝、无效证据网址拒绝，以及清单错配、文件大小与路径边界回归。没有更改原件、旧导入报告、Parser 或数据库 Schema。

官方编号说明已通过校验 HTTPS 下载到 `artifacts/legal-corpus/full-release/constitutional-numbering-official-20260907-beijing/`，原始 HTML 与下载日志、三源绑定、旧质量基线及新 v2 清单均保留。它支持1993/1999/2004年修正案的延续编号，不是三份法规全文逐字对照。最终实际 CLI 清单读取→同一 MySQL 只读一致视图核对，三份均通过；不带审核时仍仅 `article_sequence_invalid`，质量摘要等于修复前基线，修改审核后旧批准摘要失效。报告 `real-numbering-verification-final.json`。合成隔离 MySQL 还验证了审核记录与摘要在发布日志和完成快照中完整保存，测试库已删除，生产写入0；报告 `numbering-review-mysql-probe-final.json`。

最终后端 **2760 passed / 1平台符号链接跳过 / 1既有Starlette警告**，Ruff全部通过，mypy218源文件通过。命令为 `uv run python -X utf8 -m pytest tests/unit tests/api tests/contract -q`；首次误用 pytest 控制台入口产生3个既有Fixture导入错误，改用仓库既定模块入口后通过，未改测试断言。没有前端或Schema修改，本轮未运行其检查。

本轮对8份完整报告的 **14,280个版本**重新执行实际固定 Yuan/window-mean/L2/1792 配置下的只读发布质量核对：**104份仍被 `article_sequence_invalid` 阻断，3份编号误报解除**，没有其它机器阻断项，报告仍 `passed=false`。结果位于 `artifacts/legal-corpus/full-release/reviewed-numbering-full-quality-20260906T202812Z/`，4项检查代码摘要在批次前后不变；此过程未加载模型、启动搜索或发布索引。该范围不是全部16,006份；此前未完成报告的1,702份及未入库24份仍须接续处理。

原107份结构问题的证据汇总现在为46份历史v3解析通过、26份新v4实源案例通过、3份延续编号质量通过、**32份仍待排查**，见 `publication-structure-resolution-evidence-index-20260906T203056Z.json`。多数Parser修复尚未以新版本应用到事实库，所以这份证据汇总的32与实际库质量阻断104不是同一计数；不能据此宣布全部107本轮v4验收通过。下一步处理残余引用型决定、畸形条头/前缀与正文缺口，固定完整v4运行时后保留旧事实进行新版本导入，并补齐完整发布清单与全集复查。

CPU向量接续仍为同一PID58004，04:33采样 **1534/6127来源、65199行缓存统计，该次接续累计完成编码42391行**，私有内存2848.93 MiB，CUDA未初始化、缓存错误0，5项冻结runtime摘要均未变；证据 `.superpowers/sdd/cpu-persistent-observation-20260907T043354.json`。继续CPU4线程、window1、外层8、每批暂停0.1秒及既有内存停止线；GPU计算暂停，整卡采样924 MiB显存/62°C，这不是硬件掉卡根治验收。只有原MySQL容器运行，OpenSearch保持停止。全集向量化和正式发布尚未完成。以下为此前切片与历史证据。

2026-09-07 03:39 更新：[段内条头与精确正文计划](plans/2026-09-07-embedded-article-headings.md)本切片已完成，最终独立spec/quality及跨任务集成复审通过。仅精确v4启用段内条头恢复、全文/空白精确保留、有边界版本摘要及CLI重复导入一致校验。修复了句号后无前置空白的漏识别、显式换行右锚被重复读取，以及逐句号复制剩余正文的二次开销；匹配使用当前行的原字符串位置边界，原件不改。最终后端 **2717 passed / 1 平台跳过 / 1 既有Starlette警告**，Ruff全部通过，mypy218源文件通过。确定性字符切片核算验证旧重复复制缺陷已消除，不代表整个Parser生产容量验收。

最终只读实源检查覆盖13份来源/17处位置：**15处恢复、2处紧凑条头继续阻断，11份通过完整条号检查**；13份原件SHA和正文区全部字符/空白不变，626块（含54子块、623索引叶）逐字符覆盖、实际内存ImportService及replay checker通过。报告 `artifacts/legal-corpus/full-release/embedded-article-v4-readonly-check-20260907-bounds-final.json`。吉林实验动物第四十四条及山东高速公路第四十四条仍需单独处理，后者还涉及前文截断，不能靠补齐号码宣称正文完整。前导标题10/10、段首前缀7/7真实回归也通过，见相应 `*-bounds-final.json`。实际合成DOCX使用同段 `w:br` 普通条头作右锚，经真实Parser→隔离MySQL首次导入/replay通过：3条文/6块，全文、空白、所有ID/字段不变；测试库已删除，生产库写入0。报告 `v4-mysql-replay-probe-20260907-bounds-final.json`，已不使用手工Parser契约。没有前端/Schema变更，本次未运行其检查，也未运行GPU或OpenSearch。

v4仍未全量冻结，未进行生产重导入或全集别名发布。续做证据索引 `v4-resolution-evidence-index-20260906T193926Z.json` 将原107份结构问题范围分为46份历史v3解析通过、26份新v4实源案例通过、35份待继续排查；另外2份已验证标题案例不在原107内。该索引不是可执行发布清单，也不是全部107份的本次v4重验。下一步处理残余特殊编号、引用型决定、其它前缀和正文缺口；3份宪法修正案原件的延续编号已做只读记录，官方检索摘要可作为进一步核验线索，完整官方正文尚未下载对照，编号策略未更改。具体版本冲突与来源修复继续单独留证。

CPU接续同一PID58004仍运行，03:38采样 **1208/6127来源、51455行缓存，本轮新增29080行**，私有内存2843.48 MiB，CUDA未初始化、缓存错误0。Get-Process确认进程存活、5项冻结runtime摘要全部未变，证据 `.superpowers/sdd/cpu-persistent-observation-20260907T033828.json`；GPU计算继续暂停，这不代表显卡硬件故障已修复。OpenSearch仍停止，只有MySQL容器运行。03:02只读事实库核对仍15982/16006来源、缺24份；当前向量任务外另有9855份版本身份，已记录 `v3-cache-remaining-inventory-20260906T190225Z.json`，启动下一批前还需重新核对质量/图结构/身份，该文件不能直接作为发布选择。天津电子出版物缺第六条材料已完成三个正文段与政府转载文本的去空白对照，证据 `tianjin-electronic-article6-evidence-20260907/verification.json`，原件未改、条头纠正未应用；转载来源及会议次数矛盾保留。以下是上一完成切片与历史证据。

2026-09-07 02:12 更新：段首 U+E004 条头识别切片已通过最终独立复审，见 [实施计划](plans/2026-09-07-prefixed-article-headings.md)。仅精确 `corpus-docx-v4` 启用，普通条号双锚连续闭合、引号/括号边界明确，原前缀和正文不删除；大条号间距采用候选数量有界校验，普通/前缀补充条均阻断恢复区间。最终根代理对 **7 份真实来源、13 处条头**的只读验证再次全部通过：输入 SHA 不变、原段落与正文逐字符保留，实际 `_prepare/_require_importable`、条号质量门禁和层级分块入口通过。报告 `artifacts/legal-corpus/full-release/prefixed-article-v4-readonly-check-20260907-final.json`；本批168个块均为父块，超长父子覆盖由合成回归验证。最终后端 **2660 passed / 1 平台跳过 / 1 既有 Starlette 警告**，Ruff 全部通过，mypy 217 个源文件通过。没有前端/Schema改动，本次未运行其检查，也未写库或发布；v4 尚未全局冻结。

同一 CPU 接续仍运行，02:11 采样 **713/6127** 来源、30268 行已缓存，本轮新增8199/命中22069，私有内存2820.27 MiB，CUDA未初始化、缓存损坏及读写失败0。这仅代表当次采样，全集向量化尚未完成。下述单模型、6 GiB私有内存上限、可用物理/提交容量停止条件继续有效；OpenSearch 保持停止。下一解析切片聚焦真正段内粘连：两份局部审计合并已定位17处，其中15处为有空白分隔的条头，2处紧凑条头不纳入直接恢复（其中1处前文截断）。另需先明确导入 `.strip()` 正规化与新分段边界空白的保存策略，不能以Parser全文保留冒充数据库覆盖验证；设计记录 `.superpowers/sdd/embedded-article-next-slice-design-notes.md`。其它短前缀、未知缺口和外部版本冲突仍单独留证处理。

2026-09-07 01:39 更新：CPU 线程复用修复已通过最终独立复审，根代理完整后端 **2631 passed / 1 平台跳过 / 1 既有警告**，Ruff、mypy 216 通过。最终32次变长真实模型对照向量完全一致、单线程复用、私有提交量约2700 MiB后趋稳；有限真实256行新增255/命中1，缓存错误0，模型已正常退出。随后恢复单一 CPU 缓存接续，当前 PTY 77759 / PID 58004，报告 `artifacts/legal-corpus/full-release/v3-cache-warm-20260906T173807Z/summary.json`；01:39 采样514/6127来源、21981行（本轮新2/命中21979），私有内存2576.67 MiB、CUDA未初始化。CPU4线程、window1、外层8；每批前后检查私有提交量<6 GiB、可用物理>=2 GiB、提交容量>=4 GiB，低于条件即停止保留缓存。**GPU计算继续暂停**。模型运行期间冻结 execution/provider/cache/windows 与本次脚本；不要并发第二模型或修改其执行代码。

OpenSearch 两个准确配置键已写入 Compose 并通过独立复审与13项配置契约。原容器 persistent 设置也已回读成功：逐项Bulk审计true、请求正文false。有限实际验证 `opensearch-audit-repair-20260906t173706z.json`：修改前30秒自身Bulk样本86，修改后的观察窗自身Bulk=0；两条合成文档产生4条业务审计、正文exists计数0，诊断索引已删除，全部原别名一致；原容器最终正常停止，给 CPU 向量接续保留资源。审计 indexing 计数在观察期间1068→3108，因此只确认新时间窗内该自审计路径消失，**未证明全部后台写入静止或重新验收容器峰值内存**。早期两次设置请求因 Sensitive 键不能放入 transient（即使null）而HTTP400拒绝，另一次就绪探针过早；旧失败报告保留，最终仅写persistent。

下一步：监测上述单一 CPU 接续的私有提交量与缓存进度，继续处理剩余 v4 解析纠错及真实发布门禁；OpenSearch 下次独占资源启动时补充持续写入/资源采样。事实库仍15982/16006，全集索引与正式发布尚未完成。以下为此前阶段证据。

2026-09-07 01:15 诊断更新：四组独立 CPU 合成实验均正常关闭、无 CUDA。变长 32 次调用时，旧执行器私有内存从 2,540.93 增至 3,547.19 MiB；单线程复用对照从 2,542.08 增至 2,700.75 MiB，第二轮后基本稳定，32 条对应向量完全一致。现已实现实例级持久工作线程，聚焦 44 项通过，正在独立复审；尚未恢复真实模型长任务。这支持线程复用修复，不代表 GPU 掉卡根治或全集容量通过。

OpenSearch 的 12 条白名单审计样本全部指向自身审计索引的 BulkRequest。对应 3.8.0.0 上游代码确认默认整包审计路径没有覆盖 BulkRequest 的自审计排除，而逐项解析路径有该保护；准确动态设置已核对。原取样排序使用旧时间字段，不能声称这 12 条是最新记录；验收改用本版本的 `@timestamp`。容器取样后再次正常停止，当前尚未应用修复，全部索引与卷保留。证据 `artifacts/legal-corpus/full-release/opensearch-audit-metadata-sample-20260907.json`，分析 `.superpowers/sdd/opensearch-self-audit-evidence-20260907.md`。以下 00:47 等记录为此前阶段快照。

2026-09-07 00:47 资源状态更新：CPU 单窗口接续在观察到进程私有提交内存约 30 GiB 后已通过 STOP 正常排空，当前 **CPU/GPU 向量计算均暂停**；`v3-cache-warm-20260906T162731Z/summary.json` 保留 510/6,127 固定选择来源、21,723 行缓存、本轮新增 3,221/命中 18,502，状态为操作员停止导致的 failed，不能记作完成。当前无模型进程。旧 CPU 第二次异常的 8 行复测虽通过，不能据此认定长任务稳定。只读 16 次空操作确认 `BoundedEmbeddingRunner` 每次创建不同 native thread；这是需要对照验证的资源复用线索，尚非内存增长根因结论。下一步优先有停止上限的单 CPU 内存/线程对照，再决定实现修复。

本地 OpenSearch 采样约 7.8 GiB、213% CPU，本次启动安全审计索引约 152 万次写入，业务法规索引没有写入；原因尚未确认。已核对容器 ID、项目与卷后正常停止搜索容器（退出 143、非 OOM），保留所有索引、别名与数据卷，**搜索当前不可用，MySQL 正常运行**。现场快照为 `opensearch-resource-{before,after}-pause-20260907.json`，未读取审计正文或关闭审计控制。恢复前须排查异常审计写入。两项资源调查及下一实验边界见 `.superpowers/sdd/cpu-private-memory-growth-audit-20260907.md`；下方“运行中”均为此前快照。

前导标题解析切片已通过最终独立复审，见 [实施计划](plans/2026-09-07-leading-title-parser-profile.md)。仅显式 `corpus-docx-v4` 启用，默认/旧 v3 不变；限制前导标题范围、条号尾式和闭合沿革，保留来源文字、换行与位置。基本法误匹配、批复尾式遗漏、内部括号错配与规范引用内正文信号遗漏均完成合成回归。最终真实 **10/10** 核对通过：原件哈希与 span 一致，显式全文模式 `_prepare/_require_importable` 保留全部主段落；结果 `leading-title-v4-readonly-check-20260907-final2.json`。最终后端 **2,625 passed / 1 skipped / 1 warning**（平台符号链接限制、既有 Starlette 提示），Ruff 全部通过，mypy 216 个源文件通过。没有前端/Schema 改动，本轮未运行其检查，也没有写库或发布；最近事实库核对仍为 15,982/16,006。本切片通过不代表全部 v4 纠错已完成或已固定长批次运行时。

下一执行入口：[CPU 内存有界对照计划](plans/2026-09-07-cpu-memory-diagnosis.md)，随后排查 OpenSearch 审计写入异常并恢复服务。语料主线继续处理余下编号、引用/附件编号域与来源证据问题，所有必要纠正完成后再固定统一新版本进行入库和全集质量核验。保留旧 v3 ID、证明和发布别名，不重跑已完成的 Word 转换/文件切块。

2026-09-07 00:15 多报告集合发布入口已接通并通过最终独立复审，见 [实施与验收记录](plans/2026-09-06-multi-report-release-set.md)。`--release-set` 绑定完整原报告的字节摘要、顺序及数量，拒绝失败/中断报告、重复来源/版本和混合解析身份；成员证据进入质量摘要和候选快照，发布边界另验证严格类型、连续范围及候选实际版本数量。单报告入口及原有摘要形状保留。最终后端 **2,597 passed / 1 skipped / 1 warning**（平台符号链接限制、既有 Starlette/httpx 弃用提示），Ruff 通过，mypy 215 个源文件通过；没有前端或数据库 Schema 变更。

真实八报告集合覆盖 **14,280 个版本**，已在同一次 REPEATABLE READ 事实视图完成 `--check`，仍为 **107 个 `article_sequence_invalid`**，CLI 如实退出 1。输出 `artifacts/legal-corpus/full-release/release-set-quality-20260906T160833Z/`；`verification.json` 确认八份报告哈希未变、范围总数准确、阻断版本集合与此前逐批检查一致、运行任务代码摘要未变。00:15 新只读对账 `live-v3-scope-20260906T161519Z.json` 确认现库仍为 **15,982 / 16,006 份**，剩余 24，无额外来源或同源多个 v3 版本。没有初始化该检查的 Embedding Provider、构建索引或切换正式别名；原中断的 1,702 份尚未纳入完整发布选择。

进一步只读重算这 107 份：当前 `_prepare/_require_importable` 全部成功，**46 份当前条号门禁已通过、61 份仍阻断**。46 份是旧 v3 入库事实尚未应用已有自动编号恢复，需要使用固定新解析身份生成新版本；不能覆盖旧条文和 ID。有效结果 `.superpowers/sdd/current-parser-107-recheck-v2.json`；第一次审计 helper 误用 UUIDv4 的错误单独记录，不算来源失败。83 份中段跳号的 95 个缺口已逐处分类为自动编号 47、粘连/前缀 30、畸形条头 9、未知 9；该局部分类不等于发布通过。下一增量按仍阻断的 61 份和未入库结构材料实现必要恢复/内容类型支持，然后固定新版本运行时，重导及全集复查；新增编号支持应先核对样式层级和重启语义，见 `.superpowers/sdd/numbering-next-version-standard-notes.md`。

一份此前机关未知的军民合用机场规定，已通过民航局官方签署通知确认发布机关为国务院、中央军委；原件哈希和第一条对应核验通过，单份导入预检 1 passed，清单 `reviewed-airport-issuer-v3-ready-20260906.jsonl` 与 `official-airport-issuer-evidence-20260906/assembly-binding.json` 绑定证据，**尚未写库**。宁夏渔业办法第二个重复“第二十六条”已取得两份官方域名文本的局部正文/“第二十七条”对应证据，见 `.superpowers/sdd/ningxia-duplicate-heading-evidence.md`；原件及派生均未改写。

CPU 向量接续任务仍在运行，00:15 采样完成 383/6,127 个固定选择来源，本轮实际新增 5,253 条向量、缓存命中 10,715 行，缓存损坏/读写失败均 0，CUDA 未初始化。当前输出仍为 `artifacts/legal-corpus/full-release/v3-cache-warm-20260906T152817Z/`；不要并发第二模型或恢复 GPU 计算。AGENTS.md 已增加长批次实现快照和解析身份不可漂移的长期规则。下方保留前次阶段证据。

2026-09-06 23:28 非破坏 replay 已实现并通过独立复审：聚焦 95 项、隔离 MySQL 8 项通过；后端整体 2,558 passed / 1 skipped / 1 warning，Ruff 通过，mypy 214 个源文件通过。实际原第三批 4,745 份恢复结果为 **4,736 replayed、9 source_proof_conflict、0 imported，complete=false**，新报告 `artifacts/legal-corpus/full-release/import-v3-batch-003-recovery-20260906-001.jsonl`。恢复前后 `replay-batch-003-{before,after}-identity.json` 中全部 4,745 个版本、199,770 条条文、213,908 个分块的 ID/关系/正文摘要完全一致；原清单、中断报告和转换清单摘要也一致。没有覆盖旧事实或伪造成功汇总，这份失败报告不能用于发布。9 份冲突正在核查来源结构证明差异；剩余 1,702 份的完整发布质量检查尚未完成。

CPU 向量第一轮在 23:23 因 `model_provider_unavailable` 停止，已新增 7,006 行、复用 3,691 行缓存，正常 drain，CUDA 未初始化；不能把进程退出 0 当作向量完成。针对紧邻失败位置的 8 行 CPU 单独复现通过，暂未复现根因。运行脚本增加只含异常类型/栈位置及批次 ID/哈希的诊断后，23:28 接续单一 CPU 模型，输出 `artifacts/legal-corpus/full-release/v3-cache-warm-20260906T152817Z/`；仍为 4 线程、窗口微批 2、外层 8，GPU 计算暂停。

23:33 来源证明审计确认上述 9 份 replay 冲突唯一差异为新版读取器新增 `automatic_numbering_not_resolved` 标记；source/input/structure 摘要和条文数量一致，DB proof 仍匹配原报告。同一 `corpus-docx-v3` 标签下发生质量证明语义变化，未发现正文结构变化，但不能据此证明两个完整运行时等价。报告 `.superpowers/sdd/replay-nine-proof-conflicts-audit.md`；后续解析纠错应固定代码快照、升级派生版本并保留旧事实，不临时过滤质量标记。CPU 接续任务已越过此前失败位置，273 个来源完成、10,698 行缓存复用、新增 167 行，CUDA 未初始化，仍在运行。

只读来源调查另确认 9 份首部漏条号可依据 DOCX `numPr` 与 `numbering.xml` 恢复，未修改原件或 Parser。两份此前疑似缺条的青海/果洛文档在官网正文亦使用畸形条头，邻近正文匹配，须按有来源证明的结构纠错处理，不能标记为真实正文缺失。这些调查尚未转化为新的解析版本或发布验收。下一增量为固定新版解析身份与针对已定位结构错误的合成回归，并接入可追溯的多报告集合选择；保持现有 v3 ID 和正式别名，不用失败恢复报告冒充发布验收。

2026-09-06 23:01 继续扩大真实发布质量核对：较早五份完整 v3 报告覆盖 11,019 个版本，发现 90 份 `article_sequence_invalid`；连同上一轮的 3,261 份，目前分批检查 **14,280 份、107 份被机器门禁阻断**。报告位于 `artifacts/legal-corpus/full-release/prior-quality-20260906T145230Z/`；剩余 1,702 份中断记录须安全恢复后补查。这些是各批独立事实视图，尚非一次全集发布检查。没有切别名或重建索引。

23:01 阶段的安全 replay 切片及恢复前快照见 [实施计划](plans/2026-09-06-nondestructive-corpus-replay.md)；实施、复审及实际恢复结果已更新在本节上方。

2026-09-06 晚间按用户“继续推进项目、减少 GPU 显存占用”接续：系统回读驱动已为 616.56，原 MySQL/OpenSearch 容器 ID、数据卷核对后恢复。接续前事实库 `corpus-docx-v3` 有 12,721 个唯一来源；本轮新增 **3,261 份**（接续 3,043 + 机关/标题补充 215 + 恢复解析 3），三个完整导入报告均 0 失败。22:41 独立只读对账确认 **15,982 / 16,006 份**已入库，剩余 **24 份**，无额外来源、无同源多个 v3 版本；明细为 `artifacts/legal-corpus/full-release/live-v3-scope-20260906T144144Z.json`。

自动条号恢复的重复定义、空可见条头丢失及非首层编号三个边界均已修复并通过独立复审；只读重算此前入库的 11 份自动条号文档，结构摘要全部一致。后续实际发布质量检查覆盖本轮全部 3,261 份：**17 份 `article_sequence_invalid` 被阻断**（机关补充 7、接续 9、恢复解析 1），另外 3,244 份无机器阻断项；完整报告仍记录来源/日期等复核提示，不能将入库成功当作发布通过。三个 `quality-*.summary.json` 保存问题来源及版本，正式 `dataset_v1` 未切换。该检查是事实库质量核对，没有加载模型或构建新索引。

最终代码检查：`pytest tests/unit tests/api tests/contract -q` 为 2,547 passed / 1 skipped / 1 warning（平台符号链接不可用；Starlette/httpx 弃用提示），Ruff 全部通过，mypy 213 个源文件通过。10 份机关补充文档的失败均精确确认为无可解析条号，已显式保留全文模式并逐行记录变更依据，214 份新版清单全部预检通过；没有对重复或歧义条号使用该回退。

Embedding 新增实例级 `window_batch_size`（默认 8，严格 1..8），本机设 2；聚焦 78 测试及独立审查通过。实际 GPU 验证单进程 PyTorch 分配器限制 50%，采样整卡显存约 2.6 GiB、PyTorch 峰值 allocated 1,302.43 MiB / reserved 1,344 MiB；632 行后触发新的 80°C 保守停止线并正常关闭，未完成 120 秒阶段，不能宣称持续稳定。GPU 预计算停止，改为 CPU 单模型、4 线程复用原缓存接续固定 6,127 版本，报告 `artifacts/legal-corpus/full-release/v3-cache-warm-20260906T141725Z/`；22:42 采样已新增 2,762 条向量、复用 3,691 条缓存，缓存损坏/读写失败均 0，CUDA 未初始化，该任务尚未完成。下方上午状态保留为阶段记录。

下一执行切片：修复并核对上述 17 份发布条号问题；处理剩余 24 份（14 份复合附件/引用/原文编号等结构问题及 10 份机关或直接证据冲突）；对旧 1,702 份中断批次先实现保持 Chunk ID 的严格 replay，再自然重跑生成完整报告，最后加入有原报告摘要的多批发布集合。现有 replay 会无条件重建 Chunk UUID，不得直接重跑旧批次来凑完整报告；只读设计已保存在 `.superpowers/sdd/multi-report-release-design-review.md`。向量缓存继续用 CPU，勿并发第二模型或重新尝试 GPU 重负载。

最新优先事项是用户要求先修复反复 GPU 掉卡，见 [本机 GPU 排查记录](../gpu-recovery-2026-09-06.md)。已查明机械革命狂暴模式含核心 +100 MHz、显存 +500 MHz，切到均衡后配置与实际时钟核对为零偏移；短验证通过，两轮较长验证触发 85°C 保守停止线，五分钟验证未完成，全部测试已停止，不能宣称已根治。已确认 WHEA 报错端口为 GPU 父端口；用户此前游戏中也发生外接屏失联与整机卡死。全集向量任务暂停，未更改驱动或 BIOS。第二次手动重启使第三批入库停在 1,702 份成功记录、无完整 summary，后续须重新选择剩余来源接续；下文 10:32 的数字是重启前阶段快照。

当前执行入口为 [真实全集发布计划](plans/2026-09-06-full-real-corpus-release.md)。用户已明确确认16,006份候选人工审核现行有效，并要求缺日期不阻塞入库/发布；该批确认分别保存为 `artifacts/legal-corpus/reviews/user-current-review-20260906.json` 和 `user-date-policy-20260906.json`，未知日期保留null。旧候选中的pending是审核前快照，不能覆盖这次确认。补充机关时直接发现的个别官网废止冲突另行留存，不将冲突当作已解决。

真实业务库已做两次一致性备份并升级至 `20260906_15`。2026-09-06 10:32 恢复服务后实时回读，`corpus-docx-v3` 已有 **11,019 个唯一原件来源证明**；五份完整导入报告均无失败，不能将保留的旧 v2 版本再次计为新增来源。统一预检清单还有 4,745 份待导入，已启动第三批；另外 124 份机关补充清单已通过来源预检。逐批报告位于 `artifacts/legal-corpus/full-release/`，旧失败和重试记录均保留。全集入库与正式别名切换尚未完成。

真实 GPU 环境位于 `.superpowers/venvs/embedding-cuda`。已完成固定 Yuan 快照的 `#token-window-mean-v1` 策略：实际模型输入每窗不超过 512 token，均值后 L2，1792 维；64 行真实复测 GPU 约 30.09 行/秒、CPU 约 0.997 行/秒，仅代表该样本。落盘向量缓存支持校验与断点复用；首次固定 6,127 版本任务完成 913 行后遇 GPU 超时，原失败证据保留。10:30 的新 CUDA 运算和两行真实模型推理通过。Docker 随后由用户手动打开，原容器 ID/数据卷核对一致并恢复，未改动 Docker/WSL 配置；向量任务沿原选择集继续，全集向量化尚未完成。

已通过独立试点别名 `dataset_full_trial_20260906` 发布五份真实法规，主索引 72 行、导航 5 行，10 次真实检索均找到目标并完成 MySQL 权威全文和来源回填，见 `trial-retrieval-verification-v2.json`。试点没有长条子块，不能替代真实子块召回验收。正式 `dataset_v1` 尚未切换，最近核对仍为旧索引 `lawyer_dataset_b085352f09c8`。备份本体位于 `.superpowers/backups/full-corpus-20260906/`，未做恢复演练。下列 S6 计数为各阶段历史证据。

2026-09-06 已完成确认范围内 **16,006 份文件的文本切块**。最终独立全范围核验 **16,006 份 / 921,117 块 / 0 问题**，`complete=true`、`scope_complete=true`；中文 CSV 全部 16,006 行为 completed。输出位于 `C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\chunks`。734 份结构待复核，图片未做 OCR；其中 1 份经明确标记的受限静态恢复，正文和页眉另做独立逐段对照。文件格式与查找方法见 [切块文件使用说明](../legal-corpus-chunk-files.md)。原件未改写，136 份 Word 控制日志均确认设置恢复，无遗留 Word。

项目已具备较多后端业务切片与可构建 Vue 前端，但尚不能认定为全栈生产交付完成。当前主线是“真实法规数据集 + 结构化分块 + 有据问答”。S2 category 已贯通全栈；S1 保守回退、父子持久化、叶子索引和单文件 CLI 已接线。S3 标题/编章导航已接发布、检索与 API 装配并通过隔离 OpenSearch 测试；本次没有重发布真实数据集，重启前观察为旧整条块。文件导出、S6a2 来源准备、S6a3 来源证明、S6a4 受控批量导入、S6b1 持久发布/保守恢复、S6b2 集合清单发布/事实质量检查、S6b3 有界批次构建、S6c1 CPU Embedding 可复现运行和 S6c2 有界模型执行已接线。真实 Yuan 在独立冻结环境及隔离容器复测通过，超时/取消保留实际占用、忙状态和退出清理已验证；下一步统一实际检索测试配置并验证搜索到权威条文回填。真实重发布及 S4 A/B、S5/S6 其余工作继续推进。

本次盘点有 167 个后端源文件、168 个后端测试 Python 文件、10 个 Alembic 迁移。数字仅描述此次基线，不能作为未来验收指标。

## 能力与实际边界

以下源码位置除前端外均相对 `backend/src/lawyer_agent/`；“存在”表示检查了代码，外部运行结果单独列出。

| 能力 | 已存在的实现与入口 | 实际边界 |
| --- | --- | --- |
| 身份/租户/授权 | `api/v1/auth.py`、`accounts.py`、`tenants.py`、`platform.py`、邀请、审计、权限与会话模块；MySQL/Redis 集成测试 | 真实短信/邮件/微信 Provider 及上线参数未完成生产复核；前端微信/手机号入口为禁用占位 |
| Matter/文档/规则审查 | `matter_documents.py`、`reviews.py`、`rule_checks.py`、`rule_packs.py`；案件列表/筛选/编辑/状态/owner/参与方/冲突检查，文档版本/复核、规则包与 DOCX 报告 | HTTP 能力多于 UI；工作台仅覆盖租户切换、案件列表/新建、DOCX 登记、风险项和报告下载 |
| 原件存储 | `infrastructure/objects/object_store.py` 的 `LocalObjectStorePlaceholder`；DOCX 登记可解析并保存相关数据 | 受控 Object Key 是占位引用，不提供真实 MinIO 原件持久化、预签名上传/原件下载；报告下载不代表原件下载完成 |
| 法规导入与读取 | `corpus_source.py` 原件/派生双哈希准备；`cli/corpus_publish.py --source/--preflight`；migration13 与 `application/legal_source_proof.py` 将来源证明接入同一导入事务 | 2026-09-07 11:12实际只读核对15,982/16,006份v3来源已入库、24份未入库；Parser v4纠错尚未重新入库。未知日期不补造，来源证明不等同不可变原件对象存储；早期5份试点及合成回读/回滚验证不是全集验收 |
| 批量导入与集合发布 | reviewed 批量导入；集合 `corpus_publish_set --check` 和审核摘要绑定发布；有界主索引/导航批次；单文件共享质量检查；migration14 持久发布及 `corpus_publication status/resume` | 用户已确认候选现行有效，未知日期保留。8份完整报告覆盖14,280版本，1,702个已入库版本尚缺完整报告；最新该选择质量检查仍有104个条号结构阻断。合成多批次发布/恢复及真实5源检索试点已验证，全集容量、剩余向量、完整报告与正式别名发布未完成 |
| 层级分块 S1/S4 前置 | `legal_chunk_structure.py` 保守回退，`legal_index_chunks.py` 校验父链及选择叶子；migration12 放开同条同类型多块，仓储拓扑插入/保存点回滚 | 短条独立父块、可信长条子块已接入 CLI；子块目前直接挂条文，尚非完整款项嵌套树；真实 OS/模型重发布及 A/B 待验收 |
| 法规类别 S2 | LegalCategory 七值，migration11 精确二进制 CHECK，导入/CLI/ORM/HTTP/工具与前端中文呈现 | 旧行 unknown；category 不一致重导入显式拒绝；不自动从目录推断或更新既有分类；迁移和真实 MySQL 回读已验证 |
| Embedding/检索 | 默认 Yuan /1792/L2，CPU/离线 Provider及单任务后台执行；embedding-cpu 锁依赖、只读缓存及CPU镜像；OpenSearch BM25+k-NN/RRF、MySQL 权威条文证据组装 | 冻结Windows环境和只读断网Linux容器真实3文本推理通过；并发/超时占用/关闭通过受控测试，线程不能强杀。真实检索效果及全量性能未验收；旧真实集成测试仍有BGE/512硬编码和缺模型skip，须统一显式配置 |
| 有据问答 | `application/legal_retrieval_qa.py`：Evidence → structured claims → Citation Gate；`POST /legal/questions` 与 `/stream`；前端 `AskView.vue` | SSE 实际为 `started → answer/error → done`，完整门禁后发送答案，不是逐 token 法律答案流；门禁主要检查 ID/授权/日期/状态，不能代替语义支持与法律正确性评测 |
| 普通流式对话 | DeepSeek Provider → ModelGateway → `POST /legal/chat/stream` → `ChatView.vue` | 直接转发消息，接受 system/user/assistant；没有接入 Evidence/Citation Gate 或正式人工审批，不能作为“已核验法律回答”验收 |
| 工具网关 | `application/mcp_gateway.py`，`api/v1/agent_gateway.py`，`api/dependencies.py`，前端 `AgentToolsView.vue` | 显式允许 meta 及 4 个 corpus 只读工具；是本地受控 Registry/HTTP 分派，不代表外部 MCP 协议连接或模型自主 Tool Calling 已完成 |
| AI Job 运行时 | AI Job HTTP、签名信封、Outbox Publisher、Inbox/权威 Claim/Consumer 等模块 | Effect/Retry/Maintenance、Synthetic Handler Harness、Compose 多进程和故障注入等已明确延后；不要因存在 Worker 文件而宣称完整任务执行闭环 |
| 部署 | Vue/Nginx Dockerfile、CPU API Dockerfile、Compose 中 api/web/mysql/redis/rabbitmq/opensearch/minio | S6c1 API镜像成功构建且隔离CPU模型推理通过；未据此认定整个Compose栈、Web镜像、业务E2E或生产就绪 |
| 其余目标业务 | 总体规格覆盖文书起草、租户知识库、专题合规、教育、生产运维 | 不应从现有通用 Matter/Rule Pack/AI Job 积木推断全部场景已实现 |

## 当前主线的执行依据

- [总体架构](../superpowers/specs/2026-08-31-lawyer-agent-enterprise-architecture-design.md)：产品与安全边界。
- [身份与授权规格](../superpowers/specs/2026-09-01-multitenant-identity-authorization-design.md)：涉及身份/租户时读取。
- [持久 AI Job 规格](../superpowers/specs/2026-09-03-tenant-durable-ai-job-runtime-design.md)与[运行时计划](plans/2026-09-03-tenant-durable-ai-job-runtime.md)：涉及任务执行时读取，保留明确延期范围。
- [集合发布试点与层级分块方案](plans/2026-09-10-corpus-set-publish-pilot.md)：当前 S1–S6 主线。S1 有实现，后续条目按实际缺口验收。
- [单文件导入发布 CLI](plans/2026-09-10-corpus-publish-cli.md)：现有导入入口。
- [检索问答装配](plans/2026-09-10-retrieval-qa-composition-non-model.md)、[问答 SSE](plans/2026-09-10-retrieval-qa-sse-non-model.md)、[对话 SSE](plans/2026-09-10-chat-stream-sse-endpoint.md)：三者不可混为同一协议。
- [旧全栈审计](plans/2026-09-10-fullstack-delivery-audit.md)仅作为历史切片证据；其“全部完成”和旧 BGE 默认值不能覆盖本次核验结果。
- [生产上线待复核](../project-decisions/pending-production-reviews.md)：开发可继续，但不能越过未批准的生产门禁。

## 后续优先事项

### 文档切块交付要求（用户最新补充）

全部已确认范围的文档切块完成并核验后，主动告知切块文件的绝对路径、文件清单与处理结果。范围沿用 `F:\ai律师数据库\法律法规数据库`，不擅自扩到案例/裁判文书库。原件只读。

可交付的 UTF-8 JSONL 切块及来源/处理清单已导出到 `C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\chunks\`，按源文件组织；该目录已忽略，不将法规原文提交 Git。**文件切块与独立完整性核验完成，全量入库和索引发布尚未完成。** 目录中的 `文件清单.csv` 可按原始文件查找各份 `chunks.jsonl`、`paragraphs.jsonl`、`document.json` 的绝对路径；`verification.json` 保存最终完整性证据。

全量汇总必须区分成功、需转换、无法解析、重复跳过与待核对；存在未解决文件时不能用“多数成功”宣称全部切块完成。输出要包含源文件路径/哈希、Parser 版本、父子关系和质量标记，不能把未知法规日期/效力变为已确认。

2026-09-05 范围盘点及随后补查：**16,006 份 Word 文件 = 12,826 DOCX + 3,179 DOC + 1 DOCM**。DOCX/DOC 分布：地方法规 11,785/3,168，法律 338/2，监察法规 2/0，司法解释 92/8，宪法 6/0，行政法规 603/1。2026-09-06 按文件头核对，33 份 DOCX 实际为 OLE 旧 Word，因此受控转换候选共 **3,212 份**。原扩展名与来源路径保持不变。

补查其余扩展名后，发现地方法规/吉林下还有 1 份 `.docm`，实际 Word 范围修正为 **16,006 份**；此外 184 ZIP 继续按原要求跳过。DOCM 只以安全 ZIP/XML 静态读取并标记 `macro_enabled_container_no_execution`，不送入 Word、不执行宏。默认导出与全量核验已纳入 DOCM，`--docx-only` 仍明确只选择 DOCX。

真实刑法样本只读抽查发现增补条号被截断，产生 38 组重复编号；修复与独立复审已通过。再次核验 465 条、49 条增补条、零重号，1,123 段/66,412 字符覆盖一致，465 条内存导入映射通过；原件哈希未变。普通无空白条头保留旧行为，分界不明的增补候选明确拒绝，不并入前条；普通段首引用消歧仍为已知解析边界。[切块文件导出计划](plans/2026-09-05-corpus-chunk-file-export.md)已完成文件交付验收，机械切块完整性与结构/法律元数据核验分别记录。

导出器与独立核验器已完成首版修复及复审：真实子块偏移、UTF-16 DTD/entity 拒绝、可见分隔符、空文档拒绝、缓存版本/内容校验、路径隔离与来源哈希时序均已接入。离线导出/读取缓存为 v2，旧分块接口保持兼容。司法解释 92 份 DOCX 试点独立核验 **92 份 / 3,498 块 / 0 问题**，18 份结构待核对；试点目录不代表交付全集。

历史首轮全集导出 **12,795 份完成 / 3,177 份待转换 / 34 份失败**；独立核验已完成的 **737,607 块**，其余问题均为来源未完成。随后修复 33 份 OLE DOCX 的格式识别与 1 份大图片 DOCX 的读取限制；这些旧失败数不代表当前交付状态。

单文件 DOC 转换的路径、fingerprint、有效正文三项复审均已通过。按 [小批次复用计划](plans/2026-09-05-word-conversion-batches.md)完成的合成 26 文件试验仅启动两个实例；5 份真实司法解释试点总计 18.181 秒（含退出），原件哈希不变。批处理空闲退出竞态已修复并复审通过。全量 Word 批次 3,211 成功，唯一异常 `地方法规/福建/厦门经济特区粮食安全保障规定_20221028.doc` 普通重试与 OpenAndRepair 均在打开阶段失败（0x800A1897 / 6295）；其后按 [受限静态恢复计划](plans/2026-09-06-static-legacy-text-recovery.md)完成独立版本的恢复，并保留原失败记录。74 个正文段、2 个页眉段逐段内容/顺序相同、38 条序列相同，原件哈希不变；证明文件为 `artifacts/legal-corpus/static-recovery-verification.json`。累计 3,212 个成功转换记录中须区分 3,211 Word 与 1 静态恢复，未关闭 Office 文件校验。

历史格式修复后快照为 12,801 完成 / 3,205 待转换；已被最终导出替代。最终 **16,006 完成 / 0 待转换 / 0 失败**，共 **921,117 块（含 22,381 子块）**，独立核验退出 0。734 份结构待复核，其中 12 份重复条号触发保守回退；5,186 份有图片/文本框未 OCR 标记，2,246 份有自动编号未解析标记。它们的可读取文本已覆盖，不代表图片或结构、法律效力已专业审核。

最终 `文件清单.csv` 为 16,006 行，全部 completed，来源集合和块数与 manifest/核验报告一致。`artifacts/legal-corpus/import-candidates.jsonl` 是16,006份审核前候选快照，原pending和空字段不改写；用户此后已确认现行效力及缺日期处理，新的 `full-release/reviewed-*.jsonl` 绑定该审核记录并补充可追溯机关/地域证据。不得把原候选文件直接传给正式导入，也不重复要求用户确认同一审核。后续不要无目的重跑全量 Word 转换覆盖已验证的静态恢复记录，优先沿现有双哈希派生产物做来源预检。

### 1. 接续已确认的语料与分块主线

S2 category 已完成开发与独立审查（包括 MySQL 大小写/尾空格约束修正），见 [S2 计划](plans/2026-09-05-corpus-category-s2.md)。父子持久化与 CLI 接线是 S4 前置，不能等同于完整 S4 验收。

之后依照既定方案推进 S3 标题/章节定位、S4 子块召回与整条回填、S5 其它内容分段器、S6 分批全量导入。落实前必须解决以下已发现的衔接问题：

- S1 已按“仅超长条拆分、缺失/不一致结构回退整条”收敛；子块仍直接指向条文父块，不等于完整“款→项→目”嵌套树。
- `corpus_publish` 已接层级函数；父子顺序、图校验、失败保存点恢复、叶子过滤通过测试。MySQL 合成 DOCX 测试验证子块命中去重后恢复完整权威条文；实际 OS/模型链路仍须验证。
- 受控CLI重放已校验既有条文与块图并保留Chunk UUID；v4及本轮v5实际重放事实/ID不变。EvidenceAssembly按version/provision回填；历史发布快照和跨系统回滚整体仍须验收，不能仅因新重放通过而视为完成。
- S6b1 已把单文件 CLI 改为先提交发布记录，再切 OS alias，最后原子提交 snapshot 与完成状态；已确认切换后的恢复不再次切换，确认不明则持续占用。S6b2 集合与单文件发布已接事实质量检查和审核摘要，旧未审核 ready 不能经生产恢复入口开始切换。旧低层 `publish_set/publish_version` 兼容接口仍保留原协议，不能用于新的生产发布入口；未决请求人工处置仍需完善。
- 元数据候选清单、受控旧 Word 转换、受限静态恢复及批量导入 CLI 均已实现并复审；全集文本导出及独立核验完成。集合清单发布及自动门禁已接入，候选元数据和专业质量仍需核对，不能直接将离线切块推入生产索引。持久发布与恢复说明见 [操作说明](../legal-dataset-publication.md)。
- CLI 现缺省 `status_unknown`、日期 None；未给日期/版本标签时用原件 SHA-256 来源标签保持重跑稳定。既有数据中曾由旧 CLI 自动填写的日期/状态需要来源复核，不能批量静默改写为“已确认”。

### S6a2 来源准备与预检（2026-09-06）

按 [来源准备计划](plans/2026-09-06-corpus-source-preparation.md) 接入 `--source`（兼容 `--docx`）、`--source-root`、预期原件哈希、受控转换清单/根目录及 `--preflight`。正文/表格和辅助段落分离；来源身份保留原件 URI/哈希，实际读取路径/哈希、转换器版本/指纹、质量标记分别返回。默认 parser 标识改为 `corpus-docx-v2`，不能以旧版本静默重跑覆盖；结构摘要包含条号、路径、全文和段落边界。

预检输出 `corpus-preflight-v1` JSON，不创建 Settings/DB/模型/搜索客户端、不输出正文、不写业务库。其 `metadata_review_status=not_verified`、`database_provenance_persisted=false` 明确区分候选、解析证明和持久化；静态恢复来源带 `static_recovery_requires_quality_review`，普通导入拒绝。既有版本的数据库来源证明和重跑结构比对仍是下一切片，不能凭预检成功宣布全量入库完成。

独立审查发现并已用合成 RED 复现、修复：两次路径哈希之间临时替换再恢复正文可使摘要与正文不一致；普通转换缺少指纹仍被接受。现对最多 64 MiB 的同一份字节快照计算摘要并解析，保留读取后双哈希校验，所有成功转换记录强制有效指纹。修复后独立复审通过，独立合成复测及 7 组回归共 134 项通过。共享 Reader 增加字节读取，主 XML 必须为 `w:document` 且有单一直属 `w:body`，拒绝正文外段落与非法主体结构。

最终只读试点：5 份司法解释（其中 1 份受控 DOC 派生）、74 条、311 个正文段，与已验收的 `paragraphs.jsonl` 正文和辅助段落顺序/内容完全相同，双哈希与 `document.json` 一致。报告：`C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\preflight\judicial-five-20260906.json`。标题/机关仅用候选，未确认法律元数据，未入库或发布；未重跑全集切块或 Word。

最终后端本地验证：`pytest tests/unit tests/api tests/contract -q` **1,834 passed / 1 skipped / 1 warning**（19.04 秒）；跳过项为平台符号链接不可用，警告为 Starlette/httpx 弃用提示。`ruff check .` 通过，`mypy src` 189 个源文件通过。重启后 `docker ps` 无运行容器，`127.0.0.1:13306` 不可达；本次未运行 MySQL/搜索/真实模型集成，未启动或改动共享栈。前端本次未改动/重测。

### S6a3 来源证明持久化与重跑校验（2026-09-06）

按 [持久化计划](plans/2026-09-06-corpus-source-proof-persistence.md) 新增 `LegalSourceProof` 领域对象、应用门禁和 `legal_version_source_proofs` 表（migration13）。每个法规版本保存原件/读取 URI、双 SHA-256、结构摘要、实际 Loader/Parser、转换版本/指纹及质量标记。证明引用独立于 CLI 的法律来源引用覆盖参数；已校验的十六进制转换指纹在写入前归一化。

CLI 在同一 MySQL 事务里导入版本、验证/创建证明，再替换层级块。完全相同重跑不覆盖证明；缺失证明返回 `source_proof_missing_requires_review`，任一证明字段不一致返回 `source_proof_conflict`，旧块保持不变。静态恢复在应用服务调用仓储前返回 `static_recovery_requires_quality_review`。新迁移不补造旧证明，证明表非空时拒绝降级；预检仍如实返回 `database_provenance_persisted=false`，无数据库副作用。

已使用本地已有 `mysql:8.4` 建立本任务独立临时容器（127.0.0.1:13307、临时随机凭据、tmpfs 数据），与 `.worktrees/identity-authorization` 共享栈隔离。11 项实际 MySQL 检查通过：原生及派生双身份回读、相同重跑、原件/结构/转换指纹冲突、缺证明、证明或 Chunk 写入故障整体回滚、旧版本升级不补证、PK/FK/CHECK 约束、带数据降级保护、完整迁移往返和 Alembic 模型一致性。真实语料、业务库、现有别名和付费模型均未操作。验证结束后已核对容器 ID/任务标签并停止本任务容器，确认自动移除及临时凭据删除；未删除共享栈数据。

全套检查曾发现 CHECK 名称叠加命名约定后超过 MySQL 64 字符并产生 ORM 漂移；已缩短本次未发布迁移/模型的约束名，重新通过命名限制和真实 `command.check`。最终本地 `pytest tests/unit tests/api tests/contract -q` **1,921 passed / 1 skipped / 1 warning**（19.59 秒）；平台符号链接跳过和 Starlette/httpx 警告未变。Ruff 通过，mypy 193 个源文件通过。独立复审及 104 项相关合成回归通过。

下一步是按清单逐文件、独立事务的受控批量导入和集合发布恢复；不能直接把所有 pending 候选当作已核对的清单。本阶段仍保留既有并发首次导入可能因唯一键竞争失败、相同重跑重新生成 Chunk UUID 的行为；不能据此宣称并发永远成功或跨系统历史回滚已完整验收。

### S6a4 受控批量导入（2026-09-06）

按 [批量计划](plans/2026-09-06-controlled-corpus-batch-import.md) 实现 `cli/corpus_import_batch.py`；操作方式见 [批量导入说明](../legal-corpus-batch-import.md)。新清单要求显式 `legal-corpus-import-v1`、reviewed、核对记录引用、原件哈希和已核对名称/机关/法域；不会接受或自动升级旧 pending 候选。整份清单在业务服务前检查 schema、重复、路径和容量，current 行同时要求公布/施行日期。声明的 reviewed 不等于程序完成了专业审核。

单文件和批量 CLI 共用 `_import_prepared`，每份独立事务提交版本、证明和层级块。单份文件/写入错误记录稳定 code 后继续；取消继续传播。报告新建且逐行 fsync，原/转换清单摘要绑定整次运行，真实数据库 ID 只在提交完成后记录。报告 I/O 失败立即停止后续导入且不补成功 summary；中断后用原清单、新报告路径重跑，从 MySQL 证明决定 imported/replayed，不信任旧报告而跳过文件。原/派生双哈希仍逐份核验，转换清单只在一次运行中加载一次。

独立审查曾复现 Windows ADS 报告路径可绕过来源/清单保护，以及手造转换 catalog 可指向派生根外或抹掉静态恢复局限。已分别用 6 个路径 RED、2 个 catalog 漏洞 RED 复现后修复：报告拒绝 ADS/保留设备/尾点空格别名；选中转换记录重新验证路径/字段并从记录重建 provenance，不重扫全清单。最终独立复核 154 项相关回归通过，两类漏洞均已关闭。

最终 `pytest tests/unit tests/api tests/contract -q` **2,010 passed / 1 skipped / 1 warning**（22.11 秒）；符号链接平台跳过与 Starlette/httpx 警告未变。Ruff 通过，mypy 195 个源文件通过。本任务临时 MySQL 的批量/单文件/来源证明 **12 项集成通过**（14.95 秒）：混合原生/派生成功、无效文件及写入故障不污染其它事务、相同重跑保持版本/证明、提交后报告失败仅留部分结果、重新运行恢复已提交版本并继续剩余文件。测试仅使用合成资料，未批准或导入真实 pending 全集、未重跑 Word/切块、未操作共享栈或模型/别名。结束后已核对任务容器 ID/标签，停止并确认自动移除，临时凭据已删除。

S6a4 之后已完成下述 S6b1、S6b2、S6b3、S6c1、S6c2；下一步真实检索与权威条文回填、S4 A/B。完整产品和生产验收仍未完成。真实批量导入须先取得可追溯的元数据和质量核对材料，不能凭候选名称/日期直接确认现行法。

### S6b1 持久发布与保守恢复（2026-09-06）

[实施计划](plans/2026-09-06-durable-dataset-publication.md)及[恢复操作说明](../legal-dataset-publication.md)。集合服务新增不切别名的 `build_set`，单文件 CLI 在关闭构建读会话后，通过独立事务保存发布意图；切换前输出 ID。migration14 保存不可变候选、旧目标和历史记录，唯一活动别名阻止并发发布，物理索引禁止重复登记。

`ready → switching → acknowledged → completed` 每次转换独立提交；CAS 只允许一个进程发送。确认已持久化后，恢复核对目标并在一个 MySQL 事务写快照与完成状态；历史 completed 重试不会覆盖更新快照。超时/取消/确认丢失保持 switching，不重发、不自动释放。按 `status --alias` 可找回提交成功但未打印 ID 的未完成记录；`status` 仅读数据库，`resume` 不读取来源或调用模型。

OpenSearch 别名读响应严格校验；切换要求明确成功确认，HTTP 200 不能代替 acknowledged=true，异常正文不回显。build 在模型调用前拒绝非法 UUID、重复版本和串版本 Chunk。实现已独立复审。最新后端组合 **2,070 passed / 1 skipped / 1 warning（22.41s）**；全库 Ruff 通过，mypy **200 个源文件**零错误。跳过为平台符号链接，警告为 Starlette/httpx。

真实服务验证使用专属临时 MySQL（13307）和 OpenSearch（19201）、随机隔离测试库/索引及合成 4 维向量，不调用模型。两个跨系统测试验证集合构建、切换确认后快照失败恢复，以及切换成功但确认丢失后继续占用。仓储测试覆盖 CAS 竞争、事务失败回滚、历史保护、精确 CHECK、活动 ID 找回及 Alembic 升降级/无漂移检查。最终组合结果见本计划验证记录。

S6b1 的后续质量切片已在下节完成；ready 的机械状态仍不能单独证明专业批准，需检查候选中的审核信息。旧低层兼容发布接口仍不具备持久协议；未决请求需要运维证据，尚无强制释放命令。历史保存从此次协议开始，不自动补旧快照或证明完整回滚。真实 16,006 份仍未批量入库或重发布。

### S6b2 集合发布与事实质量检查（2026-09-06）

按 [集合发布质量计划](plans/2026-09-06-reviewed-set-publication.md)完成严格成功批量报告读取、数据库事实检查、稳定 SHA-256 与显式审核引用绑定。集合 `--check` 只读数据库；单文件 `--quality-check` 先导入再检查，不构建索引或发布。检查与构建共用显式 REPEATABLE READ 视图，关闭读会话后进入持久发布事务。报告绑定法规/版本身份、来源三个摘要、条文/块数量、法律元数据、全文与父子关系和模型配置；Chunk UUID 单独重建不使审核摘要失效。

未知效力/日期/类别、法域不受支持、地方地域缺失、来源证明不一致、静态恢复、条号或父子图损坏均拒绝。现有法域白名单为 `national`、`CN`，不能自动映射其它标记。外部原件覆盖率始终标记 `requires_source_review`；来源标记、降级块供专业复核，程序不能证明审核引用所指工作真实完成。重复叶子文本无法唯一定位且不能由其余叶子证明全文覆盖时保守拒绝，持久偏移仍待补齐。质量报告展示安全元数据与配置，不输出全文。

独立复审补上报告法规身份/计数核对和旧 ready 恢复绕过；生产恢复在 CAS/OS 操作前检查完整审核记录与配置，bool 维度不可冒充整数。旧 acknowledged/completed 只允许收尾或读结果，switching 保留未知占用。Embedding L2 使用缩放计算处理有限极值，拒绝零向量。

最终后端 `.venv/Scripts/python.exe -X utf8 -m pytest tests/unit tests/api tests/contract -q --tb=short`：**2204 passed / 1 skipped / 1 warning**（22.31s）；跳过为平台符号链接，警告为 Starlette/httpx。`-m ruff check .` 通过，`-m mypy src` **204 个源文件**零错误。协议、报告读取与 CLI 交叉独立复审通过。

专属 MySQL 8.4 / OpenSearch 3 隔离服务组合 **22 passed**（42.08s）：持久发布、恢复、批量导入、来源证明、层级 CLI、集合审核发布及单文件审核后重放发布。新两项端到端使用合成 DOCX 和 4 维向量，验证审核摘要失配/未知状态拒绝、检查不调用模型不写索引、实际 alias/snapshot/BM25 成功，以及单文件 Chunk UUID 重建后审核仍有效；没有调用真实模型、导入真实全集或切换 dataset_v1。测试后按容器 ID/labels 核对并移除两项专属服务，临时 MySQL 凭据已删除，共享栈未操作。`git diff --check` 通过。本次无前端变更，未重跑前端或全栈部署。

S6b2 后续的集合构建内存与批次切片已在下节完成；这不代表 921,117 块容量验收。元数据/专业审核与外部服务凭据缺口继续独立记录。

### S6b3 有界集合构建（2026-09-06）

按 [有界构建计划](plans/2026-09-06-bounded-set-index-build.md)，主索引和导航均先逐版本检查、只保留摘要及计数，第二遍重读一致后分批追加。主索引每批向量生成后立即写入，batch_size 为 1..256；导航批次固定最多 256。新 Bulk helper 逐请求最多 8 MiB，预检当前批次后以迭代方式序列化，不保存全部序列化副本。单文档超限、来源变化、Bulk 部分失败、计数不完整都阻断候选；创建及 ready 响应必须明确确认且无矛盾错误字段。

内存边界为单版本图加当前批次及每版本小摘要，弱引用测试验证跨版本不保留前一版本 Chunk；完整条文/分块的大版本仍需容量验收。主索引和导航分别严格核对全部分片及文档总数，导航检查成功后才能标记 ready。不用逐批 replace，因此后批不会删除前批，旧单版本兼容入口保留原契约。

扩大合成服务用例时发现 Parser 将普通阿拉伯/全角条号保存为裸数字，序列门禁却只接受完整 marker。现兼容既有裸数字身份，不改 Parser 或 ID，继续拒绝零、非法夹杂、重复、漏号和乱序；三个真实 Parser→Gate 契约先失败后通过，相关 147 项测试通过。主构建/Bulk、导航和条号修复均获独立复审通过，导航矛盾确认遗漏已修复复审关闭。

最终后端 `pytest tests/unit tests/api tests/contract -q --tb=short` 为 **2288 passed / 1 skipped / 1 warning**（24.31s）；Ruff 全库通过，mypy **205 个源文件**零错误。跳过为平台符号链接，警告为 Starlette/httpx。命令仍使用现有 `.venv/Scripts/python.exe -X utf8 -m ...`，没有同步或改动本地依赖。

专属 MySQL 8.4 / OpenSearch 3 服务组合 **22 passed**（47.96s），其中集合用例实际导入两个合成版本、520 条主文档和 522 条导航，以主批次 128、导航批次 256 发布；检索返回全部 520 条且覆盖两版本，证明跨批保留。审核拒绝、单文件重放、持久状态/迁移/恢复仍通过。未调用真实模型、未导入真实法规或切换 dataset_v1；本次无前端或部署文件变更，未重跑其验收。

测试后按 ID/labels 清理两项专属容器及临时 MySQL 凭据，共享栈未操作。文档链接和 `git diff --check` 通过，真实配置/语料/临时目录没有被跟踪；全部工作仍保留在未提交工作树。

S6b3 的后续CPU依赖/实际模型/镜像切片已在下节完成，仍不能将CPU小样本当作GPU或完整检索验收。

### S6c1 CPU Embedding 可复现运行（2026-09-06）

按 [运行环境计划](plans/2026-09-06-embedding-runtime.md)新增 `embedding-cpu` Extra、官方显式PyTorch CPU索引和锁依赖。uv0.12.7解析后既有60包版本集合不变；独立 `.superpowers/venvs/embedding-cpu` 使用Python3.12.4按 `--frozen --no-dev --extra embedding-cpu` 实际安装78包，Torch2.14.0+cpu/ST6.0.1导入及uv pip check通过，未同步原backend/.venv。

Provider默认CPU、仅本地权重，显式trust_remote_code=False；Settings及HTTP/集合/单文件共享装配均传参数。新增 `python -m lawyer_agent.cli.embedding_smoke --model-ref <固定快照路径> --dimension 1792 --device cpu`，三条合成短文本经真实ModelGateway验证行数、维度、有限值、L2，输出安全JSON，失败非零，不跳过、不换模型。

现有Yuan快照 `fb4ab1ed9d3447b64c79e305c8913340327668b5` 在独立Windows冻结环境通过：3行/1792维/L2，29.961s（含加载）。镜像 `lawyer-agent-embedding-cpu:20260906` 构建成功，ID为 `sha256:c71314bb7adbc4c6b2bdd0fe18b03ee523ae0c9a49e490d7a26ccab09ed8670d`，其中206个源文件哈希逐一匹配工作树。Linux/Python3.12.14容器相同权重推理通过：13.154s，3行/1792维、范数约1；UID10001、断网、只读root与模型bind、4CPU/4GiB，无OOM，退出0，专属容器已移除。

报告：[Windows冻结环境](../../artifacts/embedding-runtime/windows-frozen-smoke.json)、[Linux容器](../../artifacts/embedding-runtime/linux-container-smoke.json)。三短句含加载耗时不作吞吐对比或法律质量评测。Compose例子config --quiet通过，模型缓存使用只读bind且不自动建目录，运维准备方式见 [运行说明](../embedding-runtime.md)。根 `.dockerignore` 采用源码白名单，实际build context为2.01MB，语料/秘密/虚拟环境未进入；镜像未打包权重，未操作共享栈。

最终后端 `pytest tests/unit tests/api tests/contract -q --tb=short` **2331 passed / 1 skipped / 1 warning**（23.08s），Ruff全库通过，mypy **206源文件**通过；跳过为平台符号链接，警告为Starlette/httpx。依赖/镜像装配与Provider/Settings/smoke均已独立交叉复审。没有运行付费模型、真实法规导入或dataset_v1切换；本轮未跑数据库/搜索组合集成、前端或完整Compose业务验收。

S6c1之后的有界执行、超时资源占用与关闭切片已在下节完成；GPU、全量容量、Web镜像、真实身份Provider及其它业务仍未完成。

### S6c2 有界模型执行与关闭（2026-09-06）

按 [有界执行计划](plans/2026-09-06-bounded-embedding-execution.md)将模型加载、编码和归一化移入专属daemon线程，每Provider单任务、无队列，输入在提交前验证并tuple快照。请求超时或取消不释放仍运行的容量；忙返回503/model_provider_busy，timeout保持504，SSE只发错误事件、不发未完成答案。API生命周期、集合/单文件共享构建、smoke在成功/失败/取消时关闭；未排空安全告警，Redis和engine仍走清理。

Runner可跨event loop使用。独立复审先RED复现两处loop关闭竞态，改用单向Future桥接并立即消费已投递异常，避免关闭后回调错误或未观察异常警告，等待者仍收到原错误。线程无法安全强杀；5秒关闭等待和模型timeout均不是全请求硬截止，Gateway仍等待失败记录，审计延迟边界见 [运行说明](../embedding-runtime.md)。

最终后端 `pytest tests/unit tests/api tests/contract -q --tb=short` **2388 passed / 1 skipped / 1 warning**（29.05s），Ruff通过，mypy **208源文件**通过；skip为平台符号链接、warning为Starlette/httpx。独立生命周期复审通过。真实MySQL/OpenSearch发布回归 `test_reviewed_set_publication_e2e.py` **2 passed**（15.08s，0skip），使用合成encoder，不当作真实模型检索证明。专属服务、容器和临时凭据均已移除，未操作共享栈。

独立冻结Windows环境真实Yuan快照3文本/1792维/L2，14.317s，模型运行期间449次heartbeat、并发拒绝、关闭排空与关闭后拒绝通过：[报告](../../artifacts/embedding-runtime/windows-bounded-real.json)。重建镜像 `lawyer-agent-embedding-cpu:20260906`，ID `sha256:188630385d8fad7a8a7ba541cc1997f80a9dd8e90c06b2a126c14f7914dd8b1d`；208源文件SHA匹配。Linux真实同快照3文本/1792维/L2，13.744s，UID10001、断网、只读root/权重、4CPU/4GiB，退出0无OOM：[容器报告](../../artifacts/embedding-runtime/linux-bounded-container-smoke.json)。本轮报告另存，保留S6c1历史报告，未同步原backend/.venv。

下一可执行切片：修正 `tests/integration/opensearch/test_real_embedding_knn.py` 的隐式BGE/512、固定9200端口、缺模型自动skip与双加载；显式配置本次Yuan固定快照/1792及隔离服务，真实运行搜索与权威条文回填，再做真实条文S4 A/B。三句推理不证明检索语义、法律正确性、全量容量或完整产品完成；真实16,006份元数据仍待专业核对。

### 2. 全栈可部署性与法律输出安全

这些是本次审计发现的真实缺口，不是已完成能力：

- `httpx` 已移入生产依赖并同步锁文件；隔离 `--no-dev` 环境已验证 DeepSeek/OpenSearch/消息/FastAPI app 导入。S6c1 CPU API镜像已构建并验证模型推理，但全栈Web和业务验收仍未完成。
- 本地 Embedding 已声明CPU安装组、锁文件及只读权重挂载并实际验证；GPU、模型超时/并发及真实检索评测仍需完善。当前未找到 LangChain/LangGraph 依赖或调用，LangChain 仍是应实现的架构方向。
- Compose api 已接内部 OpenSearch 地址、模型/维度和只读 DeepSeek JSON secret，默认指向空 Key 示例。S6c1 已验证隔离容器只读权重推理，真实Key及完整服务网络运行仍未验收；后端 `.env` 与 Compose 插值仍需分别配置。
- CitationGate 的草案/日期缺失放行缺口已按 [元数据拒绝计划](plans/2026-09-05-citation-metadata-fail-closed.md)修复；43 项聚焦回归与独立审查通过。失效日当天仍保留既有包含边界，生产语义待复核。正式语义支持/法律质量评测仍是独立门禁。
- 普通聊天直接输出无引用模型正文，与总体法律证据发布基线存在缺口。后续需统一法律问题路由/证据门禁及用户可控 system 消息边界；本次没有放宽规格或擅自更改接口。
- 补完整关键用户流程、流中断/取消/重登/切租户测试；现有前端 41 项主要验证纯逻辑与 API 封装，不能代表页面端到端已验收。

### 3. 保留既有延期项

- MinIO 原件存储、预签名直传/下载及安全文件处理；工作台运行规则检查及更完整案件详情/管理。
- 真实微信/短信/邮件、外部凭据、Vault/KMS、安全和业务角色矩阵复核。
- AI Job Effect/Retry/Maintenance、Synthetic Handler Harness、Compose 多进程、故障注入及零跳过全量门禁，仍按此前明确的延期决定处理；不擅自纳入当前语料切片。
- 学校/客户完整业务、生产 Kubernetes、容量/灾备/专业法律评测不能由开发切片测试替代。

## 初始盘点验证证据（33064c2 基线）

本次使用仓库现有 `backend/.venv/Scripts/python.exe`、Ruff 与本机 Node 直接执行对应命令，没有运行依赖同步或重建虚拟环境。以下结果证明的是此工作树与现有环境，不是锁文件干净安装结果。

| 检查 | 本次结果 |
| --- | --- |
| `python -m pytest tests/unit tests/api tests/contract -q`（backend） | **1239 passed / 5 failed / 1 skipped**；12.78 秒 |
| `ruff check .`（backend） | 通过 |
| `python -m mypy src`（backend） | 167 个源文件，零错误 |
| Vitest run（frontend） | 7 个测试文件，41 passed |
| `vue-tsc -b`，随后 `vite build`（frontend） | 均通过 |
| Compose `--env-file deploy/.env -f deploy/compose.yaml config --quiet` | 通过；本次使用已安装 `docker-compose.exe` 插件的绝对路径执行 |
| Compose ps | 当前仅返回 MySQL、OpenSearch、RabbitMQ 为 running/healthy；未据此认定其余服务运行 |
| OpenSearch `/_alias/dataset_v1`、`/dataset_v1/_count`、`/dataset_v1/_mapping` | 均 HTTP 200；alias 指向 `lawyer_dataset_b085352f09c8`，72 文档，向量 1792 维 / l2 |
| MySQL/Redis/RabbitMQ/OpenSearch 全套集成、真实 Embedding/DeepSeek、镜像构建、浏览器 E2E | 本次未运行；没有据历史通过记录宣称本次通过 |

脚本测试失败说明：

- 4 项位于 `backend/tests/contract/test_dev_script_security.py`，1 项为 `test_openapi_identity_authz.py::test_migration_script_fails_closed_without_a_database_url`。
- 日志先出现子进程 UTF-8 解码失败，随后因 `completed.stderr` 为 `None` 触发字符串拼接 TypeError。最小 PowerShell 中文输出/抛错复现也出现乱码；当前启动的是 PowerShell 7.6.4。尚未修改测试或脚本，不能将这些失败算作通过。
- 1 项跳过是平台无法建立符号链接；另有 Starlette/httpx TestClient 弃用警告。
- 首选 Shell 执行工具本次因 Windows CreateProcess 访问错误无法启动，后用现有 Node 运行时执行工具；`docker compose` 在该环境未发现子命令，直接调用已安装插件后校验通过。均为此次执行环境记录，不写成永久阻塞。
- 现有三个容器的 Compose labels 指向 `.worktrees/identity-authorization`，共享 `lawyer-agent` project。后续启动/重建/停止前核对归属，避免干扰其它工作目录。

## 持续实现验证（2026-09-05 起，工作区）

2026-09-06 安全/地域修复阶段组合：**1596 passed / 1 skipped / 1 warning**（15.76s），Ruff 全库通过、mypy 183 个源文件零错误。包括 [旧 DOCX Loader XML/ZIP 安全边界](plans/2026-09-06-docx-loader-xml-boundary.md)和 [导入地域冲突](plans/2026-09-06-legal-import-region-conflict.md)修复；两者独立复核分别 22/8 项通过。地域修复只保守拒绝冲突，不代表已支持同名多地域法规并存；Loader 只对齐安全边界，完整正文语义统一仍待 S6a。

随后完成 [S6a1 元数据候选清单](plans/2026-09-06-corpus-metadata-candidates.md)与 [条号质量门禁修复](plans/2026-09-06-quality-gate-article-sequence.md)，独立审查分别通过 37 项和 61 项；前导零兼容增量另有 32 项复核通过。候选生成不读取源正文、不证明哈希已复核，正式日期/地域保持空、效力未知、审查待定；尚未接批量入库或发布。条号门禁支持百千和连续增补、拒绝未识别/重复/漏号及计数不一致，未改变发布装配。纯 Word 驱动测试与 Windows 能力探测解耦，经本机模拟不可用主机验证，尚非 Linux 真机验收。

上述改动的前一检查点：**1692 passed / 1 skipped / 1 warning**（17.27s），Ruff 全库通过，mypy **185 个源文件**零错误。

受限静态恢复新增后，最新本地组合：**1773 passed / 1 skipped / 1 warning**（18.82s），Ruff 全库通过，mypy **188 个源文件**零错误。命令为后端现有 `.venv/Scripts/python.exe -X utf8 -m pytest tests/unit tests/api tests/contract -q --tb=short`、`-m ruff check .`、`-m mypy src`。跳过为平台符号链接，警告为 Starlette/httpx。CLI/转换来源记录、导出/核验标记传播及静态二进制读取器均已独立批准；真实恢复全文对照和全量文件核验另行通过，不包含在 pytest 数量中。

以下为在初始盘点之后的新执行证据；初始 Shell 环境的 5 个脚本失败在正常 exec 环境复跑为 20 passed，未改动安全脚本。

| 切片 | 已运行验证与结果 |
| --- | --- |
| `httpx` 生产依赖 | 独立 `.superpowers/venvs/runtime-smoke` 冻结 `--no-dev` 安装，四个运行模块导入通过；Provider/Search/消息聚焦 32 passed |
| Compose AI 配置 | OpenAPI secret 契约 + Compose contract + DeepSeek Settings 合计 39 passed；Ruff、Compose config 通过；独立审查通过 |
| S2 分类 | 聚焦 unit 46 passed，前端 50 passed/typecheck/build；真实 MySQL 分类迁移、HTTP/工具 10 passed；七合法值与 `LAW`/`Law`/尾空格拒绝均验证；独立审查通过 |
| 索引叶子 | 聚焦 21 passed；全版本图预检在外部副作用前完成；独立审查通过 |
| S1 保守回退 | 聚焦 10 passed，包含阈值、短条、缺失/不一致段落、窗口逐字符覆盖；独立审查通过 |
| 父子持久化 | 真实 MySQL 层级/分类迁移/旧仓储/onboarding 4 passed；多个同类型子块、反序父链、失败保存点恢复、拒绝有损 downgrade、Alembic check 均覆盖；独立审查通过 |
| CLI 层级接线 | 准备阶段 unit 8 passed；真实 MySQL 合成 DOCX import-only、重复导入、旧 hit 整条回填 1 passed；Ruff 与 CLI mypy 通过；独立审查通过 |

CLI 回归灵敏度验证：仅在测试进程临时替换为旧整条分块器，断言在 `2 chunks > 2 provisions` 处预期失败；正常新分块器通过。此测试不加载模型、不访问外部语料。新 Chunk parser 标签为 `<来源parser>/hierarchical-v1`；无段落信息的通用 onboarding 仍保守保存整条。

镜像构建实际尝试：默认 Docker credential helper 存在路径错误；使用隔离匿名 Docker 配置后，web 基础镜像的 Docker Hub token 请求超时。API 单独构建进入 `uv sync --frozen --no-dev` 后长时间无进展，已中断。镜像均未验收，不修改用户全局 Docker 凭据设置，也未启动/停止共享 Compose 栈。

截至 S3 开始前的组合检查：`pytest tests/unit tests/api tests/contract -q` 为 **1300 passed / 1 skipped / 1 warning**（13.39s）；Ruff 全库通过，mypy 168 个源文件零错误。跳过仍为平台符号链接，警告仍为 Starlette/httpx。

同一阶段真实服务组合回归为 **15 passed**（29.37s）：CLI、父子持久化、分类迁移、旧 Chunk/onboarding 仓储、法规 HTTP/工具和 OpenSearch 写确认。CLI 多子块测试使用额外独立数据库，避免影响其它测试的降级验证。OpenSearch HTTP200但部分失败现在会中断发布；随机临时索引写/读/幂等替换/空删除通过并清理，独立审查通过。

S3 [标题/编章导航纵切](plans/2026-09-05-s3-navigation-index.md)的独立审查已通过：导航侧索引构建/标记 → alias 发布 → 标题候选定位 → 同一版本范围的 BM25 与 k-NN；无强匹配回退全局，旧索引兼容，已标记的新索引导航损坏则拒绝，显式版本查询跳过导航。导航不进入法律证据正文。

S3 审查聚焦 9 个 unit 文件 **137 passed**，对应 9 个源文件 Ruff/mypy 通过。真实 OpenSearch 导航适配器 **2 passed**；新增发布→导航→双路检索纵切 **1 passed**（13.49s，合成 4 维向量，无真实模型）；旧发布/数据集检索/MySQL 快照/CLI 组合 **4 passed**（33.01s）。均使用隔离随机索引/测试库并清理，不切换运行数据集。全库门禁仍待当前增补条号修复结束后重新执行。

增补条号修复复审通过后、导出功能开始前，最终组合 `pytest tests/unit tests/api tests/contract -q --tb=short`：**1457 passed / 1 skipped / 1 warning**（13.81s）；Ruff 全库通过，mypy 173 个源文件零错误。跳过为平台符号链接，警告为 Starlette/httpx。该结果不预先证明随后新增导出功能。

DOCX/DOCM 导出、精确定位核验与单文件 Word 转换首版修复后，组合检查为 **1521 passed / 1 skipped / 1 warning**（16.56s）；Ruff 全库通过，mypy 182 个源文件零错误。单文件 Word 转换仍待三项修复的独立复审，不将测试通过代替该门禁。司法解释全部 92 份 DOCX 已导出并独立核验：**92 份 / 3,498 块 / 0 问题**，其中 18 份结构待核对；全集导出随后启动，不能预先宣布 16,006 份全部完成。

## 每次交接的最小更新

### 2026-09-05 用户重启前暂停点

- 用户明确要求暂停，准备重启电脑；停止推进和新任务，待用户恢复后续做。所有代码与文档保留在当前未提交工作树。
- 全范围首轮导出与独立核验均已结束，当前 **16,006 份中 12,795 份完成、3,177 份待转换、34 份失败**；独立核验确认已完成文件共 **737,607 块**，全范围仍不完整。575 份存在结构待复核标记。
- 34 份失败已定位：33 份扩展名为 `.docx`、实际头部为 OLE DOC，需纳入受控转换；1 份 DOCX 因未读取的大图片触及通用成员大小限制。下一步通过测试修复格式识别和只对实际读取 XML 施加成员上限，保留总展开大小限制。`source_format.py` 尚未创建，不应假定已接线。
- 正式输出目录为 `C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\chunks`，已有 `manifest.jsonl`、`summary.json`、`verification.json` 与各文件的 `chunks.jsonl`、`paragraphs.jsonl`、`document.json`。**不得宣布全部切块完成**；最终全量成功核验后生成并提供 `文件清单.csv`。
- 单文件转换独立审查已通过。批量复用实现完成首轮测试，尚待独立审查；26 份合成文件验证两个 Word 实例有界复用，5 份真实司法解释试点成功，原件 SHA-256 不变。`artifacts/legal-corpus/converted/manifest.jsonl` 累计 7 份（原 2 份加新 5 份），尚未重新接入本轮导出。不得将这 5 份提前计入上述完成数。
- 恢复顺序：先检查 Git 与转换恢复日志/后台归属，再审查批量转换；修复 33 份伪装 DOCX 和大图片边界；批量完成转换并可恢复地重跑导出；全范围独立核验及文件清单；向用户报告真实完整目录。预计转换候选为 3,179 份 DOC 加 33 份 OLE DOCX，共 3,212 份。保留来源只读、跳过 184 ZIP、DOCM 静态读取不执行宏的边界。

记录核验提交、工作树变化、完成的纵向链路、实际命令及 passed/failed/skipped、外部服务是否真实验证、剩余缺口和下一步。历史索引、测试计数和网络故障仅作为带上下文的记录，禁止继续回填到 AGENTS.md 或推断永久有效。
