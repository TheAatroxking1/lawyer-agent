# 2026-09-10 受质量门禁保护的索引发布组合（非模型）

## 目标

索引发布编排（round 177 后）已能建索引→切别名→登记 PUBLISHED snapshot，但
**发布前没有任何条文质量校验**：`LegalCorpusQualityGate` 目前只被批次发布
（`LegalCorpusPublishService`，LoadBatch 流）使用，OS 索引发布可直接把任意
版本（空条文、条号断裂、覆盖不足）推向当前别名。spec 6.6 半自动更新要求
「通过自动质量检查…后」才形成新数据集版本。本切片把质量门禁接到 OS 索引
发布写面之前：新增组合服务先以 DB 权威条文评估质量，**不达标一律抛稳定
错误，绝不动索引/别名/快照**；达标才委托既有索引发布编排（其内部继续
indexed>0 校验与 snapshot 登记，语义不变）。

## 范围（只做这些）

- 新 `application/legal_dataset_gated_publish.py`：
  - `LegalVersionReadPort`（version_with_instrument + provisions_for_version，
    `SqlAlchemyLegalCorpusRepository` 结构满足，应用不反向依赖 infra）；
  - `LegalDatasetGatePublishService(gate, corpus, publish)`：
    `publish_version(version_id, index_name, alias, model_ref, dimension,
    batch_size)`：
    1. 读版本（缺失 → `LegalDatasetPublishError` 稳定 message，不发布）；
    2. `LegalCorpusQualityGate.evaluate(article_count, article_numbers 取
       DB 条文号按序, required_field_missing=(), parse_failures=0)`；
    3. 不达标 → `LegalDatasetPublishError`（message 带 quality issue 前缀，
       稳定可诊断），不调用 publish；
    4. 达标 → 原样委托底层 `LegalDatasetIndexPublishService.publish_version`
       （参数透传；0 文档拒发与 snapshot 登记继续在底层执行）。
  - 复用既有 `LegalCorpusQualityGate`/`LegalDatasetPublishError`，不改它们。
- 不改索引发布/别名/快照/批次发布既有行为；不加表/迁移。

## 明确不做

- 不做字段缺失/解析失败的逐条清单收集（索引发布只吃已落库 Provision，
  无 parse 输入；本文数 = provisions 数，required/parse_failures 恒为空/0
  由纯 DB 语义决定）；不做候选关系/dataset_v2 自动生成。

## 验证

- 离线单测（新 `tests/unit/test_legal_dataset_gated_publish.py`）：达标→
  底层 publish 调用一次且参数透传；空条文/条号断裂→抛
  `LegalDatasetPublishError` 且底层 publish **零调用**；版本缺失→抛稳定错；
  min_coverage 边缘（如 parse 无输入 coverage=1）不误拒；结果对象透传。
- 真实 MySQL 集成（新 `tests/integration/mysql/test_legal_dataset_gated_publish_repositories.py`）：
  真实 import 仓储导入达标版本（第一条/第二条 连续）→ 组合服务（真
  QualityGate + 真 corpus 读仓储 + fake 底层 publish 记录）→ publish 被调
  一次；再导入同 instrument 第二 label 断裂版本（第一条/第三条）→ 拒绝且
  publish 零调用 → 清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。

## 计划自检

- 覆盖 index-publish 尾注「质量门禁联动写面（发布前 gate）仍为后续」；
  纯组合、零 schema 改动；质量规则完全复用已测试的 QualityGate。
