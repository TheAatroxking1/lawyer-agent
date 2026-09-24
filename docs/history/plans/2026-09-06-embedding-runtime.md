# S6c1 可复现 CPU Embedding 运行环境

## 依据和边界

S6b3 已通过520条合成向量多批次发布；当前 pyproject 未声明 sentence-transformers/Torch，Dockerfile 仅安装基础依赖，Compose根文件系统只读但没有权重挂载。不能用本机额外安装证明可部署。本机已有 Yuan 缓存快照 fb4ab1ed9d3447b64c79e305c8913340327668b5，Torch 2.14.0+cpu / sentence-transformers 6.0.1；本切片建立明确 CPU 基线，不声称已验证 CUDA 或生产容量。

## 全局约束

- 新增 embedding-cpu 可选生产依赖并同步 uv.lock，torch 的 CPU 源为官方 https://download.pytorch.org/whl/cpu、explicit=true；其它依赖继续原索引，不从PyTorch源解析通用包。保留原基础/开发依赖版本，禁止同步现有 backend/.venv。独立环境验证 --frozen --no-dev --extra embedding-cpu；未能安装必须记录实际错误，不能把lock成功当安装成功。
- backend镜像安装embedding-cpu，CPU为明确默认；保留非root、只读根目录和现有秘密装配。Compose以只读bind挂载显式权重缓存目录，不自动下载、不把权重打包到镜像。不要启动/停止共享Compose项目，镜像或运行验证使用专属名称和独立配置。
- Provider构造新增device默认cpu、local_files_only默认True；显式trust_remote_code=False，加载/推理错误维持稳定不可用行为。通过Settings/全部实际装配入口传递device和local_files_only；不使前端或租户输入控制本地路径/远程代码。显式device可指定cpu/cuda/cuda:N/mps，但CPU镜像与本轮只验CPU。
- 公网请求默认本地缓存或绝对模型目录，缺失权重直接失败，不暗中访问Hub下载。运维预备权重单独完成；生产建议用已核对的固定快照绝对目录作为model_ref，发布与查询配置完全一致。模型ref、维度、L2和已发布索引绑定不变；本切片不增加未绑定的revision参数。
- 提供离线模型smoke入口：显式实际model_ref、dimension、device，固定合成短文本经真实Provider/ModelGateway验证行数/维度/有限值/L2，不输出向量或用户数据。失败非零，不能跳过或替换为小模型；输出安全JSON报告，区分本机环境、冻结环境及容器证据。
- 实际测试只读现有Yuan快照，最多少量合成文本；不运行外部语料/付费模型，不切真实alias。本机CPU smoke不证明镜像/全量吞吐，GPU与同步模型执行的并发/超时边界另行验收。
- UTF-8，保留脏工作树；TDD、独立审查、范围匹配验证、更新现状和操作说明。现有长期用户授权允许必要安装/下载至隔离环境及专属镜像验证，无需重复确认。

## 任务

- [x] optional CPU依赖、官方源、锁文件与独立冻结安装。
- [x] Provider/Settings及HTTP、CLI装配的CPU/离线参数与测试。
- [x] 镜像和Compose只读缓存装配、操作说明及静态验证。
- [x] 真实Yuan离线smoke、可复现运行证据、独立复审和交接。

## 验证

uv0.12.7 锁解析89packages，既有60包版本集合完全未变。独立 `.superpowers/venvs/embedding-cpu` 使用Python3.12.4，`uv sync --frozen --no-dev --extra embedding-cpu --python 3.12` 实际安装78packages；Torch2.14.0+cpu/ST6.0.1导入与 `uv pip check` 通过，无pytest。没有同步原backend/.venv。

Provider/Settings装配60项聚焦测试、smoke7项、依赖3项、Compose12项通过；均观察新增需求的RED→GREEN。最终后端 `pytest tests/unit tests/api tests/contract -q --tb=short` **2331 passed / 1 skipped / 1 warning**（23.08s），Ruff全库通过，mypy **206源文件**通过。跳过为平台符号链接，警告为Starlette/httpx。

独立冻结Windows环境实际Yuan快照推理：3行/1792维，范数全部1.0，29.961s（含模型加载）。CPU镜像 `lawyer-agent-embedding-cpu:20260906` 成功构建，ID `sha256:c71314bb7adbc4c6b2bdd0fe18b03ee523ae0c9a49e490d7a26ccab09ed8670d`；镜像206个源文件哈希逐一匹配当前工作树。

Linux容器同Yuan快照实际推理：3行/1792维，L2范数0.9999999999999999..1.0，13.154s，Python3.12.14/Torch2.14.0+cpu/ST6.0.1；UID10001、network=none、只读root与权重bind、4CPU/4GiB限制，退出0且无OOM。专属验证容器已移除，未操作共享Compose。报告位于 `artifacts/embedding-runtime/windows-frozen-smoke.json` 与 `linux-container-smoke.json`。

Compose example `config --quiet` 通过。根 `.dockerignore`新增源码白名单，实际构建上下文2.01MB，排除语料/秘密/虚拟环境；前端独立context不受影响。镜像/依赖及Provider/装配/smoke完成独立交叉复审。固定快照路径与只读缓存操作写入 `docs/embedding-runtime.md`。

不把三短句测试当作吞吐、法律语义质量、GPU、完整API栈或生产验收；同步模型执行的超时/并发边界、真实检索与S4 A/B仍需后续推进。

参考官方文档：[uv/PyTorch索引](https://docs.astral.sh/uv/guides/integration/pytorch/)、[Sentence Transformers安装](https://sbert.net/docs/installation.html)、[Yuan模型卡](https://huggingface.co/IEITYuan/Yuan-embedding-2.0-zh)。模型卡示例为3.4.1，本轮对现有6.0.1组合必须实际验证兼容，不能从版本号推断。
