# Compose AI 配置接线

目标：容器使用内部 OpenSearch 地址，DeepSeek JSON 经只读 secret 挂载，Embedding 模型/维度由环境显式传入。沿用用户自填 JSON 和无 Key 返回 503 的已批准行为。

采用现有 Compose secret 模式：LAWYER_DEEPSEEK_CONFIG_PATH 选择宿主机 JSON，默认空 Key 示例，不将 Key 本文放入 environment。容器只收到 /run/secrets/lawyer_deepseek_config。部署目录相对路径按 Compose 解析；真实文件保持 gitignore。

- [x] 新增 test_compose_contract.py 断言，先观察缺失 LAWYER_OPENSEARCH_URL 的 KeyError。
- [x] 补 Compose 配置与 compose.env.example 使用说明，可信源含 localhost:8080。
- [x] Compose contract + DeepSeek Settings 25 passed；Ruff 与 docker compose config --quiet 通过。
- [x] 更新交接；镜像模型依赖/权重仍需独立验证，不将此配置切片等同于完整容器启动。
