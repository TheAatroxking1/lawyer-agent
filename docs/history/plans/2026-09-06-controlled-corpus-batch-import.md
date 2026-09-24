# S6a4 受控批量导入与结果清单

> 使用 subagent-driven-development 分工及独立审查。保留未提交改动，不自动提交。沿已批准 S6 继续，后续集合发布恢复仍必须完成。

**目标：** 将来源准备、证明持久化和层级 Chunk 导入推广到已核对 JSONL 清单，逐文件独立事务，成功与失败均有可恢复结果。不自动批准候选，不默认切索引别名。

**边界：** 新 `corpus_import_batch` 专职批量导入/只读预检；集合发布另走后续恢复流程。报告不能冒充数据库事实或专业审核。中断后重新运行原清单，由数据库证明校验决定重跑，不信任旧报告而跳过文件。

## Task 1：已核对导入清单（独立模块）

新增 `infrastructure/documents/import_manifest.py` 和 `tests/unit/test_corpus_import_manifest.py`。

- [x] `CorpusImportRow`：Pydantic strict/frozen/extra forbid；必须显式 `schema_version="legal-corpus-import-v1"`、`review_status="reviewed"`、`metadata_review_ref`（非空，<=512）、`source_path`（字符串，规范化绝对路径）、`source_sha256`（小写64hex）、`title`（<=512）、`issuing_authority`（<=256）、`jurisdiction`（<=64）。其余 category=unknown、region_code=None（<=16）、version_label=None（<=128）、status=status_unknown、published_on/effective_on/repealed_on=None、law_number=None（<=256）、source_ref=None（<=512）。非空字符串不接受全空白；date 严格 ISO 日期。明确 current 的行至少给出 published_on/effective_on，不能确认缺日期的 current。
- [x] `LoadedCorpusImportManifest(path: Path, sha256: str, rows: tuple[CorpusImportRow,...])`；`read_import_manifest(path: Path, source_root: Path)`。受控读取同一份至多32MiB快照并取摘要，每行至多1MiB，最多20,000条，空行跳过而空清单拒绝；类型/未知字段/schema/pending/重复路径全部在任何业务操作前拒绝。不得把候选模型转换成已核对行。
- [x] 输入 manifest/root/各source路径拒绝reparse和越界，来源只允许DOC/DOCX/DOCM、拒绝Word临时文件。不用读取来源正文或要求文件已存在（缺失文件留给逐文件结果），按Windows规范化路径检查重复；路径不能依赖 cwd。稳定 `ImportManifestError` 文本，不回显行内正文/验证Payload。
- [x] TDD聚焦测试、Ruff/mypy，通过后冻结供独立审查。

## Task 2：一次运行的转换清单快照（独立于批量CLI）

修改 `infrastructure/documents/corpus_source.py`，新增 `tests/unit/test_corpus_conversion_catalog.py`；保持现有来源和CLI测试兼容。

- [x] 新 `CorpusConversionCatalog` 只读类型，记录 manifest_path、manifest_sha256、source_root、converted_root 及经过既有全部严格验证的记录索引；`load_conversion_catalog(manifest:Path, source_root:Path, converted_root:Path)`。manifest至多32MiB/20,000行、行1MiB，摘要和解析使用同一不可变快照。索引记录不可被调用者就地修改；拒绝重复源。
- [x] `CorpusSourceRequest` 末尾增加 `conversion_catalog: CorpusConversionCatalog|None=None`。供批量复用，使用前验证 catalog 的manifest路径/来源根/派生根和request一致；source原件和input派生仍在每次准备时重验路径及双哈希、按同一bytes快照解析。清单后来变化不影响已经绑定摘要的运行快照，报告记录该摘要；后续新运行重新加载。
- [x] 原单文件调用按需加载清单并保持已有稳定错误码/语义；native不需要读取清单正文，DOCM不执行转换；静态恢复局限不变。测试证明多个来源只读一次manifest，原/派生变更仍拒绝，快照摘要正确、配置错配/非法记录/重复/超限均拒绝。

## Task 3：共享事务函数与批量CLI（Root）

- [x] 从 `corpus_publish._write` 提取 `_import_prepared(session_factory, prepared)`，返回 typed `CorpusFileImportResult(imported:LegalImportResult, article_count:int, chunk_count:int)`；一次调用一个事务，原导入/证明/分块逻辑不变。单文件入口和批量入口复用，不创建模型/搜索Provider；`_prepare`可接可选转换catalog。
- [x] 新 `cli/corpus_import_batch.py` 参数：必需 `--manifest --source-root --report`，可选 `--conversion-manifest --converted-root --preflight --dataset-version --parser-version`。整份清单类型和路径校验先于Settings/DB；逐文件准备失败或导入失败写稳定失败code后继续，不吞取消/退出信号。只读预检不装配业务依赖。
- [x] report必须为新文件，拒绝与source根、派生根、manifest/转换清单重叠、reparse、已存在路径；先验证并exclusive创建后才能写业务。UTF-8 JSONL：run头记录run_id、manifest/c转换snapshot哈希、总数/模式；每份row结果fsync（source path/hash、状态preflighted/imported/replayed/failed、可公开code、真实DB version/instrument ID只在提交成功后）；末尾summary计数complete/failed。失败退出1，输入边界失败退出2，全部成功退出0。崩溃无summary即不完整，不捏造成功。日志/错误不能回显正文、SQL绑定值或秘密。
- [x] 不依据旧报告跳过，重跑读DB；报告I/O失败立即终止后续导入并明确未完成，不继续无结果写库。事务已提交但报告未写的窗口通过下次源证明重跑恢复。
- [x] 合成单测及隔离MySQL集成：混合成功/文件失败/写入失败逐行继续，事务互不污染，重复运行同版本，未知元数据不提升，pending清单/坏行/报告覆盖全局拒绝先于DB，预检不触业务，报告中断不伪装完成。

## Task 4：复审与交接

- [x] 独立审查、相关回归及最终backend全套pytest/Ruff/mypy；用独立临时MySQL容器验证批量及既有单文件证明事务。不改共享栈、不处理/批准真实pending全集。
- [x] 记录真实语料的原/派生哈希保护仍适用但本阶段不重跑全量转换/切块；清单使用说明、失败/中断恢复方法及报告路径写入文档，更新现状和下一步集合发布恢复。
- [x] 清理本任务容器及临时凭据，检查UTF-8/diff/忽略秘密，目标保持全项目完成。

## 完成证据（2026-09-06）

- Task1 43项清单校验；Task2 31项catalog用例及既有来源/导出回归；批量CLI 15项单测。通过独立复审，154项独立相关回归通过。
- 审查复现ADS来源/清单附加流绕过（6RED）及手造catalog越界/抹静态局限（2RED），均已修复。只读catalog不能替代选中记录的路径/内容/转换身份验证。
- 最终本地backend 2,010 passed /1 skipped /1 warning，22.11秒；Ruff通过，mypy195源文件通过。独立临时MySQL12项批量/证明/单文件集成通过，14.95秒。
- 临时MySQL使用已缓存mysql:8.4、独立13307端口和tmpfs合成数据；最终确认容器自动移除和临时凭据删除。共享栈/业务库、真实元数据、Word/导出、真实模型及别名未改动。
- 批量使用及报告恢复文档：docs/legal-corpus-batch-import.md。下一步为集合发布与故障恢复，不将该阶段声明为真实全集入库、索引发布或产品完成。
