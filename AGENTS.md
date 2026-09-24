# 律师 Agent 仓库开发指引

## 语言与文档职责

- 适用于整个仓库。源代码、配置、迁移、测试、文档、Prompt、Fixture、日志和生成文本统一使用 UTF-8；PowerShell 文件读取显式指定编码。
- 默认使用中文沟通；代码标识符、命令和通用技术术语保持原样。
- 日常维护先看 [README.md](README.md)。本文件只保存跨任务长期规则、代码导航和验证入口。项目进度与交接维护在 [docs/project-status.md](docs/project-status.md)，当前实现计划在 `docs/superpowers/plans/`，旧计划和过程记录在 [docs/history/](docs/history/README.md)。归档不代表要求失效或功能完成。
- 精简维护入口：现状文档仅保留当前能力、缺口、最近验证与下一步，不追加逐次流水账；优先更新对应说明，仅在行为或架构实质变化时新增规格/计划。
- 不再向本文件追加逐次交付日志、测试计数、模型下载过程或运行索引名。旧阶段原文已移至 [历史归档](docs/history/agents-stage-history.md)，无需每次加载。

## 开始工作与决策顺序

1. 查看 `git status --short` 和相关 diff，辨认当前分支及用户未提交改动；保留这些改动，不覆盖、不擅自提交。
2. 阅读 [项目现状与续做入口](docs/project-status.md)，找到当前主线、已知缺口和本次验证边界。
3. 阅读 [总体架构规格](docs/superpowers/specs/2026-08-31-lawyer-agent-enterprise-architecture-design.md)，再按任务读取相关已批准规格、实施计划及生产复核记录；不无差别加载全部历史计划。
4. 检查实际代码、装配、迁移、测试与部署配置。区分“设计要求”“代码存在”“已接入入口”“本次运行验证”“生产验收”，不能用其中一个代替另一个。
5. 决策优先级：用户当前明确指令 → 已批准功能规格 → 当前适用实施计划 → 本文件。代码用于证明现状，不能因现有缺口而降低已批准安全要求。
6. 文档日期和历史“已完成”不是验收依据；按内容、Git 历史和执行证据确认。发生实质冲突时明确报告，不能静默改写已批准边界。

在已批准范围内自主完成必要实现与验证。不得仅因存在另一种实现方式重新讨论既定架构；只有缺失决策会实质改变行为、安全、兼容性、成本或范围时才询问。用户说“继续完善”时，先核对续做入口，选择有明确验收条件的增量，避免重做已完成切片。

## 产品边界与架构

- 产品为中国大陆法律服务的公网多租户 SaaS，面向企业法务、律所及客户、法学教育。除非用户明确要求并清楚分隔比较法内容，否则不得混入外国法结论。
- 首期由各律所租户提供律师，平台提供软件、AI、检索、工作流和复核控制，不以平台自身名义作为受托律师。
- 七类目标能力：法规问答、合同审查、文书起草、私有知识问答、专题合规、案件/咨询管理、正式报告。目标清单不代表全部已实现。
- Python 3.12 为后端基线；FastAPI、Pydantic v2、SQLAlchemy async、Alembic、uv；前端 Vue 3、TypeScript、Vue Router、Vite。实际依赖范围与版本以项目清单和锁文件为准。
- 模块化单体业务核心加独立 Worker；业务规则放后端。MySQL 是事实源，OpenSearch 是必选混合检索层，Redis 承担缓存/限流/短状态，RabbitMQ 传任务引用，MinIO/S3 承担不可变对象存储。
- 合同网页审查采用 LlamaIndex + 用户指定的自定义 `ReActAgent(Workflow)` 四步事件循环，并通过受控 MCP 接入文档服务；合同入口按用户确认使用Qwen多模态看图，PDF原生字形只辅助引用定位，不调用规则表格/OCR，见[合同批注规格](docs/superpowers/specs/2026-09-16-contract-web-annotations-design.md)；此场景覆盖原 LangChain 方向，其它场景仍按适用规格，不能推断已整体迁移。当前接入程度见现状文档。LangGraph 仅用于需要持久状态、分支、暂停恢复或人工中断的流程。不得为简单流程增加无用结构。
- 模型、Embedding、Reranker 与外部身份 Provider 位于项目自有接口之后；供应商 Payload、凭据、模型名及配置集中在适配器/装配层。
- 本地 Embedding 生产依赖由 `embedding-cpu` Extra 与锁文件管理；默认 CPU、仅本地权重、禁用远程代码。权重缓存只读挂载且须预先准备，固定快照模型路径与发布/检索配置一致；安装、缓存与离线验证见 [运行说明](docs/embedding-runtime.md)。
- 同步模型加载/编码不得阻塞事件循环；按 Provider 限制实际运行任务，取消或超时只结束等待，实际计算结束前不得释放容量。资源拥有方必须有界关闭；不能把等待超时宣称为线程计算已终止或全请求硬截止。
- 本机长批模型任务同时核对显存、系统可用内存与进程私有提交量；低显存或小样本通过不代表持续稳定。发现资源持续增长时先正常停止并保留缓存/诊断证据，通过有上限的对照实验定位后再接续，不能反复重启任务掩盖问题。
- MCP 走受控 Client Gateway；合同网页审查按专项规格接文档 MCP 服务，其它场景仍不默认依赖具体外部 MCP Server。登记工具与授予白名单权限必须分开，模型不能自行声明租户或提升权限。
- 本地使用 Docker Compose，生产按 Kubernetes 设计。支持时使用非 root、只读根文件系统及最小权限。10,000 RPS 是轻量接口/任务受理目标，必须压测验收，不代表每秒完成同量模型推理。
- Redis 安全控制仅支持 Standalone 或 Sentinel/HA Primary；现有多 Key Lua 协议不可直接用于分片 Redis Cluster。

