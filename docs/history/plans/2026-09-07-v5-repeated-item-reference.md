# v5款项引用修复实施

使用subagent-driven-development和TDD；用户已授权既定范围内自主实现，不提交Git。

- [x] 根代理：保存当前脏文件基线；v5 Parser引用与旧v4反例先RED，新增明确匹配逻辑，保留原文。文件infrastructure/documents/parsers.py和新tests/unit/test_v5_repeated_item_reference.py。
- [x] 独立实现子代理：新domain/legal_parser_profiles.py管理v4/v5精确文本身份；修改application/legal_corpus_import.py、legal_chunk_structure.py、legal_dataset_quality.py与cli/corpus_publish.py接通新身份/独立hash域，聚焦CLI/导入/质量测试先RED。不得改Parser或GPU51文件。
- [x] 根代理：真实四川source新profile准备、全文/30条/叶覆盖、实际隔离MySQLCLI→质量/replay验收；再冻结新源码入口并真实新增入库，确认旧事实不变。
- [x] 聚焦和后端全套/Ruff/mypy、独立复审、新GPU补算预检失效与重验记录；维护现状与剩余清单，不能把只读准备当真实入库。

2026-09-07 15:37：本切片完成；实际证据 `.superpowers/sdd/v5-reference/verification.md` 和 `artifacts/legal-corpus/full-release/v5-sichuan-20260907T072625Z/verified-import.json`。3087后端、1隔离MySQL通过；14报告15995统一质量为15960通过/35阻断，旧15994摘要不变。96补算/3627叶新预检通过，实际GPU仍为前驱，不代表96补算或全集发布完成。
