# 2026-09-10 真实 Embedding 端到端（CPU 本地模型 → OS k-NN）

## 目标

Provider 适配器已就绪、OS k-NN/BM25/RRF 与索引编排已通过确定性合成向量验证。
本切片首次跑**真实 embedding 模型**（本机 CPU 推理），走完整链路：
`LocalSentenceTransformerEmbeddingProvider`（真实权重）→ ModelGateway →
`LegalVectorIndexingService` → OS k-NN 索引 → `search_knn` 语义近邻命中。
当前后端 venv 为 torch CPU 版（CUDA=False），用小型中英双语模型
`BAAI/bge-small-zh-v1.5`（经 hf-mirror 下载）做验证；spec 3.1 的大模型选型
以法律评测集为准（后续切片），本切片只证明「真实推理→向量→k-NN 检索」全链路可用。

## 范围（只做这些）

- 新增 `tests/integration/opensearch/test_real_embedding_knn.py`（gate 于模型可加载 +
  OS 可达；本地真实跑一次作为证据，CI/无模型环境 skip 不伪造）：
  1. 加载真实 provider（model_name=BAAI/bge-small-zh-v1.5，HF_ENDPOINT=hf-mirror 可配置）；
  2. seed 两段语义不同/相近条文 → 通过 gateway.embed 得 384 维向量（校验维度）→
     经 index_version 入 OS k-NN 测试索引；
  3. 查询文本（与其中一条语义相同）→ search_knn 首命中所属 provision 是预期条文；
     维度不一致文本的向距离更大（首名仍是预期）；
  4. 清理索引（不依赖临时库则不复用 MySQL，直喂 chunk 端口）。
- 单测补充：真实 provider 的 encode 输出为有限 float、维度等于模型输出长度
  （若模型不可加载则 skip——该单测置于可选标记）。

## 明确不做

- 不选型/固化 spec 3.1 大模型（需法律评测集）；不改领域/编排；不上传权重；
  不在无模型环境伪造通过；AI Job Effect/Retry 等用户延后项不触碰。

## 验证

- 本机真实运行（CPU）成功：embedding 维度、k-NN 近邻断言、清理索引。
- 全量 pytest：新真实模型测试在无模型/OS 环境 skip（不破坏 1250 基线）；
  ruff/mypy 零错误；无 Secret（模型走公共 HF 镜像，不写 token）；树干净。