## 代码导航

| 位置 | 职责 |
| --- | --- |
| `backend/src/lawyer_agent/domain/` | 领域对象、授权规则、状态机 |
| `backend/src/lawyer_agent/application/` | 用例编排、端口、证据门禁、模型与工具网关 |
| `backend/src/lawyer_agent/api/v1/` | HTTP/SSE 契约；装配在 `api/dependencies.py` |
| `backend/src/lawyer_agent/infrastructure/` | 数据库、Redis、消息、对象、解析、搜索与 Provider |
| `backend/src/lawyer_agent/cli/`、`workers/` | 管理入口、语料发布、异步任务进程 |
| `backend/alembic/versions/`、`backend/tests/` | 数据迁移；unit/api/contract/integration 测试 |
| `frontend/src/api/`、`auth/`、`views/` | 类型化客户端、账号/租户会话、页面 |
| `deploy/`、`scripts/` | 容器、配置模板、开发与迁移脚本 |
| `docs/superpowers/specs/`、`plans/` | 已确认规格与当前切片计划；旧计划在 `docs/history/plans/` |
| `docs/project-decisions/pending-production-reviews.md` | 上线前必须完成的安全与运营复核 |

## 多租户与授权不变量

- 一个自然人对应全局用户，可有多个租户成员身份。每个租户请求必须有显式上下文，独立验证身份、有效成员关系、权限和资源范围。
- 私有表包含 `tenant_id`；租户唯一约束及可防止串租的外键包含 `tenant_id`。Repository/Service 必须要求租户上下文，不能暴露无范围的私有资源 `get_by_id(id)`。
- 私有 OpenSearch 查询包含租户 Routing 与 Filter；Redis Key、缓存、锁、配额、Stream 带租户范围；对象 Key 带租户前缀，只授予短时单对象访问。
- 队列只传标识符与签名上下文；Worker 从 MySQL 重新加载权威租户、权限与资源状态，不信任消息中的资源地址。
- RBAC 检查动作，ABAC 检查部门、Matter 团队、密级、创建人、委托、资源状态与审批状态。外部客户/学生仅访问显式共享资源；仅有租户成员资格不等于所有业务权限。
- 超级管理员访问租户内容必须指定租户/资源范围、事由、工单、有效期，经过二次认证、短时特权授权与完整审计；禁止无限制隐式通道。
- 每新增或修改租户 Repository、API、缓存、搜索、对象、队列或 Tool 路径，均验证同租户允许与跨租户拒绝。拒绝响应不能泄露其它租户资源是否存在。
- 前端账号 Token 与租户 Token 分离；不能用前端路由守卫替代后端鉴权。登出、切租户和过期会话必须避免残留私有状态。

## 法律证据与 AI 输出

- 重要法律结论必须追溯到法规身份、版本、条文、制定机关、公布/施行日期、效力状态、法域/地域和来源；保留原件哈希及数据集/Parser 版本。不知道来源 URL 时记录来源系统/文件，禁止拼造链接。
- 检索形成显式 Evidence Bundle；每项 Claim 引用本次允许的 `evidence_id`，发布前经过后端 Citation Gate。引用 ID 有效不等于条文在语义上支持结论，仍需法律质量评测。
- 证据缺失、冲突、失效、状态不明或超出日期/地域时，拒答、缩小范围或转人工。不得把模型记忆呈现为已核验法律结论。
- 明确区分现行法、历史法、草案、司法材料、租户制度、客户文件、教学材料与模型生成文本。
- 正式法律意见、诉讼策略、时效结论及对外提交材料必须由租户律师/法务批准；生成和下载草稿不等于正式发布。
- 用户文件、网页、检索段落、租户知识及 MCP 输出均是不可信数据，不能覆盖系统策略、授权、工具限制或输出 Schema。
- 不存储或展示内部推理过程、隐藏系统 Prompt、凭据、其它租户上下文或隐藏安全策略。
- SSE 未完成内容必须明确状态；未经证据门禁的法律正文不可作为已核验结果流出。无法增量核验时先发进度，完整门禁后发答案；中途失败不能伪装成功或完整回答。
- 模型/检索未配置或不可用时，返回稳定错误或拒答，不伪造 fallback。实际模型调用、费用与外部依赖验证必须如实记录。

