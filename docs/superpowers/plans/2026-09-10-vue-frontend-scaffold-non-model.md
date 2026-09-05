# Vue 3 前端骨架（frontend/ 工程）

- 日期：2026-09-10
- 类型：非模型切片（无外部模型调用）
- 状态：已完成

## 动机

后端已有完整 HTTP 面（身份注册/登录、语料只读、法规对话 chat、租户
Matter/Document/审计等），但仓库零前端代码，用户 2026-09-10 明确转向
「全栈最终实现」并要求交付 Vue 3 前端骨架（frontend/：路由/登录/语料浏览/
对话/上传下载）。本切片先交付**可运行的 frontend/ 工程骨架 + 身份闭环**：
Vite+Vue 3+TS 脚手架、版本化 API 客户端、Access Token 会话存储、路由与
登录守卫、登录/注册页与应用外壳布局；语料浏览/对话/上传下载页面随后续切片
逐个接入（本切片内语料列表页先做最小可用渲染以证明读链路形态）。

## 范围

- `frontend/` 独立 npm 工程（node v24 / npm 11 本机可用，registry 可达）。
- 依赖最小化：`vue`、`vue-router`；开发依赖 `vite`、`@vitejs/plugin-vue`、
  `typescript`、`vue-tsc`、`vitest`。
- 开发期以 Vite dev server（默认 5173，已列入后端可信源）代理 `/api` →
  `http://127.0.0.1:8000`（compose API 端口）；token 只存浏览器
  localStorage（Refresh Token 为 httpOnly Cookie，浏览器自动随请求携带，
  前端不读取）。
- 对接后端契约（全部经真实 MySQL+Redis 全栈验证过）：
  - `POST /api/v1/auth/register` `{username,password,display_name}` → token；
  - `POST /api/v1/auth/login` `{kind,identifier,password}` → token；
  - `GET /api/v1/legal/instruments?limit=&before_id=&title=…` → 分页目录；
  - 后续：版本/条文详情、`POST /api/v1/legal/chat`、上传下载、SSE。

## 设计

- `src/api/client.ts`：fetch 封装——绝对路径拼接、`Authorization: Bearer`
  注入、非 2xx 解析 Problem Details `{status,code,title,detail}` 抛稳定
  `ApiError`、请求超时；可注入 token 供应者与 fetch 以便离线测试。
- `src/api/endpoints.ts`：强类型端点函数（auth/corpus），白名单字段与后端
  `extra="forbid"` 模型对应（仅取后端回显字段，不透传任意对象）。
- `src/auth/session.ts`：Access Token 会话存储（localStorage 键
  `lawyer_agent.access_token`，可注入 Storage 以便 node 环境单测）、
  `isAuthenticated()`、`setToken/clear`；`login/register` 成功后落 token。
- `src/router.ts`：路由表 + `requiresAuth` meta；全局守卫（无 token 访问受
  保护页 → `/login?next=…`；已登录访问 `/login` → 首页）。
- 视图：`LoginView`/`RegisterView`（错误显示 Problem Details code/title）、
  `AppShell`（顶栏+导航+登出）、`CorpusView`（最小语料目录列表，可搜索
  title、翻页 next_before_id）。
- 样式：无 UI 框架依赖，语义化 CSS（避免引入构建体积与无障碍负担），
  深浅中性色基础令牌。

## 验证

- `npm run typecheck`（vue-tsc）零错误；
- `npm run test`（vitest）绿：api client（Bearer 注入、Problem 映射、
  超时、错误 JSON）、session（持久化/登出/防 XSS 不在 token 存引用）、
  guard 决策纯函数；
- `npm run build`（vue-tsc && vite build）成功产出 `dist/`；
- 后端无改动，无需重跑全量 pytest（此切片纯前端）。

## 后续

- 语料详情（版本/条文全文）、法规对话 chat 页面与 SSE 流式问答、
  上传下载页面、Nginx 静态托管并入 compose（spec 默认含 Vue/Nginx）。
