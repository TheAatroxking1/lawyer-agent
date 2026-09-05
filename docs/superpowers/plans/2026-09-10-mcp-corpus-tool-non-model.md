# 语料检索注册为 Agent 网关工具（async handler + 真实工具）

- 日期：2026-09-10
- 类型：非模型切片（网关异步能力 + 组合器注册真实只读工具）
- 状态：已完成

## 动机

网关此前仅内置 meta.list_tools（自省）。把受控网关「用起来」：登记真实业务
工具 `corpus.instruments_search`（按名称搜索已入库公共法规目录）。登记器
handler 需要读库，故网关补 async 分派 `call_async`（自动 await 可等待返回，
同步 handler 亦兼容），HTTP 调用端点改走 call_async。

## 范围

- `application/mcp_gateway.py`：`AllowedToolRegistry.ensure_meta()`（内置
  meta.list_tools 无条件存在，不再只对空登记注册）；`MCPClientGateway`
  __init__ 调 ensure_meta；新增 `call_async(name, args)`（与 call 同白名单/
  Schema/错误映射；handler 返回可等待对象时 await，await 异常亦映射
  handler_failure，日志 name/ok/latency）。
- `api/v1/agent_gateway.py` POST /tools/call 改 `await call_async`。
- `dependencies._build_mcp_gateway_http_service(session_factory)`：登记
  `corpus.instruments_search`（args title 子串可选 / limit ∈ {10,20,50,100}
  默认 20，additionalProperties False），handler 用与语料 HTTP 同源
  `LegalCorpusQueryService(SqlAlchemyLegalCorpusReadUnitOfWork)` 异步读库并只
  返回白名单字段（id/title/issuing_authority/jurisdiction/region_code）。
- meta + corpus 都出现在 GET /tools（meta 现在无条件存在）。

## 验证

- ruff/mypy strict（164 文件）零错；全 unit **1131 passed + 1 skipped**（新增
  2 项 async：call_async await 异步 handler 成功、校验/未知映射）；
- **2 项真实 MySQL+Redis 全栈**更新：GET /tools 同时含 meta.list_tools 与
  corpus.instruments_search；调用 corpus 工具（空语料库）→ ok true 输出 [];
  limit 传字符串 → invalid_arguments；meta 调用/unknown/未知键 422 保持。

## 后续

把问答/检索链路、更多只读语料能力（版本/条文详情）逐步注册为网关工具并由
Agent 按白名单调用；MCP 外部 Server 兼容层明确不在首期。
