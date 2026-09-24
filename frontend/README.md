# 律师 Agent · Web 前端（Vue 3）

统一配置与维护入口见[项目首页](../README.md)。

仓库前端工程（`frontend/`）。Vue 3 + TypeScript + Vite + Vue Router，无 UI
框架依赖；开发期以 Vite dev server 代理 `/api` 到本地 FastAPI（127.0.0.1:8000）。

## 目录

- `src/api/`：类型化 API 客户端（Bearer 注入、Problem Details 错误映射、
  超时）与端点函数，镜像后端 `backend/src/lawyer_agent/api/v1/*` 契约；
- `src/auth/`：Access Token 会话存储与路由守卫决策（纯函数，可单测）；
- `src/router/`：路由表 + 登录守卫 + 页面标题；
- `src/layouts/`、`src/views/`、`src/components/`：应用外壳与页面。

## 本地运行

前置：后端及其 MySQL/Redis 容器已启动（见 `deploy/compose.yaml`，
API 监听 `127.0.0.1:8000`），且注册账号可用（`POST /api/v1/auth/register`）。

```bash
npm install
npm run dev        # http://localhost:5173（后端可信源已含该 Origin）
```

法规对话依赖 DeepSeek Key：复制 `deploy/deepseek.config.example.json` 为
`deploy/deepseek.config.json` 并填入自己的 Key，以
`LAWYER_DEEPSEEK_API_KEY_FILE=<绝对路径>` 启动后端；未配置时对话返回 503，
不会伪造回复。

## 校验

```bash
npm run typecheck   # vue-tsc 零错误
npm test            # vitest（api client / session / guard 纯逻辑）
npm run build       # vue-tsc && vite build → dist/
```
