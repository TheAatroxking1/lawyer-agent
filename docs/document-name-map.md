# 文书名称映射的查询与使用

本次已为全部 **16,006 个 document_id** 建立文书显示名称映射，覆盖来源 **921,117 个块**。名称取自原文件名，仅移除最后的扩展名；日期、版本后缀及原文件中的其它字符保留。它是显示名称，不代表从封面识别或人工核验的法律正式标题。

## 文件位置

```text
C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\attu-import\20260914T142608Z-7c17c736\document-names\
├── document_names.json   document_id → document_name
└── report.json           来源绑定、摘要、覆盖计数和命名规则
```

JSON内容示例（此项已实际对照Milvus验证）：

```json
{
  "711aa125aecd3505ddda997e1ab0ee762a34ab2084e4d0263c5f50b49e08da22": "人民检察院刑事诉讼规则_20191230"
}
```

映射共约2 MB；16,006个ID对应16,004个不同显示名称，存在2组同名文书。相同名称不会覆盖或合并ID，按名称查找会返回全部候选，需要结合原追溯文件中的来源路径等信息进一步选择。文件日期后缀不在本工具中解释为施行日期。

## PowerShell查询

在仓库根目录运行：

```powershell
$mapDirectory = "C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\attu-import\20260914T142608Z-7c17c736\document-names"

# 按已有ID查名称。
python -X utf8 scripts/build_document_name_map.py lookup --directory $mapDirectory --id 711aa125aecd3505ddda997e1ab0ee762a34ab2084e4d0263c5f50b49e08da22

# 按名称片段查所有匹配ID；保留同名、不同版本的全部候选。
python -X utf8 scripts/build_document_name_map.py find --directory $mapDirectory --name "人民检察院刑事诉讼规则"
```

`find` 使用字面片段匹配，不自动处理简称、书名号或版本选择；无匹配返回空列表。未知ID、名称冲突或文件摘要不一致会明确报错。

## 给本地RAG结果补名称

下面的类可从仓库根目录导入；如果将来部署后端，需在相应检索适配器中正式装配并提供映射文件，不能假定容器能访问宿主机路径。

```python
from pathlib import Path
from scripts.build_document_name_map import DocumentNameMap

names = DocumentNameMap(Path(
    r"C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\attu-import\20260914T142608Z-7c17c736\document-names"
))

# hits是Milvus实际返回的记录列表，查询outputFields须包含document_id。
# hits = response["data"]  # REST响应的记录列表，具体取法按调用客户端处理。
# 每次查询前无需重新读取映射；在进程中初始化一次后复用names。

# 已得到hits时执行：
# enriched_hits = names.enrich(hits)
# 结果保留text、article_no、chunk_id等原字段，并新增document_name。

candidates = names.find("人民检察院刑事诉讼规则")
document_name = names.lookup(
    "711aa125aecd3505ddda997e1ab0ee762a34ab2084e4d0263c5f50b49e08da22"
)
```

后续调用reranker和回答模型时，要显式把`document_name`与条号、正文一起组成上下文。新增本地映射不会自动改变已有向量、BM25输入或提示词。用户明确指定文书时，可先通过名称查ID，再在Milvus中使用`document_id`过滤；存在多个候选时不能自动取第一项。

## 生成和校验

```powershell
python -X utf8 scripts/build_document_name_map.py build --report "C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\attu-import\20260914T142608Z-7c17c736\report.json"
```

本次已实际执行，无需重复生成。相同来源重跑只核验，保持输出字节及修改时间不变；来源或输出损坏会拒绝覆盖。工具对照原manifest摘要及逐文档身份、路径、来源摘要，核对完整文档/块数和已完成导入报告的绑定，再发布输出；不读取或修改外部原件，不计算embedding。

实际验证：15项合成测试、Ruff（backend配置）、严格mypy通过；16,006项真实逐项对照及重跑不变检查通过；前/中/后三个Milvus记录成功补名，数据库行数仍为921,117。证据在同一批次的 `milvus-blog-lawyer_db/document-name-map-verification.json`。

## Attu 集合名称列

用户进一步授权后，已在`blog.lawyer_db`新增`document_name`：VarChar、nullable、最大1024字节，不开启Analyzer/分区键/聚簇键。**921,117条已全部回填，空名称0，1,231组逐条名称回读及2,462项原字段/向量样本检查通过**。2026-09-15 11:05北京时间验收：两索引Finished、pendingRows=0，加载100%；前/中/后三份样本的稠密、BM25、RRF混合检索共9次检查均命中来源，返回名称全部与映射一致。证据为 `milvus-blog-lawyer_db/document-name-backfill/final-verification.json`。

更新后的索引物理行数可能暂时高于921,117，因为upsert保留待回收的旧版本；实际可查询记录已用Strong `count(*)`核对为921,117。名称验收不以索引物理行数冒充逻辑记录数。

刷新Attu的集合结构和数据页面；查询或搜索时在`outputFields`加入`document_name`，即可随正文返回名称。调用模型时仍需显式传入该字段，数据库列不会自动接入公网问答入口。

运行工具：[backfill_milvus_document_names.py](../scripts/backfill_milvus_document_names.py)。从仓库根目录执行：

```powershell
# 只读预检
python -X utf8 -m scripts.backfill_milvus_document_names
# 授权写入或恢复未完成回填
python -X utf8 -m scripts.backfill_milvus_document_names --apply
```

每组1024条局部更新，只提交chunk_id和名称。每组名称全量回读，首尾原字段/向量回读通过后才推进断点。遇到未知ID、来源不符或已有名称冲突会停止。证据目录创建`STOP`文件可在当前组完成后暂停；恢复前移除STOP，再运行原命令。完成后重跑只核验总数和空名称，不重复更新已确认批次。

原向量和BM25输入保持原样，不调用Embedding，也不使用GPU。原初次导入脚本固定旧Schema，不应绕过其Schema保护再次整行导入。
