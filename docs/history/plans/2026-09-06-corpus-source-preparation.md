# S6a2 来源准备与单文件预检 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. 保留工作区未提交改动，不自动提交。

**Goal:** 让原件、受控派生和导入正文使用同一套可追溯读取逻辑，并接入现有单文件导入和无数据库副作用的预检。

**Architecture:** 新增只读来源准备适配器，原件身份独立于读取路径。使用离线导出已有 DocxExportReader/LegalStructureParser 语义；现有 corpus_publish 的 --docx 保持别名兼容，新增 --source 及受控转换参数。预检在创建 Settings/DB/模型/搜索客户端之前返回 JSON；后续 S6a3 单独落地来源证明的数据库持久化与批量导入事务，不能把本阶段预检证明当作已持久化的来源记录。

**Tech Stack:** Python 3.12、现有 Pydantic/dataclass、stdlib、已有 DOCX Reader/Parser，无新依赖。

## Global Constraints

- UTF-8；原件只读；实际范围为 `F:\ai律师数据库\法律法规数据库`，不重跑已完成全集转换/切块。
- 不开 Word，不执行宏/域/外部关系。DOCM 静态 XML；OLE DOC/DOCX 必须使用原件及受控转换清单，双哈希检查。
- 不能自动确认日期、效力、机关、地域或静态恢复质量。未知保持未知；普通导入静态恢复来源在人工质量审批机制落地前明确拒绝，预检允许报告其局限。
- 不改变业务库、既有法规版本、dataset_v1 或共享容器。类型化来源准备的 JSON 证明与未来 DB provenance 持久化分别验收。

### Task 1：共享只读来源准备

**Files:** Create `backend/src/lawyer_agent/infrastructure/documents/corpus_source.py`、`backend/tests/unit/test_corpus_source.py`。

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class CorpusSourceRequest:
    source: Path
    source_root: Path
    expected_source_sha256: str | None = None
    conversion_manifest: Path | None = None
    converted_root: Path | None = None

@dataclass(frozen=True, slots=True)
class PreparedCorpusSource:
    source_path: Path
    input_path: Path
    source_sha256: str
    input_sha256: str
    document: ParsedDocument
    auxiliary_paragraphs: tuple[ExportParagraph, ...]
    quality_flags: tuple[str, ...]
    conversion_provenance: ConversionProvenance | None

def prepare_corpus_source(request: CorpusSourceRequest) -> PreparedCorpusSource: ...
```

- [x] TDD 先验证缺入口失败，再实现 native DOCX/DOCM、DOC/OLE DOCX 转换路径及双哈希。Root/source/input/manifest 路径在读取前拒绝 reparse、来源越界、来源/派生根重叠；不是 Word 标签来源直接拒绝。
- [x] 每份来源只读，校验可选预期原件哈希格式及值；读取后复核原件/派生未变。转换清单流式有界行读取，使用 ConversionRecord 类型，所选源重复/缺失/失败/非法版本或指纹、来源/派生越界均拒绝，不能读取任意清单指定路径。
- [x] DocxExportReader 的 body/table 进入 ParsedDocument，source_ref 为原件 URI、source_sha256 为原件摘要；辅助段落独立返回，保留所有读取与转换质量标记，不混入法规正文。无有效正文拒绝。
- [x] 单测包含 tab/br/cr 与辅助段落、空/DTD、源与派生篡改、哈希格式、转换记录重复/缺失/越界、DOCM 不执行转换、静态恢复标记、读取中变更、中文/reparse 路径。聚焦 pytest/Ruff/mypy 通过并独立审查。

### Task 2：接线现有单文件导入和预检

**Files:** Modify `backend/src/lawyer_agent/cli/corpus_publish.py`；extend `backend/tests/unit/test_corpus_publish_preparation.py`、新增 `backend/tests/unit/test_corpus_publish_preflight.py`。

- [x] 测试先证明 --source/--preflight 与转换原件身份当前缺失。新增参数 --source（--docx 兼容）、--source-root（native 默认来源 parent）、--source-sha256、--conversion-manifest、--converted-root、--preflight。
- [x] 提取内部 PreparedImport：command、articles、source、structure_sha256。保留 _prepare_import(args) 的旧二元返回契约；实际 _run 使用同一个 PreparedImport，避免重复读取。结构摘要对条号、路径、完整条文及段落边界进行 canonical JSON SHA-256，不只拼接正文。
- [x] 默认 parser_version 改为 corpus-docx-v2，显式旧值仍由操作者控制但不能改实际 source loader_version。日期缺失的 version_label 取原件 source_sha256，source_ref 默认原件 URI；原件与派生双身份均在预检 JSON 返回。新 parser 默认导致旧导入重跑冲突时保留显式冲突，不能静默重写旧版本。
- [x] --preflight 返回稳定 JSON（schema、原件/读取路径及双哈希、实际 loader/parser、转换provenance、quality_flags、条文数、结构摘要、metadata_review_status=not_verified、database_provenance_persisted=false）；不得含全文、凭据或数据库 ID。禁止与 --import-only 同用。
- [x] 预检先于 Settings/数据库/模型装配；缺日期/效力仍保持未知，元数据没有被批准的暗示。正常导入无法解析/重复条号仍拒绝，静态恢复来源显式拒绝并要求后续人工质量审批；预检可显示其证据与局限。不增加忽略安全或质量的开关。
- [x] 合成测试证明：预检不创建 Settings/DB/模型，转换后的输入发生变化不会把其hash用作法规原件身份，辅助文本不进provisions，结构摘要对条号/分段变化敏感，静态恢复预检可读但实际导入在DB前拒绝，旧参数/未知元数据契约仍有效。

### Task 3：实际只读预检与交接

- [x] 独立代码审查并解决发现；在最终代码运行相关测试/Ruff/mypy，按新增衔接风险运行后端本地全套。
- [x] 选择已完成导出的 5 份司法解释做只读来源准备/预检，与其已验收的原件hash和导出正文逐段比对；法律元数据仅使用候选并明确未核对，不进行入库/发布。保存机器可读报告在忽略的 artifacts/legal-corpus/preflight/。
- [x] 更新项目现状：新准备逻辑与现有导入入口已接线；仍需 DB 来源证明/重跑结构校验、批量每文件事务、发布恢复、真实模型 A/B、734 份结构及法律元数据核对。整个项目目标保持不变。

## 完成证据（2026-09-06）

- 来源准备47项、CLI预检8项、快照读取6项；最终本地后端1,834通过、1跳过、1警告，Ruff通过，mypy 189源文件通过。独立复审7组134通过，并以独立合成输入确认ABA、缺指纹和非法主XML问题均已关闭。
- 复审修复扩展文件：`infrastructure/documents/export_reader.py`、`tests/unit/test_export_reader_snapshot.py`。新快照接口保持有效DOCX读取语义；主XML形状加固不等于已对全集再次验收。普通转换指纹必填；输入快照上限64MiB，成员/解压限制仍保留。
- 真实5份司法解释预检74条、311正文段、1份受控DOC派生；最终代码与已导出的原/派生哈希和正文/辅助段落逐段一致。机器报告保存在 `artifacts/legal-corpus/preflight/judicial-five-20260906.json`，法律元数据仍未核验。
- MySQL端口不可达且Docker无运行容器，本次未执行集成。旧CLI合成MySQL测试的parser默认断言已同步，但不能以旧运行结果冒充本次验证。
- 后续S6a3：持久化来源/派生证明与结构摘要，重跑按证明拒绝冲突，再实现每文件事务的受控批量导入；本切片不宣称批量入库/发布完成。
