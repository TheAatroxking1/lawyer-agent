# 计划：语料按 id 深读注册为 Agent 网关工具（corpus.instruments_search 后续补全）

## 目标

上一轮把 `corpus.instruments_search` 注册为真实只读工具，Agent 能按名称检索法规目录，
但无法从目录行继续读到版本与条文全文。本轮把「按 id 深读」补成一组受控工具，使
Agent 具备「检索 → 读身份 → 读版本清单 → 读条文全文」的完整只读链路：

1. `corpus.instrument_get`：按 instrument_id 读单法规身份。
2. `corpus.versions_list`：按 instrument_id 读该法规全部版本清单。
3. `corpus.provisions_list`：按 version_id 读单版本全部条文全文。

## 范围

- 只改 `backend/src/lawyer_agent/api/dependencies.py` 的
  `_build_mcp_gateway_http_service`：继续在同一 closed registry 内登记三个新工具
  （仍是受控白名单，零外部 MCP Server、零 schema 改动）。
- 三个工具复用 `LegalCorpusQueryService`（与语料 HTTP 同源只读服务），输出只回白名单字段。
- 扩展 `backend/tests/integration/mysql/test_agent_gateway_http_api.py`：
  seed 一个法规 + 两版本（其一含条文、其一不含），做真实 MySQL+Redis 全栈断言。

## 工具契约（输出全部为确定性 JSON，绝不抛领域错误为 handler_failure）

- 参数 UUID 字符串非法（非 UUID7）→ handler 内 `MCPGatewayError("invalid_arguments", …)`，
  经网关映射为 `ok=false / error_code=invalid_arguments`（可诊断、稳定）。
- 按 id 找不到（合法 uuid7 但库中不存在）→ **不是失败**：输出 `{"found": false, …}`
  结构化空结果，与「检索空结果返回 []」同语义，不把正常业务缺失伪装成工具故障。

1. `corpus.instrument_get`（instrument_id 必填）
   - found: `{"found": true, "instrument": {id, title, issuing_authority, jurisdiction, region_code}}`
   - missing: `{"found": false, "instrument": null}`
2. `corpus.versions_list`（instrument_id 必填）
   - found: `{"found": true, "instrument": {…同上…}, "version_count": N, "versions": [{id, version_label, status, published_on, effective_on, repealed_on, law_number, dataset_version, parser_version}]}`
   - missing: `{"found": false, "instrument": null, "version_count": 0, "versions": []}`
3. `corpus.provisions_list`（version_id 必填）
   - found: `{"found": true, "version": {id, version_label, status, …}, "provision_count": N, "provisions": [{id, provision_no, level, structure_path, title, full_text}]}`
   - missing: `{"found": false, "version": null, "provision_count": 0, "provisions": []}`

字段白名单与 HTTP 契约一致（`LegalVersionSummary` / `ProvisionSummary` 的子集）；
日期/枚举转字符串、structure_path 元组转列表，保证 JSON 可序列化；不回显原始参数、
不泄任何租户私有或密钥内容（公共语料只读）。

## 验证

- ruff + mypy strict（164 文件）零错。
- 全 unit 套件保持 green（`1131 passed + 1 skipped`）。
- Agent 网关全栈（真实 MySQL+Redis）扩展后 green：工具清单含 5 个工具（meta + 4 corpus）；
  instrument_get 命中/未命中/坏 uuid；versions_list 命中两版本/未命中；provisions_list
  命中条文全文/空版本 0 条/未命中；既有断言保持。
- 提交：feat（dependencies 注册三个工具）+ test（全栈 seed 与断言）+ docs（本计划 + AGENTS）。

## 后续（非本轮）

- 真实逐 token 流式对话（provider chat_stream → SSE → ChatView）。
- 原始文件下载与 MinIO 对象存储真实持久化（占位对象存储当前不存字节，需先落地存储层）。
