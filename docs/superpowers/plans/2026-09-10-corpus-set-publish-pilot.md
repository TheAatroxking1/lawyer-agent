# 计划：多法规数据集发布（set publish）· 司法解释试点 → 全量法律法规

## 背景与缺口

现有 `corpus_publish` 每次发布**一个 instrument 版本**到一个 alias（dataset_v1 只含
一部法）。`F:\ai律师数据库\法律法规数据库` 有上万个 docx/doc，其中大量法规要一起
进问答，需要一个「把多部法规（多 version）embed 进**同一索引**并原子切一个数据集
别名」的机制，且检索侧无需改动（命中 doc 自带 version_id/provision_id，证据组装
从 MySQL 按 id 恢复，天然跨法规）。

## 范围（用户确认）

- 只处理 `法律法规数据库`（排除案例库/裁判文书库）。
- 先设计 + 试点跑通再全量：**司法解释子目录 92 docx**（试点取前 5 份跑通机制）。
- .doc（老式二进制）用本机 MS Word COM 批量转 .docx 后并入，不在此试点阻塞。
- 元数据：文件名带 `_YYYYMMDD`、目录路径带分类 → 自动生成清单草稿供核对，
  不猜测不可得字段（发布机关等从标题/目录规则半自动 + 人工抽查）。

## 设计

### 新应用服务 `legal_dataset_set_publish.py`

```
LegalDatasetSetPublishService(chunks, gateway, search, alias, snapshot, ...)
  publish_set(version_ids: tuple[UUID,...], index_name, alias,
              model_ref, dimension, batch_size, dataset_parser_version)
    -> DatasetSetPublishResult(index_name, indexed_documents, previous_target,
                               version_count)
```

行为：
1. 逐 version `chunks_for_version` 读 chunk（缺失/空版本可配置跳过或报错）；
2. 分批 embed（gateway，维度/记录复用）；
3. `ensure_index(index_name, vector_dimension)` 一次；
4. `replace_documents(index_name, 全部文档, parser_version=dataset_parser_version)`
   （索引为本次新建，delete-by-parser 为空操作，幂等安全）；
5. `indexed==0` → 抛稳定 `LegalDatasetSetPublishError`（不发布空集）；
6. `alias.publish_dataset` 原子指向新索引（返回 previous_target）；
7. snapshot 登记：manifest 白名单 {index_name, alias, version_ids, model_ref,
   dimension, indexed_documents, instruments(数量)}；quality_metrics
   {indexed_documents, dimension}；复用 inventory 仓储 upsert（dataset_name=alias）。

复用而非改动既有单版本 `LegalDatasetIndexPublishService`（保持其契约与测试）。

### 批量导入/发布 CLI `corpus_publish_set`

读取 JSONL 清单（每行一个文件+元数据）：每份先走受控导入（与 corpus_publish 同一
逻辑）得 version_id，全部导入后再调用 set publish 到指定 alias（默认 dataset_v1）。
幂等：同一批清单重跑 → 各文件 replay 同 version，集合重建为新的物理索引再切别名。

### 清单预生成 `scripts/prepare_corpus_manifest.py`

扫描给定目录（默认 司法解释）：.docx → 产出 JSONL 候选：
- title = 文件名去日期后缀、清理 `+`→`、` 等；
- 日期 = `_(\d{8})`；发布机关：含「最高人民法院、最高人民检察院」→ 该组合，
  否则含「最高人民法院」→ 最高人民法院，未知 → 空（标待填）；
- version_label=f"{date} 版"；source_ref=绝对路径 file://；其余由 CLI 默认。

### 试点运行

司法解释目录 docx 按名称排序取前 5 → 清单 → 导入 5 部 → set publish 到 dataset_v1
（新索引）→ 验证：OS 别名解析/文档数=Σ条文、snapshot version_ids=5、
k-NN 跨法规问题命中正确条文（如 刑事解释类问题）。通过后再扩试点到 92 docx，
最终处理 法律(338)/行政法规(603) 并设计 地方法规(近 1.5 万) 的分批与去重策略。

