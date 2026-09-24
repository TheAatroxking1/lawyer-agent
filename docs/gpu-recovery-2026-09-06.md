# 2026-09-06 本机 GPU 掉卡排查

## 最新运行状态：2026-09-07 11:42

用户明确允许适量GPU并要求提速，已从原CPU任务正常排空切换到温度自适应GPU接续。活动PID51652，使用项目已有 `.superpowers/venvs/embedding-cuda/Scripts/python.exe`；报告 `artifacts/legal-corpus/full-release/v3-cache-gpu-adaptive-20260907T034110Z/summary.json`。未改驱动、BIOS、超频或TDR配置。旧CPU failed 记录的异常链是operator_stop，不是再次掉卡。

两轮各300秒实际向量接续：首轮新增2237行/7.443行每秒，峰温69°C；改用直接只读NVML遥测后，第二轮新增4328行/14.412行每秒，峰温77°C、NVML used峰值2760MiB、冷却24秒。两轮均无新增相关事件或缓存错误，正常排空。CPU近期实际新增约4.253行每秒，第二轮观察约3.39倍；不同输入的运行观察不是严格同样本基准，不承诺长时间稳定或固定全集完工时间。汇总 `artifacts/legal-corpus/full-release/adaptive-gpu-validation-20260907T034051Z.json`。

