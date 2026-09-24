# 持久数据集发布与恢复

单文件入口 `python -m lawyer_agent.cli.corpus_publish` 在主索引和导航索引机械核验通过后，提交发布记录并输出 `publication_id`，随后切换别名。导入事务、构建读会话与发布状态事务分离；`--import-only` 不发布。构建成功不代表法律内容或真实模型质量已通过专业验收。

## 检查与审核集合

集合入口接受带逐文件 `metadata_review_ref` 的完整成功批量导入 JSONL 报告；预检报告、部分失败报告、缺少证明或重复版本不能用作发布选择。该入口读取数据库事实和报告，不重新读取外部原件。

多个完整报告使用 `--release-set`，与 `--import-report` 二选一。集合文件为严格 UTF-8 JSON：`schema_version` 为 `legal-corpus-release-set-v1`，`total` 为全部选择数，`reports` 是按固定顺序排列的成员；每项包含报告绝对路径 `path`、原始字节的 64 位小写 SHA-256 `sha256`、该报告选择数 `selection_count`。集合只引用报告，不能拼接或补写原导入事件。

入口逐份核对报告摘要与完整性，并拒绝跨报告重复来源、重复版本及不同 dataset/parser/chunk parser。集合最多 1 MiB、256 份报告、累计 20,000 项；成员沿用单报告 64 MiB 有界读取。全部选择进入同一次 REPEATABLE READ 检查与构建；报告路径、摘要和选择范围进入质量摘要及候选快照。单报告调用保持原有序列化和审核摘要形状。

```powershell
.venv/Scripts/python.exe -m lawyer_agent.cli.corpus_publish_set --import-report C:\reports\import.jsonl --alias dataset_v1 --check
.venv/Scripts/python.exe -m lawyer_agent.cli.corpus_publish_set --import-report C:\reports\import.jsonl --alias dataset_v1 --review-ref review-ticket-123 --expected-quality-sha256 <检查结果quality_sha256>
.venv/Scripts/python.exe -m lawyer_agent.cli.corpus_publish_set --release-set C:\reports\release-set.json --alias dataset_v1 --check
```

`--check` 在显式 REPEATABLE READ 会话中检查数据库事实，不调用模型、不建索引、不写发布记录。检查失败退出非零。专业复核应核对结果中的质量标记和外部原件完整性，并保留独立审核凭据。发布时必须提供审核引用和完全匹配的质量摘要；配置、元数据、证明或内容变化后需要重新检查与复核。检查和构建共用同一数据库事实视图；模型显式使用 L2 归一化。

单文件入口同样要求 `--metadata-review-ref`；先使用 `--quality-check` 导入并输出检查结果，再使用 `--review-ref` 与 `--expected-quality-sha256` 发布。`--preflight` 和 `--import-only` 不要求这些发布凭据。单文件检查可能先写入导入及分块数据，但不写索引或发布记录。单文件 `selection_sha256` 使用原件 SHA-256，集合使用完整导入报告文件 SHA-256；质量摘要另绑定权威内容及完整配置。相同导入重放核对已存条文和完整分块语义图，保留原 Chunk UUID；存在差异时拒绝，不借重放重建旧图。

候选历史保存 `quality_sha256`、`review_ref`、`selection_sha256`、`normalization` 和安全质量报告。审核引用是操作者提供的凭据，程序不能证明专业审核实际完成；自动检查通过也不能替代法律结论质量或原件解析完整性的独立审核。

自动检查核对报告中的法规/版本身份、三个来源摘要及条文/块数量与数据库一致，检查效力、日期、类别、地域、来源证明、全文摘要、条号连续性、父子图及全文父块和叶子文本覆盖。当前法域仅接受既有明确标记 `national`、`CN`；其它标记需验证并显式支持，不能自动改写为中国法。地方类别必须有地域。静态恢复、未知元数据及损坏图不能发布；其它来源质量标记和降级块列入待专业复核事项。

报告的 `configuration` 显示摘要绑定的 alias、模型、维度、L2、parser 和选择文件摘要，逐版本摘要展示元数据和来源信息。`source_text_coverage=requires_source_review` 表示未核验外部原件到数据库的完整性，不能当作覆盖率 100%。迁移 `20260906_15` 已增加父块相对字符偏移；历史 null 偏移仍走保守定位，重复文本无法唯一定位且剩余叶子不能证明覆盖时拒绝。模型 L2 归一化拒绝零向量，使用缩放计算避免极大/极小有限值溢出或下溢。

