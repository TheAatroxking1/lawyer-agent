# 2026-09-10 Matter owner 解锁指派（非模型）

## 目标

上一 owner 指派切片（2026-09-10）只有「指派/换人」，没有「解除」：
DB `owner_membership_id` 与域模型均可空，但一旦指派后无法清空负责人
（律师离职、案件暂停等场景）。AGENTS owner bullet 明确把「解锁指派」列为本切片后的延后项；
本切片补上与 `assign_owner` 完全对称的解除路径。

## 范围（只做这些）

- 仓储 `SqlAlchemyMatterRepository.clear_owner(context, matter_id, *, expected_version)`
  与 `assign_owner` 同构：锁读 Matter（本租户，None→None→服务 404）；
  CAS UPDATE 置 `owner_membership_id = NULL`、`version+1`（每次成功都版本递增并审计，
  与元数据 PATCH/assign 的「每次写即版本+1」语义一致）。
- 服务 `MatterDocumentHttpService.clear_matter_owner(context, matter_id, expected_version,
  trace_id)`：先读 Matter（404）；CAS 失败 → `MatterDocumentConflict`（409，过期 If-Match）；
  成功登记 `matter.owner` 审计 reason=`unassigned`。
- HTTP：`DELETE /api/v1/tenants/{tenant_id}/matters/{matter_id}/owner`（可选 If-Match 强 ETag）
  返回 `{matter}` + 新 ETag；path tenant 强校验（错配 404 `tenant_resource_not_found`）。
- 真实 MySQL+Redis 全栈：指派→GET 有 owner→DELETE（If-Match）→200、owner null、version+1、
  ETag 更新→过期 If-Match DELETE 409→无 owner 重复 DELETE 仍 200（对称于 assign 语义）→
  不存在 matter 404→租户 B path 层 404/资源层 404→审计两行（assigned+unassigned）且 B 不可见。

## 明确不做

- owner 详情展示/团队角色 ABAC/permission code 化；不引入 Idempotency-Key
  （与 status/metadata/assign 的无 key 路径一致）。

## 验证

- 聚焦 MySQL 全栈解锁测试 + 既有 owner/status/update/matter/audit 回归；
  全量 pytest（Redis 全并行抖动按已知模式隔离重跑）；ruff/mypy 零错误。