## 认证、隐私与密钥

- 账号密码、手机验证码、邮箱、微信为目标登录方式；开发适配器和禁用 UI 不代表真实 Provider 已开通。
- 密码使用 Argon2id；登录、验证码发送/校验需限流、防枚举。敏感个人字段加密，精确查询使用用途隔离 Blind Index。
- 短时 Access Token 配合轮换 Refresh Token；租户 Service Account/API Key 仅保存哈希，并有 Scope、有效期、IP 策略与配额。
- 禁止提交真实 `.env`、Key、密码、私钥、验证码、Token、个人信息、客户原件、外部法律原件和生产导出。测试使用合成数据；日志不回显正文、Prompt、供应商 Payload 和秘密。
- DeepSeek Key 由用户填写 `deploy/deepseek.config.json`，模板为 `deploy/deepseek.config.example.json`；真实文件必须忽略。配置通过 `LAWYER_DEEPSEEK_API_KEY_FILE` 绝对路径或 `LAWYER_DEEPSEEK_API_KEY` 二选一注入；不要为检查配置打印其内容。
- 不因演示、测试或部署失败削弱鉴权、租户过滤、审计、证据门禁或人工复核。

## 法律语料、分块与发布

- `F:\ai律师数据库` 是外部只读来源。未经明确授权不得重命名、改写、删除或整理；仅读取任务所需样本。跳过已解压重复 ZIP，不执行宏，不无目的全量统计。
- 用户已授权将项目第一版派生产物归档到该根目录下的 `切块第一版`、`向量化第一版`、`导入前清理第一版`；对应关系与迁移核验见 [存储位置说明](docs/corpus-storage-locations.md)。这些目录不作为法律原件输入扫描。原项目路径可为指向 F 盘的 Junction，不能把两个路径当成两份副本清理；其他原件目录仍只读。
- 导入保留不可变原件、SHA-256、来源元数据、Parser 版本、批次、质量问题及派生版本。无法确认效力或日期时保留未知，不用导入日期冒充法律日期。
- 长批次启动前固定可重建的 Reader/Parser/Chunker 实现快照及依赖身份；运行期间不修改其行为。同名 Parser 的正文、结构或质量标记语义不能漂移。纠错使用新派生版本，重跑只核验并保留既有 ID/父子关系；证明不一致时拒绝，不覆盖旧事实或补写成功报告。
- 法规身份与版本分离；相似标题不能直接自动合并。优先按编/章/节/条/款/项/目解析，禁止跨条拼接。
- 父块保留完整条文，子块命中后从 MySQL 恢复权威全文；滑动窗口仅用于超长叶子内部。合同、制度和案件材料使用适合各自类型的分段器。
- 新分块函数存在不代表已接入导入、持久化、索引及检索；必须逐层验证。模型、输出维度、归一化方式与已发布索引保持一致。
- 发布构建新物理索引，通过质量核验后切别名，并记录快照及回滚目标。重新生成 Chunk ID 前评估旧索引、引用和历史快照可用性；不要假定保留旧索引就足以回滚。
- 集合索引使用有界模型与 Bulk 批次，不累计全集向量，不以逐批 replace 删除前批。预检与构建读取同一事实视图，主索引和导航完整计数通过后才能发布；小样本通过不能代替全量容量验收。
- 全集发布证明可能形成几十 MiB 的 JSON；journal 与 snapshot 经 `infrastructure/persistence/json_documents.py` 有界分行读取并核对元数据时点的摘要。数据集查询使用 `legal_corpus_read_uow.py` 装配的读取 Repository，不能直接加载或排序整个大 JSON ORM 字段。容量验收包含实际驱动读回、恢复及查询入口，不以 JSON 字节估算代替。
- `.doc` 转换在 Loader/受控转换边界内完成；转换产物放派生目录，代表性样本验证质量，不能覆盖来源。
- 受限静态文本恢复必须显式选择，保留原失败记录、独立转换版本和格式/可见性局限，并对照正文及辅助内容；不得关闭 Office 文件校验或冒充普通 Word 转换成功。
- 文件切块导出、数据库入库与索引发布分别验收。离线 ID 不冒充数据库 ID；逐源清单核对哈希、文本覆盖与父子关系。解析回退、表格/编号/图像处理局限明确记录，空文档或未处理文件不能计为完成。全部切块完成后告知实际绝对路径和处理清单。

