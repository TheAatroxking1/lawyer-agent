# 2026-09-10 法规文件级差异（解析产物间纯比较，非模型）

## 目标

spec 6.6「半自动更新」要求更新前先生成候选数据集并**计算文件与条文级
差异**；已实现的 `legal_corpus_diff` 只能比较**已入库的两个版本**
（DB 两 version），磁盘上的新来源文件尚未导入就无法与现网内容对比，
「文件级 diff/候选 LegalInstrument 关系/dataset_v2 生成仍为后续」
（版本 diff 切片尾注）。本切片新增**文件级 diff**：对两份 DOCX 解析产物
（`ParsedArticle` 视图序列）做纯比较，不写库、不依赖导入状态，输出
added/removed/modified/unchanged 四组与「是否有实质变化」，作为半自动
更新人工审核与 dataset_v2 发布判断的第一步。

## 范围（只做这些）

- 新增 `application/legal_source_diff.py`：
  - `SourceDiffEntry`（provision_no/structure_path/full_text）、
    `ModifiedSourceEntry`（provision_no + previous/current 两全文）、
    `LegalSourceDiff`（changed 标志 + added/removed/unchanged 与
    modified 四组）稳定冻结值对象。
  - `ParsedSourceArticle` Protocol（provision_no/structure_path/text）——
    复用文件级解析产物形状（parser 的 `ParsedArticle` 结构满足；
    application 不反向依赖 infrastructure）。
  - 纯函数 `diff_source_articles(old_articles, new_articles) ->
    LegalSourceDiff`：按 `provision_no` 键 + 全文比较分组（规则与
    `legal_corpus_diff.version_diff` 一致）；**输入顺序即稳定顺序**
    （文件未落库、无 char_start，不做猜测排序）；identical 时
    changed=False；空输入安全（两端都空 → changed=False）；
    同号重复输入一律稳定拒绝（与导入/解析同语义，绝不静默吞并）。
  - 错误族 `LegalSourceDiffError(ValueError)` 稳定 message。
- 不改 `legal_corpus_diff`/导入/DB；不建表；不读外部语料。

## 明确不做

- 不做「候选 LegalInstrument 关系」与 dataset_v2 自动生成（仍是后续；
  本切片只交付文件级判断原料）；不做文件差异 HTTP 端点；不把两份文件
  的版本归属当 DB 版本处理。

## 验证

- 离线单测（新 `tests/unit/test_legal_source_diff.py`）：真实 `ParsedArticle`
  视图构造 old/new 两文件——增/删/同号异文/未变分组与顺序；identical
  changed=False；空输入；重复条号稳定拒绝；modified 携带 previous/current
  全文。
- 真实 MySQL+合成 DOCX 集成（新
  `tests/integration/mysql/test_legal_source_diff_repositories.py`）：
  两份 docx bytes → `ZipDocxLoader`→`LegalStructureParser` 解析为两套
  解析产物 → `diff_source_articles` 分组正确；再各自经 round-173 映射 +
  受控导入成 DB 两个版本 → `LegalCorpusImportService` 导入 → 用既有
  `version_diff` 比对库内版本，断言**文件级 diff 与条文级 diff 分组
  一致**（证明文件级判断与落库后判断等价）→ 清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy
  零错误；无 Secret；树干净。
