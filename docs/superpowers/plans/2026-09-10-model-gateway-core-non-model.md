# 2026-09-10 Model Gateway 核心（非供应商 / 可离线验证）

## 背景与授权

用户已授权运行 embedding（目标 rev 28）。阶段 1 检索/问答链路（Embedding、
OpenSearch 混合检索、DeepSeek 问答、SSE）此前整体延后且仓库中**没有任何**模型网关代码。
勘察结论：本机 GPU（RTX 4080）+ hf-mirror/ModelScope/PyPI 可达，但 backend venv 未装
torch/sentence-transformers/opensearchpy/langchain，OpenSearch 容器未运行，真实模型下载/索引
是后续多轮工程。本切片先按 spec 7.2 建 **Model Gateway 核心**——供应商无关、可完全离线单测，
后续真实 Provider（Embedding/Rerank/DeepSeek）都插在该自有接口之后，满足
「模型与 Embedding 供应商必须位于项目自有接口之后，不得在领域代码散布供应商 Payload」不变量。

## 范围（只做这些）

- 领域纯模型 `lawyer_agent/domain/model_gateway.py`：
  - 枚举/值对象：ModelCapability、EmbeddingVector（固定维度校验）、RerankScore；
  - `CallLimits`（timeout、max_attempts、退避基数）与有界重试纯函数（拒绝无限重试）；
  - 无供应商 Payload；可确定性验证。
- 应用层 `lawyer_agent/application/model_gateway.py`：
  - `ModelProviderPort`（embed / rerank / chat，供应商实现的自有接口）；
  - `ModelGateway` 门面：调用前校验（文本非空、向量维度）、计时、失败映射，
    **无备用 Provider 时供应商错误明确抛出、绝不伪造 fallback**；
  - `ModelCallRecorderPort` + `ModelCallRecord`（model_ref/operation/latency_ms/
    status/error_code/token 计数/cost 占位）——门面每次调用必写记录（内存实现供测试）。
- 单测：能力校验、维度/空文本拒绝、有界重试计数与退避、成功/失败调用都产生记录、
  超时与错误映射、供应商异常不伪造成功。

## 明确不做

- 不接真实模型/OpenSearch/DeepSeek/SSE（后续切片在自有接口之后接入）；
- 不做 AI Job Effect/Retry/Maintenance、Synthetic Harness、故障注入、Compose 多进程等
  用户明确延后项；不加 pyproject 依赖、不加表迁移。

## 验证

- 聚焦单测（纯离线）；全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff check src tests / mypy src 零错误；无 Secret，工作树干净。
