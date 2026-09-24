# v4 段内条头分割与精确正文保存实施计划

> 使用 subagent-driven-development，按任务独立实现、spec/quality复审；用户已授权持续完善，在既定来源保留与保守条号恢复边界内推进，不重新请求既定架构确认。

**目标：** 将已定位且具有严格句界、独立条头与连续双锚的段内条文拆开，保留全部原字符至事实源和分块，避免不同条文错误合并。

**架构：** 先为精确v4启用正文精确持久化与有边界的版本摘要，再由独立检测器返回原段内切分span，Parser只对原segment正规化一次。只有结构证据充分的候选恢复，其余保留原文和阻断。

**技术：** Python/Pydantic已有导入端口、原ParsedDocument、现有条头/中文数字语义、hierarchical-v2。无新依赖或Schema。

## 全局约束

- 只在 `corpus-docx-v4` 启用新解析/持久化语义；分块精确保留策略只在 `corpus-docx-v4/hierarchical-v2`。默认、v3、近似标签、旧内容摘要保持不变。v4尚未全量冻结，无v4事实库长批次导入。
- 保留输入ParsedDocument、原件、段落ordinal、前缀、空白及文本；真实段落偏移与正规化条文坐标分开，不伪造原件坐标。每个新split只切原字符串，片段无重叠/缺失、拼接完全等于原segment。
- 不改源F盘、Loader、旧编号恢复、质量gate、GPU/模型/缓存代码、索引别名或用户审核元数据。CPU PTY77759/PID58004持续运行，execution/provider/cache/windows/scripts冻结，禁止第二模型或并发OpenSearch。
- 不使用来源SHA/标题白名单，不补正文、不猜紧凑条头，不靠条号齐全证明正文齐全。原v3 ID/证明/快照保留；保留脏工作树，不提交。
- 依据 `.superpowers/sdd/embedded-article-boundaries-current-20260907.json` 和 `first-two-embedded-boundaries-20260907.json`：17处正文粘连位置，15处带条头分隔，2处紧凑条头单独处理（其中一处前文截断）。位置恢复数不等于整文通过数。

## Task 1：v4 正文持久化、摘要与精确分块

文件：修改 `backend/src/lawyer_agent/application/legal_corpus_import.py`、`legal_chunk_structure.py`；新合成测试 `backend/tests/unit/test_v4_exact_provision_text.py`。必要时扩展现有聚焦测试，不更改Parser或CLI。

- [x] 安全合成RED覆盖：v4 ImportService保留条文首尾/内部空白及E004、准确hash/char坐标；同内容replay成功，空白或条文边界变化拒绝同label；v3/default/near-label仍按旧strip和旧摘要。至少两个不同条文切分但拼接相同的v4command必须具有不同摘要。
- [x] `_derive_provisions(..., *, parser_version: str | None = None)` 和 `_content_hash(..., *, parser_version: str | None = None)` 接收显式版本；生产ImportService及 `_version_matches` 均传command.parser_version。仅精确v4使用未经strip的full_text，旧版本原样。版本摘要v4采用带域标记的确定性UTF8结构序列（含provision_no/level/structure_path/title/full_text）而非无分隔正文拼接，编码固定并测试一致性；旧算法不变。空白全文仍拒绝。
- [x] 仅精确v4/hierarchical-v2从原paragraph片段建立unit，要求原片段拼接准确等于full_text；分类可用只读lstrip视图，unit内容和起止位置必须保留空白。不要把不能建立结构的长条静默降成超长父叶；沿用既有失败关闭/容量契约并明确测试。保留已有窗口算法。
- [x] 长条合成验证至少含重复段落、首尾空白、E004与跨多个窗口；使用真实ImportService产物和hierarchical/leaves入口，要求每个字符（包括空白）被叶覆盖，内容严格为父块对应切片，父块全等事实正文。无需真实DB或模型。
- [x] 聚焦import/structure/exactleaf测试、Ruff、mypy通过，独立spec/quality复审；保存独立before/diff/report，之后才能进入Task2。

## Task 1b：真实 CLI replay 的版本策略接线

根代理检查实际 `_import_prepared` 发现额外 `assert_provisions_match` 无条件strip，不能只凭ImportService单测判定重试可用。补充这项集成后才能完成Task1并进入Task2。

文件：`application/legal_corpus_import.py` 中提取共同纯文本策略；`application/legal_corpus_replay.py` 与 `cli/corpus_publish.py` 接线；扩展 `tests/unit/test_legal_corpus_replay.py`、`test_corpus_publish_cli.py`。路径均在backend/src/lawyer_agent或backend/tests下。

