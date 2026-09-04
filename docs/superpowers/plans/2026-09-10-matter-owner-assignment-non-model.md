# 2026-09-10 Matter Owner Assignment（非模型）

## 目标

补齐 spec 8.4「Assignment：律所租户分派自己的律师或法务人员」在 Matter 上的写路径：
租户内授权成员可把某 Matter 指派给**同租户 active 的本所成员**（owner/internal）作为负责人。
DB `tenant_matters.owner_membership_id` 列与领域 `Matter.owner_membership_id` 早已存在
（迁移 20260905_08），但没有任何端点/仓储方法写入它——本切片补上整条链路并做跨租户反向验证。

## 范围（只做这些）

- 领域纯规则：`matter_owner_membership_assignable(member_type, status)` 只放行
  `member_type ∈ {owner, internal}` 且 `status == active`（“自己的律师或法务人员”）。
- 仓储 `SqlAlchemyMatterRepository.assign_owner(...)`：租户范围校验候选成员存在且可指派
  （同 `tenant_id`，不存在/非 active/非 owner|internal 一律 422 不区分，防跨租户探测）；
  锁读 Matter + `version` CAS UPDATE 写 `owner_membership_id` 并版本递增。
- 服务 `MatterDocumentHttpService.assign_matter_owner(...)`：先读 Matter（404），
  候选指派失败 → `MatterDocumentInvalidRequest`（422），CAS 失败 → `MatterDocumentConflict`（409），
  成功登记 `matter.owner` 审计（reason=`assigned`）。
- HTTP：`PUT /api/v1/tenants/{tenant_id}/matters/{matter_id}/owner`
  body `{owner_membership_id}`、可选 `If-Match` 强 ETag、返回 `{matter}` + ETag。
- `MatterSummary` 增加可空 `owner_membership_id`（只读投影，加法变更），
  `_matter_summary` 一并带出，使 GET/list 也能读到负责人。
- 真实 MySQL 全栈 HTTP 测试 + 跨租户双层反向（path 404 `tenant_resource_not_found`、
  resource 404 `matter_document_not_found`、A 引用 B 成员 422 统一错误）+ 审计行断言。

## 明确不做（保持现状的延后项）

- Idempotency-Key（与 `matter.status`/元数据 PATCH 一致，无 key 路径）；如需后续单独接。
- 拒绝路径审计（与 matter.status/matter.update 成功审计口径一致）。
- 解锁指派 / owner 展示详情 / 团队与角色 ABAC / permission code 化 / 冲突 Register 强制门槛。

## 验证

- 聚焦：新 MySQL 全栈 owner 指派测试、unit 纯规则与 body 校验测试。
- 回归：既有 matter/audit 集成测试；全量 pytest（既有 Redis 全并行抖动按已知模式隔离重跑）。
- ruff check src tests / mypy src 零错误。
