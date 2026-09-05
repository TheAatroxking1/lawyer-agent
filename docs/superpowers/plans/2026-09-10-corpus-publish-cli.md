# 语料导入发布 CLI（corpus_publish）与真实 dataset_v1 跑通记录

## 目的

检索问答（有据问答）的检索链路需要「真实法规语料」在运行栈上发布为数据集别名
（如 `dataset_v1`）。此前导入/发布只存在于服务层与测试，没有面向用户的运行入口。
本命令补上整条流水线的 CLI，并在开发栈真实跑通。

## 命令

```
# 后端目录下，先设好数据库连接（dev 栈默认如下）
$env:LAWYER_DATABASE_URL = 'mysql+asyncmy://lawyer:lawyer-local-password@127.0.0.1:13306/lawyer_agent'
# embedding 走本地权重，强制离线避免网络探测
$env:HF_HUB_OFFLINE = '1'; $env:TRANSFORMERS_OFFLINE = '1'

uv run --no-sync python -m lawyer_agent.cli.corpus_publish `
  --docx 'C:\...\sample.docx' `
  --instrument-title '法规名称' --issuing-authority '发布机关' `
  --jurisdiction national --published-on 2026-09-10 --effective-on 2026-09-10 `
  --law-number '文号（可选）' --alias dataset_v1
```

流水线：DOCX → `LegalStructureParser`（只认「第X条…」等编号条文）→ 显式元数据映射
→ 受控导入（version+provisions，同文件重跑幂等 replay）→ 派生 PROVISION chunk →
本地 `BAAI/bge-small-zh-v1.5` 分批 embed（ModelGateway 记录调用）→ OpenSearch
k-NN 索引（索引名缺省 `lawyer_dataset_<hex12>`）→ `LegalDatasetIndexPublishService`
原子切 `dataset_v1` 别名并登记 snapshot。零条文/空文件/坏参数返回非零码并不写库。

## 生成样例语料（可选）

```
python scripts/make_sample_law_docx.py 'samples/demo/房屋租赁样例.docx'
```

样例含 10 条租赁/租金/违约金相关条文，便于验证「逾期支付租金/违约金」类问题检索命中。

## 2026-09-10 真实跑通证据（开发栈）

- 主库 `lawyer_agent`：`alembic upgrade head` 成功（legal corpus schema 建表）；
- CLI 输出：`imported … articles=10 chunks=10`、
  `published index=lawyer_dataset_7791fc0a0c4d alias=dataset_v1 indexed_documents=10 previous_target=None`；
- OpenSearch：`/_alias/dataset_v1` → `lawyer_dataset_7791fc0a0c4d`，`_count=10`；
- MySQL：`legal_dataset_snapshots` 中 `dataset_v1` 为 `published`（released_at 有值，
  manifest 指向上述索引/version，indexed_documents=10）；`legal_versions=1`、
  `legal_provisions=10`、`legal_chunks=10`。

## 备注

- 同一 instrument+label 的相同内容重跑会幂等 replay（同 version id），不会重复写；
  换 alias/index 发布会生成新 snapshot，旧索引保留可回滚。
- 清理历史测试残留索引/数据不在本命令范围；如需可另列清理脚本。
