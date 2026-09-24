# 单文件导入接入层级分块

依据既定 S1/S4 和“未知日期/效力不得伪造”规则，接通已经验证的引擎。迁移12及叶子发布为依赖；只运行合成文件和隔离服务验收，不自动重建当前 dataset_v1。

- [x] 增加可测试的 CLI 准备阶段，加载/解析/元数据验证全部先于数据库连接。测试缺省 status_unknown、日期 None、日期严格格式、显式状态/日期/category 原样映射。
- [x] 默认版本标签有公布日期时沿用日期标签；没有日期时使用 source-sha256:<完整原件哈希>，表示来源修订且重跑稳定，不用当天充当法律版本日期。默认来源用绝对文件 URI，不拼造网页。
- [x] CLI 使用 derive_hierarchical_chunks，将 parsed.articles 和持久化 provisions 一对一连接；短条完整父块，长条子块，索引通过已有叶子门禁。分块 parser_version 后缀标记 hierarchical-v1，法律版本的来源 parser 元数据保持原契约。
- [x] 单测先失败再实现；真实 MySQL 合成 DOCX --import-only 验证长条多子块、短条完整、重复导入、元数据未知、无模型调用。补子块检索结果经权威整条回填的验证。
- [x] Ruff/mypy/聚焦 pytest、独立审查、现状文档更新。普通 onboarding 无段落资料时仍使用完整条文，不从拼接后的文本猜段落。
