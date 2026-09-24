# 当前 Milvus 集合的 PyMilvus 混合检索与重排设计

日期：2026-09-15。状态：用户确认后已实施本地Docker查询并完成PyMilvus实测；语义模型重排与标注质量评测仍为后续范围。用户指定参考 Milvus 多向量混合搜索文档并使用 PyMilvus。运行说明见[Docker查询](../../milvus-query.md)。

## 目标与现状

为本地 `http://localhost:19530` 的 `blog.lawyer_db` 提供可复用查询入口，输入问题，返回带文书名称、条号、正文与块身份的有序结果。

现有集合有921,117条记录，`vector`为1024维/COSINE，`sparse_vector`为从`text`生成的BM25向量；`document_name`已完成回填。既有REST检索验收不能替代本次PyMilvus SDK验收。此次检查的`D:/Python/python.exe`环境没有安装pymilvus。

后端当前`LegalHybridSearchService`装配的是OpenSearch，采用双路召回与RRF。先实现独立的本地Milvus查询模块，未来经检索适配器接入应用；不把当前离线64位十六进制ID转换成原事实库UUID，也不在此切片更换公网检索架构。

## 方案选择

| 方案 | 作用与代价 | 本次选择 |
| --- | --- | --- |
| RRF融合 | 按多路名次排序，不需要比较COSINE与BM25原始分数，也不调用额外模型 | 默认 |
| 加权融合 | 对各路归一化分数赋权，适合已有评测支持明确偏好时 | 对照实验，初始可试稠密0.6/BM25 0.4，不宣称最优 |
| 融合后语义重排 | 专门模型阅读问题与候选文本，再评分；增加延迟及模型费用/算力需求 | 后续独立接入，默认关闭，供应商未选定 |

