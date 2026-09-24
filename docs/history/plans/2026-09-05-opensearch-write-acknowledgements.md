# OpenSearch 写入结果核验

已发现真实发布风险：replace_documents 忽略 delete-by-query HTTP/部分失败，并只检查 Bulk HTTP 状态而不检查逐项错误。按发布前质量门禁要求，失败必须中断，不能继续切 alias。

- [ ] TDD：delete HTTP失败、timed_out/failures/version_conflicts；Bulk HTTP200但errors=true、逐项失败、结果数量不足/格式错误必须抛稳定 OpenSearchError；错误不回显文档正文或供应商原始Payload。
- [ ] 删除成功之后才发 Bulk；逐项确认本批 index 操作成功，核对响应数和请求 IDs，失败时不返回成功。空文档仅执行并验证删除，不发送空bulk。
- [ ] 保留成功与幂等替换契约，更新原先过度简化的Mock响应使其符合真实服务契约；单/集合发布测试证明写失败不调用alias/snapshot。
- [ ] 独立临时 OpenSearch 索引真实 BM25 写/读回验证（不加载Embedding、不触碰dataset_v1），确保新增响应检查兼容实际服务；清理仅本次创建的测试索引。
- [ ] Ruff/mypy、聚焦测试和独立审查，记录跨系统快照/alias一致性仍未解决。

范围：infrastructure/search/opensearch.py 的replace_documents及辅助验证、相关tests/unit与临时索引集成测试。不会改变检索查询协议、字段/索引mapping或业务数据集。
