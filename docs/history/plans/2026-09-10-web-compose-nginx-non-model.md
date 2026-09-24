# Web 服务并入 Compose（Vue 静态托管 + Nginx 反代 API）

- 日期：2026-09-10
- 类型：非模型/部署切片（spec 默认栈含 Vue/Nginx）
- 状态：已完成

## 动机

spec 默认 Compose 栈含 Vue/Nginx。此前前端仅以 Vite dev server（5173）运行；
本切片让 `deploy/compose.yaml` 增加 `web` 服务：多阶段镜像构建 Vue SPA 并用
非特权 Nginx 托管，同源反代 `/api/`、`/health/ready` 到 api 服务（Cookie 与
CSRF Origin 干净），`localhost:8080` 进入 api 可信源默认清单。

## 范围

- `frontend/nginx/nginx.conf`：listen 8080；SPA history fallback；/assets/
  长缓存；`/api/` 反代 api:8000（HTTP/1.1、Host/X-Forwarded-*、proxy_buffering
  off 以透传 SSE、read_timeout 120s、client_max_body_size 25m）；`/health/ready`
  反代；gzip。
- `frontend/Dockerfile`：node:22-alpine 构建（npm ci → vue-tsc && vite
  build）→ `nginxinc/nginx-unprivileged:1.27-alpine`（非 root），拷贝 conf 与
  dist，EXPOSE 8080。
- `frontend/.dockerignore`（node_modules/dist/.git…）。
- `deploy/compose.yaml`：新增 `web` 服务（context ../frontend、ports
  127.0.0.1:8080:8080、depends_on api healthy、read_only + tmpfs /tmp 与
  /var/cache/nginx + no-new-privileges、healthcheck wget 首页）；api 环境
  `LAWYER_TRUSTED_ORIGINS` 默认加 `http://localhost:8080`。

## 验证

- `docker compose -f deploy/compose.yaml config --quiet` 通过；
- 镜像构建尝试：`docker compose -f deploy/compose.yaml build web` 在拉取
  node:22-alpine / nginx-unprivileged 基础镜像时因 **Docker Hub 当前不可达**
  （registry-1.docker.io 直连超时，环境网络限制）失败——非配置/代码问题；
  仓库内 `npm ci && npm run build` 已在本机通过，待网络可达后在 CI/本机复跑
  build 即可产出镜像；
- 注意：当前已运行的开发栈由另一 compose 文件（.worktrees 副本）启动，端口
  8000/9200 等在线不受影响；`docker compose up -d web` 需在目标 compose 文件
  下执行（同 project 名下勿与旧栈混启）。

## 后续

上传下载页面、AskView 真实逐 token 流、MCP HTTP 化等主线仍按顺序推进。
