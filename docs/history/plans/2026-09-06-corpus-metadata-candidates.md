# S6a1：导入元数据候选清单

执行依据：已批准集合发布计划中的 `prepare_corpus_manifest.py`，以及项目指引要求未知日期/效力保持未知、原件身份与派生身份分离、相似标题不得自动合并。本切片仅准备供核对的候选，不做导入或发布。

## 范围与接口

- 新增类型化候选行和 CLI `lawyer_agent.cli.corpus_manifest`，脚本 `scripts/prepare_corpus_manifest.py` 为薄入口。输入为已有离线 `manifest.jsonl`、只读来源根与输出 JSONL 路径；稳定按来源相对路径排序，显式 limit 在合法来源选择之后生效。
- 每个来源行保存 schema、原件绝对路径/相对路径/SHA-256、导出状态、候选 title/category、文件名日期候选、候选机关、需要核对的字段。原始文件名和原件哈希保留；离线 ID 不作为数据库 ID。
- 标题和目录只能产生候选：文件名后缀日期只能写 `filename_date_candidate`；`published_on`、`effective_on` 保持 null，`status` 固定 status_unknown，`review_status` 固定 pending。不把日期后缀当公布/施行日期；不因含机关名而视为已经人工确认。
- 非 completed 来源也保留，并加入来源未完成的问题，不能给出可直接发布的假清单。无需读取正文、调用模型、加载 DB/服务或改变已有数据；逐行验证来源路径在给定根内、拒绝重定向/重复来源/错误 SHA-256/schema。真正导入时仍需重新验证原件与派生哈希，本候选不替代独立核验。
- 只从严格 YYYYMMDD 后缀生成有效日历日期候选；无效日期加入问题且不猜测。不从正文中自动推断地域或法律效力；region_code 保持 null。候选类别只使用已确认顶级分类目录映射。
- 输出必须与来源根隔离，拒绝指向输入 manifest、自身来源文件及任何 reparse 路径。UTF-8 原子写入，不覆盖原件；无副作用失败不留半份最终清单。
- `failed` 来源允许已有 manifest 的 `source_sha256=null`，但必须标记 `source_hash_missing`；其他状态缺少 SHA-256 或任意非空非法 SHA-256 均拒绝。候选统一 `hash_verification=pending`，仅检查清单哈希字段格式，不读取正文或冒充原件哈希复核。

## 验证

TDD 先复现缺入口/行为失败，再覆盖中文路径、合法/非法日期、无日期、机关候选、未知类别、完成/待转换/失败状态、limit/排序/重复来源、路径越界/重定向、原件与输出隔离、UTF-8 roundtrip。聚焦 pytest/Ruff/mypy 与独立审查通过；真实候选清单在最终文件导出后另行生成，不改变本次切块完成条件。

## 不在本切片内

正式元数据核对、数据库 provenance/身份迁移、统一 Reader 准备、批量 import-only、集合发布恢复/S4 A/B 均继续作为后续切片。不将“候选生成可运行”宣称为“全量入库可运行”。

## 当前开发验证（2026-09-06）

已实现 `application/legal_corpus_manifest.py`、`cli/corpus_manifest.py` 与脚本薄入口。首轮新增测试 23 failed（缺入口 RED）；实现后扩展到 37 passed，涵盖 Windows junction、输出 hardlink、原子替换失败、薄脚本实际 help 调用。Ruff 与 mypy 聚焦通过，独立审查重跑 37 项通过并批准；随后后端全量本地检查 1,692 passed / 1 skipped，Ruff 与 mypy 通过。最终全集导出后已真实生成 `artifacts/legal-corpus/import-candidates.jsonl`：16,006 行，来源均完成导出；逐行核对 review_status/hash_verification 为 pending、日期/地域为空、status_unknown。实际元数据审批和导入仍未完成。

在已安装本项目的后端环境运行示例（路径按实际输入显式给定）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m lawyer_agent.cli.corpus_manifest --source-root <只读来源根> --manifest <现有导出manifest.jsonl> --output <候选输出.jsonl> --limit 5
```

输出中正式日期/地域始终 null、法律状态 status_unknown；所有行 review_status/hash_verification 均 pending。输出留存导出状态和质量问题，不把 source_paragraph 回退或未完成来源直接转换成可入库条文。
