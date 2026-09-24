# 合同审查 Workflow 核心框架实施计划

> 执行方式：本次在现有 codex/project-completion 分支内顺序执行，保留用户脏工作树，不提交用户已有改动。采用 TDD；只使用独立虚拟环境安装 contract-review Extra。

**目标：** 用户指定的四步自定义 LlamaIndex Workflow、两个 MCP 服务/七个工具契约、可信授权上下文、强制结果门禁与存储接口可实际运行和测试。

**架构：** 框架 SDK 留在 infrastructure，应用层维护强类型输入输出和端口。所有 Provider 显式注入；缺少文档、检索、授权、门禁或存储能力时不能伪造成功。合成演示独立标识，不宣称真实 OCR、法规或生产集成。

**范围：** 本轮不启动公网服务、不实现 PDF 页面、不下载模型、不访问客户合同、不切换现有索引或数据库。

## 文件与交付顺序

- [x] 依赖：backend/pyproject.toml 与 uv.lock 声明 contract-review Extra；独立环境冻结安装，核对实际 SDK。
- [x] 契约：backend/src/lawyer_agent/application/contract_review/contracts.py 定义文档/证据/风险/结果模型、运行预算与可信范围；ports.py 定义授权、七项服务及校验/存储接口。先验证参数上限、跨文档引用、额外字段拒绝。
- [x] MCP：backend/src/lawyer_agent/infrastructure/contract_review/mcp_servers.py 定义文档五工具及法律两工具；每次调用取得可信身份，先授权再进入业务服务。工具 Schema 不包含租户/用户自报参数。tests/unit/test_contract_review_mcp.py 验证真实 SDK 工具发现和调用、未授权/跨租户拒绝、参数/结果不合规拒绝及异常脱敏。
- [x] 工作流：backend/src/lawyer_agent/infrastructure/contract_review/workflow.py 保留四个指定步骤，增加 validate_review 和 persist_review；格式化/解析使用 LlamaIndex，模型经端口调用，无默认供应商。tests/unit/test_contract_review_workflow.py 先复现缺失框架，再验证 Action/Observation/最终结果、空响应、一次修复、步数/工具/响应大小限制、取消、失败不保存及无原始推理输出。
- [x] 网关/装配：同目录 tools.py 定义从可信 MCP 客户端到受控工具集的接线，llm.py 定义既有 ModelGateway 适配器；覆盖未知工具、越权、文档范围、结果 schema、唯一调用 ID、真正异步调用。不得依赖 SDK 工具筛选代替服务器鉴权。
- [x] 合成演示：backend/src/lawyer_agent/cli/contract_review_smoke.py 提供不连接外部模型的闭环 smoke；在真实 Workflow 调度与 MCP SDK 上运行合成事实，输出明确 synthetic 标记及通过门禁后的风险结果。
- [x] 验收/文档：docs/contract-review-core.md 说明安装、运行、扩展接口、已验证与未接入能力；同步 docs/project-status.md 与设计状态。

## TDD 与验收

每组先运行 `python -X utf8 -m pytest tests/unit/test_contract_review_*.py -q` 观察缺失行为失败，再实现；首次模块缺失使用明确断言，不能把依赖安装失败当 RED。

用独立环境执行：聚焦 pytest、后端 unit/api/contract、Ruff、mypy；运行 synthetic smoke。真实 SDK 的内存客户端/传输测试验证协议边界，Mock 只替代尚未接入的业务 Provider。未运行 OCR/VLM、数据库迁移、前端或公网 HTTP 端到端时明确记录。

检查最终新增文件与 scoped diff、UTF-8 和 git diff --check。运行结果填写实际记录，不预填通过数量。
