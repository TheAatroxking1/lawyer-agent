# S2 法规类别贯通实施计划

> 执行：使用 subagent-driven-development 分工与复核；用户已要求持续完成项目，不在切片之间重新询问是否继续。未经明确授权不提交 Git。

**目标：** 将已确认层级分块方案中的法规 category 贯通导入、数据库、HTTP/工具读接口及前端展示。

**架构：** category 属于 LegalInstrument 稳定身份。采用 LegalCategory StrEnum：constitution、law、administrative_regulation、judicial_interpretation、local_regulation、supervisory_regulation、unknown。旧行、旧调用缺省 unknown；不根据标题/目录推断。输入必须为强类型枚举，数据库使用非空 VARCHAR(32)、unknown server default 和取值 CHECK。既有身份 category 不一致时显式拒绝导入，不静默覆盖已有分类。

**约束：** UTF-8、中国大陆法、保留既有变更；不重写已发布 migration、不修改主库；本阶段不改变 chunk/embedding/已发布索引。API 响应新增 category，前端镜像此必需字段。

## Task 1：后端类别纵切

文件：domain/legal_corpus.py；application/legal_corpus_import.py、legal_corpus_import_mapping.py；infrastructure/persistence/models/legal_corpus.py、repositories/legal_corpus.py；api/v1/legal_corpus.py、api/dependencies.py；cli/corpus_publish.py；backend/alembic/versions/ 新增 11 迁移。

- [x] 先在 unit 新测试验证枚举完整性、旧输入 unknown、非法值拒绝、显式类别导入读回、映射透传、相同类别 replay、不同类别冲突且不写入。
- [x] 运行聚焦 pytest 观察缺失 category 的预期失败。
- [x] 添加 LegalCategory、默认字段与校验，仓储双向映射、列表/详情/工具投影；CLI 增 --category choices，缺省 unknown。
- [x] 新 migration down_revision=20260905_10：add column category VARCHAR(32) NOT NULL DEFAULT 'unknown'，添加合法取值 CHECK；downgrade 先 drop CHECK 再 drop column。
- [x] MySQL 测试旧版本迁移保留旧行 unknown、新分类写读、非法 DB 值拒绝、Alembic check；HTTP 与工具响应 category 一致，认证不变。
- [x] 执行聚焦 unit、相关真实 MySQL 集成、Ruff 与 mypy；核对新增响应字段引起的白名单断言更新。

## Task 2：前端类别呈现

文件：frontend/src/api/types.ts；frontend/src/lib/format.ts、format.spec.ts；CorpusView.vue、CorpusInstrumentView.vue；必要测试 Fixture。

- [x] 先测试 legalCategoryLabel：constitution→宪法、law→法律、administrative_regulation→行政法规、judicial_interpretation→司法解释、local_regulation→地方法规、supervisory_regulation→监察法规、unknown/空值→类别未知；未识别字符串原样展示。
- [x] 观察失败，再补函数及类型 LegalCategory；InstrumentSummary.category 为 LegalCategory。
- [x] 目录行和身份详情显示 category 中文名称，不改变版本效力状态，不新增筛选 API。
- [x] 运行 npm test、typecheck、build；后端由 Task 1 同步支持字段。

## Task 3：审查与交接

- [x] 独立审查后端/前端完整 diff：安全边界、迁移兼容、导入 replay、投影一致性、测试真实性。
- [x] 修正发现并运行覆盖测试；git diff --check。
- [x] 更新 docs/project-status.md 的 S2 实际完成情况、命令结果及下一步；不把切片完成当成项目完成。
