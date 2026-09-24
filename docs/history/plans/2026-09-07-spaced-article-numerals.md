# 条号数字内部水平空白实施计划

依据同日spaced-article-numerals-design及已批准全集目标。使用subagent-driven-development实施和独立复审；保留脏树、不提交。GPU PID51652及handoff PID2820正在运行，不可修改其报告列出的49项冻结依赖。

- [x] 快照拟修改的Parser、测试及必要CLI文件，建立本次增量diff基线。新增候选模块仅精确v4启用，禁止改全局普通ARTICLE_HEADING影响旧profile。
- [x] TDD：普通左右锚之间的中文/ASCII/全角数字内部TAB/空格/全角空白恢复；原文、段落、偏移、span准确，长正文层级覆盖；连续候选组可恢复。
- [x] 反例：缺锚/跳号/重复、紧凑引用、补充条、章节/序言屏障、引号/括号中或不闭合/错配、跨行数字、未知字符、空正文；默认/v3/v4-extra不启用。先记录测试预期失败，随后最小实现。
- [x] 聚焦Parser/前缀/段内/标题/profile/exact-text测试及Ruff/mypy，实际diff提交独立任务复审，修复Important。
- [x] 根代理实际CLI验证乌鲁木齐来源：sha不变、条号35恢复、完整条序通过、正文逐字符保留、内存import/replay/层级覆盖；必要邻近回归不覆盖旧报告。
- [x] 最终后端pytest tests/unit tests/api tests/contract、Ruff、mypy；核验冻结GPU/handoff文件与进程；更新现状/证据索引和交接，不称生产库条号阻断已解除。

上述六项已完成。实源乌鲁木齐67→68条，68块/叶全部字符覆盖；独立复审P2修复后通过，最终与发布hash兼容修复一起全套2923 passed、Ruff/mypy219通过，并进入86源冻结v4正式导入、replay和MySQL质量通过批次。详见现状13:13记录与 `.superpowers/sdd/spaced-zero-width-review-final-20260907.md`。

根代理同时继续审计剩余结构阻断，独立只读工作不能修改本增量文件。接续发布所需12份原始结构阻断及新增5份条序问题继续留证，不用此单源成功代替全集验收。
