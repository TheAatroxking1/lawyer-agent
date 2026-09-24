# Docker PyMilvus 查询实施计划

> 使用 subagent-driven-development：依赖与容器打包可独立处理，主任务实现查询及测试，随后集成验收。

目标：按用户确认的设计，实现本地blog.lawyer_db查询容器，保留现有数据及公网OpenSearch装配。

架构：tools/milvus_query/legal_query包含配置/Embedding、PyMilvus只读查询、CLI；deploy/milvus-query.compose.yaml加入已有external milvus网络。凭据只读挂载deploy/secrets，禁止COPY到镜像。

约束：Python3.12；模型qwen3.7-text-embedding/1024/COSINE；BM25输入text；两路各100、RRF60、融合50、返回10；非root、无GPU、有资源上限；保留用户脏工作区，不提交Git。

- [x] 建立聚焦失败测试：参数/向量校验、双路范围、RRF配置、输出、精确查询、API错误脱敏及禁止自动降级。
- [x] 实现配置与无导入副作用的查询Embedding适配器，复用现有合法配置；实现hybrid/bm25/dense/exact/status命令。
- [x] 固定PyMilvus3.0.1与传递依赖，建立Docker镜像与独立Compose入口；容器内18项新单测通过。
- [x] 用现有向量和原文完成12次真实SDK检索及范围/精确/空范围验证；实际调用1次自然问题Embedding，前后记录数921,117。
- [x] Ruff、独立源码mypy、Compose、独立复审与运行证据完成；文档已更新。质量评测与回答模型接入明确留待后续。
