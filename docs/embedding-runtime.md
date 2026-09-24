# CPU Embedding 运行环境

后端镜像使用固定 uv 0.12.7，按锁文件安装生产 `embedding-cpu` Extra：`uv sync --frozen --no-dev --extra embedding-cpu`。该步骤安装运行依赖，不下载或打包模型权重。模型默认 `device=cpu`、`local_files_only=true`，Provider 禁用远程代码。CUDA/MPS 设备选项不代表本 CPU 镜像已支持或验证 GPU。

## 权重与只读挂载

Compose 的 `LAWYER_MODEL_CACHE_DIR` 是宿主机 Hugging Face 缓存根目录，对应容器 `/models/huggingface`；容器 `HF_HOME` 固定为该路径。缓存根目录通常包含 `hub/`，其中模型快照引用的 `blobs/` 也必须完整保留，不能只复制失去目标的快照符号链接。

默认宿主目录为相对 `deploy/` 的 `../artifacts/model-cache`。Compose 不自动创建该目录，防止生成意外的 root 所属目录；目录不存在时启动失败。操作者可从仓库根目录显式创建：

```powershell
New-Item -ItemType Directory -Path artifacts/model-cache -Force
```

空目录仍不具备推理能力。权重准备属于独立运维步骤；应核对来源、快照与完整性，再放入目录，或将 `deploy/.env` 的 `LAWYER_MODEL_CACHE_DIR` 指向已有完整缓存根目录。不要把权重提交到 Git。Linux 宿主目录需允许容器 UID 10001 读取文件、遍历目录；无需给容器写权限。

建议生产将 `LAWYER_EMBEDDING_MODEL_REF` 设为已复核的容器内固定快照绝对路径，例如：

```dotenv
LAWYER_MODEL_CACHE_DIR=C:/model-cache/huggingface
LAWYER_EMBEDDING_MODEL_REF=/models/huggingface/hub/models--IEITYuan--Yuan-embedding-2.0-zh/snapshots/fb4ab1ed9d3447b64c79e305c8913340327668b5
LAWYER_EMBEDDING_DIMENSION=1792
LAWYER_EMBEDDING_DEVICE=cpu
LAWYER_EMBEDDING_LOCAL_FILES_ONLY=true
```

上述快照路径必须实际存在且完整。路径以宿主/容器各自视角解释；宿主机执行 CLI 时不能直接使用容器 `/models/...` 路径。发布与检索必须使用相同模型、维度及 L2 配置。仅填写 Hub 模型名会依赖缓存内可变引用，不适合作为未经锁定的生产模型身份。

挂载始终只读，API 根文件系统保持只读、非 root 和原文件秘密装配。缺少本地模型时返回不可用，不自动从 Hub 下载。不要通过改成可写挂载或关闭离线设置掩盖缺少模型的问题。

## 并发、超时与关闭

每个 Provider 实例最多运行一个同步任务，没有等待队列。模型加载、编码和向量归一化都在实例惰性创建的单一自有 daemon 工作线程执行，顺序请求复用线程和已加载模型；并发请求立即收到 `model_provider_busy`，问答 HTTP 返回 503，SSE 发送稳定错误而不发送未完成答案。多个进程或 Provider 实例各自拥有模型与容量，部署容量应按实例数计算。

`timeout_seconds` 是等待模型任务的预算，超时返回 `model_provider_timeout`（504）。请求取消或超时后，已经运行的线程仍可能继续计算，容量保持占用直到实际任务结束；迟到结果不会作为该请求答案返回。线程无法安全强杀，永久挂起需要重启所属进程。需要逐任务强制终止时，应改用独立模型进程，不能靠释放并发槽继续堆积任务。

API 生命周期及发布 CLI、smoke 在退出时停止接单并最多等待 5 秒；未排空会记录不含文本和凭据的警告，不能理解为计算已经停止。该等待预算也不等于包含数据库审计、网络响应和关闭清理的端到端硬截止。Gateway 在取消时仍记录失败；审计存储迟滞可能延长取消交付时间，尚未增加独立审计超时协议。

## 验证边界

使用固定模型引用后缀 `#token-window-mean-v1` 时，Provider 按最多 512 token 的窗口编码，逐文本汇总窗口向量后执行 L2 归一化。构造参数 `window_batch_size` 控制单次编码的窗口数，默认 8，只接受 1..8 的整数；资源紧张的单实例可设为 2。此参数不改变文本覆盖、窗口顺序、维度或模型/缓存身份，只减少一次模型前向的窗口数量，不是整卡显存上限，也没有新增同名环境变量。普通非窗口模型引用不使用此参数分批。

