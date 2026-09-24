# 混合版本向量补算实施计划

使用subagent-driven-development逐任务实现，根代理负责装配与验收，不并行派发多个实现子代理。

**Goal:** 为95个合格补充版本提供绑定真实发布证据的向量入口，随后串行补齐缓存。

**Architecture:** 新增独立混合选择Reader/事实核验与运行入口，复用已有固定模型缓存、温控、单模型锁。所有新文件避开活动51项依赖，不修改旧v3协议。

**Tech Stack:** Python、现有SQLAlchemy只读事务、pytest、现有CUDA环境。

- [x] 1. 新增`.superpowers/sdd/mixed_gpu_selection.py`及`test_mixed_gpu_selection.py`：严格选择Reader、质量/完整proof绑定、逐版本事实和叶检查；先RED后实现。仅允许无阻断成员，父全集报告保持原状态。
- [x] 2. 新增独立mixed worker及测试：使用上述Reader，复用Provider/RecoveryPacer/锁/干净恢复门禁，记录完整import闭包；旧脚本字节不变。无模型预检与真实运行明确分开。
- [x] 3. 由真实95版本audit生成新协议清单，在真实MySQL只读事务逐版本预检，核对全部输入/51文件不变，不启动模型。
- [x] 4. 聚焦测试、静态检查、独立复审；修复发现后归档脚本/输入SHA。旧进程存活时不得GPU验收，补算等待同进程正常完成排空退出；异常不自动重试。
- [x] 5. 更新项目现状与后续串行启动命令；实际完成才更新补算结果。保留全集发布及全产品目标。

入口实现/111测试/真实95预检/审查与接续文档完成。实际GPU补算仍未执行，当前worker活跃，必须等待其正常结束；上方复选项不表示CUDA或全集发布完成。最终证据见.sdd/mixed-gpu-supplement-verification.md。