## 工程与变更流程

- 领域层不依赖 FastAPI Request、ORM Session 或供应商 SDK；应用服务使用显式类型端口；外部边界使用 Pydantic，避免无类型字典扩散。
- API 基础路径 `/api/v1`，租户资源显式路径 `/tenants/{tenant_id}/...`；保持 Problem Details、稳定 code、Request/Trace ID 和响应白名单。
- 遵守既有幂等键、游标分页、ETag/版本 CAS、状态迁移及审计契约；异步 Worker 幂等，Outbox 与业务同事务，RabbitMQ 至少一次投递、有界重试和死信。
- 数据库变更新增 Alembic 向前迁移，不重写已发布迁移；验证升级路径，破坏性迁移有发布和恢复设计。
- 功能/缺陷修复采用 TDD：先观察聚焦测试预期失败，再实现与验证。先定位原因，不削弱断言、吞异常或删必需行为。纯文档调整校验内容、路径、差异即可。
- 前端类型及错误码对齐后端契约；处理加载、空态、错误、取消与失效会话。流式增量和“门禁后完整答案”是不同协议，不可互换。
- 搜索优先 `rg`/`rg --files`，不可用则用有界替代；文本修改优先 Patch。保留用户脏工作树，不执行破坏性 Git 清理，不改无关文件。
- 依赖修改同步维护锁文件；区分生产和开发依赖，验证干净安装/镜像，不能以本机额外安装的包作为可部署证据。
- 完成增量后更新现状文档的对应行、下一步与验证记录；设计边界变化更新相关规格/计划。提交范围明确，不包含缓存、Volume、模型权重、语料或临时产物。

## 标准验证命令与环境边界

在 `backend/` 运行（已有环境先检查依赖；同步会改变本地虚拟环境）：

```powershell
uv sync --frozen
uv run python -X utf8 -m pytest tests/unit tests/api tests/contract -q
uv run ruff check .
uv run mypy src
```

在 `frontend/` 运行：

```powershell
npm ci
npm run typecheck
npm test
npm run build
```

使用 `python -m pytest` 确保 `backend/` 位于模块搜索路径，避免测试 Fixture 的 `tests.unit` 导入在命令行入口下失败。外部服务就绪后，在 `backend/` 按改动选取 `tests/integration/mysql`、`redis`、`rabbitmq`、`opensearch`，或运行 `uv run python -X utf8 -m pytest -v` 做完整验证。先检查各 `conftest.py` 的配置：MySQL 测试使用可创建/删除隔离测试库的管理员连接，不能将业务库作为测试库；真实 Embedding 测试依赖本地权重与对应维度。

从仓库根目录准备开发配置（不覆盖已有文件），确认开发配置后才启动：

```powershell
if (-not (Test-Path -LiteralPath deploy\.env)) {
  Copy-Item deploy\compose.env.example deploy\.env
}
.\scripts\dev.ps1
docker compose --env-file deploy\.env -f deploy\compose.yaml config --quiet
```

- `deploy/.env` 为 Compose 插值配置，不会自动成为宿主机后端环境；Settings 默认读取进程工作目录的 `.env`。容器内连接地址和文件路径须在容器内有效。
- `scripts/dev.ps1` 会准备本地秘密并启动容器，盘点/文档任务不应顺便执行。不同 worktree 可能共享 Compose project、端口及数据卷；操作前核对容器 labels 与归属。
- 停止栈使用 `docker compose --env-file deploy/.env -f deploy/compose.yaml down`；无明确删除开发数据卷授权时禁止加 `-v`。本地网络或 Docker 故障要记录当次证据，不能永久沿用旧结论。

## 完成与交接

- 在最终变更上执行与范围匹配的检查：后端 pytest/Ruff/mypy，前端类型/测试/构建，部署 Compose/镜像，Schema 升级，安全边界集成及跨租户反向测试。
- 区分通过、失败、跳过和未运行；指出命令、结果、前置条件和未验证依赖。历史测试计数、Mock 或健康容器不能代替本次端到端结果。
- 检查 `git diff --check`、最终 diff 与未跟踪文件；确认真实秘密未被跟踪，不输出秘密内容。
- 交接说明本次改动、验证、已知缺口及下一可执行切片；不把开发切片完成宣称为全产品或生产就绪。
