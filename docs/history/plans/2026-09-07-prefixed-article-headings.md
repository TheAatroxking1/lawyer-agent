# v4 前缀条头识别实施计划

> 使用 subagent-driven-development：独立实现、任务复审及根代理真实只读验证。用户已授权持续完善，在既定结构解析、来源保留与失败关闭边界内完成，不改旧 v3。

**目标：** 识别原段落以 U+E004 加水平空白起始、随后具有完整条头和正文且上下条号连续闭合的结构；不删除原字符或把私用字符冒充法定换行。

**依据：** `.superpowers/sdd/embedded-article-boundaries-current-20260907.json` 是对旧 gap 审计中仍阻断来源的实际原件/既有转换件摘要核对，29个位置中13个为 U+E004 段首前缀、2个为其它短前缀、14个真正位于正文之后。该切片只解决已证实的13个段首位置；正文粘连、其它符号前缀、畸形条号仍单独处理，不能推广忽略全部私用字符。

## 全局约束

- 仅精确 `LegalStructureParser(profile="corpus-docx-v4")` 启用；默认、v3和近似版本标签行为保持不变。
- 原 ParsedDocument、段落 ordinal、文本、前缀、换行和输入哈希不变；新增的附加分类必须带 paragraph_index、paragraph_ordinal、marker_start 及原前缀文字。原 ParsedArticle 文本和 paragraphs 保留前缀，char_start/end 按现有规范化拼接坐标保持连续，不伪造原文件绝对坐标。
- 不改变 Loader/旧编号恢复、质量 gate、模型/缓存/向量代码，不导入或写数据库，不改变别名，不启动模型/OpenSearch/Word。
- 活动 CPU 接续使用冻结 embedding execution/provider/cache/windows，任何实现代理不得修改这些文件。
- 不按 SHA、完整标题、特定法条数白名单放行，不补正文、条号或缺失字符，不自动降级全文模式。无连续闭合或语法歧义仍拒绝恢复。
- 保留脏工作树，不提交。以修改前文件快照建立本任务 diff。

## Task 1：Parser 接入及合成 TDD

修改 `backend/src/lawyer_agent/infrastructure/documents/parsers.py`；新增小模块 `prefixed_article.py` 与 `backend/tests/unit/test_prefixed_article_parser.py`。只在确有集成需要时补 CLI v4 聚焦测试，不修改 CLI 已有 profile 装配。

- [x] 建立输入检测：候选只接受段首恰好一个 U+E004 后至少一个水平空白，然后普通完整 ARTICLE_HEADING；条号后须有可见空白分隔和非空正文。不能以 strip 删除私用字符或改变存储正文。条之候选、复合条款引用、无分隔或其它字符前缀不能恢复。
- [x] 候选或连续候选组仅在既有显式普通条头 N 与下一个显式普通条头 M 之间、候选序列恰为 N+1..M-1 时恢复。允许普通续段，不把章节/序言当条号。没有左/右锚、重复/倒序/缺口或补充条序列时拒绝恢复；邻接标题 span 不能作锚。数字解析与现有质量 gate 一致，可使用现有 `application.legal_corpus_publish._ARTICLE_NO/_cn_number`，不另造宽松数字语义。
- [x] 对位于未闭合引用/引号或括号区间的候选拒绝恢复；至少覆盖中文引号、书名号及常见中英文括号。只做结构判断，不猜引述内容的法律含义。保留源文，无法确定引用边界则不恢复。
- [x] Parser 用附加 classification view 识别条号，实际 article text/paragraphs 从原字符生成。返回不可变 `PrefixedArticleSpan` 附加元数据；条头识别与条号提取必须使用同一 marker_start，不能识别成功却产生前缀伪条号。既有前导标题保护继续工作。
- [x] RED 至少覆盖：合法单一/连续候选；全文/段落/坐标/附加span不变；默认/v3不变；未知前缀、正文内部E004、无锚/跳号/重复、条款引用、补充条、无分隔、跨段未闭合引用不恢复。应避免让测试只检查某个内部函数返回值而不验证 Parser articles。
- [x] GREEN 运行新增测试、existing parser/leading-title/CLI profile及 hierarchical/import 聚焦测试，Ruff、mypy；独立 spec/quality 复审通过。真实来源读取由根代理独占执行。

## Task 2：根代理来源与发布预检验证

- [x] 对前述13个位置涉及的唯一来源读取同一已核对载体，检查哈希、旧/v4条序列、span与原前缀逐字符一致。记录完整条序列门禁结果，不能因该局部修复就把其它缺口计为通过。
- [x] 通过 `_prepare`、质量 gate、hierarchical 派生的实际入口检查正文覆盖、父子关系与元数据，不写库；原 v3证明/ID不变。
- [x] 最终代码完整后端回归、diff check、项目状态与交接更新；下一切片处理真正段内粘连及其它已定位问题。v4仍未全局冻结，不启动新版本长批次导入。

## 最终证据（2026-09-07 02:12）

Task1 原始diff/report与独立复审发现均保留于 `.superpowers/sdd/prefixed-article-task-1*`。两项复审问题通过新fixdiff/report修复，最终独立复审PASS/PASS，记录 `prefixed-article-final-review.md`。根代理最终真实7/7来源、13处span全部通过，无写库；报告 `artifacts/legal-corpus/full-release/prefixed-article-v4-readonly-check-20260907-final.json`。最终完整后端2660passed/1平台skip/1既有Starlettewarning，Ruff全部通过，mypy217通过。当前切片完成，不等于v4全部纠错、全集入库或正式发布完成。
