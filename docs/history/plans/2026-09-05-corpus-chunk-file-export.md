# 法规切块文件导出与旧 Word 转换实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. 不提交 Git；用户已授权持续执行。

**Goal:** 为已确认范围内每个 Word 文件生成可核查、可续跑的 UTF-8 切块文件，全部处理核验后告知用户实际绝对路径。

**Architecture:** 文件切块是独立离线派生产物，不要求先补全法律元数据或发布索引。DOCX 读取、结构分块、文件导出、DOC 转换分离；每份文档有来源哈希、处理记录、块关系、覆盖验证。未核验的日期、效力、制定机关保持未知；导出 ID 不冒充 MySQL 权威 ID。

**Tech Stack:** Python 3.12、现有 DOCX/XML 读取与结构分块、PowerShell/MS Word COM、JSONL/JSON。

## Global Constraints

- 只处理 `F:\ai律师数据库\法律法规数据库`；排除案例/裁判文书库、压缩包和 Word 锁文件；源文件只读。
- 补充路径盘点发现 1 份 `.docm`，纳入“所有 Word 文档”切块：只经同一安全 ZIP/XML 读取器静态读取，标记宏容器未执行，禁止交给 Word 打开或执行宏。当前范围为 12,826 DOCX + 3,179 DOC + 1 DOCM；184 ZIP 按既定要求跳过。
- 输出根为仓库 `artifacts/legal-corpus/chunks/`，已 Git 忽略；转换放 `artifacts/legal-corpus/converted/`。输出不得在源目录或其祖先目录；不覆盖原件。
- 文件层 ID 使用内容/路径派生哈希，显式 `id_namespace=offline-export-v1`；不写业务库、不切 alias、不调用模型。
- 结构失败不可丢弃内容；按源段落保守保存并标记 `structure_review_required`，不为其捏造条号。无法读取、转换或覆盖校验失败是未完成文件。
- 每个源文件均有处理记录，包括内容重复文件；不按相似标题合并。重跑需要验证输入哈希、输出哈希、导出版本及配置，损坏或不完整结果须重建。
- 全量完成只表示文件机械切块与覆盖核验；结构待核对、法律元数据未核对与数据库入库/索引发布分别记录，不能声明法律质量或生产验收。

## Task 1: 可核验的 DOCX 切块导出

Files: 新增 `backend/src/lawyer_agent/application/legal_corpus_export.py`、`backend/src/lawyer_agent/infrastructure/documents/export_reader.py`、`backend/src/lawyer_agent/cli/corpus_export.py` 与相应 unit tests。

接口由本任务实现并在报告列明。CLI：

```powershell
.venv\Scripts\python.exe -X utf8 -m lawyer_agent.cli.corpus_export --source-root 'F:\ai律师数据库\法律法规数据库' --output-root '..\artifacts\legal-corpus\chunks' --limit 5
```

- [x] 先写聚焦失败测试：短条父块不合并；长条复用现有分块且父子可恢复；增补条独立；标题/前言/尾注无遗漏；无条文件保守逐段；结构解析失败仍保留段落且标记；XML 表格/脚注/尾注/文本框处理边界明确；源/输出重叠拒绝；重复内容有独立来源记录；输出损坏重跑重建；中断后续跑；ZIP/XML限额与实体拒绝。
- [x] 创建保守读取器：正文按 XML 文档顺序读取段落，避免嵌套段落重复；附属脚注/尾注及页眉页脚单独标明 XML part，不混入正文条文。表格位置、自动编号、图片/嵌入对象记录质量标记，不声称 OCR 或完整排版还原。拒绝 DTD/实体；读取前限制压缩包成员展开体积。
- [x] 结构导出复用 `LegalStructureParser` 与 `derive_hierarchical_chunks`。每条父块保存全部文字及段落引用；标题/章节/序言等未归入条文的原段单独输出。对解析歧义/重复条号保守回退为原段，不产生错误条文身份。按源段落、原始正文文本范围验证覆盖；导出保留的源段落可逐段重建读取结果。
- [x] 每源文件输出 `<相对路径哈希>/document.json`、`chunks.jsonl`、`paragraphs.jsonl`，原子写入，元数据最后完成；记录 source_path/source_sha256、input/converted hash、loader/parser/export版本、配置、未知法律字段、块数/父子数/覆盖数、质量标记和输出哈希。JSONL 中块含稳定 ID、父 ID、正文、条号（可空）、层级路径、来源段/范围。
- [x] CLI 有明确 source/output 参数；排序枚举 `.docx/.doc/.docm`，不跟随目录 junction/symlink 越界，不打开压缩包。单份异常写稳定 code，继续其余文件；`.doc` 在无已验证转换记录时标为待转换。写 `manifest.jsonl`、`summary.json` 和中文 `README.md`。完成计数基于当前清单，不把历史记录混入当前完成数。`--limit` 明示为试点。
- [x] 运行新增 unit、Ruff、mypy；独立审查；司法解释 5 份试点确认输出、父链和覆盖，再扩到 DOCX 全量。报告实际成功/待核对/失败/待转换，保留失败来源。

## Task 2: 有界、只读 DOC 转换

Files: 新增 `scripts/convert_corpus_doc.ps1`、必要单文档 worker 与测试；转换清单接入 Task 1 CLI。

- [x] 先写失败测试验证源/目标隔离、只允许 DOC、超时/密码/失败不输出完成记录、复跑验证原件及产物哈希、COM 安全参数。
- [x] 使用独立 Word.Application，隐藏窗口、禁宏、禁链接更新、禁交互提示；只读打开，不记最近文件，不保存源文档。输出 DOCX 在派生目录，成功后关闭本次文档/实例；不得终止其它 Word 实例或修改用户 Word 全局偏好后不恢复。
- [x] 单份运行设超时，清理仅限可证明由本次创建的进程和临时文件。失败保留稳定原因及来源记录；不得一份卡死阻断全部批次。
- [x] 转换清单记录源绝对路径/哈希、产物相对路径/哈希、转换器版本、状态。导出器必须校验转换来源一致后读取，保留 DOC 原件为 source 身份。
- [x] 真实只读转换代表性 2 份，核对文字段落及输出可读；通过审查再执行剩余 `.doc`，每批更新汇总。

## Task 3: 全量核验与文件交付

- [x] 验证所有当前源文件均有唯一清单项；源未改变、输出存在且哈希一致、JSONL 可读、块父链/源范围有效、读取段落覆盖完全。
- [x] 对无法读取、转换失败、覆盖失败逐项修复后重跑；存在未解决文件时不得宣布全部切块完成。
- [x] 更新项目现状与 README：实际文件总数、结构化与保守回退数、质量边界、未处理数、核验命令；单独保留结构/法律元数据待核对清单。
- [x] 用户收到可点击绝对目录路径、清单与说明文件路径，以及简洁处理结果。继续既定入库/发布/检索与项目完善主线。

## 最终验收（2026-09-06）

完整范围 16,006 份，最终导出全部成功、待转换 0、失败 0；独立 `corpus_verify` 退出 0，16,006 份 / 921,117 块 / 0 问题、complete/scope_complete 均 true。734 份结构待复核，图片/文本框未 OCR、自动编号/表格版式等局限逐源标明；1 份 Office 校验拒绝原件经明确静态恢复并另作正文/页眉全文对照。全部 16,006 行中文 CSV 已生成并核对来源集合、状态和块数。

交付根为 `C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\chunks`；每源切块位置查 `文件清单.csv`，完整性证据查 `verification.json`。原件只读、语料未进入 Git，本次结果不代表法律质量、全量数据库入库或索引发布验收。
