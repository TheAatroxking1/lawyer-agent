# 父子分块持久化接线

S4 前置：现有 legal_chunks 的 (version_id, provision_id, chunk_type) 唯一约束拒绝同条多个款/项，仓储读取映射也缺 paragraph/item。本切片实现可重复、安全的父子块入库，不重建业务库或当前索引。

- [x] 真实 MySQL 先验证多个同类型子块（逆序输入、嵌套父链）写入/读回失败，验证旧版本块不变、重复替换与清空。
- [x] 新 migration12（接11）先建 version_id 普通索引承接外键，再移除旧唯一索引；ORM 同步。降级前查重，有多子块时明确拒绝，禁止删行后强行降级；兼容数据可降级再升级，Alembic check 无差异。
- [x] 仓储补齐 ChunkType 映射，写入前校验目标版本、父链闭合/无环/无重复/同条同版本以及 provision 实际归属；拒绝时保留旧块。
- [x] 锁定版本以串行化替换；保存点内先解除旧父链再删除，按父节点优先分层 flush 插入。插入失败回滚替换；保留调用方事务所有权。FAILED 块可以保存以供质量追踪，由发布门禁拒绝索引。
- [x] MySQL 回归旧整条块及层级块、新旧迁移、无效图拒绝和版本归属；Ruff、mypy、独立审查。

范围：models/legal_corpus.py 的 LegalChunkModel、repositories/legal_corpus.py 的 Chunk 仓储及辅助函数、新 migration12、新 tests/integration/mysql/test_hierarchical_chunk_persistence.py。其他分类改动由 S2 负责，编辑时保留。
