# 律师 Agent

日常维护从这里开始。当前重点是 **合同审查：LlamaIndex ReAct + MCP + 网页批注**；既有账号、多租户、案件、法规问答、语料处理和检索功能继续保留。

**当前交付边界：** 第一版 PDF 上传、解析/OCR、法律检索/全文读取、审查门禁、持久化和网页批注已接入；合成合同已通过真实 Qwen、Docker RAG、全文、MySQL 和隔离 MinIO 的后端联合验证。完整网页环境和客户 PDF 尚未联合验收；Word、法律效力核验及生产部署仍有缺口。见 [项目状态](docs/project-status.md)。

**面试演示入口：** 登录后打开 `/chat`，欢迎页的主按钮是“选择面试官的合同”。选择或拖入 PDF 后，会直接进入真实的 Qwen、多轮 MCP、第二版 RRF/rerank、法律证据和网页批注流程；结果页的“MCP 工具链”面板只展示工具名、能力和授权状态，不暴露 Schema 或凭据。没有模型额度时，旁边的“查看固定样例”才使用内置合成合同，不调用 Qwen 或真实法律检索。

## 我想改什么

| 维护任务 | 直接打开 |
| --- | --- |
| 看当前进展、已知问题、下一步 | [项目状态](docs/project-status.md) |
| 不清楚 src 每个文件做什么 | [后端源码导航：阅读顺序与逐文件职责](backend/README.md) |
| 改 Agent 执行流程、MCP 调用 | [contract_review 工作流](backend/src/lawyer_agent/infrastructure/contract_review/workflow.py)；[MCP 服务](backend/src/lawyer_agent/infrastructure/contract_review/mcp_servers.py) |
| 改风险字段、原文定位或结果校验 | [数据模型](backend/src/lawyer_agent/application/contract_review/contracts.py)；[校验门禁](backend/src/lawyer_agent/application/contract_review/gate.py) |
| 配置并运行合同审查 | [第一版运行说明](docs/contract-review-core.md)；[实际装配入口](backend/src/lawyer_agent/infrastructure/contract_review/runtime.py) |
| 填写 API Key | 本页下方“配置放哪里” |
| 改 API / 后端业务 | [backend/src/lawyer_agent](backend/src/lawyer_agent/)；[后端说明](backend/README.md) |
| 改网页 | [frontend/src](frontend/src/)；[前端说明](frontend/README.md) |
| 维护现有 Milvus / 内网 RAG | [查询说明](docs/milvus-query.md)；[HTTP 接口说明](docs/rag-http-handoff.md) |
| 查询第二版滑窗：LlamaIndex RRF＋模型重排 | [运行与配置](docs/milvus-query.md#第二版滑窗llamaindex-rrf模型精排)；[单一CLI入口](tools/milvus_query/legal_query/window_cli.py) |
| 找语料、向量和导出文件 | [存储位置](docs/corpus-storage-locations.md) |
| 查历史决策或旧计划 | [历史资料入口](docs/history/README.md) |

## 配置放哪里

| 文件 | 用途 / 状态 |
| --- | --- |
| `deploy/.env` | 本地 Docker Compose 参数；模板为 [compose.env.example](deploy/compose.env.example) |
| `deploy/deepseek.config.json` | 现有 DeepSeek Key，填写 `{"api_key":"你的Key"}`；模板为 [deepseek.config.example.json](deploy/deepseek.config.example.json) |
| `deploy/secrets/dashscope.json` | 阿里云 Key、模型、地址；加载器与 Provider 已实现，[填写步骤](docs/superpowers/specs/2026-09-16-agent-api-key-design.md) |
| `deploy/secrets/contract-review.json` | 合同服务的模型配置路径、RAG、全文目录、S3；从 [模板](deploy/contract-review.config.example.json) 创建并按实际环境填写 |
| [backend/src/lawyer_agent/config.py](backend/src/lawyer_agent/config.py) | 后端配置字段及校验；宿主机与容器配置分别生效 |

真实配置文件已被 Git 忽略。DeepSeek 文件通过 `LAWYER_DEEPSEEK_API_KEY_FILE` 绝对路径加载；Compose 使用 `LAWYER_DEEPSEEK_CONFIG_PATH` 指向宿主机配置文件。不要把阿里云 Key 填入 DeepSeek 文件。

## 运行入口

**只看合同 Agent 合成演示：** 在仓库根目录打开 PowerShell，以下命令使用独立环境，不需要 API Key。

```powershell
Push-Location backend
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Split-Path (Get-Location)) '.superpowers/venvs/contract-review'
uv sync --frozen --extra contract-review
uv run --frozen --extra contract-review python -X utf8 -m lawyer_agent.cli.contract_review_smoke
Pop-Location
```

**启动原有开发栈：** 核对 `deploy/.env` 后在根目录运行 `./scripts/dev.ps1`。该脚本会准备开发配置和秘密文件，并构建/启动 Docker 服务；演示核心 Workflow 不需要启动整套服务。

**单独开发网页：** 后端就绪后在 `frontend` 执行 `npm ci`、`npm run dev`。网页使用 Vue 3；ReAct 指 Agent 的推理与工具调用模式。

合同页面为 `/contract-review`。需登录、选择租户，并配置 `LAWYER_CONTRACT_REVIEW_CONFIG_FILE`；缺少服务配置时明确返回不可用。具体迁移、环境变量和端口见[运行说明](docs/contract-review-core.md)。

## 目录分工

```text
backend/        后端业务、Agent、数据库迁移与测试
frontend/       网页
deploy/         运行配置、密钥模板与容器定义
docs/           当前状态、操作说明与设计规格
  history/      旧计划和历次执行记录，日常无需逐份阅读
scripts/        开发启动、迁移、语料操作命令
tools/          独立 Milvus 查询及内网 RAG 服务
LlamaIndex/     现有学习笔记，不是生产入口
samples/        合成示例
artifacts/      生成数据与证据，部分路径为指向 F 盘的 Junction
```

`.superpowers/`、`.sdd/`、`.worktrees/` 和缓存目录属于开发辅助材料；日常配置从本页进入。不要直接清理 `artifacts`，尤其不要沿 Junction 删除 F 盘数据。

## 修改后的验证

后端在 `backend` 执行 `uv run python -X utf8 -m pytest tests/unit tests/api tests/contract -q`、`uv run ruff check .`、`uv run mypy src`。合同 SDK 测试须安装 `contract-review` Extra；具体命令见[框架说明](docs/contract-review-core.md)。

前端在 `frontend` 执行 `npm run typecheck`、`npm test`、`npm run build`。服务集成按改动另外验证，不把单元测试视为生产验收。

协作长期规则见 [AGENTS.md](AGENTS.md)。新增进度只更新项目状态的对应条目；历史记录放历史目录，避免重新把当前入口写成流水账。
