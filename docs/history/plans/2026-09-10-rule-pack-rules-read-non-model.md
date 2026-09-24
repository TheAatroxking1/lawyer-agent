# 2026-09-10 Rule Pack 规则只读回读（非模型）

## 目标

Rule Pack 管理 HTTP API 已有 建包/列表/加规则/启停/激活，但客户端无法**读回一个 pack 的规则集**：
- `GET /rule-packs` 只返回 pack 摘要（id/name/version/active）。
- 仓储 `rules_for_pack` 仅供引擎读取且**只含 enabled 规则**。
结果：管理 UI 刷新后看不到 pack 内有哪些规则、各自 risk/enabled 状态。

本切片补上规则只读回读，与既有「管理写入必有管理查询」的对称读（matter 列表/详情、pack 列表）一致。

## 范围（只做这些）

- 仓储 `SqlAlchemyRulePackRepository.list_pack_rules(context, pack_id)`：
  租户范围，返回该 pack 的**全部**规则（含 disabled），按 `created_at, id` 稳定升序；
  pack 不在本租户 → 空（调用方 404）。引擎侧 `rules_for_pack`（enabled 过滤）不动。
- 应用 `RulePackAdminHttpService.list_pack_rules(context, pack_id)`：
  先 `require_uuid7(pack_id)`；pack 存在性走既有 `_find_pack_by_id` → 不存在/跨租户
  `RulePackAdminNotFound`（资源层 404）。
- HTTP：`GET /api/v1/tenants/{tenant_id}/rule-packs/{pack_id}/rules` →
  `list[RuleSummary]`（id/trigger_kind/label/risk_level/enabled，与加规则响应同一投影）；
  path tenant 强校验（错配 404 `tenant_resource_not_found`，不进服务）。
- 真实 MySQL+Redis 全栈测试：建 pack v1 → 加 2 规则（high/low）→ 停用其一 →
  读回含 disabled 全量且顺序稳定；空 pack 读回 `[]`；不存在 pack 404
  `rule_pack_admin_not_found`；租户 B path 层 404 / B 资源层 404 / B 读 A 的 pack 规则 404。

## 明确不做

- 不改 RuleSummary 投影（不加 pattern/suggestion 等字段——非本切片目标）；
  不做模板规则库、permission code 化、Pack 删除/回滚等延后项。

## 验证

- 聚焦 MySQL 全栈规则回读测试 + 既有 rule_pack admin/idempotency/state 测试回归；
  全量 pytest（Redis 全并行抖动按已知模式隔离重跑）；ruff/mypy 零错误。
