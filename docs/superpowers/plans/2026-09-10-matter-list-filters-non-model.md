# 2026-09-10 Matter 列表搜索/过滤（非模型）

## 目标

给 `GET /api/v1/tenants/{tenant_id}/matters` 增加只读过滤参数，
让「案件/咨询事项管理」可按键查找、按状态收敛列表。上一份 keyset 分页切片
（2026-09-08 计划）明确把「搜索过滤」留作延后项；本切片补上，保持纯读路径、
租户隔离与既有分页语义不变。

## 范围（只做这些）

- HTTP：`GET /matters?limit=&before_id=&title=&status=&kind=` 可选查询参数
  - `title`：非空白关键字，trim 后≤512 字符；子串匹配（大小写不敏感，MySQL 默认排序规则）。
  - `status`：`open|active|closed|archived` 之一。
  - `kind`：`contract_review|litigation|legal_advice|compliance|other` 之一。
  - 非法/空白/超长 → 422（沿用 `matter_document_invalid_request` 语义/校验口径）。
- 服务 `list_matters`：校验 + 透传过滤条件；keyset 游标、limit 1–100 规则不变。
- 仓储 `list_matters`：租户范围 WHERE 之上追加 title/status/kind 过滤，
  排序与 `(created_at, id)` 键集语义不变，未知/越权 `before_id` 仍 404。
- 真实 MySQL 全栈 HTTP 测试：种子多样 title/kind/status 后按 title 子串、
  status、kind、组合过滤各返回正确子集；过滤与分页共存；非法参数 422；
  租户 B path 层 404、B 自己的过滤列表永不含 A 的 id。

## 明确不做

- owner 过滤、团队/角色 ABAC、permission code 化、审计类搜索、Matter 全文检索（仍需模型授权）。

## 验证

- 聚焦：新 MySQL 全栈列表过滤测试；既有 list/status/matter HTTP 回归。
- 全量 pytest（Redis 全并行抖动按已知模式隔离重跑）；ruff/mypy 零错误。