本机 2026-09-06 接续脚本曾设为 2；实测仍触发 80°C 停止线，故全集向量缓存接续改用 CPU 单模型、4 线程。2026-09-07 的独立接续脚本进一步设为 1：只允许 CPU，启动前至少保留 4 GiB 可用物理内存，每批前后至少保留 2 GiB，可用提交容量至少 4 GiB；低于阈值停止并保留缓存。这是本机操作脚本的保守停止条件，不是 Provider 通用配置或内存硬上限。该结果和 GPU 限额的实际边界见 [GPU 排查记录](gpu-recovery-2026-09-06.md)。降低批次不能视为掉卡问题已经解决。

该单窗口 CPU 长任务随后观察到约 30 GiB 进程私有提交内存增长，已通过 STOP 正常排空。仅观察 Working Set 或系统可用物理内存不足以约束单进程私有提交量。2026-09-07 的受控变长对照支持将每调用新线程改为实例持久线程：最终执行器32次调用私有内存约2,542→2,700 MiB并趋稳，对应向量完全一致。随后真实有限256行（新增255/命中1）约65秒完成，末批私有提交量约2.67 GiB，缓存错误0、线程退出、CUDA未初始化。01:38 已据此恢复单一 CPU 长批次，每批前后增加私有提交量6 GiB停止线；启动前物理4 GiB、运行时物理2 GiB/提交4 GiB停止条件继续保留。GPU计算继续暂停；有限验证不能代表全集容量验收，活动进度和冻结代码范围见项目现状。

关闭按 monotonic 总预算异步等待真实线程退出，包含线程本地对象析构阶段；不得在事件循环使用无界 join。零预算只查询当时是否已退出。完整证据见 [CPU 内存诊断计划](history/plans/2026-09-07-cpu-memory-diagnosis.md)，保留首次复审失败与最终双通过记录。

独立生产环境安装与离线检查示例（在 `backend/`，不要覆盖现有开发虚拟环境）：

```powershell
$env:UV_PROJECT_ENVIRONMENT = '../.superpowers/venvs/embedding-cpu'
uv sync --frozen --no-dev --extra embedding-cpu --python 3.12
Remove-Item Env:UV_PROJECT_ENVIRONMENT
../.superpowers/venvs/embedding-cpu/Scripts/python.exe -m lawyer_agent.cli.embedding_smoke --model-ref C:/model-cache/huggingface/hub/models--IEITYuan--Yuan-embedding-2.0-zh/snapshots/fb4ab1ed9d3447b64c79e305c8913340327668b5 --dimension 1792 --device cpu
```

`embedding_smoke` 只使用三条固定合成短文本，经真实 Provider 与 ModelGateway 检查行数、维度、有限值和 L2；仅输出 JSON 指标，不输出向量，失败退出非零，无缺模型跳过或替换。它不读取业务 Settings、连接数据库或 OpenSearch。宿主与容器须分别执行，不能用宿主结果代替容器验收。

2026-09-06 实际证据：独立冻结 Windows/Python3.12.4 环境的 Yuan 快照推理通过（3行/1792维/L2，29.961s，含加载）；Linux/Python3.12.14 镜像同快照也通过（13.154s），UID10001、断网、只读根和只读权重，4CPU/4GiB限制，无OOM。使用 Torch2.14.0+cpu、sentence-transformers6.0.1。一次三短句测试不能作为吞吐基准或法律质量评测。

接入有界后台执行后的复测也通过：Windows相同冻结环境3行/1792维/L2，14.317s，执行期间449次事件循环heartbeat、并发拒绝、关闭排空及关闭后拒绝；重建Linux镜像后同快照3行/1792维/L2，13.744s，保持上述隔离条件。分别见 [后台执行报告](../artifacts/embedding-runtime/windows-bounded-real.json) 与 [容器复测报告](../artifacts/embedding-runtime/linux-bounded-container-smoke.json)。并行工作负载与缓存状态不同，不能用两次耗时比较性能；受控单测另验证超时/取消和关闭竞态。

可在仓库根目录检查配置语法，不启动服务、不输出秘密配置：

```powershell
docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml config --quiet
```

静态契约测试与 Compose 配置解析只能证明依赖安装指令、参数和挂载声明正确，不能代替干净冻结安装、镜像构建或实际模型推理。现有宿主虚拟环境也不能作为镜像依赖齐全的证据。实际运行验证及未通过项记录在本切片实施计划和项目现状中。

依赖源配置依据 [uv 官方 PyTorch 指南](https://docs.astral.sh/uv/guides/integration/pytorch/)；模型加载方式参考 [Yuan 模型卡](https://huggingface.co/IEITYuan/Yuan-embedding-2.0-zh)。模型卡的旧版本示例未直接作为兼容证明，上述6.0.1组合已实际加载和推理。
