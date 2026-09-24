# 混合Parser集合发布实施

按同日设计及用户全集目标执行，使用TDD和subagent-driven-development，不提交Git、不询问已批准范围。所有生产改动先核对51 GPU冻结路径；v4存档不可改，新集合执行工具用新身份。

1. [x] 新typed provenance/config/report及canonical摘要：只改新增domain/legal_release_provenance.py和旧domain/legal_dataset_quality.py序列化入口、新聚焦tests。先RED，全报告行覆盖/来源替代/顺序/JSON类型/界限反例，旧digest不变。明确接口后通知下游。
2. [x] v3清单Reader：新增独立release_set_v3.py与旧release_set_manifest.py明确分派（必要返回类型联合），复用严格完整报告读取及编号证据；真实header、逐行old/new、最终digest、大小总量严格核验。旧v1/v2原路径不变。Task1完成后再启动新的实现子代理，不并行派发多个实现子代理。
3. [x] 应用质量适配：根代理负责legal_dataset_quality.py新配置分支逐成员身份与数据集校验；v2摘要与可持久化facts摘要，旧schema/digest保留。可与Task2子代理独立推进，文件互斥，独立测试/复审。
4. [x] CLI+publication域集成：根代理装配新配置及manifest摘要引用，完整证明仅存quality.configuration一份；明确v2候选验证，模型/维度不变；恢复拒绝篡改/降级，旧持久journal兼容。禁止在GPU运行时启动模型或OpenSearch。
5. [x] 全套后端、Ruff/mypy及隔离MySQL真实CLI→quality→candidate→持久JSON→恢复验证。独立最终复审，修复Important后定稿。
6. [x] 只读实际构造13份报告75替代/15994选择的新清单，沿用3份有明确版本绑定的宪法编号审核。实际一致视图完整质量，不能只跑通过子集。核对旧报告/DB事实/51 runtime不变，保存新的可重建发布源码入口。
7. [x] 维护现状、证据和后续实际OpenSearch验收/缓存缺口。未完成35已有质量问题及12未入库不得计完成；保留完整产品目标。

根代理在Task1期间独立准备真实13报告逐行替代关系/冻结交叉表及CLI/恢复集成测试，避免空等或与agent同时改文件。每任务快照当前脏文件并生成真实增量diff供复审；只读报告不能当实现完成。

本切片已执行并验证：后端3044通过/1跳过、Ruff/mypy224、隔离MySQL14通过；完整59MB JSON往返通过。真实全集质量15959无阻断/35阻断，另12份未入库，全集发布尚未完成。最终源码存档与证据见project-status.md的14:21记录。
