# 阶段 1 先行切片：法规语料、版本模型与证据骨架 Implementation Plan（非 Embedding）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付“法规数据与有据问答”中**不依赖 Embedding 模型运行**的先行切片：法规语料导入/解析、不可变数据集快照与版本模型、条文级 Chunk 骨架、Evidence Bundle 与 Citation Gate 后端校验。OpenSearch 混合检索、Dense Vector、Reranker、DeepSeek 问答与 SSE 仅在用户另行授权后作为后续切片实施。

**Scope / 边界（明确延后）：**
- 不运行 Embedding 模型、不建 Dense 索引、不调用生成模型；BM25/向量/SSE/模型网关全部延后。
- 语料只读源 `F:\ai律师数据库`：只取当前任务所需最小样本，禁止递归全量读取；跳过 ZIP。
- 本切片不产生面向客户的法律结论；只建立数据、解析、版本、检索单元与证据校验骨架，任何“现行有效”宣称必须有来源与状态证据。
- `.doc` 经受限转换属于延后能力；本切片对 `.docx` 以统一 Loader 接口 + 最小真实样本验证 Parser 行为。

**Architecture:** MySQL 是语料、版本、质量与证据的唯一事实源；不可变原件哈希与派生数据分开记录。`LegalInstrument`（稳定法规身份）与 `LegalVersion`（版本文本）严格分离；相似/缺文号只生成“候选关系”，未经规则或人工确认不得合并。检索单元 `Chunk` 以完整条文为 Parent、必要时以款项目为 Child，禁止跨条拼接。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.0、Alembic、asyncmy、MySQL 8.4、python-docx（经统一 Loader 接口）、pytest、Ruff、mypy、Docker Compose。

## Global Constraints

- 全部代码、迁移、测试、配置和文档使用 UTF-8；产品仅面向中国大陆法规与法律服务场景。
- Python `>=3.12,<3.14`；沿用现有 SQLAlchemy/Alembic 版本约束；新增依赖经 `uv lock` 固化并在文档说明用途。
- MySQL 8.4/InnoDB/`utf8mb4`/UTC `DATETIME(6)` 是事实源；UUIDv7 以不交换字节序的 `BINARY(16)` 保存。
- 来源文件作为不可变原件：文件哈希、Parser 版本、导入批次、派生文档版本分别记录；原件不落 Git、不落 MySQL BLOB，只记录受管对象引用。
- 国家法规数据属公共全局只读数据（无 tenant_id 或使用保留的 platform 范围），租户私有知识留待后续切片；本切片所有公开读取仍须通过显式读取端口，禁止无条件全局通道。
- 每条证据至少包含 `evidence_id`、名称、版本与公布/施行日、效力状态、条号与结构路径、完整原文、来源文件/哈希/数据集版本、访问授权结果（规格 6.5）。
- 结构/解析失败写入批次质量清单：文本覆盖率、条号连续性、重复、必需字段、异常格式；质量不达标不得发布数据集别名。
- 使用 TDD：先写聚焦失败测试并观察预期失败，再实现、跑聚焦与静态检查并形成独立 Commit。
- 不修改已发布 Migration；新 Revision 固定从 `20260903_06` 向前。生产回滚、数据清理与归档留待后续。
- Secret/.env/语料原件不提交；日志与 Fixture 脱敏；`F:\ai律师数据库` 只读不改写不删除。

## File Map

```text
backend/
  alembic/versions/20260904_07_phase1_legal_corpus.py
  src/lawyer_agent/
    domain/legal_corpus.py          # LegalInstrument/LegalVersion/Provision/LegalChunk/质量与状态
    application/legal_corpus.py     # 导入/盘点/解析/发布应用服务与端口
    application/evidence.py         # EvidenceBundle/CitationGate 校验（后端，无模型）
    infrastructure/persistence/models/legal_corpus.py
    infrastructure/persistence/repositories/legal_corpus.py
    infrastructure/documents/loader.py     # Loader 统一接口
    infrastructure/documents/docx_loader.py
    infrastructure/documents/parsers.py    # 编章节条款项目 结构识别（含降级路径）
  tests/
    unit/test_legal_corpus.py  test_docx_parser.py  test_evidence.py
    integration/mysql/test_legal_corpus_repositories.py  test_legal_corpus_migration.py
    integration/mysql/test_evidence_gate.py
```

## 里程碑 A：语料领域模型与 Migration

### Task A1: 版本模型、Chunk 与快照 Schema

**Files:** `domain/legal_corpus.py`、`models/legal_corpus.py`、Migration `20260904_07`、`tests/integration/mysql/test_legal_corpus_migration.py`、`test_legal_corpus.py`

**Interfaces:** `LegalInstrument`、`LegalVersion`、`Provision`、`LegalChunk`、`DatasetSnapshot`、`LoadBatch`、`QualityIssue`。

- [ ] **Step 1: 写模型与迁移失败测试**
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现 domain 值对象与表模型**：复合唯一键含 `(legal_instrument_id, …)`；`LegalVersion` 记录公布/施行/失效日与 `status`（现行/已废止/状态未确认/历史草案/教学材料等）；状态未确认不得宣称现行。提供 `dataset_v1` 快照与清单、质量指标、解析器版本、失败项。
- [ ] **Step 4: 实现 Migration + 往返**：`20260903_06 -> head -> 20260903_06 -> head`；Tenant 无关字段保留 platform/公共读取端口。
- [ ] **Step 5: 验证并提交**：`uv run pytest …-v; uv run ruff check .; uv run mypy src`
- Commit: `feat: model versioned legal instruments and provisions`

