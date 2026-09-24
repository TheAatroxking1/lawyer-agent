# 温度自适应GPU接续

用户已明确允许适量GPU并要求优先提速。仅调整本机运行工具，保持现有Provider、FP32权重、1792维、512 token窗口均值/L2和缓存身份，原件/数据库/索引不改。CPU与GPU模型串行切换；不改驱动、超频、TDR或硬件功率设置。

- [x] 读取实时GPU、OEM均衡零偏移、当前缓存进度及近期CPU实际新增吞吐。
- [x] 新建忽略目录操作工具与小型策略测试；先验证测试失败，再实现74°C暂停/68°C恢复的滞回，80°C仍停止。每个模型微批次检查，不仅每个外层8行批次；温度/显存/时钟/OEM异常、新增WHEA/显示事件或STOP停止。
- [x] 正常停止CPU并核对provider排空、线程和进程退出，保存断点。新GPU进程采用window2、PyTorch分配器25%上限（约3GiB，非整卡硬上限），整卡已用超过5GiB或剩余不足6GiB则停止；保留宿主内存停止线。
- [x] 先在真实剩余来源上执行300秒有限接续，生成的有效向量直接缓存。保留逐批吞吐、温度峰值、显存峰值、冷却时间及异常记录；不能将有限通过宣称为硬件根治或全部长任务稳定。
- [x] 有限验证通过且较CPU近期窗口有提速后，用同一已验证工具启动持续接续，前序完整来源计数来自同一选择集、已排空报告；部分来源通过缓存命中接续，独立记录继承计数与本轮缓存命中/新增，不将重复缓存访问当成新向量。
- [x] 更新项目现状、GPU运行说明及活动报告路径，报告实测收益与范围；任何失败先保留缓存和证据，不能自动反复启动GPU施压。

实现位置：`.superpowers/sdd/adaptive_gpu_policy.py`、`test_adaptive_gpu_policy.py`、`warm_v3_embedding_cache_gpu_adaptive.py`。旧CPU脚本和4个provider相关runtime保持冻结，不重跑与本机操作无关的全后端测试。

## 本次复核与追加优化

- 初次启动误用backend CPU环境，在分配器初始化处失败，GPU微批/模型调用/缓存写入均0；原报告保留。实际使用既有 `.superpowers/venvs/embedding-cuda/Scripts/python.exe`（torch2.14.0+cu130），工具新增CUDA构建预检及环境记录。
- CPU停止点4030来源，173484缓存访问行，provider已排空、线程和进程退出。原脚本将operator_stop记为failed，异常链明确为人为切换，不是掉卡。最近599.51秒新增2550行，即4.253行/秒。
- 第一轮300.547秒新增2237行，7.443行/秒，峰温69°C、无新增相关事件、缓存错误0，正常暂停/排空。该轮实际来源完成记录全为非空，未发生下述复审发现的故障。
- 独立复审发现写失败或空叶子可能仍前移断点；新增 `adaptive_gpu_checkpoint.py`，在继承统计、取得叶子、批结果计数前、来源完成前失败关闭。4项测试先红5fail再通过；实际CacheProvider注入原子替换OSError及实际空叶子probe通过，临时目录清理、无模型/DB/生产cache写入。
- 实测旧逐微批SMI子进程检查耗时约68.9ms；新增 `local_nvml_telemetry.py` 直接只读驱动采样约0.075ms，仍逐微批执行全部检查。与SMI温度/功率/时钟对照一致；NVML v1 used多包含约286MiB驱动保留量，报告注明口径，free一致。只使用初始化/读取/关闭API，没有设备设置。接口依据：[NVIDIA NVML](https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html)。
- 最终共13项操作测试、py_compile及实际故障probe通过。独立复审原P1/P2关闭，见 `.superpowers/sdd/adaptive-gpu-review.md`。第二轮采用同一window2/25%分配器/原阈值继续300秒真实接续；结束后核对并启动相同工具长运行。


最终：第二轮300.312秒新增4328向量、14.412行/秒，峰温77°C、NVMLused2760MiB、冷却24秒，事件/缓存错误0、runtime一致且正常排空。11:41使用相同代码从4166/6127启动持续GPU接续，PID51652，报告v3-cache-gpu-adaptive-20260907T034110Z；实测提速动作完成，全集/项目目标继续。
