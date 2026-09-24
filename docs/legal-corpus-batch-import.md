# 受控批量导入

文件切块、数据库导入和索引发布是三个验收步骤。本入口只将已核对清单中的法规逐文件入库，或做不连接数据库的预检；不会切换 OpenSearch 别名。

## 准备已核对清单

`artifacts/legal-corpus/import-candidates.jsonl` 保留原始 pending 候选快照，不能直接交给批量导入。用户已确认本批 16,006 份候选现行有效，并明确允许缺失日期；这两项确认分别记录在 `artifacts/legal-corpus/reviews/user-current-review-20260906.json` 和 `user-date-policy-20260906.json`。实际导入使用绑定审核引用的独立 reviewed 清单，仍须验证原件哈希、制定机关、地域和转换/结构证据；人工效力确认不等于这些机器检查已经通过。

导入清单为 UTF-8 JSONL，一行一个 `legal-corpus-import-v1` 对象。必须显式提供：

- `schema_version: "legal-corpus-import-v1"`、`review_status: "reviewed"`。
- `metadata_review_ref`：本次人工核对记录的引用。该字段是操作者提供的声明，不是程序完成了专业法律审核，也不是电子签名。
- `source_path`：原件的绝对路径；`source_sha256`：实际原件的 64 位小写十六进制 SHA-256。DOC 转换后仍填写原 DOC 的身份。
- `title`、`issuing_authority`、`jurisdiction`：已核对的显式值，不自动从文件名推断。

可选 `category`、`region_code`、`version_label`、`published_on`、`effective_on`、`repealed_on`、`law_number`、`source_ref`、`date_review_ref`。日期使用 `YYYY-MM-DD` 或 null；缺失日期保持 null，缺省效力为 `status_unknown`。显式 `status: "current"` 默认要求公布和施行日期；本批通过 `date_review_ref` 绑定用户日期例外确认后可保留 null，不能用导入日期补齐。有日期不自动证明现行有效。未给日期/版本标签时沿用原件摘要版本标签。

整份清单在接触业务服务前校验：不接受未知字段、旧候选 schema、pending、重复来源、越界或 reparse 路径。来源根和清单路径须为绝对路径，来源根须实际存在。来源文件可暂时缺失，此时该文件会产生失败记录。当前限制为 32 MiB 清单、每行 1 MiB、最多 20,000 份。

## 运行

在仓库 `backend` 目录使用现有虚拟环境；以下清单/报告文件名为待替换的操作示例，不表示真实已核对文件已经存在：

```powershell
.venv\Scripts\python.exe -X utf8 -m lawyer_agent.cli.corpus_import_batch `
  --source-root 'F:\ai律师数据库\法律法规数据库' `
  --manifest 'C:\已核对清单\approved.jsonl' `
  --report 'C:\导入结果\batch-preflight-001.jsonl' `
  --preflight
```

涉及旧 DOC 或实际为 OLE 的 DOCX 时，另提供 `--conversion-manifest` 和 `--converted-root`，两者必须成对。本项目已有转换清单位于 `artifacts/legal-corpus/converted/manifest.jsonl`；使用它时，参数填写该文件和目录的实际绝对路径。运行内只加载一次受限快照并记录摘要；每份文件仍检查原件/派生的路径、哈希及同一字节快照。不会打开 Word 或执行宏。

预检通过并确认导入目标数据库后，去掉 `--preflight`，换一个新的报告路径即可导入。数据库连接沿用后端 `LAWYER_*` Settings。每份文件单独提交版本、来源证明与层级块；文件或单次写入失败不会回滚其它已成功文件。静态恢复来源须提供并通过独立质量证书及哈希核验；缺少相应证明时返回 `static_recovery_requires_quality_review`，清单的 reviewed 声明不能单独解除该限制。

报告必须为新文件，不能位于来源/派生根中、覆盖清单或既有结果，也不允许 Windows 附加数据流、设备名或尾点/尾空格别名。首次数据库连接不可用时停止本次运行，避免逐份重复请求不可用服务。

## 结果和中断恢复

JSONL 首行为 `event: run`，绑定清单及转换快照摘要、总数和模式。每份 `event: file` 包含原件路径/摘要、状态与稳定错误码；`imported`/`replayed` 的真实法规版本 ID 只在事务提交成功后写出。每行 flush/fsync，不包含正文、SQL 参数或异常 Payload。

末行 `event: summary` 汇总 `imported`、`replayed`、`preflighted`、`failed` 和 `complete`：

- 退出 0：本次选中清单全部成功；不代表全集已经导入或已经发布。
- 退出 1：存在逐文件失败，或数据库/报告运行中断。
- 退出 2：清单、路径或全局参数无效，未产生完整结果。
- 没有 summary：运行不完整，不能从已有几行推断其余文件成功。程序可能在数据库提交后、结果落盘前中断。

恢复时使用同一份原清单和新的报告路径重新运行。程序重新核验每份来源并查询数据库，不因旧报告曾写成功而跳过。相同证明重跑为 `replayed`；缺失旧证明返回 `source_proof_missing_requires_review`，证明变化返回 `source_proof_conflict`，不得静默覆盖旧版本。修正失败来源后，也应保存新的核对记录和清单。

相同重跑会逐项核对已存条文及完整分块父子图，匹配时保留原 version/provision/chunk UUID，不重新写入分块。正文、偏移、父子关系等存在差异或缺失时拒绝，不自动修补已有事实。解析规则有意变化须使用独立派生版本与可审计迁移，不能借重跑覆盖旧证明或补写虚假成功汇总。

并发首次导入仍可能因既有唯一键竞争失败。历史发布快照与跨系统回滚、集合发布恢复、专业法律质量及真实模型验收仍需后续完成。
