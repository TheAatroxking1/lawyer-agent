# 已有向量全集导入 Milvus

用户在本任务明确授权：将已转换的法律文本及向量导入其已建的向量数据库，完成后说明操作方法。

## 已核实的边界

- 本机 Milvus 3.0，REST入口 `http://localhost:19530`，数据库 `blog`，集合 `lawyer_db`，collectionID `469076060698085606`；初始强一致count=0。
- 8个输入字段与转换脚本一致；主键chunk_id、AutoID=false；text启用chinese Analyzer，BM25 Function输出sparse_vector。稠密AUTOINDEX/COSINE和稀疏SPARSE_INVERTED_INDEX/BM25已存在。未更改用户Schema；chunk_id额外启用了english Analyzer，暂保留。
- 来源 `artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/report.json` 初查仍running；原进程54248正在逐批读回校验，绝不修改其运行时或将暂存目录冒充ready。
- 用户明确授权导入，使用官方Milvus REST API分批完成，与Attu查看的是同一数据库，不逐个操作411个GUI上传。

## 操作与验证步骤

1. 等待来源最终full ready及import目录出现；核对错误数、总文档/总块/批次计数及源报告摘要。
2. 编写并测试独立 `scripts/import_attu_jsonl.py`：固定本机目标，按报告逐文件校验SHA；每256行一次upsert；集合共享文件锁；新任务必须空集合；pending批次登记与不确定写入回读核对；原向量不重算，源正文不修改。
3. 执行只读预检；实际写入3个请求，强一致回读首尾原文/向量。成功后同一状态文件接续全集；监视本机和Docker资源，出现错误保存进度不清空集合。
4. 核对全部请求确认的IDs与条数、每个文件首尾回读、最终强一致count、索引与加载状态，并执行稠密与BM25检索探针。
5. 保存结果与过程报告，更新project-status。区别“Milvus导入完成”和原项目MySQL/OpenSearch生产发布，不把本次导入当成全产品交付。

## 不做的操作

不修改API Key、不调用百炼、不删除现有记录/集合、不改变授权或网络配置、不修改外部原件、不将用户原始导出目录移动或覆盖。

## 执行结果

全部步骤已实际执行。来源full ready：16,006文档、921,117条、411文件、错误0。首批768条和6个首尾样本回读通过后接续，最终3,693请求确认、411文件回读通过、Strong count=921,117。Flush确认后两个索引均Finished，各indexedRows=totalRows=921,117，pendingRows=0，加载100%；前/中/后3个来源的稠密、BM25、RRF检索共9次探针通过。最终证据在来源目录的 `milvus-blog-lawyer_db/final-verification.json`。

独立导入器16项合成测试、backend配置Ruff、严格mypy通过。复审发现的无pending接续覆盖风险和不同报告锁不共享问题已修复并验证，全部真实写入发生在修复及最终复审之后。未做无关应用变更或全栈生产发布。
