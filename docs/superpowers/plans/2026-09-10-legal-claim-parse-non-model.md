# 2026-09-10 模型 claims 输出解析器（受控单次修复，非模型）

## 目标

Claim 门禁已能裁决结构化 claims；缺**把模型返回文本解析成受校验
`LegalClaim`** 的解析器。模型文本是不可信内容；解析必须：按约定 JSON 形状
`{"claims":[{"text":…,"evidence_ids":[uuid…]},…]}` 严格读取，额外键拒绝，
UUIDv7/必填校验交给 `LegalClaim`；**结构化输出失败最多进行一次受控修复**
（剥离代码围栏/截取首个 JSON 对象），仍失败则安全终止（稳定错误），绝不
静默丢字段或多次盲猜。解析器与供应商无关，供后续 QA 链消费。

## 范围（只做这些）

- `application/legal_claim_parse.py`：
  - `LegalClaimsParseError`（稳定 message；含 raw 是否回显的约束：不泄漏整段
    原文，只带定位/原因）；
  - `parse_claims_text(text, *, max_claims=20, max_text_chars=2000,
    max_evidence_per_claim=20) -> tuple[LegalClaim, ...]`：
    1. 输入非空字符串（空白/超长整体拒绝）；
    2. 顶层必须 JSON 对象且含 `claims` 数组；每个 claim 仅允许
       `text`（str 非空、≤max_text_chars）与 `evidence_ids`
       （≥1 且 ≤max_evidence_per_claim，全部 UUIDv7 字符串、无重复）；
       claim 内未知键 → 解析失败；
    3. 首次解析失败 → 一次受控修复（剥离 markdown 围栏；仍无 → 截取
       `{…}` 平衡段再试一次）；修复也失败 → `LegalClaimsParseError`
       （安全终止，不再重试）；
    4. claims 总数 ≤ max_claims；随后构造 `LegalClaim`（其自有校验兜底）。
  - 纯函数、无副作用；不落库/不接模型/不接 HTTP；不加依赖。
- 不改 `LegalClaim`/门禁契约；`LegalClaim` 复用自 `legal_claim_gate.py`。

## 明确不做

- 不做多轮/自适应修复、不做 schema 训练回调、不做供应商重试策略；
  不做证据门禁（上层把解析结果交给 ClaimGate）；不动 Schema；不接网络。

## 验证

- 聚焦单测：规范 JSON 解析（含 markdown 围栏）；围栏/前缀损坏一次修复成功；
  纯坏 JSON → 稳定错误；缺 `claims`/claims 非数组/claim 非对象/缺 text/空
  text/未知键 → 失败；evidence_ids 缺失/空/非 uuid/重复/超上限 → 失败；
  超 claims 上限 / 超 text 上限 → 失败；返回的 LegalClaim 可被门禁消费
  （与 claim gate 冒烟组合）；错误信息不包含整段原文。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
