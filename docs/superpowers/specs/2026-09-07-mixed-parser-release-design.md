# 显式混合Parser集合发布与来源替代

## 目标与依据

用户已授权全集入库、向量化与发布。真实现状：15,983来源的完整v3报告集合和86份通过真实质量检查的v4新报告，其中75个来源重复、9个此前报告缺口与2个旧v2升级来源新增到当前集合。目标是构造15,994个唯一来源的新集合，替换75个旧版本，保留旧事实/报告/快照且可恢复发布。现有v1/v2要求同Parser且全选，必须新增显式协议，不能删检查或裁剪旧报告。

依据总体架构6.2–6.4、AGENTS发布/来源约束及只读定位 `.superpowers/sdd/mixed-parser-release-design-audit-20260907.md`。本设计不改变法规效力审核、日期策略、成员Parser行为、模型维度或归一化。

## 方案选择

采用release-set-v3完整报告+显式成对替代+最终选择摘要。全量重导到v4会重做已验收来源并可能引入未审新解析差异；直接混拼或最后一条覆盖会失去来源选择与回滚证明。新协议明确保留全部非重复来源，只移除被精确绑定的from，不提供无理由任意过滤能力。

## 清单与规范来源证明

新增独立类型模块`domain/legal_release_provenance.py`，旧ReleaseSelection/ReleaseConfiguration实例布局不改。使用`MixedReleaseConfiguration(ReleaseConfiguration)`携带`provenance: ReleaseSetProvenance`（kw-only），父parser_version固定`release-set-v3/hybrid-v1`，父selection_sha256为清单原字节摘要，report_members必须空；provenance另存最终选择摘要。旧序列化及质量schema/digest不变。

清单schema `legal-corpus-release-set-v3`包含`total`最终数、`selection_sha256`最终选择摘要、`reports`完整报告声明、`replacements`成对替代、`numbering_reviews`（可空，沿旧绑定规则）。reports每项path/sha256/selection_count仍指完整成功报告，新增dataset_version/parser_version/chunk_parser_version声明并核对真实header。

类型契约：

- `ReleaseReportIdentity`：report_path、report_sha256、selection_count、dataset_version、parser_version、chunk_parser_version。来源Parser与chunk必须为既有hierarchical-v1/v2合法配对。路径/摘要/计数严格有界。
- `ReleaseEntry`：report_sha256、row（从1计，完整报告file行顺序）、selection: ReleaseSelection。条文审核如有由清单绑定后保存在selection；解析身份从唯一所属report查得。
- `ReleaseReplacement`：old: ReleaseEntry、new: ReleaseEntry。两个entry均来自已声明报告，source规范路径、source_sha和instrument_id完全相同，version_id不同。角色及旧新方向显式；旧来源不删除。
- `ReleaseSetProvenance`：manifest_sha256、selection_sha256、reports:tuple、entries:tuple（仅最终selected，确定顺序）、replacements:tuple。所有完整报告的每一行必须恰好落在selected或replacement.old，不能漏行/重复行；replacement.new必须就是selected中的同一entry。成员由report唯一SHA引用，报告路径亦唯一。一个source最多一对替代，不支持循环、链、三代；同source无显式替代拒绝。所有dataset相等，最终source/version唯一。

来源身份按现有file URI解码+Windows normpath/normcase规则比较，禁止仅按文本SHA合并不同路径。selection中每项source/input/structure、instrument/version、模式/数量、审核引用均进入证明。清单from/to表达式直接声明old/new完整entry字段，Reader与实际报告逐字段比较（numbering_review在仅最终选择上绑定）；不存在的source/version/report/行、不同source/hash/instrument、空替代、重复替代均拒绝。

最终选择摘要采用`release-selection-v3`域分隔的canonical JSON SHA256，内容包含reports身份、最终entries和replacements（不包含manifest_sha256/selection_sha256自身，避免递归）。字典key排序、UTF8、紧凑分隔、禁NaN；UUID按规范字符串，nullable与空字符串不同。公开`selection_digest(reports,entries,replacements)`供离线清单构造使用，读取时严格重算，不允许缺省摘要或自动覆盖错误声明。

证明JSON采用schema `release-provenance-v1`，有严格`to_dict/from_dict`往返，未知字段、bool冒充int、重复/非法UUID/过长值均拒绝。最多256报告、20,000最终来源、40,000完整行、256替代；完整序列清单/证明JSON最大64MiB，所有报告累计字节上限512MiB。v1/v2继续原1MiB清单与旧门禁；v3不因为新增上限而放宽旧输入。编号证据沿既有4MiB单件/16MiB总量及严格source/input/structure绑定。

## 质量与批准

