# 落地对话页 + 延时登录弹窗（微信/手机号/账号密码三入口）

- 日期：2026-09-10
- 类型：非模型切片（纯前端 UX；真实微信 OAuth/短信网关后置，用户已确认）
- 状态：已完成

## 动机

用户两点要求：(1) 登录需支持微信、手机号（后端登录 kind 本就含
phone/email/wechat_unionid/wechat_openid，但注册只建 username、无绑定/证明
流程、无 OAuth/短信设施——用户确认本轮“UI 全做 + 后端骨架、真实短信/微信
后置”）；(2) 打开站点直接进对话页，真正与 AI 对话时才弹登录框。

## 范围

- 路由：`/` 重定向到 `/chat`（落地即对话）；`home` 移至 `/home`（首页保留在
  导航里）；chat/home/not-found 为**公开页**，语料与详情仍受保护（匿名 → 登录
  ?next 原路径）；已登录访问 login/register → 跳 `/chat`。
- 守卫重构：公开集合与登录表单集合分离（`AUTH_FREE_ROUTE_NAMES` vs
  `AUTH_FORMS_ROUTE_NAMES`），纯函数语义保持可单测；默认 next=/chat。
- 响应式认证 `src/auth/state.ts`（`authState` + `refreshAuth`）：登录/登出后
  顶栏、对话页立即响应（不依赖刷新）。
- `AuthPanel.vue`：三页签 微信 / 手机号 / 账号密码。前两者为**未开通占位**
  （说明接入微信开放平台 OAuth、短信网关后可启用；按钮禁用，不假装可用）；
  账号密码真实调用既有 login 端点，成功 → 存 token + `refreshAuth` + emit
  success。
- `AuthModal.vue`：Teleport 遮罩对话框（Esc/遮罩关闭、aria-modal），复用
  AuthPanel，供对话页发送时弹出；`go-register` 事件导航 `/register`。
- `ChatView`：游客可浏览/输入，文案提示“点击发送即登录”；发送或点示例时
  未认证 → 保留输入打开 AuthModal，登录成功（`success`）→ 自动补发刚才那句
  （pendingSend 标志，不丢内容不重复发）；真实 401 清 token 后同样弹窗重登；
  发送按钮游客态文案「登录并发送」。
- `LoginView` 改为三页签 AuthPanel；`RegisterView` 成功跳 `/chat`。
- `AppShell`：游客顶栏显示 注册/登录（登录带 next 回原页），已登录显示退出。

## 验证

- `npm run typecheck` 0 错误；vitest **30/30 绿**（guard 规格更新为公开/受保护
  新集合 + 已登录不弹回对话页）；`npm run build` 成功分包。
- 后端零改动。

## 后续（真实第三方登录，需外部凭据 + 后端流程）

微信开放平台 OAuth（AppID/AppSecret/回调）扫码登录与回调、手机号绑定/验证码
登录（短信网关或开发模拟器）、邮箱绑定；绑定后 username/phone/email/wechat
四种 kind 的真实识别登录；届时移除 AuthPanel 占位文案。
