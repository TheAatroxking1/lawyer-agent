# 2026-09-10 检索命中 → 条文证据组装（Parent 恢复 → Evidence Bundle）

## 目标

spec 6.5 检索链路为：BM25/Dense → RRF → 可选 Rerank → **Parent 恢复（恢复完整条文）**
→ Evidence Bundle → Citation Gate。当前 OpenSearch 客户端、真实 Embedding、
RRF、混合检索服务、EvidenceItem/EvidenceBundle/CitationGate 均已就绪，缺
**把检索命中（`LegalSearchHit`）恢复成权威条文证据并组装 `EvidenceBundle`** 的
编排服务。只读组装，不执行模型调用。

## 范围（只做这些）

- `application/legal_evidence_assembly.py`：
  - `LegalEvidenceAssemblyService`（注入只读 `LegalEvidenceQueryPort` + 可选
    `EvidenceAccessPort`）：
    - `assemble(*, hits)`：输入有序 `LegalSearchHit`（来自混合检索/截断结果）；
      按 `version_id` 分组读权威 `LegalVersion`+`LegalInstrument`，读该 version
      全部 `Provision` 后按命中 `provision_id` 过滤 → 按命中顺序生成
      `EvidenceItem`（evidence_id=provision.id，instrument_title/版本元数据/
      provision_no/provision_text/source_ref/dataset_version 取自权威库，
      authorized 由 EvidenceAccessPort 判定，缺省公共语料全部授权）；
    - 同一 version 内多个 chunk 命中同一条文时按 provision 去重（保首个命中顺序）；
    - **禁止伪造**：命中引用不存在的 version/provision → 抛稳定
      `LegalEvidenceAssemblyError`（不静默丢弃）；版本缺少 source_ref 或
      dataset_version（来源不可追溯）→ 抛错拒组；
    - 空 hits → 返回 `None`（无证据，由上层 QA 链路拒答/降级）。
  - 服务只做恢复与组装：不查 CitationGate（门禁归上层）、不做租户知识、
    不改 EvidenceItem 域模型。
- `SqlAlchemyLegalCorpusRepository` 增只读方法
  `version_with_instrument(version_id)`（join instruments 返回
  `(LegalVersion, LegalInstrument) | None`），供组装按 id 取权威版本元数据；
  仍不暴露无限制 `get_by_id`（公共语料读取始终在版本/条文维度上显式定位）。

## 明确不做

- 不改 EvidenceItem/EvidenceBundle/CitationGate/语料 HTTP 契约；不接租户私有
  知识证据与授权 port 实现；不做 QA/SSE；不加依赖。

## 验证

- 聚焦单测（纯离线，fake query/access port）：按命中顺序映射字段、同 version
  同条文去重、空 hits → None、缺失 version/provision 抛错、缺 source_ref/
  dataset_version 抛错、authorized 由 port 决定。
- 真实 MySQL 集成：seed instrument+version（含 source_ref/dataset_version）+
  provisions → 组装 hits → EvidenceBundle 字段断言 → CitationGate 对 target_date
  放行/拒答闭环（含跨版本同一 provision_no 不串）。
- 全量 pytest（Redis 抖动按已知模式隔离重跑）；ruff/mypy 零错误；无 Secret；
  树干净。