`ReleaseQualityService.check`支持MixedReleaseConfiguration。先验证传入selection顺序/完整值等于provenance.entries（不能只比集合），再按version排序执行已有检查。每个成员使用从report确定的独立ReleaseConfiguration视图检查proof及chunk，不以集合parser字符串跳过检查。DB version/proof与source parser相等，chunk与声明chunk parser相等；dataset_version还需核对共同报告dataset。所有旧metadata、日期、条号、内容摘要、父子图、覆盖和计数检查保留。

混合质量输出schema `release-quality-v2`；旧v1报告/digest字节契约保持。v2输出`version_digests`有序事实摘要，以及序列化configuration和summaries，digest覆盖schema、configuration（含完整provenance）、version_digests和versions摘要内容，使持久快照可重算审查摘要；事实SHA不能替代再次读DB验证，但可阻止重排/替换已存摘要或展示summary时仍沿用旧批准hash。

新`MixedReleaseQualityReport(ReleaseQualityReport)`携带version_digests并返回v2；`release_configuration_dict`对新配置委托明确序列化，对旧对象原样返回。旧QualityReport.to_dict保持旧schema，不通过隐式检测字段来伪装新报告。

## 发布与恢复

CLI加载v3后构造MixedReleaseConfiguration，现有同一REPEATABLE READ事务执行质量及build。builder snapshot.parser_version使用固定集合schema身份，每个索引文档仍保留真实chunk.parser_version。完整证明仅保存于quality.configuration.provenance一处；candidate.manifest的`release_selection_provenance`保存`release-provenance-ref-v1`引用，字段为schema_version、manifest_sha256、selection_sha256、provenance_sha256。候选验证解析完整证明并重算canonical证明摘要，逐项核对引用，不伪造旧release_reports的连续全选范围。

真实容量证据见`artifacts/legal-corpus/full-release/mixed-release-json-capacity-20260907.json`：MySQL max_allowed_packet为64MiB，重复证明的JSON下界77,611,754字节超限；单份证明加已有summary为54,590,116字节，另需事实摘要及快照字段。因此保留一份完整证明及摘要引用，最终仍须验证实际序列化大小与隔离MySQL往返，不能以估算代替容量验收。

实际15,994质量输出完成后，完整候选格式JSON默认序列化为58,989,921字节；隔离MySQL读回暴露asyncmy 0.2.14大包缺陷：Windows Proactor在持有memoryview时扩容失败，单行达到16MiB时length-encoded前缀又可被误当EOF。证据`.sdd/asyncmy-large-json/reviewer-diagnosis.md`，17MiB真实publication store.get回归同样失败。修复采用持久层有界JSON传输：保留原JSON列与完整自含证明，通过单次SELECT的物化payload CTE按128KiB二进制字段分行，客户端校验连续序号/总长/UTF8/JSON；总文档64MiB、最多512行。不改数据库驱动、依赖锁或运行中的GPU。调用方第一次读取元数据时同时取得JSON字节SHA，后续分行读取须核对，以拒绝READ COMMITTED下并发变化造成的元数据/JSON混合视图。发布journal所有读路径及正式snapshot读取均须接入；小字段仍走同一有界协议。此传输修复和真实完整JSON往返通过前，不计容量验收完成。

域层候选验证增加明确v2质量分支：强类型解析配置/证明、校验model/ref/dimension/l2、集合schema、清单hash、最终digest、确定selected顺序==candidate.version_ids；检查quality summaries/事实digest覆盖同一selected集合且无blockers；重算v2批准digest。任何新证明出现但缺v2质量均拒绝，不允许降级到旧验证绕过。DatasetPublication序列化/JSON恢复必须保存这些字段；旧journal及v1质量分支保持原语义。

替代只改变新索引的选择，原DB版本/条文/Chunk及原物理索引保留。恢复不用当前磁盘清单重选来源；JSON保存完整proof，报告原字节与源码存档单独保留。公共模型snapshot/1792/l2不变；最终叶文本相同可命中现有cache，变化文本必须补算，禁止按source复用向量。

## 验收与运行边界

本轮先做合成TDD、真实15,994混合集合只读质量和隔离MySQL发布/恢复往返。GPU计算PID25876、启动器58724仍为9,803选择，49冻结文件不可改；OpenSearch保持停止，因此实际搜索发布验收须在模型排空后执行，不能把Mock当最终全集验收。新86v4与此前9新v3缓存另行准备，不改活动选择。

必须验证：旧v1/v2兼容及黄金digest；未声明/错配/链式替代拒绝；合法75替代后15994唯一来源；逐成员Parser篡改拒绝；v4实际导入摘要通过；全部质量/配置/summary/成员/from-to字段篡改使旧批准失效；持久JSON恢复后证明相同、旧事实不变；限定目录/大小/数量/缓存路径边界；最终整套后端和适配的隔离MySQL集成。全集质量仍有未修来源，不能为发布移除它们或把部分通过称全集完成。
