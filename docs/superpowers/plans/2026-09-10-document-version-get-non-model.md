# 2026-09-10 Document 版本只读 GET（非模型）

## 目标

Document/Review/上传工作流是「写回读缺」：登记（POST 幂等）与复核（POST 幂等）都有响应投影，
但**没有任何 GET** 能读回一个文档版本当前的 upload/review 状态；
`GET /matters/{id}/documents` 只返回 header（display_name/current_version_no）。
UI 刷新/协作场景无法确认某版本是 accepted/pending/approved/changes_requested，
只能靠再次登记（幂等）或再次复核（写操作）间接获得。本切片补一个只读 GET。

## 范围（只做这些）

- 应用：`DocumentReviewHttpService.get_version(context, document_id, version_no)`，
  复用 UoW `documents.find_version(tenant_id, document_id, version_no)`；
  不存在/跨租户 → `DocumentReviewNotFound`（与 POST review 同一稳定码
  `document_review_not_found`，同一目标资源口径）。
- HTTP：`GET /api/v1/tenants/{tenant_id}/documents/{document_id}/versions/{version_no}`
  返回只读投影（id/document_id/version_no/kind/file_name/upload_status/review_status/
  review_reason，白名单字段不含 object_key/sha256/mime/size）；path tenant 强校验
  （错配 404 `tenant_resource_not_found`，不进服务）。
- 真实 MySQL+Redis 全栈：登记 docx → GET（accepted/draft）→ submit → GET（pending_review）
  → request_changes(reason) → GET（changes_requested+reason）→ approve → GET（approved，reason 清空）
  → 不存在版本 404 → 租户 B path 层 404 / 资源层 404。
- 契约：GET 响应模型 `extra="forbid"` 只暴露白名单字段（单测覆盖额外字段拒绝）。

## 明确不做

- 不改 Review 状态机/幂等语义；不做 download/派生版本落库/MinIO 预签名、
  结论自动发布门槛、permission code 化等延后项。

## 验证

- 聚焦 MySQL 全栈 GET 读回测试 + 既有 document review/register/rule check/matter 测试回归；
  全量 pytest（Redis 全并行抖动按已知模式隔离重跑）；ruff/mypy 零错误。
