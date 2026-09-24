# 2026-09-10 质量门禁失败项落库 + 批次质量明细读 HTTP（非模型）

## 目标

批次读面（round 179）已能看 LoadBatch，但质量门禁失败产生的
`QualityReport.issues`（no_articles/coverage/required/sequence 等）只随
REJECTED snapshot 的 metrics 摘要落库，`legal_quality_issues` 表与
`QualityIssue` 域/模型自 Schema 建好起**没有任何写路径**，也没有公开读端点。
spec 6.6「自动质量检查…拒绝发布」应有可盘点的失败明细。本切片：
（1）门禁失败时把每条 issue 原子落为 `legal_quality_issues` 行（幂等重建：
先删该 batch 旧行再插，重复发布失败不累积）；（2）新增
`GET /api/v1/legal/load-batches/{batch_id}/quality-issues` 白名单读回
（issue_type/message，不含文件内容）。

## 范围（只做这些）

- domain `legal_corpus.py`：`QualityIssue` 值对象已存在且完备（id/batch_id/
  file_sha256/issue_type/message），不改；仅加稳定错误
  `LegalQualityIssueError(ValueError)` 到相关服务模块。
- `application/legal_corpus_publish.py`：
  - `LegalCorpusPublishPort` 增 `replace_quality_issues(batch_id, file_sha256,
    issues: tuple[str,...])`（门禁失败明细的幂等写面）；
  - `LegalCorpusPublishService.publish`：`report.passed is False` 且
    `batch_id is not None` 时调用 `replace_quality_issues`（issue_type 取
    `issue.split(":")[0]`、message 存完整 issue；成功路径不写行）。
- `infrastructure/.../legal_corpus_inventory.py`：`replace_quality_issues`
  （同事务先 DELETE batch_id 旧行再 INSERT）；只读 `quality_issues_for_batch`
  （按 created_at/id 稳定升序）。
- 应用 `legal_corpus_read.py` 与 `api/v1/legal_corpus.py`：新增
  `LegalCorpusQueryService.quality_issues(batch_id)`（复用
  `load_batch` 存在性校验：缺失 404 `legal_corpus_load_batch_not_found`）+
  `GET /api/v1/legal/load-batches/{batch_id}/quality-issues` 返回白名单
  `QualityIssueSummary`（issue_type/message，`extra="forbid"`）。
- 纯后端写/读：不加新表、不加依赖；QualityIssue 写仅限门禁失败路径。

## 明确不做

- 不做 quality issue 的逐文件归属（gate 是批次聚合，行只记录 batch 级
  file_sha256）；不做成功路径/警告写行；不做 issue 处置/编辑。

## 验证

- 离线单测：publish 失败 → `replace_quality_issues` 收到
  issue_type/message 分解（含 `sequence_break:...` 冒号拆分）；成功路径
  零调用；batch_id 为空失败不写。
- 真实 MySQL 集成（扩展 `test_legal_corpus_repositories.py`）：真实
  inventory repo：inventory 建 batch → publish 失败（门禁不过）→
  quality_issues 读回正确 → 再次失败 publish 幂等仍同集合 → 读面查询服务
  经真 repo 读回。
- 真实 MySQL+Redis 全栈（扩展既有语料 HTTP 全栈）：seed batch + 两 issue
  行 → GET quality-issues 白名单读回 → 未知 batch 404 → 未认证 401 →
  清理。
- 全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；ruff/mypy 零错误；
  无 Secret；树干净。
