# 零宽条头前缀实施

使用subagent-driven-development与TDD，在既有全集发布目标内执行，不重新讨论用户已授权范围；不提交Git，保留脏树。

- [x] 快照prefixed_article.py及test_prefixed_article_parser.py到独占目录；新增精确v4的U+200B正反例并观察RED。
- [x] 最小前缀正则增加1..8个U+200B后可选水平空白；原匹配及闭合算法不改。测试字符/spans/坐标保留、连续混合组、默认/v3/v4-extra不启用、超限/混入/跨行/引用/补充/缺锚/章节拒绝。
- [x] 聚焦原前缀/标题/段内/profile/exact-text测试和Ruff通过，独立任务复审实际diff；与数字空白agent不同时改Parser。
- [ ] 根代理真实福建SME来源CLI1..51及全文/内存导入/replay/父子块覆盖；源和派生SHA不变。合并在本轮Parser整体检查后更新现状，不能将未生产入库的修复算成发布完成。

前三项实现与复审完成，引用伪锚P2已关闭；最终联合后端2923通过。最后一项实际执行但未达到通过条件：福建仍50条，第1段沿革“（… )”宽度错配导致全局边界不清，候选与左右锚均保守拒绝。不得勾选实源成功或解除阻断；根因 `.superpowers/sdd/fujian-zero-width-boundary-diagnosis-20260907.json`，后续另做有证据的沿革边界修复、新Parser身份。本轮冻结v4不再扩此行为。
