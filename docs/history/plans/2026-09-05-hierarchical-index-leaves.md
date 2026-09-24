# 父子块索引叶子选择（S4 前置）

> 使用 subagent-driven-development 执行并独立复核。属于已批准父子块检索方案的内部接线；不改HTTP，不操作真实dataset，不提交Git。

目标：单法规/多法规索引服务仅向量化叶子（无子节点的完整条文父块亦为叶子），避免父子重复召回。旧一条一块数据行为保持。

## Task 1：选择器与两种索引入口

文件：新增 backend/src/lawyer_agent/application/legal_index_chunks.py；修改 legal_vector_indexing.py、legal_dataset_set_publish.py；新增 unit/test_legal_index_chunks.py，扩现有 test_legal_vector_indexing.py、test_legal_dataset_set_publish.py。

接口：select_index_leaves(chunks: tuple[LegalChunk, ...]) -> tuple[LegalChunk, ...]。保留输入相对顺序，不改原对象。空输入返回空。校验重复ID、缺失父节点、自指/环、父子不同version/provision均抛 LegalIndexChunkError(ValueError)，失败质量块拒绝参与发布，不静默丢掉损坏数据。无父节点整条和表格/附件可独立作为叶子。

- [x] RED：旧整条保持；父+两子只返回两子；嵌套只叶子；乱序父子仍按输入叶序；重复/悬空/串条/串版本/环/failed拒绝；空集。
- [x] GREEN：用ID映射校验图和祖先链，线性或接近线性处理；避免递归深度问题。
- [x] RED：单版本索引的向量文本和输出文档只含子叶、整条无子父仍入索引；集合发布对应计数与snapshot.indexed_documents为叶子数；损坏图在embed/search/alias前失败。
- [x] GREEN：两个索引入口在读取chunks后、任何外部副作用前调用同一选择器。集合服务须预检全部版本，再开始embed，不能先向量化第一版本才发现第二版本损坏。
- [x] pytest选择器/两个index服务相关测试；Ruff+mypy strict。报告实际RED/GREEN证据。

限制：数据库多同类子块约束迁移、CLI派生接线、S1短条/歧义回退、S3标题定位及真实索引A/B尚属后续验收，不因本前置完成宣称S4全完成。
