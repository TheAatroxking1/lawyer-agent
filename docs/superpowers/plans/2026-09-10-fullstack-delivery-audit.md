# 交付核验：全栈最终实现审计（2026-09-10 主线交付清单）

## 一、对照主目标逐项核验（均已完成并有测试证据）

| 主目标交付物 | 交付位置 | 证据 |
| --- | --- | --- |
| DeepSeek key 由用户 JSON 配置文件填写（模板已提交、真实文件 gitignore、绝不入库） | `deploy/deepseek.config.example.json`、`config.py`（deepseek_api_key/_file） | 16 项单测；密钥 repr/exclude 永不泄露 |
| DeepSeek chat provider 经 ModelGateway | `infrastructure/providers/deepseek.py` + `application/model_gateway.py` | provider 单测 10 项 + chat_stream 8 项 |
| 法规对话 HTTP（POST /legal/chat） | `api/v1/legal_chat.py` | 服务单测 + MySQL+Redis 全栈 |
| 真实逐 token 流式对话（chat_stream → /legal/chat/stream SSE → ChatView） | provider/gateway/端点/前端三步 | unit 1131→1150；SSE 全栈；vitest/build |
| Vue 3 前端骨架（路由/登录/语料浏览/详情/问答页/上传下载工作台/Agent 工具面板） | `frontend/` | typecheck 0 错、vitest 41/41、build 分包正常 |
| 检索问答（编排+HTTP+SSE 端点+前端消费） | `application/legal_retrieval_qa.py`、`api/v1/legal_retrieval_qa.py`、AskView | 编排单测 15、检索问答全栈 10、前端 SSE vitest |
| MCP 受控客户端网关（白名单化工具 + HTTP 面 + Agent 面板 + 语料只读工具 + 显式 allowlist） | `application/mcp_gateway.py`、`api/v1/agent_gateway.py`、dependencies | mcp_gateway 24 项、Agent 网关全栈 3 |
| 真实语料入库/发布编排 | 导入/映射/onboarding/diff/publish/gated/snapshot 系列 | 各切片 MySQL 集成 + 全栈（OS/MySQL 容器真实跑通） |
| Embedding/OpenSearch 检索（含真实本地模型端到端） | embedding provider + opensearch client + 融合 | 真实 Embedding E2E 54.8s green；混合检索全栈 |

## 二、最近一次全量门禁证据（本轮复跑）

- 后端：ruff `All checks passed`；mypy strict `164 files` 零错；全 unit **1150 passed + 1 skipped**。
- 前端：`vue-tsc -b` 0 错误；vitest **7 文件 41 passed**；`vite build` 分包正常（index gzip 38.1 kB）。
- 真实 MySQL+Redis 全栈（本会话近期复跑）：Agent 网关 3 passed；corpus+chat（含 /chat/stream SSE 事件序、中途 504 error 事件）6 passed；其余语料/身份/检索全栈均在此前轮次绿。
- Docker Compose：`docker compose config --quiet` 通过（api/mysql/redis/rabbitmq/opensearch/minio/web）。

## 三、用户侧运行说明（唯一必需手工步骤）

1. 复制 `deploy/deepseek.config.example.json` → `deploy/deepseek.config.json`，填入自己的
   DeepSeek API Key（该文件已被 .gitignore 忽略，绝不入库）。
2. 启动后端时设置 `LAWYER_DEEPSEEK_API_KEY_FILE=<deploy/deepseek.config.json 绝对路径>`
   （或用 `LAWYER_DEEPSEEK_API_KEY` 环境变量）。
3. 开发运行：容器栈（MySQL/Redis/OS/RabbitMQ/MinIO）就绪后启动后端（uv/uvicorn），
   前端 `npm run dev`（Vite 代理 `/api` → 127.0.0.1:8000）或经 compose `web` 服务
   （http://localhost:8080，镜像 build 需 Docker Hub 网络可达后执行一次）。
4. 语料问答的检索链路需要：本地 embedding 权重（BAAI/bge-small-zh-v1.5）已装 +
   OpenSearch 在线 + `dataset_v1` 已通过发布编排上线 + DeepSeek key（即用户步骤 1-2）。
   无 key 时所有对话/问答面给出稳定 503 引导，绝不伪造回复。

## 四、环境性 / 外部凭据延后项（非本次主线交付阻塞，勿重开为门禁）

- web（Vue+Nginx）Docker 镜像本地 build：Docker Hub 不可达（环境性），仓库内 npm 构建已绿，
  网络恢复后 `docker compose build web` 一次即可。
- MinIO 真实对象存储 / 原始文件上传下载：对象存储当前为受控占位（登记仅元数据，
  报告产物下载可用）；需对象层真实持久化后接线（既有延后项）。
- 工作台「运行检查」按钮：需激活规则包 + 条文输入（租户运营流程）。
- 微信 OAuth / 手机号短信 / 绑定端点：需外部凭据与后端流程（UI 为未开通占位，诚实不假装）。
- AI Job Effect/Retry/Maintenance、Synthetic Harness、Compose 多进程、故障注入、
  零跳过全量门禁：用户明确延后，不重开。

## 五、结论

主目标列出的全部主线交付物均已实现、验证并提交（git 历史自 2026-09-10 起逐切片可回溯）。
剩余项均为环境性阻塞（web 镜像）或用户已明确延后/需外部凭据的项。建议：在 web 镜像
build 或用户端到端体验（配置 key 后）完成前，目标保持激活以观察；若用户确认接受上述
延后项，即可将目标标记完成。
