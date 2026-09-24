# Reviewed Article Numbering Implementation Plan

> 执行方式：同一任务内使用 subagent-driven-development，一名实现者、独立任务审查及最终集成审查；根任务并行准备官方证据和只读实源验收。用户已授权范围内自主实现，不提交当前脏工作树。

**Goal:** 让经官方证据核对的连续延续条号通过发布结构检查，同时保留默认缺号拒绝和不可变来源绑定。

**Architecture:** v2集合清单叠加显式审核，领域类型验证，既有质量服务与持久发布报告传递。不改变导入、Parser 或数据库Schema。

**Spec:** `docs/superpowers/specs/2026-09-07-reviewed-article-numbering-design.md`

## Global Constraints

- 保留当前脏工作树和旧报告，不提交，不修改外部来源。
- 禁止修改CPU向量任务五项冻结runtime，禁止加载模型、GPU或启动OpenSearch。
- 不按标题/哈希硬编码例外，不从观察到的第一个条号自动推断合法起点。
- 默认序列规则与历史quality digest不变；有审核时完整绑定版本、source/input/structure SHA、计数与证据SHA。
- 全部正文、分块图、元数据、效力、日期与最终review digest门禁保留。

## Task 1: 编号审核端到端发布契约

**Files:**
- Modify `backend/src/lawyer_agent/domain/legal_dataset_quality.py`
- Modify `backend/src/lawyer_agent/application/legal_corpus_publish.py`
- Modify `backend/src/lawyer_agent/application/legal_dataset_quality.py`
- Modify `backend/src/lawyer_agent/infrastructure/documents/release_set_manifest.py`
- Create `backend/src/lawyer_agent/infrastructure/documents/release_numbering_review.py` if needed to keep evidence loading separate from set assembly.
- Test existing unit modules `test_legal_dataset_quality.py`, `test_release_set_manifest.py`, `test_corpus_publish_set.py` (inspect actual filename), plus focused `test_reviewed_article_numbering.py`.

**Interfaces:**
```python
@dataclass(frozen=True, slots=True)
class ArticleNumberingReview:
    first_article: int
    last_article: int
    review_ref: str
    evidence_ref: str
    evidence_sha256: str

# Append to ReleaseSelection and ReleaseSourceSummary:
numbering_review: ArticleNumberingReview | None = None

# Existing gate retains its old behavior when absent:
def _sequence_break(self, numbers: tuple[str, ...], *,
                    numbering_review: ArticleNumberingReview | None = None) -> str | None: ...
```

- [x] Save touched-file before snapshots in `.superpowers/sdd/reviewed-numbering-before/`; include untracked baseline content in task diff.
- [x] Write focused failing tests before implementation and record exact failing outcome. Minimal positive sequence is `("第三条", "第四条")` with review `(3,4)`; without review it fails. Negative sequences include `("第三条", "第五条")`, duplicate, reverse, first/last truncation, supplement, invalid numeral. Domain rejects bool, strings, invalid ranges, non-HTTPS/credential URL, invalid hashes, mode/count mismatch.
- [x] Add validated immutable type and optional fields. Include nonempty record in summary/digest. Remove `numbering_review` from the legacy digest's selection and summary source if None, and preserve absent field in legacy public report serialization. Add manual-review reason for explicit numbering review. Test old deterministic digest before/after with fixed UUID fixtures and review evidence/ref changes invalidating prior approval.
- [x] Extend gate using explicit reviewed start/end, with no renumbering. For reviewed mode the validated expected interval is strict: no supplements, number count equals last-first+1, first==start, ordinary increments, end==last; invalid returns a non-None break reason even if input is empty. Keep default implementation unchanged.
- [x] Add strict v2 manifest parsing exactly per spec; v1 refuses numbering_reviews. Bind records only after existing report validation. Evidence loader reads ordinary files through existing path boundary, bounded 4MiB per file/16MiB total, checks actual SHA and caches by normalized path. Apply `dataclasses.replace(selection, numbering_review=review)` after exact version and three-hash match; reject duplicate, unknown, mode/count mismatch. Map failures to stable body-free `ReleaseSetManifestError` codes.
- [x] Exercise loader -> actual CLI `load_release_input` -> quality service -> candidate/report persistence payload using synthetic fixtures; verify source mismatch remains rejected and old reviewed digest cannot approve changed review. No live model/search or production writes.
- [x] Run focused unit tests, Ruff and mypy; report changed files, before/diff identities, exact red/green results and concerns in `.superpowers/sdd/reviewed-numbering-implementation.md`.
- [x] Independent task reviewer reads spec, scoped before/diff and evidence; fix Important/Critical findings through fresh fix agent then re-review.

## Root acceptance (after implementation freezes; may run alongside read-only review)

- [x] Save official download and local three-source range binding, keeping the distinction between numbering evidence and full-body comparison.
- [x] Load immutable complete reports through new v2 set; on the three target selections execute real MySQL read-only quality check in one repeatable-read view before/after review. Preserve unchanged IDs and source/input SHA, no production writes. Record all blockers and digest change, not just range pass.
- [x] Run `uv run python -X utf8 -m pytest tests/unit tests/api tests/contract -q`, `uv run ruff check .`, `uv run mypy src` from backend on final code, `git diff --check` at root, and inspect scoped diff/untracked paths.
- [x] Final integrated review (most capable available model) examines the scoped package and operational evidence; do not claim whole-product completion.
- [x] Update project-status and progress ledger with actual acceptance and CPU observation; keep whole-project goal active.

## 2026-09-07 最终执行结果

本切片完成，任务复审和最终集成审查均通过，无遗留发现。集成包 `reviewed-numbering-integrated-20260906T202831Z.diff`，SHA256 `be43c0d2dbd157b5bb6556d1cc78dd744e777a31d9e63d786daee7a2e04a489c`，覆盖六文件；全部before快照、两次修复的RED/GREEN、独立审查及根验证保留在 `.superpowers/sdd/reviewed-numbering-*`。

最终 `uv run python -X utf8 -m pytest tests/unit tests/api tests/contract -q` 为2760 passed、1平台skip、1既有Starlettewarning；whole-backend Ruff与mypy218文件通过。三源实际CLI/READ ONLY同视图前后质量核对及合成隔离MySQL发布日志/快照往返通过；隔离库已删除。`git diff --check` exit0，64个既有CRLF提示，249项脏工作树保留，无提交。

额外对v2集合的全部14,280版本执行了冻结代码下的只读质量核对：3份编号审核通过，104份仍因article_sequence_invalid阻断，报告passed=false，质量摘要 `3a7ef88e68418fc6efae0ebe4aca531807a2e1b38261ba1e3423e8178971d12d`。报告目录 `artifacts/legal-corpus/full-release/reviewed-numbering-full-quality-20260906T202812Z/`。该范围不是全部16,006，未加载模型、未启动OpenSearch、未切别名。

CPU PID58004维持原接续，04:33采样1534/6127来源、65199行缓存统计、CUDA未初始化、私有内存2848.93MiB，5项冻结runtime摘要不变。GPU计算继续暂停。后续继续剩余32份结构调查、v4冻结和新版本导入、完整清单与全集发布验收；不将本计划完成标记为全产品完成。
