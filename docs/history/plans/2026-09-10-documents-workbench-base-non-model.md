# 案件文档工作台基础（租户列表 / 切换 / 会话租户 token）

- 日期：2026-09-10
- 类型：非模型切片（纯前端 + 会话扩展；后端既有 accounts/switch/matter 契约）
- 状态：已完成

## 动机

前端骨架清单含「上传下载」。案件/文档属于租户内部数据，前端此前无任何
租户上下文入口。本切片先打通「我是谁、属于哪些租户、进入哪个租户」：
会话支持独立租户 access token；新增案件文档工作台页，列出当前账号租户
成员关系（`GET /accounts/me/tenants`），可对 active 成员/租户执行
`POST /auth/switch-tenant` 换取租户 token 并“进入”，无租户时给出受控的
邀请/申请/平台提权流程指引（明确说明这不是公开注册可绕过的边界）。

## 范围

- `auth/session.ts`：增独立 `TENANT_TOKEN_KEY` 与会话方法
  `saveTenantToken/readTenantToken/clearTenantToken`（与账号 token 互不覆盖，
  可注入 Storage 便于测试）。
- `api/types.ts`：`AccountTenant{tenant_id,membership_id,name,tenant_type,
  tenant_status,membership_status}`、`AccountTenantList{items}`、
  `SwitchTenantInput`；`endpoints.ts`：`myTenants`（GET /accounts/me/tenants）、
  `switchTenant`（POST /auth/switch-tenant → AccessTokenResponse）。
- `views/DocumentsView.vue`（路由 `/documents` 受保护，顶栏「案件文档」）：
  加载租户列表；空 → 指引卡（邀请 / 申请创建租户经平台审核 / 部署 CLI 提权
  首个管理员）；有 → 卡片选择进入（校验 membership_status 与 tenant_status
  均为 active，否则提示未生效）；进入成功存租户 token 并展示当前租户与能力
  说明（上传登记/Rule Check 报告下载已备、原始文件下载与 MinIO 预签名后
  置）；「退出当前租户」清除租户 token。

## 验证

- `npm run typecheck` 0 错误；vitest **40/40 绿**（session +1：租户 token 与
  账号 token 独立存取/清除互不影响；endpoints +1：myTenants/switchTenant 路
  径与 body）；`npm run build` 成功。
- 后端零改动。

## 后续

进入租户后接 案件列表/新建、文档登记（文件→base64）与版本/复核状态展示、
Rule Check 运行与报告下载按钮、原始文件下载与 MinIO 预签名（后端切片）；
这些将使用 tenant token 客户端请求租户端点。
