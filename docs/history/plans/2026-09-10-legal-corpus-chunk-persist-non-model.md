# 2026-09-10 语料 LegalChunk 生成与持久化（非模型）

## 背景

spec 6.3「检索单元 Chunk 以完整条文为 Parent」、6.4 混合检索消费 chunk。仓库里
`LegalChunk` 域模型与 `legal_chunks` 表早已存在（迁移 20260904_07），但**没有**
任何 chunk 生成或持久化代码——docx parser 只产出 Provision 行，检索索引无从喂料。
本切片补上确定性的 chunk 派生与落库（非模型、非 tenant），是后续 OpenSearch/Embedding
索引的原料层。

## 范围（只做这些）

- 应用纯函数 `legal_corpus_chunks.derive_chunks(version_id, provisions, parser_version)`
  → 每个 Provision 生成一个 `LegalChunk`（chunk_type=PROVISION、quality=OK、
  content=provision.full_text、content_hash=sha256、无 parent——parser 输出是条文级，
  款项目子 chunk 等 parser 演进后再加）；空文本/哈希不符由域模型拒绝；按 char_start 排序。
- 仓储扩展（公开只读语料边界，复用 SqlAlchemyLegalCorpusRepository 会话风格）：
  - `replace_chunks_for_version(version_id, chunks)`：单事务先删该 version 旧 chunk 再插入
    （派生可重建、幂等）；
  - `chunks_for_version(version_id)`：按 provision char_start 稳定升序读回。
  - 显式端口边界，不新增裸 get_by_id。
- 单测 + 真实 MySQL 全栈：seed 两 version 各 1 条文 → derive+replace(v1) → 读回
  chunk 1 行、内容/哈希正确 → 再次 replace 幂等仍 1 行 → v2 无 v1 chunk。

## 明确不做

- OpenSearch/Embedding/SSE/问答链（后续切片）；款项目/表格附件子 chunk；
  表结构迁移；不加依赖。

## 验证

- 聚焦单测 + MySQL 全栈测试；全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff check src tests / mypy src 零错误；无 Secret；工作树干净。
