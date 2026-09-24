# 保持分块身份的语料重跑

本切片落实已批准全集发布计划的安全恢复要求。现有来源版本 replay 后仍无条件重建 Chunk UUID，不能直接用它恢复中断批次。

## 全局约束

- 已有 version、provision、chunk UUID 与父子引用必须保持不变；重跑不能删除、替换或补写既有派生事实。
- 相同来源/输入/结构证明、元数据、版本内容、Parser 和完整 Chunk 语义图一致才允许 replay。不同或缺失时明确失败，不能只比较数量。
- Chunk 语义比较包含版本/条文归属、正文、类型、质量、Parser、相对偏移、父子拓扑；仅比较时忽略新生成的 Chunk UUID，将父 UUID 映射为语义父节点。歧义、孤儿或环均拒绝。
- 已存在来源证明和导入服务的严格校验继续生效；新版本仍在单来源事务内正常创建。报告写失败仍停止后续操作，不能补写虚假 summary。
- 保留用户脏工作树，不提交、不清理；真实库、来源和 GPU 不用于实施者测试。隔离 MySQL 测试由 root 配置与执行。

## Task 1：非破坏 replay

先以单测观察现有 replay 调用 replace、同数量但错误语义图被接受的失败；再实现有类型的完整语义图比较及 `_import_prepared` 接线。匹配时返回既有 DB 计数；不匹配时回滚并返回稳定错误，不能修补。

测试覆盖：相同父子图但独立生成 UUID 时一致；正文、类型、质量、偏移、Parser、provision、parent 关系、缺块/多块或重复节点不同必须拒绝；顺序变化不得误判同一图。缺失条文或改写 DB 条文不得在同一版本上静默恢复。

补充真实隔离 MySQL 回归：首次导入保存全部 ID 和关系，重复导入后逐行相同；篡改同数量 Chunk 内容或关系后 replay 失败且全部旧事实不变；模拟 commit 后 report file 前中断，使用新报告路径重跑产生真实 replayed 和完整 summary。测试用合成来源及隔离库，禁止连接业务库作为测试库。

## Task 2：独立复审与恢复验证

生成仅本切片变更的 review diff，独立复审规格与质量；修复全部阻断项。通过后 root 核对真实原清单/转换清单/中断报告摘要，在严格不改 ID 的前提下重跑原完整批次到新报告。前后核对 ID 摘要与事实计数，失败保持显式状态，不切发布别名。

多报告集合格式、已入库解析纠错与正式发布属于后续切片；不得伪造合并 import run。

## 2026-09-06 执行证据

- Task 1 已实现、独立复审 APPROVED。聚焦 95 项、隔离 MySQL 8 项通过；完整后端 2,558 passed / 1 skipped / 1 warning，Ruff、mypy 通过。复审记录 `.superpowers/sdd/replay-task-1-independent-review.md`。
- Task 2 实际重跑已完成一次尝试：4,736 replayed、9 source_proof_conflict，报告 `import-v3-batch-003-recovery-20260906-001.jsonl` 的 `complete=false`。不能作为完整发布选择，继续定位 9 份差异，不放宽来源证明校验。
- 恢复前后 4,745 版本、199,770 条文、213,908 分块的数量和全部身份/内容摘要一致；原清单、原中断报告、转换清单摘要一致。证据在 `artifacts/legal-corpus/full-release/replay-batch-003-before-identity.json` 与 `replay-batch-003-after-identity.json`。
