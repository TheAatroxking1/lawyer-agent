# S3 法规标题/编章导航纵切

依据已确认的集合发布层级方案，导航文本只来自 MySQL 法规标题与编/章 structure_path，不使用模型生成摘要。采用独立派生导航索引，与一个主物理索引一一对应；主 alias 仍是唯一在线切换点。实施全过程不重建现有 dataset_v1。

## 共享契约

- 新 `domain/legal_navigation.py`：`NavigationDocumentKind`（instrument/structure）；冻结 `NavigationDocument(navigation_id: str, version_id: UUID, kind: NavigationDocumentKind, locator: str, content: str, parser_version: str)`；冻结 `NavigationSearchHit(version_id: UUID, locator: str, score: float)`。ID 为64位小写SHA256，UUID要求v7，文本非空，score有限。
- `navigation_index_name(main_index_name)` 位于 domain/legal_navigation.py：先按现有物理索引名白名单验证，返回 `lawyer-nav-` + SHA256(UTF8主索引名)前32位。
- 应用 `LegalNavigationIndexService(source, search).build(*, version_ids: tuple[UUID,...], main_index_name: str, parser_version: str) -> NavigationBuildResult(index_name: str, indexed_documents: int)`。
- 应用 `LegalNavigationSearchService(search).candidate_versions(*, main_index_name: str, query: str, candidate_limit: int = 5, scan_size: int = 50) -> tuple[UUID,...] | None`；None表示合法的全局检索路径，不表示基础设施故障。
- Adapter `OpenSearchNavigationClient` 新文件 `infrastructure/search/legal_navigation.py`，与既有REST client同base_url/可注入transport。方法：`ensure_navigation_index(index_name)`；`replace_navigation_documents(index_name, documents: tuple[NavigationDocument,...], *, parser_version)`；`count_documents(index_name) -> int`；`mark_navigation_ready(main_index_name)`；`navigation_schema(main_index_name) -> int | None`；`search_navigation(index_name, *, query, limit) -> tuple[NavigationSearchHit,...]`。仅支持schema=1；主mapping._meta字段为legal_navigation_schema，写入时保留其它meta字段。共用已有写回执检查；错误不带正文。

## Task 1：导航模型、构建与定位（独立文件）

- [x] TDD：标题+去重编章前缀，ID确定性、来源版本/条文归属、空/错误来源在任何写入前拒绝。
- [x] 构建先读完并验证全部version；每version标题一文档，合法编/章节点各一文档，忽略节/未知节点。structure locator用当前编/章标题，content包含法规标题与该节点完整路径；ID按version/kind/完整路径确定，不合并不同位置的同名章。
- [x] 顺序ensure sidecar→replace→count（严格等于预期且>0）→mark主索引ready。任何失败不写marker。
- [x] 定位先查主marker；旧索引无marker全局，非1标记或sidecar缺失/异常必须失败。NFKC、去空白和书名号后，完整locator连续出现在query才收窄。弱匹配/无强候选/候选超过上限/扫描达到scan_size（可能截断）均全局；保持候选顺序去重。

## Task 2：OpenSearch 导航适配器与多版本范围

- [x] Task1共享domain契约就绪后，Adapter TDD覆盖mapping、typed bulk与逐项回执、count格式、marker保留其它meta、未知schema、畸形hit、错误脱敏。空删除同现有严格回执。
- [x] 原search_bm25/search_knn新增 `version_ids: tuple[UUID,...] | None = None`，与version_id互斥；空/重复/非法tuple拒绝，两类查询使用相同terms过滤。保持旧None/单version请求不变。
- [x] 临时真实OS验证导航建/写/查/count/marker，多version范围；只清理本测试索引。

## Task 3：实际发布/问答入口接线

- [x] 单/集合发布服务必需注入navigation builder，主块索引成功→导航构建/核验/marker→alias→snapshot；失败不动alias/snapshot。
- [x] snapshot manifest加navigation_index/navigation_schema_version=1/navigation_documents，metrics加navigation_documents；保留主indexed_documents仅叶子数。
- [x] HybridSearch新增互斥version_ids并透传BM25/kNN，仍单次embed。DatasetSearch必需navigation，显式version跳过导航，其余读取候选后只把非None范围传给Hybrid；全局仍用原调用参数。
- [x] 更新CLI、api/dependencies及所有实际构造/测试double；不留下新发布绕过导航的可选旁路。旧已发布索引依据无marker兼容检索。
- [x] 纵向集成：合成多法规→主+导航发布→标题明确查询范围→一般问题全局→删除已标记sidecar硬失败→旧无marker/alias回滚兼容；不使用付费模型。

## Task 4：复核与续做

- [x] 独立规格/代码质量审查，聚焦及组合pytest、Ruff/mypy、真实临时OS/MySQL必要验证；更新project-status。
- [x] 不宣称真实语料A/B提升或S4/S6完成。别名切换后MySQL snapshot失败的既有跨系统窗口仍需后续解决；marker仅保证导航与主物理索引配对。
