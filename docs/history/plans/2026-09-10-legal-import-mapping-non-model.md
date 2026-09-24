# 2026-09-10 解析产物→导入命令自动映射（非模型）

## 目标

受控导入切片（`legal_corpus_import.py`）已实现 `LegalImportCommand`
（显式元数据 + 有序 `LegalProvisionDraft`），DOCX 结构解析切片已产出
`ParsedInstrument/ParsedArticle`（条号/结构路径/全文），但两者之间没有任何
自动桥接——生产若要从解析结果落库，仍要手写每条 `LegalProvisionDraft`。
本切片新增应用层**纯映射**：给定显式元数据 + parser 产物（按结构视图
鸭子类型接受，不反向依赖 infrastructure），自动生成受校验的
`LegalImportCommand`，使「DOCX→导入命令」链路可全自动、可离线单测、
不猜测任何元数据字段。

## 范围（只做这些）

- 新增 `application/legal_corpus_import_mapping.py`：
  - `LegalImportMetadata` 冻结值对象：与 `LegalImportCommand` 一一对应的
    全部显式元数据（title/issuing_authority/jurisdiction/region_code/
    version_label/status/published_on/effective_on/repealed_on/law_number/
    source_ref/dataset_version/parser_version），不发明、不含条文。
  - `ParsedArticleView` Protocol（provision_no/structure_path/text）——
    parser 的 `ParsedArticle` 结构上满足，应用层不 import infrastructure。
  - `map_parsed_articles(metadata, articles) -> LegalImportCommand`：
    - articles 非空（无条文不导入，稳定错误信息）；
    - 每个视图必须暴露 provision_no（非空文本）/structure_path（字符串元组）/
      text（非空文本），否则稳定拒绝（绝不静默丢弃或伪造字段）；
    - provision_no 去重校验（与导入命令同规则，提前稳定报错）；
    - 按输入顺序每一条映射为 `LegalProvisionDraft(provision_no=…, level=ARTICLE,
      structure_path=…, title=None, full_text=text)`（title 一律 None：parser
      当前不抽离条题，heading 文本并入 full_text，不猜测拆分）；
    - 元数据逐字段透传进 `LegalImportCommand`，强类型（mypy strict）。
  - 复用 `LegalCorpusImportError` 错误族（稳定 message），不改导入服务契约。
- 不改 `legal_corpus_import.py` 既有行为、不加 HTTP 端点、不读外部语料。

## 明确不做

- 不做「从 DOCX 文本猜测 title/authority/日期/状态」等元数据启发式（质量
  门禁禁止伪造来源）；不做导入 HTTP 端点、候选 LegalInstrument 关系
  （仍为延后）；不重写 parser。

## 验证

- 离线单测（新 `tests/unit/test_legal_corpus_import_mapping.py`）：真实
  `ParsedArticle` 对象映射为命令——顺序/条号/路径/全文/level=ARTICLE/
  title=None/元数据透传；空 articles、缺字段视图、空全文、重复条号稳定拒绝；
  与 `LegalStructureParser` 组合（合成 ParsedDocument→parse→map）验证
  heading 文本并入全文、结构路径为父级标题。
- 真实 MySQL 集成（新 `tests/integration/mysql/test_legal_corpus_import_mapping_repositories.py`）：
  合成 DOCX bytes→`ZipDocxLoader`→`LegalStructureParser`→显式元数据→映射→
  `SqlAlchemyLegalCorpusImportRepository` 导入→`version_at`/`provisions_for_version`
  读回字段与 parser 产物一致（含多段条文的全文拼接）→同命令 replay 幂等同
  version id→同 label 异内容冲突回滚零写入→清理；真实 docx→命令→MySQL
  全链路。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
