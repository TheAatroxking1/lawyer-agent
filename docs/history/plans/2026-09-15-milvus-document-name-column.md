# Milvus 名称列回填

用户已明确要求在 Attu 集合增加名称列。目标为现有 `blog.lawyer_db`，复用已验证的 document_id 映射。

1. 核对集合身份、921,117条记录、已完成导入报告及名称映射摘要。
2. 新增 `document_name`，VarChar、nullable、最大1024 UTF-8字节；最长真实名称405字节。无主键、分区键、聚簇键或Analyzer。
3. 使用Milvus 3.0 REST `entities/upsert` 的 `partialUpdate: true`，只提交chunk_id和document_name。每组最多1024条，同一集合共享文件锁，按文件和行保存断点。
4. 每组先核对完整主键集合和document_id，拒绝冲突的已有名称；更新后逐条回读名称，首尾回读原8字段和1024维向量。中断后重放未确认组，名称一致则跳过写入。
5. 先完成首批1024条验证，再全集执行。结束时核对总数、空名称数、Schema、索引加载与稠密/BM25/混合检索。
6. 更新名称说明与项目现状，向用户报告Attu刷新入口和真实验证结果。

本切片不改变向量输入、BM25文本输入或生产问答装配。名称保持原文件名派生规则，不自动认定为封面正式标题。

执行结果：2026-09-15完成全部921,117条，1,231组、2,462项原字段/向量样本检查；全库null和空串名称均0。两索引Finished、pendingRows=0，加载100%，9次检索验证全部通过。25项相关单测、Ruff、严格mypy通过；原集合ID及既有字段/Function保持不变。最终证据见批次`milvus-blog-lawyer_db/document-name-backfill/final-verification.json`。

API依据：[新增字段](https://milvus.io/api-reference/restful/v2.6.x/v2/Collection%20(v2)/Add%20Field.md)、[3.0局部更新](https://milvus.io/api-reference/restful/v3.0.x/v2/Vector%20(v2)/Upsert.md)。
