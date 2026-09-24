# v4 空括号条头前缀实施计划

依据 [设计](../../superpowers/specs/2026-09-07-empty-parenthesis-article-prefix-design.md)。使用 subagent-driven-development：一个有界实现任务、独立任务复审，根代理并行准备只读来源验收，完成后集成复审。保留当前脏工作树，不提交或切换目录。

## 全局约束

仅精确v4启用。新前缀是恰好 `（）` 加至少一个水平空白；原有 U+E004 加水平空白保持。沿用左右连续锚、引用闭合、章节及补充条屏障；不补条号、不补正文。所有原字符及位置保留。不得修改 embedding runtime、数据库、原件、既有报告，不启动模型、OpenSearch或Word。只在独立测试/只读验收中证明行为，不进行生产导入或发布。

## Task 1：实现与任务复审

- [x] 快照 `backend/src/lawyer_agent/infrastructure/documents/prefixed_article.py`、`backend/tests/unit/test_prefixed_article_parser.py` 到独占 `.superpowers/sdd/empty-parenthesis-before/`；基于该脏树快照生成本次diff，不能用HEAD差异冒充本次修改。
- [x] TDD：新增参数化Parser测试。合法 `第一条 甲。/（） 第二条 乙。/第三条 丙。`，断言三条、第二条原文/paragraphs、prefix和marker_start=3、坐标连续、输入不变；连续新旧混合候选组也通过。默认/v3/v4-extra不启用。新前缀逐项覆盖ASCII/括号内字符或空白/重复/无分隔/跨行、补充条、引用、缺锚、跳号/重复、章节/补充屏障、未闭合及错配边界拒绝。
- [x] 在backend运行 `uv run python -X utf8 -m pytest tests/unit/test_prefixed_article_parser.py -q`，记录预期失败后才改实现。
- [x] 最小实现只改候选前缀与模块说明：`_PREFIX = re.compile(r"^(?:\ue004|（）)[^\S\r\n\v\f\u2028\u2029]+")`。不要调整其余恢复算法或条号门禁；若该实现无法满足验收，先提供具体失败证据再决定是否扩大设计。
- [x] 同命令通过，再运行 `uv run python -X utf8 -m pytest tests/unit/test_prefixed_article_parser.py tests/unit/test_embedded_article_parser.py tests/unit/test_leading_title_parser.py tests/unit/test_corpus_publish_v4_profile.py tests/unit/test_v4_exact_provision_text.py -q`；Ruff检查修改文件。报告新旧文件SHA、命令及结果；独立复审本任务diff/设计并解决发现。

## Task 2：根代理真实预检与整体验收

- [x] 通过实际转换目录、审核元数据、CLI `_prepare` 复核双鸭山目标；原/v4 article区域逐字符一致，新增span与源段落21/offset3吻合；全条序列通过并验证所有层次块及叶子覆盖。
- [x] 复跑7份既有前缀样本；复跑32份实际导入审计。旧32源审计文件保留，不覆盖；不要将其它局部候选当成来源通过。
- [x] 最终运行后端 `uv run python -X utf8 -m pytest tests/unit tests/api tests/contract -q`、`uv run ruff check .`、`uv run mypy src`。独立集成复审后执行 `git diff --check`、核对脏树及embedding冻结SHA；更新 `docs/project-status.md` 和 `.superpowers/sdd/progress.md`。

## 下一项

单独处理条号中水平制表符、章节/段内右锚与零空白私用前缀的组合。明确缺失正文与修改决定引用结构仍保留阻断。本次计划不扩大到这些行为。


## 最终结果（2026-09-07 11:16）

全部步骤完成。独立任务/集成复审通过，完整diff为 .superpowers/sdd/empty-parenthesis-integrated-20260907T031022Z.diff；根代理验收见 .superpowers/sdd/empty-parenthesis-root-verification.md。实际目标18条/18块/18叶、7源回归通过、32源新增1通过/31残余，原文不改，未入库发布。本次后端2787passed/1平台skip/1既有warning，Ruff及mypy218通过。CPU向量继续，5项冻结runtime不变。
