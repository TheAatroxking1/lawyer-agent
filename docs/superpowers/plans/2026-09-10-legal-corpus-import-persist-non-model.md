# 2026-09-10 受控法规版本导入落库（instrument / version / provisions 写路径）

## 目标

语料解析（DOCX → ParsedInstrument）、盘点、dataset_v1 发布、Chunk 持久化、
OpenSearch/Embedding 检索均已就绪，但 `legal_instruments` /
`legal_versions` / `legal_provisions` 三张表**在生产代码中没有任何写入路径**
（既有集成测试全靠裸 SQL seed），解析产物无法真正入库，版本化语料查询与
检索链路缺少数据源头。本切片补齐受控导入：一条命令原子地写入一个
instrument（按标题+法域精确匹配复用，绝不自动合并相似法规）、一个版本
（同 instrument+version_label 幂等/冲突）及其全部条文，随后可被既有
`version_at` / `provisions_for_version` / 证据组装读取。

## 范围（只做这些）

- `application/legal_corpus_import.py`：
  - 输入值对象 `LegalImportCommand`（instrument 身份：title/issuing_authority/
    jurisdiction/region_code；version 元数据：version_label/status/公布施行废止日/
    law_number/source_ref/dataset_version/parser_version；有序条文 draft 列表）
    与 `LegalProvisionDraft`（provision_no/level/structure_path/title/full_text）。
  - `LegalCorpusImportService.import_version(command)` 应用服务：
    1. 校验：条文非空、provision_no 不重复、draft 字段非空；按条文全文顺序生成
       连续 char_start/char_end 与 content_hash（sha256），构造 domain
       `Provision` / `LegalVersion`（version content_hash = 全部条文正文拼接的
       sha256，可复算）；
    2. instrument 归属：按 `(title, jurisdiction)` 精确查找——命中且机关一致则
       复用既有 id；命中但机关不一致 → 冲突错误（相似法规不自动合并）；未命中
       则新建；
    3. version 归属：同 `(instrument_id, version_label)` 已存在且**内容哈希与全部
       元数据（状态/日期/文号/来源/数据集/解析器）一致** → 幂等 replay 返回既有
       版本（不重复写）；已存在但内容或元数据任一不同 → 冲突错误；
    4. 原子写：instrument（如新建）+ version + 全部 provisions 单事务；
    5. 返回 `LegalImportResult(instrument_id, version_id, replayed)`。
  - 错误：`LegalCorpusImportError` 族（`instrument_conflict` /
    `version_conflict` / 校验错误），稳定 message 便于映射。
- `SqlAlchemyLegalCorpusImportRepository`（既有 `repositories/legal_corpus.py` 内新增）：
  - `find_instrument_by_identity(title, jurisdiction)`、`create_instrument`、
    `find_version(instrument_id, version_label)`、`create_version`、
    `create_provisions`（flush，同事务由 UoW/调用方控制）。
- 不改 Schema、不改读取仓储契约、不接 HTTP（导入端点/平台权限后续切片）、
  不碰 `F:\ai律师数据库` 与 ZIP；测试用合成正文。

## 明确不做

- 不做 DOCX→ParsedInstrument→ImportCommand 的自动映射与元数据提取（文件名/
  日期/机关抽取留给后续 parser 演进切片）；本切片导入命令显式给出元数据。
- 不改 inventory/publish/load batch 语义；不做跨版本差异合并或候选关系。

## 验证

- 聚焦单测（纯离线 fake 仓储）：新建落库（顺序/哈希/字段）、相同命令 replay
  不重复写且 replayed=True、同 label 异内容 409 语义、同 title+jurisdiction 异
  机关冲突、条文空/重复/坏字段校验、与既有读取侧闭环不在此层。
- 真实 MySQL 集成：导入 version 2020 → `version_at`+`provisions_for_version`
  读回一致 → 相同命令 replay 不产生新行 → 冲突版本报错 → 同 instrument 第二版
  独立成行 → 清理；alembic upgrade head。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
