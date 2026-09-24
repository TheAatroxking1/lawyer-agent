# v4发布质量摘要兼容修复

实际124源候选验收发现：78个按各自内容模式可继续的v4来源，进入真正发布条文检查后全部出现 `version_content_hash_mismatch`。导入端已经对精确v4按条文数量、身份、层级、路径、可空标题和原始正文生成带域分隔的结构摘要；发布端仍只计算连接正文摘要。不能删除检查或改写事实库摘要。

本修复依据用户已授权全集发布范围执行。变更仅 `application/legal_dataset_quality.py` 和对应测试，复用导入端规范算法，从按实际坐标排序的权威Provision重建草稿字段；只有精确v4启用结构摘要，旧版本保持原检查与质量摘要。任何正文、字段、顺序或边界篡改继续拒绝。

- [x] 实际ImportService生成普通条文和non-article两种v4版本，接真实ReleaseQualityService，先观察失败。
- [x] 最小兼容修复，不改导入算法、已有DB、Parser或GPU依赖；正例通过，正文/结构/顺序篡改拒绝，旧profile回归。
- [x] 独立复审、最终后端unit/api/contract、Ruff和mypy；冻结新源码存档，旧045500Z/045835Z档保留但不作生产入口。
- [x] 重新执行124源候选的实际CLI→内存导入/replay→发布条文检查→逐字符覆盖，只有全部匹配者形成通过清单。8份标题解释因原metadata为articles另做content_mode证据审核，不临时吞法条或篡改旧清单。
- [x] 固定新存档执行真实入库、严格replay及MySQL发布质量验收，完成后记录新来源/新派生版本的实际数量和旧版本不变证据。

问题证据：`artifacts/legal-corpus/full-release/v4-verified-candidates-20260907T045923Z/`。其78份失败是本次新发现的真实接线缺口，前面的Parser测试通过不能替代发布链路验收。两个早期v4存档均未用于生产写入；向量运行的49项冻结依赖不含这两个修复文件。
