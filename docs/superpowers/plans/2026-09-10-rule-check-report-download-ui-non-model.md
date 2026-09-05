# 文档风险项查看与 DOCX 报告下载（工作台内）

- 日期：2026-09-10
- 类型：非模型切片（纯前端；后端 rule_checks 只读/导出已全栈覆盖）
- 状态：已完成

## 动机

工作台已支持文档上传登记，缺风险结果查看与「下载」。本切片在每份登记文档
上提供「风险项」列表（GET /tenants/{id}/documents/{doc}/risk-issues）与
「报告下载」（GET …/report.docx → fetch blob 保存，租户 token 认证），与
后端既有 Rule Check 读面闭环。

## 范围

- `api/types`：`RiskIssueSummary`（provision_no/matched_text/risk_level/
  status/evidence_level/disposition_reason?）。
- `api/tenant.ts`：`listRiskIssues(tenantId, documentId)` 与
  `downloadReport(tenantId, documentId)`（带租户 Bearer、非 2xx 抛错、
  blob+文件名）。
- `DocumentsView`：文档行内 风险项/报告下载 按钮 → issues 内联列表（级别·
  命中文本·条号·状态）或「暂无风险项」；报告下载以临时 objectURL 触发保存；
  操作错误横幅；下载中原位禁用。

## 验证

- `npm run typecheck` 0 错误；vitest 41/41 保持；`npm run build` 成功。
- 后端真实语义（运行检查→列表→报告读回）已由 Rule Check 全栈覆盖。

## 说明/后续

报告下载仅在 Rule Check 曾对该文档运行过才有产物；「运行检查」需激活规则包
与条文输入，仍属租户运营流程；原始文件二进制下载与 MinIO 预签名属既有延后
切片。
