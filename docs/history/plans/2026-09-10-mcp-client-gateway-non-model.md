# MCP 客户端网关（受控、白名单化工具分派）

- 日期：2026-09-10
- 类型：非模型切片（应用层；无外部 MCP Server 依赖，符合首期边界）
- 状态：已完成

## 动机

spec：MCP 通过**受控 Client Gateway** 增强 Agent，首期核心能力不得依赖任何
具体外部 MCP Server。仓库此前无网关代码。本切片新增网关本体：工具只能由
登记器注册（closed set）+ 可选启用白名单 + 严格输入 Schema 校验，按精确名
分派；未知/未启用/参数非法/处理器失败均回稳定错误码，绝不静默强转、绝不
动态导入执行、绝不伪造输出。

## 范围

- `application/mcp_gateway.py`：
  - `MCPGatewayError{code,message}`；
  - `ToolSpec`（name 白名单 `^[a-z][a-z0-9_.]{0,63}$`、description ≤512、
    input_schema 子集校验：顶层 object + 白名单键 type/properties/required/
    additionalProperties/description；属性仅 string/integer/number/boolean/
    array(object)/null + description/items/enum，未知键与坏类型/坏数组
    items/坏 enum/required 未声明一律稳定拒绝）；
  - `validate_args(spec, raw)`：非对象、缺 required、未知参数
    （additionalProperties 关闭时）、类型不符、enum 越界、数组逐项类型
    不符 → `invalid_arguments`；
  - `AllowedToolRegistry.register/spec/handler/specs`（重复登记
    `duplicate_tool`）；
  - `MCPClientGateway(registry, allowlist?)`：`allowed(name)`、`call(name,
    args) -> ToolResult{ok,output|error_code,error_message}`；空登记时自动
    注册内置 `meta.list_tools`（列出当前可用工具名/说明）；错误分派
    unknown_tool / tool_not_allowed / invalid_arguments /
    handler_failure（处理器失败信息不回显参数/异常细节）；成功与失败走
    结构化脱敏日志（name/ok/code/latency_ms）。

## 验证

- ruff + mypy strict（163 文件）零错；**21 项单测** green：注册调用、
  未知工具、启用白名单（空集禁全部）、重复登记、坏名字/坏 Schema 族、
  spec 校验、参数非法族（类型/缺必填/未知键/enum/数组项）、合法载荷、
  处理器失败稳定文案（不回显）、meta.list_tools 自注册、specs 稳定排序；
- 全 unit 目录 **1129 passed + 1 skipped**（前 1108 + 21）。

## 后续

接入 Agent 侧（如把既有合法检索/语料只读能力注册为网关工具并授予 Agent
调用）、网关暴露于 ApplicationServices/HTTP、外部 MCP Server 兼容层（明确
不在首期）。
