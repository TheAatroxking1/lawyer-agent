# S6c2 有界本地 Embedding 执行

## 问题与目标

S6c1 真实CPU模型已在冻结环境及容器运行。Provider 当前 async embed 内同步加载/编码/归一化，阻塞事件循环，timeout_seconds 未参与执行控制。将同步操作移出事件循环并限定资源占用，保持所有模型/维度/L2/离线与质量发布边界。

## 全局约束

- 每个Provider最多一个同步执行任务，无等待队列。整个加载、编码、结果转换/归一化在该任务中完成；参数/输入在提交前验证并快照为不可变tuple，不让调用者后续修改影响后台任务。
- 使用专属daemon线程和concurrent Future桥接，不用默认执行器/无限任务队列，不持久空转线程。忙时立即ModelProviderBusy（503/model_provider_busy）；同一Provider的顺序调用重用惰性加载模型。
- timeout_seconds是请求等待预算：有限正数，async超时返回ModelProviderTimeout；取消传播CancelledError。超时或取消不得释放后台任务容量，直到同步函数真正返回；丢弃迟到结果并消费迟到异常，不能变成成功回答或未观察Future异常。跨event loop重复使用不绑定失效loop。
- 线程不能安全强杀；不得声称超时已终止CPU/GPU计算。任务永不返回时该Provider保持busy，需进程退出/运维重启，不能释放容量继续堆积任务。严格逐任务强制终止需要未来独立模型进程边界。
- `aclose(timeout_seconds=5.0)->bool` 先禁止新任务，再有限等待当前任务；True表示已排空，False表示仍有同步任务。幂等，不重开；调用关闭被取消仍保持关闭状态。daemon线程不成为无限解释器退出等待条件，未排空必须如实记录。
- API资源生命周期、集合/单文件发布与smoke拥有并关闭自己创建的Provider；无Key/quality --check不创建模型资源。异常/取消均走清理，既有DB/Redis清理不可因模型清理失败而跳过。
- Gateway记录Embedding取消为失败后继续传播取消；HTTP与SSE对busy返回稳定503，对timeout保持504，不流出未完成法律正文。现有租户/账号授权、Evidence/Citation Gate不变。
- 等待预算只约束Runner等待，不是包含数据库审计/HTTP响应/关闭清理的端到端硬截止。Gateway取消后仍等待失败记录；审计存储迟滞可延长交付取消的时间，独立审计超时协议留作后续运行时工作，不宣称本切片已解决。
- 用threading.Event确定性控制阻塞编码，测试事件循环仍推进、并发拒绝、timeout/cancel后仍busy、完成后可再次调用、late error无警告、跨loop、关闭等待/拒新与线程启动失败。不得靠长sleep或真实模型制造挂死。
- 保留UTF-8脏工作树，TDD和独立复审。不操作共享栈；真实Yuan只读少量smoke并核对镜像源码，不能据此宣称完整吞吐或生产就绪。

## 接口与任务

- [x] `BoundedEmbeddingRunner.run(operation, *, timeout_seconds)`、`aclose`及稳定busy错误/取消记录。
- [x] Provider提交完整同步工作、输入验证与生命周期转发。
- [x] API/CLI/smoke资源关闭，HTTP/SSE忙与超时协议验证。
- [x] 独立复审、后端检查、真实CPU验证与交接。

## 验证

Provider新需求先观察15失败/2通过（同步线程ID、可变输入、无效预算、取消/超时与供应商转换异常），接入后通过。Runner和Gateway先复现缺模块、取消漏记录及字符串被拆成字符批次，再修复。线程测试由Event控制，覆盖单任务、跨loop、超时/取消容量保留、启动失败、有界关闭和迟到异常。

独立复审发现并RED复现两处关闭竞态：loop在检查与投递间关闭导致回调日志异常；投递异常后loop停止，下一轮消费未执行导致未观察Future警告。改为自有单向Future桥接并在投递异常后立即标记已观察；不取消源Future，不改变等待者获取原异常。最终四组85项聚焦回归通过。生命周期接线经独立只读复审通过，关闭失败/取消不跳过Redis/engine清理，--check不创建Provider。

最终后端 `.venv/Scripts/python.exe -m pytest tests/unit tests/api tests/contract -q --tb=short`：**2388 passed / 1 skipped / 1 warning**（29.05s）；Ruff全库通过，mypy **208源文件**通过。跳过为平台符号链接，警告为Starlette/httpx。`tests/integration/mysql/test_reviewed_set_publication_e2e.py` 在专属MySQL/OpenSearch上 **2 passed**（15.08s，0skip），使用真实线程Provider及合成encoder；这两项不算真实模型检索验收。专属服务和临时凭据均已删除，未操作共享Compose。

独立冻结Windows环境真实Yuan快照、CPU/离线三短文本：3行/1792维/L2，14.317s（含加载），模型执行期间事件循环heartbeat推进449次，并发拒绝、关闭排空和关闭后拒绝均通过。报告 `artifacts/embedding-runtime/windows-bounded-real.json` 绑定Provider与Runner源码SHA；ticks和耗时不是响应延迟或吞吐基准。

Linux镜像已重建：`lawyer-agent-embedding-cpu:20260906`，ID `sha256:188630385d8fad7a8a7ba541cc1997f80a9dd8e90c06b2a126c14f7914dd8b1d`；208个源文件SHA全部匹配工作树。同快照实际推理3行/1792维/L2，13.744s，Python3.12.14/Torch2.14.0+cpu/ST6.0.1。UID10001、network=none、只读root/权重bind、4CPU/4GiB，退出0、无OOM，专属容器已移除。报告 `artifacts/embedding-runtime/linux-bounded-container-smoke.json`，保留S6c1历史报告不覆盖。

未改变依赖锁或同步原backend/.venv，未导入真实语料、调用付费模型、切换dataset_v1或运行前端业务验收。下一步统一实际模型检索集成的显式配置（旧测试硬编码BGE/512且缺模型自动skip），验证真实Embedding到搜索/权威条文回填，再推进真实条文S4 A/B；本切片不代替法律审核、全量容量或生产验收。