- [x] RED：exact v4真实full_text带边缘空白时assert_provisions_match应通过，旧strip后的实际事实必须拒绝；旧/default/near标签保持原检查。CLI `_import_prepared` 的实际replay检查（不mock该检查）应接受v4准确正文且不replace旧chunk。可复用已有测试中的会话/仓储替身，但不能仅断言函数收到某关键字。
- [x] 文本策略在import模块提取为单个纯helper供 `_derive_provisions` 和replay使用；`assert_provisions_match(..., *, parser_version: str | None = None)` 保持默认旧策略；CLI显式传prepared.command.parser_version。所有语义字段/hash/字符范围检查继续执行，不忽略空白或强行放行。
- [x] 聚焦import/replay/CLI/chunks、Ruff/mypy，独立before/diff/report和复审通过；原nullable-title修复及重复段落测试保持。根代理可在Task2最终接入后以隔离MySQL合成样本验收首次导入和replay，生产语料库保持零写入。

## Task 2：严格段内条头检测及Parser集成

文件：`backend/src/lawyer_agent/infrastructure/documents/parsers.py`；新 `embedded_article.py` 和 `backend/tests/unit/test_embedded_article_parser.py`。若引用边界扫描需要复用，可小范围抽取现有prefixed模块的共同纯逻辑，保持其现有行为并回归。不要复制整段现有逻辑。

- [x] 新不可变 `EmbeddedArticleSpan` 至少含paragraph_index、paragraph_ordinal、marker_start（原段内偏移），明确split_start及左侧分隔符归属；`ParsedInstrument.embedded_article_spans` 返回原位置。新切片应保留空白在左片段，条头从marker开始，原segment只在第一次按既有规则strip外部空白。
- [x] 候选必须在原段落/既有显式换行segment内部，前方为句号及零个或多个水平空白、至多一个E004（其前后水平空白都可为零）；完整普通ARTICLE_HEADING后仍必须有水平空白+同段正文。这里“紧凑条头”指条号与后续正文之间没有空白，不是句号与条头之间没有空白。无句界、紧凑条头、补充条、复合款项引用不恢复。不得把已经有换行的段内条头重复拆分或越过既有条头事件闭合。
- [x] 建立有序候选/普通锚点/屏障事件，候选组只在最近明确左N/下一个明确右M之间完整补齐N+1..M-1。支持同段多个候选和跨段续段；数字比较按候选数量有界，拒绝重复/倒序/缺口。章节/序言/前导标题/补充条/未解前缀条头是屏障，不充当普通续段。
- [x] 引号/书名号/括号状态必须在候选原偏移处检查，跨段未闭合或错配失败关闭；仅段首检查不够。普通锚点亦不能处于引用内。保持当前前导标题及前缀条头功能，共存但不能交叉误闭合。
- [x] Parser分类及条号读取用同一视图；原text/paragraphs是原连续切片。通过Task1的实际导入产物与分块验证无字符丢失，全文、坐标、ordinal/span可追溯。旧v3的全文/条号/偏移不变。
- [x] 合成RED涵盖以上正反边界、极端数字安全、跨段引号、窗口/父子覆盖以及v4 `_prepare` 装配；GREEN聚焦Parser/leading/prefixed/import/chunks/CLI，Ruff/mypy与独立复审通过。

## Task 3：根代理真实来源验收与交接

- [x] 对17处读取同一已经核对载体，保存before/after源SHA、原段落span、完整条号门禁和逐源状态；预计15处可进入严格恢复候选，2处继续阻断，不预设全通过。
- [x] 实际 `_prepare/_require_importable`、ImportService内存端口/显式v4派生、hierarchical/leaves验证：原文区拼接、所有空白、正文/父子内容/边界覆盖；原件不变，数据库写入0。需要外部源修正者单列。
- [x] 最终完整后端回归、隔离MySQL合成DOCX首次导入/replay全文及旧ID保持测试（根代理独占，使用既有lawyer_test随机库fixture，禁止指向业务库）、diffcheck和未跟踪秘密检查、项目状态/交接；记录仍未完成的紧凑条头、畸形编号、其它前缀/未知正文和具体版本冲突。v4未全部完成前不启动长导入/发布。


## 2026-09-07 03:39 最终验收

所有任务已完成并经最终独立复审通过；保存初始漏检与两轮修复记录，未覆盖失败证据。当前行pos/endpos匹配同时修复显式换行组合和重复后缀复制；后者以确定性切片字符核算回归，不宣称生产容量通过。最终2717 passed、1平台skip、1既有Starlette warning，Ruff全通过、mypy218通过。实源13份/17位置中15恢复、2预期紧凑条头阻断；11份条号门禁通过，13份原件与全文/空白不变，626块/54子块/623叶覆盖通过。实际同段w:br合成DOCX→隔离MySQL及replay3条/6块通过，ID及所有字段不变，临时库已删除；生产库写入0。相应根代理报告均为 full-release 下的 *bounds-final.json；独立总复审见 .superpowers/sdd/embedded-plan-final-review.md。前导标题10/10及段首前缀7/7最终实源回归通过。v4未全集冻结，全集入库/向量化/发布尚未完成；CPU活动运行范围保持冻结、GPU与OpenSearch仍暂停。
