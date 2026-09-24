# 已有 JSONL 向量导入 Milvus

本说明对应用户在 Attu 创建的本机 `blog.lawyer_db`，通过 Milvus REST API 批量写入。Attu 与脚本连接同一数据库，完成后可以在 Attu 查看数据。它不是原项目 MySQL/OpenSearch 发布入口。

## 2026-09-15 实际完成结果

- 全集转换通过：16,006 份文档、921,117 条文本块和向量，生成 411 个 JSONL，错误数 0。
- 首批 768 条真实验证通过后接续，累计确认 3,693 个请求；最终 Strong count 为 921,117。
- 全部文件核对 SHA-256；每个文件首尾共 822 项回读核对正文、字段及向量。
- 最终 Flush 成功，稠密与 BM25 索引均 Finished，两个索引各覆盖 921,117 条，pendingRows=0；集合加载 100%。
- 前、中、后三个文件的原向量检索、BM25 原文检索、RRF 混合检索共 9 次探针均命中来源；这是存储和检索入口验收，不是一般法律问题的问答质量测评。
- Attu 中选择数据库 `blog`，打开 `lawyer_db` 并刷新数据和索引页面即可查看。本次通过官方 API 写入，Attu 的文件上传任务记录不承担本次导入进度。

最终证据：`artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/milvus-blog-lawyer_db/final-verification.json`。

## 本次输入与目标

- 来源报告：`artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/report.json`。
- 只接受 `scope=full`、`status=ready`、错误数为 0 的报告，并要求同级 `import/` 目录存在。转换仍在校验时不能导入暂存文件。
- 目标：`http://localhost:19530` → `blog` → `lawyer_db`；脚本固定目标，不从语料中读取连接地址。
- 使用原 `qwen3.7-text-embedding` / 1024 维向量，不调用百炼，不使用 GPU。
- 写入 8 个字段：`chunk_id`、`vector`、`text`、`document_id`、`model`、`chunk_type`、`article_no`、`parent_chunk_id`。已有 BM25 Function 从 `text` 自动生成 `sparse_vector`。
- 原始内容、主键、父子关系和向量均保留；更完整的追溯信息留在来源目录的 `provenance/`。

## 执行命令

**名称列变更提示（2026-09-15）：** 当前集合已新增`document_name`，已使用独立的 [名称回填脚本](../scripts/backfill_milvus_document_names.py) 完成921,117条回填及最终检索验收。下面是初次导入时的历史命令；原导入器固定旧Schema，新增字段后会拒绝继续，不能移除校验强行重跑，否则整行upsert可能清空名称。名称回填工具使用 `python -X utf8 -m scripts.backfill_milvus_document_names --apply`，当前已经完成，无需重复执行。

在仓库根目录的 PowerShell 运行，Python 3.12 标准库即可，不需要安装 PyMilvus。

```powershell
$reportPath = "C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\attu-import\20260914T142608Z-7c17c736\report.json"

# 只读预检：检查报告、集合字段及现有行数。
python -X utf8 scripts/import_attu_jsonl.py --report $reportPath

# 首次小批验证：3 个请求，每个请求最多 256 条，写入后自动暂停。
python -X utf8 scripts/import_attu_jsonl.py --report $reportPath --apply --max-requests 3

# 继续同一任务：自动从保存的位置接续。
python -X utf8 scripts/import_attu_jsonl.py --report $reportPath --apply
```

首次任务要求集合为空。脚本不会清空集合或删除其它记录；不满足条件时停止。不要同时用 Attu 或其它客户端向此集合写入。检查点绑定原报告摘要、集合 ID、连接目标和请求大小；更换报告或重建集合后不能直接沿用旧检查点。

## 写入与接续规则

1. 每个 JSONL 在读入时核对文件名、大小、SHA-256 和行数；内存只保留一个文件，不累计全集向量。
2. 每 256 条调用一次 `entities/upsert`，逐请求核对服务器返回的主键集合和确认条数。
3. 发送前先落盘 `pending`，确认后再推进检查点。网络中断导致结果不确定时，接续先回读当前批次，只接受与原输入完全相符的标量及允许 float32 舍入差异的向量；不会把未知记录直接覆盖。
4. 每份文件首尾记录回读，检查正文、身份及全部 1024 维向量。最终使用 Strong 一致性查询 `count(*)`，要求与来源报告和确认条数完全相等。
5. 集合级本机文件锁防止此脚本的不同导入任务并发；它不能阻止其它软件直接写库，因此还会检查库中数量和待写主键冲突。

## 结果与验证边界

状态文件位于来源报告同级：

```text
milvus-blog-lawyer_db/
├── schema-before.json    导入前的集合结构
├── schema-after.json     最终集合结构
├── state.json            输入绑定、文件/行游标、确认条数、待确认批次及最终状态
├── smoke-verification.json  首批768条及6个样本回读结果
├── flush.json            最终落盘请求确认
├── resource-samples.jsonl   运行中的资源采样
└── final-verification.json  最终行数、索引、加载与9次检索探针
```

- `paused`：按请求上限主动暂停，可以接续。
- `failed` / `interrupted`：先查看 `last_error`，排除故障后用原命令接续；不要删除检查点或清空集合。
- `data_verified`：写入确认、每文件首尾回读及全量数量核对通过。仍须检查索引构建、集合加载及实际检索，不能仅凭此状态宣布所有搜索功能已验收。

实际验收还检查稠密 `AUTOINDEX/COSINE`、稀疏 `SPARSE_INVERTED_INDEX/BM25`，并使用已有向量进行相似度检索、使用文本进行 BM25 检索。这证明存储与检索入口可用，不替代真实问答质量、跨租户授权、来源回填及法律引用门禁的端到端验收。

API 依据：[Milvus Upsert](https://milvus.io/api-reference/restful/v2.6.x/v2/Vector%20(v2)/Upsert.md)、[全文检索](https://milvus.io/docs/full-text-search.md)。
