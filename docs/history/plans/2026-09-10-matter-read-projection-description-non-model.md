# 2026-09-10 Matter 只读投影补 description（非模型）

## 问题

上一「Matter 元数据编辑 HTTP API」切片（2026-09-09）允许 PATCH 写入
`description`（≤4000、可置空），DB 往返测试确认落库；但所有响应模型
（create/get/list/status/owner 返回的 `MatterSummary`）从未包含
`description`。结果是客户端能写、不能读——元数据只读投影缺口。

## 目标（只做这些）

- `MatterSummary` 增加可空 `description: str | None = None`（加法变更，
  现有各端点响应自动带出；与 `owner_membership_id` 加法投影同口径）。
- `_matter_summary` 从 `Matter.description` 填充。
- 真实 MySQL 全栈测试：创建带 description → GET/列表/owner 指派响应均读回；
  PATCH 改 title+description 后读回更新值；description-only 保留 title；
  显式空 description 后读回 null；租户 B path 层 404、B 列表永不含 A 的
  description；既有字段（id/title/kind/status/version/owner）不回归。

## 明确不做

- 不新增字段（无 Migration）；不改写路径语义；不做 owner/团队角色 ABAC、
  permission code 化、搜索补全等延后项。

## 验证

- 聚焦 MySQL 全栈读回测试 + 既有 matter create/get/list/update/owner/status
  全量回归；全量 pytest（Redis 全并行抖动按已知模式隔离重跑）；ruff/mypy 零错误。
