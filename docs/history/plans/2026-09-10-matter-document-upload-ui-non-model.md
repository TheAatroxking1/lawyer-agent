# 租户内案件列表 / 新建 + 文档上传登记（案件文档工作台 II）

- 日期：2026-09-10
- 类型：非模型切片（纯前端 + 租户 API 客户端；后端既有 matter/documents 契约）
- 状态：已完成

## 动机

工作台基础切片打通「进入租户」，本切片补齐进入后的**案件与上传登记**闭环：
租户 token 客户端列出/新建案件、按案件登记 DOCX（白名单校验后端完成）、
查看文档版本。下载侧仍受限于需 Rule Check 产物（报告下载）与原始文件后端
（MinIO 预签名）后置，页面上如实说明。

## 范围

- `api/types.ts`：MatterSummary/MatterPage/CreateMatterInput（kind 五枚举）/
  RegisterDocumentInput/DocumentSummary/DocumentHeaderSummary（镜像后端严格
  模型）。
- `api/tenant.ts`：`tenantApiClient`（base `/api/v1`、tokenProvider 取
  `session.readTenantToken()`，与账号 token 隔离）+ `listMatters`（GET
  /tenants/{id}/matters?limit=50）、`createMatter`（POST）、`listDocuments`
  （GET …/documents）、`registerDocument`（POST，payload_b64）。
- `DocumentsView` 扩展：进入租户后显示案件列表（标题/类型/状态/文档数
  「▼」展开）；「新建案件」（名称 + 类型五选一，创建后刷新）；案件内：
  DOCX 选择（.docx/对应 MIME，≤25MB 前置拦截）→ FileReader(arrayBuffer)→
  base64 → `registerDocument`（后端白名单/MIME 校验 → 201 DocumentSummary）→
  展示最近登记版本（file_name/version_no/upload_status/review_status）与
  文档版本列表；分块错误横幅与上传中状态。

## 验证

- `npm run typecheck` 0 错误；vitest **40/40 绿**（既有 + 会话/端点）保持；
  `npm run build` 成功（DocumentsView gzip 3.8 kB）。
- 真实端到端需 active 租户成员 + tenant token（后端切换/登记流程已全栈
  覆盖）。

## 后续

规则包激活后的 Rule Check 运行与 DOCX 报告下载按钮、案件详情/参与方、
原始文件下载与 MinIO 预签名（后端切片）。