## 集合构建批次

`--batch-size` 控制每次模型输入与主索引追加文档数，允许 1..256，CLI 默认 20。构建先逐版本检查完整父子图并记录摘要，再逐版本读取、核对摘要，每批向量生成后立即追加到新索引。导航同样两遍预检，每批最多 256 项。不会为了分批写入删除已有批次；旧的单版本 replace 接口不用于集合构建。

每个 Bulk HTTP 请求最多 8 MiB（包括 UTF-8 内容、操作行和换行），按完整文档拆分，单文档超限拒绝。每批确认并刷新后，主索引和导航分别检查完整分片计数；实际计数必须与预检和写入数量精确相同，导航全部通过才标记 ready。来源变化、部分写入、计数不一致或创建/ready 确认矛盾均不能产生可发布候选。

构建内存与单版本图及当前批次有关，不随全集向量累积；特别大的单版本、长事务、真实模型速度和全量性能仍须验收。失败可能留下未发布的物理索引，保留证据后按索引归属处理，不能用已有索引名重跑或切别名绕过失败。

## 查询与保守恢复

在 `backend/` 使用与发布时相同的数据库及 OpenSearch 配置：

```powershell
.venv/Scripts/python.exe -m lawyer_agent.cli.corpus_publication status --publication-id <UUIDv7>
.venv/Scripts/python.exe -m lawyer_agent.cli.corpus_publication status --alias dataset_v1
.venv/Scripts/python.exe -m lawyer_agent.cli.corpus_publication resume --publication-id <UUIDv7>
```

`status` 仅读 MySQL，无模型、来源文件或 OpenSearch 依赖。若发布进程在提交后、打印 ID 前中断，可通过 `--alias` 找到未完成记录；完成后的历史记录按 ID 查询。输出仅含 ID、状态、目标及计数，不含法规正文或供应商异常。

| 状态 | `resume` 行为 |
| --- | --- |
| `ready` | 先验证完整审核记录及配置一致性，再 CAS 占用、核对旧目标并尝试一次别名切换 |
| `switching` | 返回 `publication_outcome_unknown`，保留占用，不重发 |
| `acknowledged` | 只核对当前目标并原子提交快照及完成状态，不再次切换 |
| `completed` | 返回持久结果，不构建、不切换 |

超时、取消或切换确认丢失可能留下 `switching`。一次查询发现别名已指向候选索引，也不能证明不存在尚未执行的请求，因此自动恢复不会将其标记成功或释放占用。需运维核查请求是否终止、实际目标及审计证据；本切片没有强制释放或强制确认命令。`publication_alias_conflict` 同样保留记录供核查。

旧的未审核 `ready` 记录不能通过生产恢复 CLI 开始切换，返回 `publication_quality_review_required`；旧 `acknowledged`、`completed` 仍允许确认后的收尾或只读返回。底层恢复服务保留兼容参数，新生产调用必须启用 `require_review=True`。这些校验保证记录内部一致性，不构成密码学签名或已控制数据库情况下的防篡改证明。

同一别名不能并发存在多个未完成发布，物理索引不能重复使用。不要通过重跑新发布、直接改别名或删除记录绕开冲突。构建失败可能保留未发布候选索引；此入口不自动清理。此协议不等于 OpenSearch CAS，要求应用发布统一经过持久入口。

运行前须已升级至 migration14（`20260906_14`）。有发布历史时降级拒绝自动删除记录。历史记录从接入此协议开始保留，不会自动补齐旧快照或证明旧索引可完整回滚；旧索引、旧版本和原件的保留策略需另行验收。旧低层 `publish_set/publish_version` 兼容接口仍未使用此协议，新增生产入口应组合 `build_set` 与持久发布服务。

集合清单 CLI、数据库事实检查、审核摘要绑定和有界批次构建已接入；真实法规元数据和法律质量审核、真实模型效果、全量容量及未决请求人工处置仍待验收。现有 16,006 份候选均未因此成为已批准发布数据。
