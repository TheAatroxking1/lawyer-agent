# 阶段 2 先行切片：Matter、Document 版本与文件元数据 Implementation Plan（非 Embedding/非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付规格 8.2/9.4 中**不依赖 Embedding 或生成模型**的先行切片：Matter 业务项目、Document 原件/版本/解析状态、文件上传元数据流（预签名对象引用占位 + SHA-256/MIME/配额校验 + ACCEPTED/人工处理状态机）。合同条款识别、风险规则、RAG、Drafting/Review 生成与 DOCX/PDF 导出均属后续模型/导出切片，不在本计划。

**Scope / 边界（明确延后）：**
- 不调用生成模型、不做条款/风险 AI 识别、不接 OpenSearch、不做 SSE。
- 对象存储使用「受管对象引用端口」抽象：本切片不部署 MinIO 直传，先落元数据与状态，真正的预签名分片直传作为后续切片。
- 病毒扫描/沙箱转换使用端口占位：默认拒绝不可信格式并进入人工处理状态；不实现真实 AV。
- `.docx` 解析复用阶段 1 Loader/Parser（只读、拒宏）；`.doc`/ZIP 延后。

**Architecture:** MySQL 是 Matter/Document 版本/上传状态的事实源；原件永不覆盖、只读。Matter 是租户业务项目（参与方/负责人/时间线/截止日/资源引用）；Document 保存原件引用与派生版本；解析状态机 `UPLOADED → VALIDATING → ACCEPTED / NEEDS_REVIEW → PARSING → READY / FAILED`。高/中风险时限结论（诉讼时效等）需律师确认，本切片只存事实不计算期限结论。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.0、Alembic、asyncmy、MySQL 8.4、python（标准库 hashlib）、pytest、Ruff、mypy、Docker Compose。

## Global Constraints

- 全部代码/迁移/测试/配置/文档 UTF-8；仅中国大陆法律与法律服务场景。
- MySQL 8.4/InnoDB/`utf8mb4`/UTC `DATETIME(6)`；UUIDv7 `BINARY(16)`。
- 每条租户私有数据强制 `tenant_id`；复合唯一键与外键包含 `tenant_id`；所有 Repository 显式接收 `TenantContext`；必须带跨租户反向测试。
- Document/Matter 更新采用乐观锁 `version`/ETag；重要写支持 Idempotency-Key。
- 原件与派生物只以受管对象引用（object_key）保存，绝不将原件写入 Git/MySQL BLOB/日志；测试使用合成小文件。
- 解析失败/密码保护/不可信格式进入 `NEEDS_REVIEW` 人工状态，绝不绕过安全检查或假装成功。
- 文件上传会话不暴露任意路径；对象引用仅允许白名单受管 key；读取带租户前缀隔离。
- 使用 TDD：失败测试 → 实现 → 聚焦与静态检查 → 独立 Commit。
- 不修改已发布 Migration；新 Revision 固定从 `20260904_07` 向前。
- Secret/.env/语料原件不提交；日志与 Fixture 脱敏。

## File Map

```text
backend/
  alembic/versions/20260905_08_phase2_matter_documents.py
  src/lawyer_agent/
    domain/matter_documents.py       # Matter/Document/DocumentVersion/UploadState/Review 常量
    application/matters.py           # MatterService + ports + TenantContext 校验
    application/documents.py         # DocumentService + 上传元数据流 + object_ref 端口
    infrastructure/persistence/models/matter_documents.py
    infrastructure/persistence/repositories/matters.py
    infrastructure/persistence/repositories/documents.py
    infrastructure/objects/object_store.py  # 受管对象引用端口（本切片占位实现）
  tests/
    unit/test_matter_documents.py  test_document_service.py
    integration/mysql/test_matter_documents_migration.py  test_matter_repositories.py
    integration/mysql/test_document_upload_flow.py
    api/test_ai_job_runtime_unchanged.py  # 保持既有门禁不受影响
```

## 里程碑 A：Matter 与 Document 领域模型与 Migration

### Task A1: Matter 与 Document 版本模型 Schema
- domain 值对象 + 7–9 张表 Migration（matters/documents/document_versions/document_upload_sessions/matter_parties 等，含复合唯一/复合 FK 含 tenant_id）
- 集成测试：fresh upgrade 往返 + `alembic check`
- Commit: `feat: model tenant matters and versioned documents`

### Task A2: Matter Repository 与跨租户反向
- `MatterQueryPort`（list/page/create/update 用 TenantContext；`get_by_id` 只接受 tenant+id）
- 跨租户反向测试：A 租户无法经 id 读 B 租户 matter
- Commit: `feat: query tenant matters safely`

## 里程碑 B：Document 上传元数据与对象引用

### Task B1: 对象引用端口与上传会话
- `ObjectStorePort`（`create_upload_target(key)` / `confirm_upload` / `delete_key`）；占位实现只允许 `tenant_<id>/...` 前缀
- 上传会话创建 + Idempotency-Key；集成测试断言对象 key 白名单/租户前缀与非法 key 拒绝
- Commit: `feat: scope document object references per tenant`

### Task B2: 完成校验与状态机
- 完成后校验 size/MIME/SHA-256/配额 → `ACCEPTED` 或 `NEEDS_REVIEW`；AV/转换端口占位（不可信一律 NEEDS_REVIEW 除非测试 stub 放行）
- 投递解析任务（复用阶段 1 Loader 端口，真实 docx 样本）→ `READY/FAILED`
- 集成测试覆盖大小/MIME/哈希不符拒绝、NEEDS_REVIEW 不绕过、真实 docx ACCEPTED→READY
- Commit: `feat: accept and validate tenant document uploads`

## 里程碑 C：Review 状态机占位与切片门禁

### Task C1: Document Review 状态（DRAFT→PENDING_REVIEW→APPROVED… 常量/表占位，不做生成）
- 状态常量 + 迁移列 + 最小 Repository 查询；不接模型
- Commit: `feat: stage document review states`

### Task C2: 切片总门禁
- 全量 pytest（真实 MySQL）、ruff、mypy、Migration 往返、secret 扫描、AGENTS.md 阶段说明
- Commit: `docs: complete phase2 non-model matters slice`

## Plan Self-Review

- Spec coverage：规格 8.2/9.4/9.1 的 Matter/Document/文件元数据/Review 状态；模型/条款/RAG/导出/SSE/直传 MinIO 明确延后并标注。
- Security review：租户隔离贯穿 Repository/对象 key；无路径穿越；原件只读；NEEDS_REVIEW 不绕过。
- Placeholder scan：真实 .docx 样本验证解析；AV/转换/预签名为显式端口占位并在门禁中说明。

## Exit Criteria

本切片在里程碑 C2 门禁通过后才算完成。证据：真实 MySQL、Migration 往返、跨租户反向测试、合成文件 + 真实 docx 样本、ruff/mypy 零错误、无 Secret、工作树干净。生成模型/条款风险识别/OpenSearch/SSE/MinIO 直传/PDF 导出为明确未验证项。