### Task A2: Repository 与跨租户/越权读取约束

**Files:** `repositories/legal_corpus.py`、`tests/integration/mysql/test_legal_corpus_repositories.py`

**Interfaces:** `LegalCorpusQueryPort`（显式公开范围读取）、`LegalCorpusImportPort`。

- [ ] **Step 1–2: 失败测试**：只能经显式端口读取；不能出现无 tenant 约束的私有资源 `get_by_id` 误用。
- [ ] **Step 3: 实现 Repository**：按版本号/时间点过滤；版本间差异查询（候选关系不自动合并）。
- [ ] **Step 4: 验证并提交**。Commit: `feat: query versioned legal corpus safely`

## 里程碑 B：语料盘点与受控导入

### Task B1: 盘点、哈希与 Manifest

**Files:** `application/legal_corpus.py`、`tests/integration/mysql/test_legal_corpus_repositories.py`（扩展）

- [ ] **Step 1–2: 失败测试**：同一文件重复导入识别；哈希不一致拒绝；Manifest 记录清单/parser 版本。
- [ ] **Step 3: 实现盘点**：最小样本（只读 `F:\ai律师数据库` 中的少量 `.docx`），计算 SHA-256，跳 ZIP，禁止改写原件。
- [ ] **Step 4: 验证并提交**。Commit: `feat: inventory and fingerprint legal corpus batch`

### Task B2: Loader 接口与 DOCX 解析（编章节条款项目）

**Files:** `infrastructure/documents/loader.py`、`docx_loader.py`、`parsers.py`、`tests/unit/test_docx_parser.py`

- [ ] **Step 1–2: 失败测试**：代表性样本验证真实 Parser 抽取“编、章、节、条、款、项、目”与标题/机关/文号/日期；异常格式降级为“标题+段落”并标记质量。
- [ ] **Step 3: 实现 Loader/Parser**：优先法律结构切分；表格/附件独立分块但保留关系；`.doc` 转换与 ZIP 跳过为延后。
- [ ] **Step 4: 验证并提交**。Commit: `feat: parse structured legal provisions from docx`

### Task B3: 质量门禁与数据集发布

**Files:** `application/legal_corpus.py`、`tests/integration/mysql/test_legal_corpus_repositories.py`（扩展）

- [ ] **Step 1–2: 失败测试**：覆盖率/条号连续性/重复/必需字段不达标拒绝发布 `dataset_v1`。
- [ ] **Step 3: 实现发布**：成功/降级/失败清单 + 质量指标原子落库，`dataset_v1` 快照记录引用版本。
- [ ] **Step 4: 验证并提交**。Commit: `feat: gate and publish dataset_v1 snapshot`

## 里程碑 C：Evidence Bundle 与 Citation Gate 骨架（无模型）

### Task C1: Evidence Bundle 值与访问校验

**Files:** `application/evidence.py`、`tests/unit/test_evidence.py`、`tests/integration/mysql/test_evidence_gate.py`

- [ ] **Step 1–2: 失败测试**：每条证据必含 `evidence_id/名称/版本/日期/状态/条号/原文/来源哈希/数据集版本/授权`；缺失或状态未确认即校验失败。
- [ ] **Step 3: 实现 Evidence 组装与后端校验**：`evidence_id` 唯一；目标日期与版本不冲突；授权来自读取端口而非调用方声称。
- [ ] **Step 4: 验证并提交**。Commit: `feat: assemble and verify evidence bundles`

### Task C2: Citation Gate

**Files:** `application/evidence.py`、`tests/…`

- [ ] **Step 1–2: 失败测试**：`claims[]` 引用不在本次 Bundle 的 `evidence_id` → 拒绝；原文无法完整展示 → 降级/人工复核。
- [ ] **Step 3: 实现 Citation Gate**：只允许引用允许的 `evidence_id`；无证据/冲突/超范围一律拒答或缩小结论（本切片以合成结论验证，不接模型）。
- [ ] **Step 4: 验证并提交**。Commit: `feat: gate citations against evidence bundles`

## 里程碑 D：切片总门禁

- [ ] **Step 1: 运行全量门禁**：`uv run pytest -q`（真实 MySQL）、`uv run ruff check .`、`uv run mypy src`、Migration 往返、secret 扫描、`git diff --check`。
- [ ] **Step 2: 更新 AGENTS.md 阶段说明**（简短记录本切片完成与延后项）。
- [ ] **Step 3: 提交**。Commit: `docs: complete phase1 non-embedding corpus slice`

## Plan Self-Review

- Spec coverage：覆盖规格 6.1–6.5 的语料/版本/Chunk/证据骨架与 12 节阶段 1 的非 Embedding 部分；Dense/OpenSearch/SSE/DeepSeek 明确延后并在文件内标注。
- Placeholder scan：每个任务含失败测试、实现、验证与 Commit。
- Security review：公共数据经显式端口；Evidence 授权来自服务端；原件只读且哈希记录。
- Release review：状态未确认不得宣称现行；不产生面向客户法律结论。

## Exit Criteria

本切片在里程碑 D 总门禁通过后才算完成。最终证据必须包含：真实 MySQL、Migration 往返、最小真实 `.docx` 解析样本、质量门禁、Evidence/Citation Gate 合成校验、Ruff/mypy 零错误、无 Secret、工作树干净。三节点 RabbitMQ、Kubernetes、Vault/KMS、OpenSearch/Embedding、Reranker、DeepSeek、SSE、MCP、LangGraph 与生产容量仍为明确未验证项。
