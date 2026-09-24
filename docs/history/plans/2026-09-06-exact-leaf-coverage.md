# 持久化精确叶子覆盖

关联 [真实全集发布](2026-09-06-full-real-corpus-release.md)。真实质量检查发现首条头被派生子块剥离、重复正文只能歧义查找两个问题。

1. TDD 新增 LegalChunk 可选成对位置字段 parent_relative_char_start/end，语义为相对权威 Provision 全文的半开区间。迁移15添加 nullable 列与 CHECK，不自动回填，不触碰旧发布版本。
2. 新 hierarchical-v2 派生保存已计算的精确位置，并将首段条头保留在首叶子；普通 Parser 与源结构摘要不变。
3. 质量验证位置边界、切片等于块正文、父子位置包含和覆盖；有位置时不使用模糊字符串定位。旧 NULL 块仍走原保守路径。摘要绑定新位置，旧NULL摘要兼容。
4. 报告明确 chunk_parser_version，旧报告缺省 hierarchical-v1。发布要求配置和每个Chunk一致，源Proof仍绑定原Parser版本。
5. 根任务先备份再执行迁移；本任务仅用随机隔离MySQL验证升级、约束、ORM往返与迁移check。既有未发布版本由根明确选集重放重派生并重新核验；旧5发布版本不动。

## 本次验证与交接

- 7 项位置/完整覆盖回归先 RED；质量重复文本回归先 RED。最终8组单测219 passed，Ruff、10源文件mypy通过。
- 随机隔离MySQL4 passed，包含14→15升级、Alembic check无漂移、位置往返、CHECK拒绝无效位置、非空位置拒绝降级、NULL后降级/再升级。
- 未执行业务迁移或旧版本重派生。迁移15由根任务备份后执行；升级前不得启动使用新ORM的业务进程。
- 新报告header保存chunk_parser_version，旧报告缺省raw/hierarchical-v1；单文件与集合发布使用实际分块版本。源Parser及source proof版本不由此切片改写。
- 只重建未发布明确选集，保留原成功报告；结构Parser后续变动须另建来源版本，单纯位置/条头叶覆盖修复允许重派生块并重新质量审核。
