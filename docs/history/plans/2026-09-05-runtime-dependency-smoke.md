# 运行时依赖可复现修复

沿用总体架构的生产镜像与供应商适配器要求；本轮仅修复已确认的依赖分组错误，不改变业务协议。

- [x] 在独立 .superpowers/venvs/runtime-smoke 环境运行 uv sync --frozen --no-dev，再导入 DeepSeek/OpenSearch/消息模块，观察 ModuleNotFoundError: httpx。
- [x] 将既有 httpx 版本约束从 dev 移到 project.dependencies；uv lock --offline 仅移动分组，未升级包。
- [x] 隔离环境冻结生产安装后，DeepSeek/OpenSearch/消息拓扑及 FastAPI app 均可导入。
- [x] Provider/OpenSearch/消息聚焦回归 32 passed，Ruff 通过。

现有开发 .venv 不同步、不删除额外模型包；Embedding 可选安装组与容器配置接线作为后续独立切片。