## 验证

- ruff+mypy strict 零错；新增服务单测（多版本合并/空集拒发/幂等重建/snapshot 字段）；
- 试点 5 份 docx 真实 MySQL+OS 跑通 + 语义 spot-check。
- .doc→.docx 转换脚本（Word COM）单测/抽查 2 份。

## 备注

- 不触碰 案例库/裁判文书库；zip 文件忽略（按用户要求）。
- dataset_v1 语义 = “当前语料全集”（多法规）；如需按“部”筛选问答，后续加
  instrument/alias 选择，不在此轮实现。

## 结构化父子层级切块方案（2026-09-10 用户确认采用）

用户明确采用“法规—章节—条—款项”父子层级切分，要点：

1. **文档级记录**：每份法规一条文档级元数据——名称、类别（法律/行政法规/司法
   解释/地方法规/监察/宪法）、制定机关、地域、公布/施行日期、效力状态、版本。
   现有 `legal_instruments/legal_versions` 已覆盖大部分；缺口 = **类别(category)**
   字段（新增列 + 导入元数据 + 迁移），效力状态复用 `status`。
2. **层级识别**：parser 升级为“编—章—节—条”树识别（现有 `structure_path` 已带
   编/章路径；需把“条内 款—项—目”的段落边界保留下来，而不是合并成一段文本）。
   识别失败或歧义时**保守回退为整条父块**，绝不猜测拼接。
3. **父子分块**：每一条 = **独立父块**（不跨条拼接；极短条也不与下一条合并）；
   条文超长时按 款/项/目 拆**子块**；每个子块继承完整层级路径 + 该条全文引用，
   chunk 表已有 `parent_chunk_id` 可表达父子。
4. **滑动窗口（底层兜底，判断为有必要）**：仅当某个叶子（款/目）文本仍超阈值
   （建议 ~500–600 字）时，在其内部按固定窗口（如 400 字）滑切、重叠（如 60 字）、
   尽量在句末对齐；每个窗口仍归属唯一 条+款+项+目 并继承该条全文，**绝不跨款跨条**。
   默认开启且参数可调；若启用 BGE-M3（长文本）可将阈值调大或关闭。
5. **检索与回填**：检索用子块（或父块）提高精度 → 命中后**回填完整条文**与相邻
   必要条文（由 MySQL 权威表取全文，CitationGate 校验不变）。
6. **标题/章节级摘要索引**：对法规标题与 编/章 级标题生成摘要索引，支持两段式
   “先定位法规→再定位条款”（后续切片，独立索引/文档类型）。
7. **内容类型分段器解耦**：企业制度/合同/案件材料使用**另外的分段器**，不照搬法规
   规则；管线预留按 collection 选择分段器与窗口参数的扩展点。

### 实施切片（建议顺序）

- S1 结构化分块引擎：parser 保留条内段（款）边界 → `derive_hierarchical_chunks`
  （父=条，子=款/项/目，继承路径+条全文，长叶滑动窗口兜底）+ 单测与真实 docx 样本
  （含超长款）验证；chunk 表不新增列（父链用 parent_chunk_id）。
- S2 文档级 category 字段（迁移 + 导入映射/清单 + 契约/全栈更新）。
- S3 标题/章节摘要索引 + 两段式定位检索。
- S4 法规检索链路切到子块（召回）并保持整条回填证据组装；A/B 对照（整条块 vs
  父子子块）用真实条文数据出指标证据。
- S5 其他内容类型分段器（企业制度/合同/案件材料）+ 各自窗口参数。
- S6 全量导入（司法解释 92 → 法律/行政法规 → 地方法规分批），期间 .doc 经 Word COM
  转换。

### 现库数据说明

当前 5 部司法解释 72 块为“一条一块”旧模型产物；S1 落地后对已导入版本用新分段器
重建 chunks（`replace_chunks_for_version` 幂等）并重发布 dataset_v1 即可切换，旧索引
保留可回滚。
