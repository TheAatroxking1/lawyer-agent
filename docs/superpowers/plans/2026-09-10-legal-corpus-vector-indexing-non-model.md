# 2026-09-10 语料向量索引编排（provider 可注入）

## 背景

Embedding 已授权；Model Gateway 核心、LegalChunk 原料层、OS k-NN/RRF 已就绪。
缺的是把三者串起来的**编排服务**：从 MySQL 读 version chunks → 经 gateway 批量 embed →
建/更新 OS k-NN 索引文档。真实权重/Provider 由后续适配器注入；本切片服务本身
provider-agnostic，可完全离线单测，并用确定性合成向量做真实 OS k-NN 联调。

## 范围（只做这些）

- 应用 `application/legal_vector_indexing.py`：
  - 端口：chunk 读源（chunks_for_version）、gateway（复用 ModelGateway.embed）、
    OS client（OpenSearchRestClient）。
  - `LegalVectorIndexingService.index_version(version_id, index_name, model_ref, dimension,
    batch_size=64)`：读 chunk → 分批 embed（gateway 校验维度/记录调用）→
    `ensure_index(index_name, vector_dimension=dimension)` → `replace_documents`
    （文档 = 标识符 + content + content_vector list；幂等重建）；返回已索引数量。
  - 错误：chunk 空 → 不索引返回 0；gateway 失败向上抛稳定错误；维度不符由 gateway 拒。
- 单测（离线）：fake chunk/gateway/transport；断言批量切分、文档含 vector、
  ensure_index 带 dimension、replace 调用、空 chunk 短路。
- 真实 OS 确定性联调：合成向量（按文本哈希生成定长）→ index_version → search_knn
  最近邻为自建对应文档（OS 不可达 skip）；验证 k-NN 全链路无需真实模型。

## 明确不做

- 真实 sentence-transformers/权重下载/GPU 运行（后续 provider 适配器切片）；
  Rerank/问答/SSE；改 gateway/OS 契约。

## 验证

- 聚焦单测 + 真实 OS 联调（可用则跑）；全量 pytest（Redis 抖动按已知模式隔离重跑）；
  ruff/mypy 零错误；无 Secret；树干净。
