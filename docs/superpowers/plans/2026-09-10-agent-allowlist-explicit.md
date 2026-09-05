# 计划：Agent 网关工具白名单显式收敛（specs() 投影 + 装配 allowlist）

## 目标

网关早已支持构造期 `allowlist`，但 HTTP 装配 `MCPClientGateway(registry)` 未传
allowlist——registry 即许可集，且 `specs()` 返回全部 registry 工具，列表端点在
未来新登记工具时会把「尚未授权」的工具也展示出来。本轮把受控白名单做成显式、
可演进的边界：

1. `MCPClientGateway.specs()` 在配置 allowlist 时只投影允许子集（调用方不应看到
   未授权工具的存在）。
2. HTTP 装配 `_build_mcp_gateway_http_service` 传显式 allowlist
   `(meta.list_tools, corpus.instruments_search, corpus.instrument_get,
   corpus.versions_list, corpus.provisions_list)`——未来登记新工具不会自动暴露，
   必须刻意扩元组。

## 范围

- 只改 `backend/src/lawyer_agent/application/mcp_gateway.py` 的 `specs()` 与
  `api/dependencies.py` 装配尾部；`allowed/call/call_async` 语义不变。
- 内置 `meta.list_tools` 保持注册表范围（管理员自省工具），HTTP GET /tools 走
  `specs()` 投影。

## 验证

- ruff + mypy strict 零错。
- 单测 +1：allowlist 子集时 `specs()` 只含允许工具、`allowed()` 区分允许/禁止。
- 全 unit 保持（1150 passed + 1 skipped 目标）；真实 MySQL+Redis 网关全栈
  3 passed 保持（HTTP 工具清单与调用语义不变）。
- 提交：feat（gateway+dependencies）+ test（unit）+ docs（本计划 + AGENTS）。

## 备注

- 本轮另尝试 `docker compose build web`（Vue+Nginx 镜像），Docker Hub
  registry-1.docker.io 仍不可达（连接超时）——与既有记录一致的环境性阻塞，非
  代码问题；仓库内 npm 构建已绿，网络恢复后复跑即可。
- 剩余可选延后项（不属主线阻塞）：MinIO 真实对象存储/原始文件下载、工作台
  「运行检查」按钮（需激活规则包+条文输入的租户运营流程）、微信/短信登录绑定
  （需外部凭据）。