持续运行保持FP32权重和原窗口/L2/缓存身份，window2、外层8；每个GPU微批检查温度、显存、OEM均衡零偏移和时钟，74°C暂停降温、68°C恢复，80°C仍停止，每25秒检查新增WHEA/显示事件。PyTorch分配器25%约3GiB只限制其缓存分配器，另检查整卡used不超过5GiB、free不少于6GiB；[PyTorch文档](https://docs.pytorch.org/docs/main/generated/torch.cuda.memory.set_per_process_memory_fraction.html)。[NVML](https://docs.nvidia.com/deploy/nvml-api/nvml-api-reference.html)是SMI使用的底层接口，本工具只读不改设置；本机NVML v1 used比SMI多计约286MiB驱动保留，报告明确口径。保护触发会保留断点，不自动反复重启GPU任务。这些运行控制不构成硬件掉卡根因已解决的证明。

停止当前任务可在其报告目录创建 `STOP` 文件，等待provider排空及进程退出后再接续；微批中的STOP可能被Provider包装为failed，应结合exception_chain识别人为停止。缓存写失败、损坏读取或空来源不得前移完成断点；该边界已通过真实缓存适配器故障注入和独立复审。活动运行文件摘要保存在summary，计算过程中保持不变。

## 已核对的事实

- 本机为机械革命旷世 16 Super（GM6PX8X），RTX 4080 Laptop GPU，NVIDIA 610.62。本文只记录这台机器的运行条件，不改变项目 CPU 生产基线。
- 用户确认：两次异常重启均为 GPU 掉卡后手动强制重启；曾设置过超频。替换电源标称 19.5V / 16.9A、330W、圆口；尚未验证其实际供电稳定性。
- 用户补充：此前外接显示器玩游戏也会突然无信号，笔记本内屏同时无法操作，必须强制关机重启。故障不限于本项目或 CUDA。用户随后确认：更换电源后正常使用了几个月，近期才开始出现；此时间关系提示供电值得排查，但不足以归因于适配器。
- 10:02:45 系统记录 PCIe Root Port 的 WHEA 17，10:05:45 记录 Display 4101（nvlddmkm 停止响应后恢复）。这些事件支持继续排查驱动、调优和硬件链路，不能单凭事件认定硬件损坏。
- 通过 PnP 父设备关系确认 RTX 4080 的父端口正是事件中的 `PCI\\VEN_8086&DEV_A70D&SUBSYS_12521D05&REV_01\\3&11583659&0&08`，GPU 路径为 `PCIROOT(0)#PCI(0100)#PCI(0000)`。最近七天查到两条 WHEA 17 和一条 Display 4101；日志范围不能证明此前未发生用户描述的故障。
- MSI Afterburner 保存了旧超频配置，但主面板显示核心、显存偏移均为 +0。该面板不能单独证明显卡处于原始频率；驱动曾报告显存 9501 MHz，而标称上限为 9001 MHz。
- 机械革命控制台实际启用“狂暴模式”。其 `Mode3_Profile1.json` 的核心偏移为 +100 MHz、显存偏移为 +500 MHz，TGP 配置为 175W；这一显存偏移与驱动读数一致。

## 本次调整

通过机械革命控制台切换到“均衡模式”。随后读取 OEM 配置，确认 `Mode2_Profile1.json` 为激活状态，核心和显存偏移均为 0，狂暴模式为未激活。实际模型推理中的显存频率回到 9001 MHz。

没有安装或卸载驱动，没有修改 BIOS、TDR 注册表值或 Windows 安全设置。已存在的 `TdrDelay=10`、`TdrDdiDelay=60`、`HwSchMode=1` 和关闭的 PCIe 链路省电设置保持原样。

Afterburner 原配置和系统事件保存在 `.superpowers/backups/gpu-recovery-20260906/`，该目录不提交 Git。恢复默认的 UI 点击不作为解除所有调优的唯一证明；上述 OEM 配置和实际时钟才是本次主要核对依据。

当前 `oem157.inf`（原 INF 为 `nvtfi.inf`）经 `pnputil /export-driver` 成功导出至 `.superpowers/backups/gpu-recovery-20260906/driver-610.62/`：215 个文件、2,817,613,051 字节。导出目录的 `NV_DISP.CAT` 签名状态为 Valid，SHA-256 为 `4eea115d9cf8c27cedc072ff54e2bc2d6f9dfdbc24774e2f964aa49d5311b72d`；`nvtfi.inf` SHA-256 为 `13a3ef22d3386efae369ed8d074bfd08c3c81fe153fda612095a5b91cc013a17`。这是回退材料准备，未执行恢复或驱动替换。测试停止后一次空闲采样为 55°C、15.14W、显存 810 MHz。

## 验证与恢复条件

验证使用隔离 CUDA 环境、固定 Yuan 权重及 `#token-window-mean-v1`，每批 8 行真实样本文本，不调用付费服务、不改写数据库或索引。脚本逐批核对配置、向量数量/维度/有限值/L2、GPU 温度和时钟，每约 25 秒检查新增 WHEA/显示驱动事件。温度达到 85°C、时钟超过标称上限、新增相关事件或模型异常时停止。

第一轮 30 秒连续阶段通过：60 批、480 行，峰值温度 82°C、采样峰值功率 92.92W、最高显存频率 9001 MHz，无新增相关事件，Provider 有界关闭完成。报告为 `.superpowers/backups/gpu-recovery-20260906/balanced-validation-20260906T025939Z/summary.json`。包括模型冷启动在内共 70.609 秒。

第二轮目标 120 秒、每批间隔 0.25 秒；在 616 行后达到预设 85°C 停止线，主动结束，Provider 正常关闭。这是温度保护触发，不能记作再次掉卡或完整通过。报告为 `balanced-validation-20260906T030129Z/summary.json`。

第三轮将每批间隔延长至 1 秒，120 秒阶段通过：94 批、752 行，峰值温度 82°C、采样峰值功率 64.42W，无新增相关事件，Provider 正常关闭。报告为 `balanced-validation-20260906T030405Z/summary.json`。

第四轮以相同条件计划执行 300 秒，在约 205 秒、1,280 行后达到 85°C 停止线，Provider 正常关闭，没有完成五分钟验证。报告为 `balanced-validation-20260906T030630Z/summary.json`。未观察到再次掉卡，但每批间隔 1 秒不足以证明长时间运行稳定；四轮测试均已结束，不再重复施压。85°C 是本次保守停止阈值，不是已经确认的故障温度或硬件损坏证明。

尝试通过 `nvidia-smi -i 0 -lgc 300,1500` 限制核心频率时返回权限不足，未生效，不得将其记为已完成的降频措施。

本机缓存预计算脚本 `.superpowers/sdd/warm_v3_embedding_cache.py` 已接入 `local_gpu_run_guard.py`，每批限制 8 行并间隔 1 秒，模型等待上限 60 秒（等待结束不代表线程强制终止）。首次计算前和每批前后核对均衡模式、零偏移、有效温度及显存时钟；达到 85°C 或异常时停止，保留缓存和运行报告。新增本次输出目录 `STOP` 文件可在下一批前停止。反向检查验证狂暴/非激活配置、显存 +500、85°C、非法遥测均被拒绝；真实当前状态回读通过。该脚本属于当前机器的忽略目录操作工具，不是跨设备生产配置。

全集向量任务暂不自动恢复。已解除实际 OEM 超频，但根因尚未确定，供电、散热、驱动及硬件链路仍需区分；不能把短测通过写成 GPU 已修复。后续恢复前须确认仍处于零偏移的均衡模式，并取得足够持续验证证据。

浏览器核对了 [NVIDIA Studio 616.56 官方页面](https://www.nvidia.com/en-us/drivers/details/278087/)（2026-08-26、WHQL），但该具体页面提供桌面版下载，所见支持列表为桌面型号；未确认本机笔记本安装包适配，因此没有下载执行或安装。版本较新和 WHQL 标签本身不能证明修复本次故障。

下一项有区分度的现场检查：由用户或售后提供已知正常、匹配本机型号/电压/极性/接口/功率的原厂适配器进行对照，并检查风扇、风道和散热接触。当前工具不能测量适配器带载输出或检查内部散热，需现场完成；保持均衡零偏移，不以连续压力测试替代这些检查。

## 后续软件修复准备

用户要求继续实际解决，软件侧接续驱动更新排查。[616.56 官方发布说明](https://us.download.nvidia.com/Windows/616.56/616.56-win10-win11-nsd-release-notes.pdf) 第 32 页明确列出 RTX 4080 Laptop GPU，第 34 页说明笔记本安装前应备份当前配置。已找到官方 notebook 安装包链接，但本机访问会跳转 NVIDIA 中国 CDN 并返回 HTTP 403，没有得到可运行文件。

随后打开已安装的 NVIDIA App，将驱动通道从 Game Ready 切至 Studio；应用为本机提供 616.56（2026-08-26），已点击下载。当前仅为下载准备，未安装，运行驱动仍须按安装后的实际回读验收。保留均衡零偏移及旧驱动备份，避免同时改动 BIOS、显示模式或供电参数而无法区分效果。

11:33 左右 NVIDIA App 完成约 756.5 MB 下载及程序包准备，页面显示“安装”。实际文件位于 `C:\ProgramData\NVIDIA Corporation\NVIDIA app\UpdateFramework\ota-artifacts\crd\post-processing\329a26965dc5adbc16fa25f6f16033b8\`；`setup.exe` 与 `Display.Driver\NV_DISP.CAT` 均为有效签名，安装程序签名主体为 NVIDIA Corporation。`Display.Driver\nvtfsi.inf` 含本机 `DEV_27A0&SUBSYS_12521D05` 硬件匹配。安装程序 SHA-256 为 `e275b90a99037562e8b26f6c690e64fab1802957d91e8852ba6b527659f71692`。

安装前已保存 `before-driver-update.json`，当时近三小时相关事件仍为三条。已请求用户确认实际执行自定义全新安装、重置 NVIDIA 驱动配置，尚未点击安装；若需要重启，应先提示用户保存工作。安装后先回读驱动与 PnP 状态、检查显示及新增事件，再做低负载 CUDA/真实模型验证，不直接恢复全集。

## 晚间项目接续与低显存验证

用户随后要求继续推进项目并减少显存占用。接续时 `nvidia-smi` 已报告 616.56，不能据此补写为本代理执行了安装。模型窗口批量从默认 8 调到 2，保持原始 FP32 权重、完整 512-token 分窗、均值/L2 策略；本机 PyTorch CUDA 分配器使用 0.5 比例上限（约 6 GiB），不是限制整机或全部 CUDA 外部分配的硬上限。每批还检查整卡已用不超过 8 GiB、剩余不少于 3 GiB，并将停止温度降为 80°C。

报告 `low-memory-validation-20260906T141245Z/summary.json`：计划 120 秒阶段，632 行后触发 `local_gpu_temperature_stop_80c`，Provider 正常关闭，总耗时 122.688 秒含冷启动/退出。采样整卡显存约 2.6 GiB，PyTorch 峰值 allocated 1,302.43 MiB / reserved 1,344 MiB。显存占用已受控，但温度仍触发停止，不能声称防止掉卡或根治；没有继续 GPU 长任务。

向量缓存脚本改为默认 CPU，只有显式 `cuda:0` 参数才加载 GPU；当前运行 CPU 单模型、4 线程，复用同一固定模型身份与缓存，避免继续显卡施压。`local_gpu_run_guard.py` 新增显存保留与 80°C 停止检查，5 个操作工具单测先红（4 失败）再全通过。后端微批参数改动 78 个聚焦测试、Ruff/mypy 与独立审查通过。

## 语料任务的中断边界

2026-09-07 00:22，CPU 接续任务在 431 个来源后报 `ModelProviderUnavailable → RuntimeError`，底层栈位于线性层；已保存 18,500 行缓存，本轮实际新增 7,055 行，Provider 正常排空，CUDA 未初始化。同一时段一次 PowerShell 默认配置启动出现 .NET 双映射内存释放失败；后续无配置启动正常，不能仅凭同时发生就认定二者同因。旧向量任务已经退出，没有并行加载第二模型。

对精确失败批次的 8 行，以 CPU、窗口批量 1 单独复测通过并正常关闭，报告 `artifacts/legal-corpus/full-release/cpu-second-failure-diagnostic-20260907.json`，尚未复现长任务根因。00:27 启动独立 `warm_v3_embedding_cache_cpu_guarded.py`，保留原失败脚本和报告，禁止 CUDA 参数，增加物理/提交内存停止阈值及不含正文的异常分类。新报告 `v3-cache-warm-20260906T162731Z/`；已越过原失败位置，持续稳定性仍须观察，不能据此宣称显卡或系统故障已修复。

00:44 左右，新的单窗口 CPU 任务仍出现显著私有提交内存积累，停止前采样约 30 GiB；根代理写 STOP 后正常排空，510 个来源、21,723 行缓存保留。当前 CPU 和 GPU 均无模型计算，未宣称硬件故障已修复。另查到本地 OpenSearch 持续写入大量安全审计记录，占较多内存/CPU，已正常停止本项目搜索容器、保留卷及全部索引，MySQL 保持运行。先排查这两项资源异常，再恢复相应长任务和搜索服务。

第二次手动重启中断了第三批入库，`import-v3-batch-003.jsonl` 已保存 1,702 份成功文件记录，但没有完整 summary。不能改写为已完成报告。恢复入库时根据事实库和保留的成功记录重新选择未处理来源，使用新的输出清单和报告。已有向量缓存保留，GPU 修复优先于全集入库、向量化和发布。

## 2026-09-07 01:39 CPU 接续与资源修复

GPU 计算保持暂停。受控 CPU 对照证明复用单一工作线程可显著减少本机变长编码时的私有内存累积；已修复生命周期边界并独立复审，32次模型对照向量一致，有限真实256行新增255条、缓存错误0、CUDA未初始化。恢复 CPU4线程/window1 接续并增加进程私有提交量6GiB停止线，不再只看工作集和可用物理内存。OpenSearch 自审计 Bulk 路径在修改后有限时间窗计数为0、业务审计保留且无正文；其容器当前正常停止，后台总写入/峰值资源仍待另行长观察。以上软件资源改进不代表 RTX4080Laptop 的硬件掉卡已根治。活动报告及验证边界见 project-status.md。
