# 2026-09-10 OpenSearch 数据集索引别名（原子切换，非模型）

## 目标

spec 6.1 入库流水线第 9 步「质量门禁通过后切换数据集与索引别名」、spec 6.6
半自动更新「新索引构建完成后原子切换别名，旧数据集继续用于历史问答和快速
回滚」。当前 `OpenSearchRestClient` 只有 ensure_index/replace_documents/
search_bm25/search_knn/delete_index，测试全用一次性临时索引名——**没有稳定的
「数据集别名」及其原子切换能力**，后续 dataset_v2 发布与「当前数据集合集检索」
无从谈起。本切片为 OS REST 客户端与一个轻量应用服务补别名操作
（读别名当前目标 / 原子指向新索引 / 删除别名），全部经 httpx 直连、无 SDK。

## 范围（只做这些）

- `infrastructure/search/opensearch.py`：
  - `OpenSearchRestClient.resolve_alias(alias) -> str | None`：
    `GET /_alias/{alias}` 返回当前唯一目标索引（404 → None；多目标 → 稳定
    `OpenSearchError`，别名只允许指向单索引）；
  - `OpenSearchRestClient.point_alias(alias, index_name) -> str | None`：
    若已指向同一索引 → 幂等 no-op 返回原目标；否则 `POST /_aliases` 原子
    `remove` 旧目标 + `add` 新目标（切换）；返回**切换前**目标（首次为 None）；
    非法目标名/缺失索引等非 2xx 映射稳定 `OpenSearchError`；
  - `OpenSearchRestClient.drop_alias(alias) -> None`：删除别名（404 → 视为
    已不存在幂等）。
- `application/legal_index_alias.py`：
  - 校验别名/索引名白名单（小写字母/数字/`_`/`-`，长度上限），稳定错误；
  - `LegalDatasetAliasService.publish_dataset(alias, index_name)`：先
    `resolve_alias`，若同目标则 no-op 返回；否则确保 index 存在后
    `point_alias` 原子切换，返回前目标；只做指向管理，不搬数据；
  - `active_dataset_index(alias)`：解析当前目标，无别名 → None（供检索编排
    后续使用，本切片不接检索）。
- 不改 BM25/kNN 契约；不加依赖；别名操作不写 MySQL、不涉租户数据。

## 明确不做

- 不做 dataset_v1/v2 发布编排与质量门禁联动（spec 6.6 发布流程后续切片）；
  不做「当前检索走别名」的调用方改造；不改 Schema；不接 HTTP。

## 验证

- 聚焦单测（纯离线，httpx MockTransport）：resolve 404→None、单目标解析、
  多目标抛错；point 首次切换（remove+add 请求体断言）/ 同目标幂等不发切换 /
  旧目标返回；drop 幂等；非法别名/索引名拒绝。
- 真实 OpenSearch 容器 e2e（不可达则按可用性模式 skip，不伪造通过）：
  ensure 两个索引 → publish_dataset 指向 A → 再指向 B（原子）→
  resolve 返回 B 且 A 索引仍可删除 → drop 后 resolve None → 清理两索引。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