RRF和加权融合都是检索结果融合。它们不会像语义重排模型一样重新理解候选正文，也不能保证排除页码噪声或判断法律结论成立。[多向量混合搜索](https://milvus.io/docs/zh/multi-vector-search.md)、[加权排序](https://milvus.io/docs/zh/weighted-ranker.md)。

## 数据流程与默认参数

1. 校验问题和可选文书范围，取得一次查询Embedding。
2. 构建两个`AnnSearchRequest`：稠密请求使用问题向量；BM25请求直接使用同一个问题文本，由已有Function处理。
3. 调用`MilvusClient.hybrid_search`做RRF融合，保留最多50个候选。
4. 默认直接返回融合结果前10项，并可显式选择查看全部50个候选。启用未来语义重排时，先对这50项评分，再返回10项。
5. 展示结果保留引用身份；进入回答阶段时再处理完整条文恢复、上下文预算和证据门禁。

| 设置 | 建议初值 | 说明 |
| --- | --- | --- |
| 查询Embedding | qwen3.7-text-embedding / 1024维 | 使用与入库一致的供应商、模型、维度及预处理约定；不自动切换本地模型 |
| 稠密字段 / metric | vector / COSINE | 与现有索引一致，不照搬示例中的IP |
| BM25字段 / metric | sparse_vector / BM25 | 输入问题文本，不自行计算另一套稀疏向量 |
| 每路召回数量 | 100 | `AnnSearchRequest.limit`；最多形成200个不同主键候选 |
| 融合候选数 | 50 | `hybrid_search.limit`；不是把两路各截成10后再融合 |
| RRF平滑参数 | 60 | `k`控制名次衰减，不是返回数量，也不是稠密/BM25权重 |
| 展示数量 | 10 | 与候选数分开配置 |
| 一致性 | Strong | 本地验证默认；以后按写入和时效要求评估Bounded |
| Milvus RPC超时 | 15秒 | Embedding请求另设30秒超时；不把二者混称为全链路硬截止 |

以上数量是本项目起始配置，不是官方保证的最佳值。AUTOINDEX先使用默认搜索参数；不机械复制针对IVF的`nprobe`。两路使用相同的文书/模型范围过滤，避免一路越界。

RRF按候选在各路出现的名次累加 `1 / (60 + rank)`，未出现在某路的候选在该路贡献0。按`chunk_id`合并同一命中，不按正文或文书名称合并不同版本。RRF分数只用于这次候选排序，不能解释为正确率或用COSINE阈值判断拒答。[RRF机制](https://milvus.io/docs/zh/rrf-ranker.md)。

## PyMilvus调用契约

使用`MilvusClient`，连接时显式指定`db_name=blog`。连接配置集中管理，Token从环境读取；不把凭据写入代码或文档。实施时在独立查询环境中选择与服务端3.0兼容的PyMilvus版本，实测后固定依赖版本，不直接升级业务后端环境。

| 对象/接口 | 关键设置 |
| --- | --- |
| 稠密AnnSearchRequest | data含一个1024维问题向量；anns_field为vector；param.metric_type为COSINE；limit为100 |
| 稀疏AnnSearchRequest | data含一个问题字符串；anns_field为sparse_vector；param.metric_type为BM25；limit为100 |
| RRF Function | name为legal_rrf；input_field_names为空列表；function_type为FunctionType.RERANK；params中reranker为rrf、k为60 |
| hybrid_search | collection_name为lawyer_db；reqs为上述两项；ranker为RRF Function；limit为50；output_fields显式列出业务字段 |

采用用户链接中当前版本的`Function`写法，作为查询参数传入，不向集合新增Schema Function。SDK接口也接受`BaseRanker`；同一实现只选一种配置方式，避免同时传入两种ranker。[PyMilvus hybrid_search契约](https://milvus.io/api-reference/pymilvus/v3.0.x/MilvusClient/Vector/hybrid_search.md)。

## 输入、输出与文书范围

查询模块接收：`query`、可选`document_ids`、`candidate_k`、`top_k`与受控排序方式。输入查询向量时还必须显式携带模型和维度信息。向量需非零、全部为有限数且长度1024；错误维度/模型直接拒绝。

既有`.sdd/embedding/embedding.py`是全集批处理脚本，不能直接import来生成问题向量，以免触发顶层批处理。实施时提取独立、无导入副作用的查询Embedding适配器，复用经过核对的API配置；本设计阶段不调用付费API。

返回字段：`chunk_id`、`document_id`、`document_name`、`article_no`、`text`、`chunk_type`、`parent_chunk_id`、`model`，加上`rank`与`fusion_score`。不默认返回1024维向量。PyMilvus结果由适配器从每个hit及其entity中提取，再转换为项目自有结果结构，不将供应商对象直接传给业务层。

例如查询“用人单位解除劳动合同有哪些限制”，结果展示为“文书显示名称 / 条号 / 原文 / 融合分数”，并保留chunk_id供后续引用。这个例子只规定输出形式，不预设正确法条或宣称已实测召回。

指定文书时先用现有名称映射确定document_id；同名多版本保留全部候选，未经确认不自动选择第一项。过滤条件由受控ID列表构建，使用SDK表达式参数或安全序列化，不直接拼接用户提供的完整表达式。两路AnnSearchRequest都使用同一个expr，不能只过滤稠密一路。

明确提供document_id与article_no时，提供单独的精确查询入口；它不依赖问题Embedding，也不以语义排名代替文书定位。第一版不自动从自由文本猜测文书ID。document_name没有纳入当前BM25或稠密向量输入，回填名称本身不会让标题成为新的全文搜索字段。

## 后续语义重排与条文恢复

如增加语义重排模型，每条候选输入由“文书显示名称 + 条号 + 正文”组成，保留候选索引到chunk_id的对应关系。模型只返回候选排序，不允许生成或更改候选ID；输出重复、越界或缺失必要结果须校验。超长文本按选定模型的token上限显式处理，不能静默截断后声称已比较全文。

有共同父块的多个命中，进入回答上下文时按`document_id + parent_chunk_id`恢复和去重，并保留所有命中子块ID。没有父ID的块按自身ID处理。当前离线ID只在本集合及其追溯文件中解析；父块缺失须标记不完整，不从旧MySQL事实库猜测映射。跨文书的相同条号或文本不自动合并。

## 错误与验证

模型欠费/无权限、超时、Milvus不可用均返回明确状态；默认不把失败伪装为空结果或自动退化为BM25。若后续增加显式BM25单路模式，响应必须标明实际使用的检索方式。没有候选时返回空结果及原因，不生成法律回答。

验收分两层：

- 工程验证：空问题、错误维度/模型、非有限向量、候选数小于展示数、两路范围一致、同名多版本、重复主键和返回元数据完整性；真实PyMilvus连接、三类搜索和查询前后记录数不变。开发阶段先观察聚焦测试失败，再实施。
- 质量验证：至少30条人工标注查询，覆盖指定文书条文、语义改写、多文书问题；比较稠密、BM25、RRF、加权融合的Recall@50、MRR@10、nDCG@10及延迟。参数在调参集选择，并在留出集报告与最佳单路基线差异；记录失败样本，不预先承诺混合方案必然更好。既有9个原文/原向量探针仅证明检索入口工作。

后续实施记录：Docker内已安装PyMilvus3.0.1并固定依赖，查询实现和18项单测完成；真实SDK12次检索及文书范围/精确/空范围验证通过，1次自然问题Embedding调用成功。原30题人工标注评测与回答模型接入尚未完成。详见项目现状及`artifacts/legal-corpus/pymilvus-query/final-verification.json`。
