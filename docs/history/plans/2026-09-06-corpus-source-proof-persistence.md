# S6a3 来源证明持久化与重跑校验

> 按 subagent-driven-development 分工实施并独立审查。保留用户工作区，不自动提交。延续已批准的 S6 数据导入要求，不重新讨论架构。

**目标：** 原件/派生双身份和实际解析结构随法规版本在同一 MySQL 事务中持久化；重跑必须匹配既有证明，不能凭拼接全文摘要就重建不同结构的 Chunk。

**边界：** 此阶段接入现有单文件 CLI，为后续受控批量入口提供同一服务。不确认候选法律元数据、不发布真实索引、不将证明行称作不可变原件对象存储。旧版本没有证明时明确拒绝自动补证，需要另行核对/迁移路径；不得静默补全来源、改变旧版本或重写已发布迁移。

## 固定接口与数据

新增 `domain/legal_source_proof.py` 的 frozen/slots `LegalSourceProof`：

- `source_ref: str`、`input_ref: str` 为原件与实际读取引用（CLI 均使用准备阶段的原件/派生 URI；独立于用户显式设置的法律来源引用）。
- `source_sha256: bytes`、`input_sha256: bytes`、`structure_sha256: bytes`，严格各 32 字节。
- `loader_version: str`、`parser_version: str`，非空且至多 64 字符。
- `converter_version: str | None = None`、`converter_fingerprint: str | None = None`，成对存在；转换版本至多 128 字符、指纹严格小写 SHA-256 hex。
- `recovery_reason: str | None = None` 仅允许既有 `office_validation_failed` 且为静态恢复版本。
- `quality_flags: tuple[str, ...] = ()`，排序去重的非空字符串 tuple，每项最多 128 字符，总数最多 64。引用为非空字符串，最多 4096 字符。
- 原件与读取引用或摘要不一致时必须存在转换版本/指纹。静态恢复证明保留局限，但当前导入证明服务明确拒绝自动接受。

应用 `application/legal_source_proof.py`：`LegalSourceProofPort.find_for_version(version_id: UUID) -> LegalSourceProof | None` 与 `create_for_version(version_id: UUID, proof: LegalSourceProof) -> None`；`LegalSourceProofService(repository).ensure(imported: LegalImportResult, proof: LegalSourceProof) -> None`。新版本创建证明；重跑须既有证明完全相同，缺失则 `LegalCorpusImportConflict("source_proof_missing_requires_review")`，冲突则 `LegalCorpusImportConflict("source_proof_conflict")`。不 commit，不 upsert，不修改既有证明。静态恢复在仓储操作前拒绝。

SQL `legal_version_source_proofs`：`version_id` 为主键与 `legal_versions.id` 外键；上述各字段及 created_at；哈希 BINARY(32)、引用 Text、版本字符串、JSON flags。转换版本/指纹成对的 CHECK，恢复原因约束；不添加私有/租户信息，此表为全局公共法规版本证明。Repository flush、不 commit，回读重新构造严格领域对象，异常不能静默忽略。

## Task 1：领域证明与应用门禁（可与 Task 2 独立并行）

- [x] TDD：三种摘要长度/类型、转换必需、版本/指纹配对、静态恢复约束、flags、引用/版本长度；新版本创建、相同重跑无写、缺失/冲突拒绝、静态恢复拒绝。
- [x] 实现两个新模块及 `tests/unit/test_legal_source_proof.py`。不触碰 CLI/ORM/真实文件/数据库。
- [x] 聚焦测试/Ruff/mypy、冻结后独立审查。

## Task 2：迁移、仓储与 CLI 接线（Root）

- [x] 新 migration13 从 20260905_12 升级，新增证明表不回填旧记录；有证明行时 downgrade 明确拒绝，避免丢失证明。ORM 单独模块并登记；仓储单独模块实现固定端口。
- [x] CLI 从 PreparedImport 建立 LegalSourceProof，在同一个 import/chunk 事务里先确保来源证明，再派生/替换 Chunk。预检仍不连 DB，字段 database_provenance_persisted=false 保持事实；日期/效力仍未知。
- [x] 合成测试先证缺失证明/接线失败，再验证原/派生双摘要回读、完全相同重跑、结构指纹变化（全文不变）、缺旧证明、来源/转换指纹变化均拒绝且旧块不变；证明/Chunk 写入失败整体回滚。
- [x] MySQL 验证只用本任务新建的隔离容器/测试库，使用本地已有 mysql:8.4 镜像、独立端口、随机临时凭据与临时数据；不启动或更改现有共享容器。测试数据全为合成；迁移完整升级、外键/唯一键、回滚和降级保护必须实际验证。

## Task 3：复审、整体回归与交接

- [x] 独立审查并处理发现；最终本地 backend pytest/Ruff/mypy；迁移与 CLI 的真实隔离 MySQL 集成。
- [x] 检查最终 diff/未跟踪文件/UTF-8/秘密忽略；清理本任务临时测试容器，不删除其它数据。
- [x] 更新项目现状，明确证明已入库仅指合成试验和代码接线；真实 16,006 份仍待元数据核对、受控批量导入、发布恢复及质量/模型验收。

## 2026-09-06 完成证据

- 实现并独立复审通过：领域/应用证明、migration13/ORM/Repository、CLI 同事务接线及测试。转换指纹在边界归一化；quality_flags 在领域内排序去重，静态局限自动保留并限制总数。
- TDD 观察缺模块失败，额外观察大写十六进制指纹边界失败后修复。最终本地后端1,921通过/1跳过/1警告，19.59秒；Ruff通过，mypy 193源文件通过；独立复审104相关测试通过。
- 本任务独立MySQL容器实际11项通过（最终14.07秒），涵盖原/派生证明回读、冲突/缺失拒绝和旧块保留、全事务回滚、旧版本升级、主外键/CHECK、降级保护、base/head往返和模型一致性。无真实语料入库/索引发布。
- 检查发现约束名受默认命名约定重复前缀影响而过长，导致真实Alembic漂移；已修正未发布migration13及对应模型，命名限制测试与真实command.check通过。
- 下一步沿受控批量入口推进。真实全集pending元数据、旧无证明版本迁移复核、发布恢复、真实模型/法律质量和对象原件持久化均不在本阶段完成声明内。
