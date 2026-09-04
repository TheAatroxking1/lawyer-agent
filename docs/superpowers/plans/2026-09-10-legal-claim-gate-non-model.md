# 2026-09-10 结构化 Claim 发布前门禁服务（非模型）

## 目标

spec 6.5：模型输出使用结构化 `claims[]`，每项法律结论必须绑定一个或多个
本次 Evidence Bundle 中的 `evidence_id`；后端验证「ID 存在且属于本次任务 /
有权访问 / 与目标日期不冲突 / 可完整展示 / 无证据、冲突、状态未知或引用不
支持结论时降级、人工复核或拒答」。现有 `CitationGate` 只逐条校验
`(claim_index, evidence_id)`，缺一个**对一组结构化 Claim 做发布前裁决**的
服务边界，供后续 QA 链直接消费（claims 解析在更上层/供应商侧，本切片只
裁决已解析的结构化 claims）。

## 范围（只做这些）

- `domain/legal_claim.py`（纯值对象，非供应商）：
  - `LegalClaim(text, evidence_ids)`：text 非空；evidence_ids 为 uuid7 且至少
    一个（「必须绑定一个或多个」）；去重校验。
- `application/legal_claim_gate.py`：
  - `LegalClaimGate(bundle, claims, *, target_date) -> LegalClaimGateResult`：
    1. 逐 claim 逐引用复用 `CitationGate` 裁决（in-bundle / authorized /
       生效日期 / 废止 / 状态未知 规则不变）；
    2. **claim 级**：其全部引用 allowed 才 allowed；任一拒引用首个失败 reason
       （稳定 code）；
    3. **整体**：claims 非空且全部 claim allowed 才 `allowed=True`；否则
       `refused=True` 并给出稳定 reason（`no_claims` /
       `claim_not_supported:<code>` 等）；verdicts 只携带 claim_index/
       reason/evidence_id，不泄漏 claim 正文（模型输出不可信内容）。
  - 输入校验：非法 claim（空 text / 空或非法 evidence_ids）抛
    `LegalClaimGateError`；bundle/date 强类型。
- 纯后端裁决：不改 EvidenceItem/Bundle/CitationGate；不加依赖；不接模型。

## 明确不做

- 不做 claims 解析（LLM JSON → LegalClaim）与修复（供应商/QA 层后续）；
  不做检索/证据组装/QA 链；不做租户私有知识；不动 Schema。

## 验证

- 聚焦单测（纯离线构造 EvidenceBundle）：单 claim 多引用全 allowed →
  allowed；引用不在 bundle → refused `evidence_not_in_bundle`；未授权 /
  生效前 / 已废止 / 状态未知 → 对应拒答；一 claim 多引用中任一失败 → claim
  级 refused；多 claim 全过 → allowed；空 claims → refused `no_claims`；
  空 text / 空 evidence_ids / 非 uuid7 → `LegalClaimGateError`；verdicts 不含
  claim 正文。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
