# 2026-09-10 本地 Embedding Provider 适配器（供应商隔离）

## 目标

Embedding 已授权，且 ModelGateway 核心与向量索引编排已就绪。本切片在项目自有接口
（`ModelProviderPort`）之后实现**本地 sentence-transformers Embedding Provider**，
把真实权重/编码接入 ModelGateway——领域/编排代码零供应商 Payload。
供应商依赖（sentence-transformers/torch）与权重文件只存在于 adapter 层。

## 范围（只做这些）

- `infrastructure/providers/embedding.py`：
  - `LocalSentenceTransformerEmbeddingProvider` 实现 ModelProviderPort.embed/rerank/chat：
    - embed：惰性加载模型（`SentenceTransformer(model_name_or_path)`，启动时注入
      `encode` 可调用以离线测试），返回 `EmbeddingVector`（定维校验由 gateway 再做）；
      编码失败/依赖缺失映射为稳定错误（与 gateway 错误码一致口径），绝不伪造结果。
    - rerank/chat：未配置/不支持 → 明确抛错（无备用模型时不伪造 fallback）。
  - 配置走构造参数（model_ref、model_name_or_path、dimension、device、batch_size），
    不读供应商专用 Secret；模型名属于配置不属于领域。
- 单测（离线，不加载真实模型）：注入 fake `encode`，验证返回向量维度/文本数、
  空文本拒绝、依赖缺失路径的错误映射；rerank/chat 未支持错误。

## 明确不做

- 本轮不下载权重/不跑 GPU 推理（真实模型运行由后续切片执行并如实标注外部依赖证据）；
  不改领域/编排；不新增 pyproject 依赖提交（venv 安装属本机验证步骤）。

## 验证

- 聚焦单测 green；全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff/mypy 零错误；无 Secret；树干净。
