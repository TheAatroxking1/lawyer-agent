# 律师 Agent 仓库开发指引

## 适用范围与语言

- 本指引适用于整个仓库。
- 源代码、配置、迁移、测试、文档、Prompt、Fixture、日志和生成文本统一使用 UTF-8。
- 除非用户要求使用其他语言，否则项目决策和面向用户的说明使用中文；代码标识符和已有技术术语保持其通用形式。
- 本产品仅支持中国大陆法律及法律服务场景。除非用户明确要求比较法材料并且内容已清楚分隔，否则不得将外国法律混入中国法结论。

## 修改代码前必读

1. 阅读本文件。
2. 阅读 `docs/superpowers/specs/2026-08-31-lawyer-agent-enterprise-architecture-design.md`，了解已确认的产品、安全、数据、AI 和部署决策。
3. 阅读 `docs/superpowers/specs/` 下与当前任务相关且已批准的规格。
4. 阅读 `docs/superpowers/plans/` 下当前实施计划，并检查现有代码与测试。
5. 发生冲突时，依次遵循用户当前明确指令、已批准功能规格、当前实施计划和本文件。不得静默改变已批准边界，应报告实质冲突。

不得仅因存在另一种实现方式就重新讨论已经确认的架构决策。只有缺失决策会实质改变行为、安全性、兼容性、成本或范围时才询问用户。

## 产品与责任边界

- 产品是面向企业法务、律所及其客户、法学教育场景的公网多租户 SaaS。
- 首期商业模式下，每个律所租户自行提供律师；平台提供软件、AI、证据检索、工作流和人工复核控制，不以平台自身名义作为受托律师。
- 规划的七类能力包括：法律法规问答与条文溯源、合同审查、法律文书起草、私有知识库问答、专题合规检查、案件或咨询事项管理，以及正式报告或法律意见书生成。
- 响应式 Vue 客户端调用版本化标准 API。业务规则应位于后端服务中，不得只存在于浏览器逻辑里。
- 通过无状态 API 扩容、准入控制、缓存、队列和异步任务实现高吞吐量。不得把 10,000 RPS 目标解释为每秒执行 10,000 次并发模型推理。

## 已确认架构

- 后端采用 Python 3.12、FastAPI、Pydantic v2 和 `uv`。
- AI 主干采用 LangChain。只有流程确实需要持久状态、分支、暂停/恢复或人工中断时才使用 LangGraph，不得给简单 Chain 增加 LangGraph 仪式性结构。
- MCP 通过受控 Client Gateway 增强 Agent；首期核心功能不得依赖某个具体的外部 MCP Server。
- MySQL 是事实数据源；OpenSearch 提供强制的法律混合检索；Redis 用于缓存、限流、锁和临时状态；RabbitMQ 传递异步任务引用；MinIO 保存不可变文档对象及派生产物。
- 本地开发使用 Docker Compose，生产拓扑按 Kubernetes 设计。支持时，容器以非 root 用户运行并使用只读根文件系统。
- 模型和 Embedding 供应商必须位于项目自有接口之后。不得在领域代码中散布供应商特定 Payload、凭据或模型名称。

## 当前阶段

- 阶段 0“工程与安全底座”保持进行中，直到其已批准验收门禁全部通过。
- 后端包、本地容器栈、MySQL/Alembic、全局身份、租户成员关系、租户绑定 Token、RBAC/ABAC、跨租户反向隔离测试已完成。
- “租户级持久 AI Job 运行时”增量（2026-09-03 计划）已按用户指示缩简收尾：签名信封/拓扑/Outbox Publisher、Worker 验签+Inbox+权威 Claim、Consumer、AI Job HTTP API（202/GET/Cancel）均已实现并有真实 MySQL/RabbitMQ 测试；Effect/Retry/Maintenance、Synthetic Handler Harness、Compose 多进程、故障注入与零跳过全量门禁仍为明确延后项。
- 下一主线是“阶段 1：法规数据与有据问答”中不依赖 Embedding 模型运行的任务（Embedding/OpenSearch 检索相关步骤仅在用户另行授权时执行）。
- 阶段 1 非 Embedding 先行切片已完成（2026-09-04 计划）：法规版本模型/条文/Chunk/数据集快照 Schema、时点查询、DOCX 只读解析（真实样本 134 条）、盘点/Manifest/质量门禁与 dataset_v1 发布、Evidence Bundle 与 Citation Gate 后端校验；OpenSearch/Embedding/Reranker/DeepSeek/SSE 与完整问答链路仍为待授权延后项。
- 阶段 1 增补“法规语料只读 HTTP API”非模型切片已完成（2026-09-05 计划）：`GET /api/v1/legal/instruments/{id}/version?as_of=` 时点取现行版本元数据（published/effective/repealed、law_number、dataset_version、source_ref；无生效版本 404）、`GET /api/v1/legal/versions/{id}/provisions` 按序返回条文全文；平台公共语料、登录账号可读、非租户私有；真实 MySQL 全栈验证 as_of 三态（早于生效 404 / 区间取旧版 / 之后取新版）、非法日期 422、条文全文、未认证 401；全文检索/Embedding/问答仍延后。
- 阶段 2 非模型先行切片已完成（2026-09-05 计划）：租户 Matter、Document 版本、对象引用白名单、上传会话/校验（NEEDS_REVIEW 不绕过）、Review 状态骨架与跨租户反向测试；条款/风险 AI、RAG、Drafting/导出、MinIO 预签名直传仍为延后项。
- 阶段 2 增补“Rule Pack 与确定性合同风险检查”非模型切片已完成（2026-09-05 计划）：版本化 Rule Pack/规则/RiskIssue 三表与迁移、租户范围仓储与跨租户反向测试、正则白名单 RuleEngine、OPEN→ACCEPTED/REJECTED/MODIFIED 人工处置状态机、DOCX→引擎→处置→重跑真实 MySQL 集成流；规则命中固定 `evidence_level=rule_based`，高风险/时限一律人工确认，条款识别 AI、法规 RAG、正式结论输出仍为延后项。
- 阶段 2 增补“Rule Check DOCX 报告导出”非模型切片已完成（2026-09-05 计划）：stdlib 只写 DOCX（XML 转义、拒控制字符/超长、无 DOCTYPE/ENTITY/外部关系）、报告组装服务只消费已入库文档元数据与 RiskIssue/处置记录、免责声明与「待人工核验/无命中≠无风险」措辞、DOCX 写→读回闭环与真实 MySQL seed→检查→处置→导出流、跨租户导出反向测试；PDF、正式 LegalReport 发布/审批/版本化、模板引擎、派生版本落库与下载 API 仍为延后项。
- 阶段 2 增补“Rule Check 租户 HTTP API”非模型切片已完成（2026-09-05 计划）：`/api/v1/tenants/{tenant_id}/...` 下运行检查/读取/处置 RiskIssue/下载 DOCX 报告四个端点（Pydantic 严格模型、path tenant 强校验、每请求显式 TenantContext UoW、run 幂等、处置条件更新防并发）；真实 MySQL+Redis 全栈 HTTP 测试覆盖 run→list→dispose→409→report 读回，以及 path 与资源两层跨租户反向；permission code 化、上传预签名链、条款 AI/RAG 仍为延后项。
- 阶段 2 增补“租户 Matter/Document HTTP API”非模型切片已完成（2026-09-05 计划）：`/api/v1/tenants/{tenant_id}/...` 下创建/读取 Matter、登记 Document 原件版本（docx→ACCEPTED、非白名单 MIME→NEEDS_REVIEW）、按 Matter 列出版本（含新增 DocumentHeader 领域类型与仓储 headers_for_matter）；真实 MySQL+Redis 全栈 HTTP 测试覆盖 create→get→register→list 与租户 B 的路径/资源两层跨租户反向；为 Rule Check HTTP API 提供上游闭环；上传预签名 MinIO、其余端点幂等化仍为延后项。
- 阶段 2 增补“Matter 创建 Idempotency-Key”非模型切片已完成（2026-09-05 计划）：`POST /tenants/{tenant_id}/matters` 接入既有幂等框架（membership scope + title/kind/description 指纹 + reserve→execute→complete→replay）；真实 MySQL+Redis 全栈验证同 key 同体 replay 返回同一 matter id（仅一行）、同 key 异 body → 409 `idempotency_conflict`、异 key 创建独立资源；其余写端点幂等化为后续。
- 阶段 2 增补“Document 登记 Idempotency-Key”非模型切片已完成（2026-09-05 计划）：`POST /tenants/{tenant_id}/matters/{matter_id}/documents` 接入幂等框架，指纹 = file_name/mime_type/payload SHA-256（payload 不入库），replay 返回原 document 版本投影；真实 MySQL+Redis 全栈验证同 key 同 payload replay 同 document_id（仅 1 个 header）、同 key 异 payload → 409 `idempotency_conflict`、异 key 新建文档；其余写端点幂等化仍延后。
- 阶段 2 增补“Document Review 状态机 HTTP API”非模型切片已完成（2026-09-05 计划）：`POST /api/v1/tenants/{tenant_id}/documents/{document_id}/versions/{version_no}/review` 提交/批准/驳回/要求修改人工复核（迁移 20260905_10 增加 `review_reason` 列并往返+`alembic check`；域状态机纯函数拒绝非法转换与空理由；仓储按唯一键原子更新并仅放行 accepted/ready 版本）；真实 MySQL+Redis 全栈 HTTP 测试覆盖 submit→request_changes→resubmit→approve→终态 409 与跨租户双层 404；自动发布/结论引用门槛仍为延后项。
- 阶段 2 增补“Document Review Idempotency-Key”非模型切片已完成（2026-09-06 计划）：`POST /api/v1/tenants/{tenant_id}/documents/{document_id}/versions/{version_no}/review` 接入幂等框架（membership scope；指纹 = document_id/version_no/decision/reason；result_type=`document_version`、result_id=version.id）；同 key 同体 replay 返回原版本投影且不再触发第二次状态转移、同 key 异体 → 409 `idempotency_conflict`、异 key 继续受状态机约束（终态 409 `document_review_conflict`）、无 key 路径完全向后兼容；真实 MySQL+Redis 全栈验证 replay 在版本推进至终态后仍返回 200 与原结果；启停/激活/处置幂等化仍为延后项。
- 阶段 2 增补“Rule Pack 创建 Idempotency-Key”非模型切片已完成（2026-09-05 计划）：`POST /tenants/{tenant_id}/rule-packs` 与 `POST .../rule-packs/{pack_id}/rules` 接入幂等框架（建包指纹 = name；加规则指纹 = trigger/label/pattern/risk/suggestion），replay 返回原 pack/rule id；真实 MySQL+Redis 全栈验证同 key 同指纹 replay 同 id（仅一行）、同 key 异指纹 → 409 `idempotency_conflict`、异 key 独立资源；Review/启停/激活幂等化仍有状态保护未接。
- 阶段 2 增补“RiskIssue 处置 Idempotency-Key”非模型切片已完成（2026-09-06 计划）：`POST /tenants/{tenant_id}/risk-issues/{issue_id}/disposition` 接入幂等框架（membership scope；指纹 = issue_id/status/reason；result_type=`risk_issue`、result_id=issue_id）；同 key 同体 replay 返回原 issue 投影且不重复处置、同 key 异体 → 409 `idempotency_conflict`、异 key 仍受状态机约束（非 open 409 `rule_check_conflict`）、无 key 路径向后兼容；`SqlAlchemyRuleCheckUnitOfWork` 补 idempotency 仓储并注入共享 IdempotencyService；真实 MySQL+Redis 全栈验证 replay/conflict/状态机/无 key 行为；启停/激活幂等化仍为延后项。
- 阶段 2 增补“Rule Pack 启停/激活 Idempotency-Key”非模型切片已完成（2026-09-06 计划）：`PATCH .../rule-packs/{pack_id}/rules/{rule_id}`（启停，指纹 = pack_id/rule_id/enabled，result_type=`rule_pack.rule`/result_id=rule_id）与 `POST .../rule-packs/{pack_id}/activate`（激活，指纹 = pack_id，result_type=`rule_pack.pack`/result_id=pack_id）接入幂等框架；同 key 同体 replay 返回原投影/204 且不重复状态写入、同 key 异体 → 409 `idempotency_conflict`、不存在资源带 key → 404 不 complete（可重试）、无 key 路径向后兼容；真实 MySQL+Redis 全栈验证 activate replay 仅一个 active pack、toggle replay 仅一次写入、冲突/404/无 key 行为；模板规则库、permission code 化仍为延后项。
- 阶段 2 增补“Rule Pack 管理 HTTP API”非模型切片已完成（2026-09-05 计划）：`/api/v1/tenants/{tenant_id}/rule-packs` 下创建（同名版本自动 +1、inactive）/列表/向 Pack 添加规则（正则白名单、枚举校验）/启停规则/激活唯一 active（事务清理其它）；管理纯函数与租户范围仓储写方法，MySQL 集成覆盖版本推进与唯一激活，全栈 HTTP 覆盖 建包→加规则→坏正则 422→v2→激活→联动 Rule Check 命中→停用后 409 拒绝空跑→跨租户双层 404；模板规则库、permission code 化仍为延后项。
- 阶段 2 增补“Rule Pack 规则只读回读”非模型切片已完成（2026-09-10 计划）：`GET /api/v1/tenants/{tenant_id}/rule-packs/{pack_id}/rules` 读回某 pack 的**全部**规则（含 disabled，按 created_at/id 稳定升序，响应投影与加规则一致 `RuleSummary`）；仓储新增 `list_pack_rules`（引擎侧 `rules_for_pack` 仍只读 enabled，不动）；服务先 `_find_pack_by_id` 校验归属（不存在/跨租户 → 404 `rule_pack_admin_not_found`）；真实 MySQL+Redis 全栈验证 空 pack `[]`→加 2 规则（high/low）按序读回→停用后仍可见 disabled→不存在 pack 404→租户 B path 层 404/资源层 404→B 自己 pack 规则与 A 无交集；模板规则库、permission code 化仍为延后项。
- 阶段 2 增补“租户人工写动作审计”非模型切片已完成（2026-09-05 计划）：`risk_issue.dispose`/`document.review`/`rule_pack.activate` 三个最高影响人工动作在成功路径登记 TENANT_USER 结构化审计（action/result/reason_code/trace_id/target_type/target_id，仅标识符不泄漏正文）；`_TENANT_USER_PREFIXES` 白名单扩展与三个 UoW 暴露 audit 仓储；真实 MySQL+Redis 全栈验证三类动作各产生正确审计行。
- 阶段 2 增补“拒绝路径审计”非模型切片已完成（2026-09-05 计划）：被拒处置/复核/激活尝试也留痕——409 状态冲突与 404 目标不存在（含跨租户资源层尝试）写 `result=denied`、422 非法请求写 `result=failure`，reason_code 复用稳定 Problem Details 错误码，target 记录尝试引用的资源；审计追加在业务 UoW 回滚后经独立短事务提交（失败尝试不吞事件也不误提交半成品）；路径层 404（租户不匹配、未进入服务）不产生审计噪音；真实 MySQL+Redis 全栈验证同一资源二次处置/复核、空理由、不存在 version/pack/issue、跨租户资源层尝试均产生正确 denied/failure 行且成功行不受影响。
- 阶段 2 增补“租户审计查询 HTTP API”非模型切片已完成（2026-09-05 计划）：`GET /api/v1/tenants/{tenant_id}/audit` 只读列出本租户审计（action 前缀白名单/target_type/trace_id 过滤、limit≤100、before_id 游标稳定翻页；响应仅安全字段白名单，不含 IP/UA hash 与 metadata）；仓储按 `(tenant_id, occurred_at)` 倒序查询；真实 MySQL+Redis 全栈验证三类事件可见、`action=risk_issue.` 过滤、非法前缀 422、limit=1 翻页不重不漏、租户 B 永看不到 A 的动作行；跨租户管理/导出、IP-UA 展示、保留策略仍为延后项。
- 阶段 2 增补“Matter 参与方 HTTP API”非模型切片已完成（2026-09-05 计划）：`POST/GET /api/v1/tenants/{tenant_id}/matters/{matter_id}/parties` 添加/列出参与方（display_name/kind 必填非空、长度上限；仓储 list_parties 按 created_at 升序、add 前校验 matter 归属）；真实 MySQL+Redis 全栈覆盖 add 2→list 顺序、422 空白拒绝、租户 B 双层 404；团队/角色、冲突检查仍为延后项。
- 阶段 2 增补“Matter 参与方更新/删除 HTTP API”非模型切片已完成（2026-09-06 计划）：`PATCH/DELETE /api/v1/tenants/{tenant_id}/matters/{matter_id}/parties/{party_id}` 更新（display_name/kind 至少一项、strip、version 乐观递增）与物理删除；仓储 update/remove 均按 `(tenant_id, matter_id, party_id)` 归属校验，无 FK 依赖；真实 MySQL+Redis 全栈覆盖 改名称保 kind→改 kind 保名称→version 递增→空/空白 body 422→DELETE→list 剩 1→对不存在 party PATCH/DELETE 404→租户 B path/资源双层 404；团队/角色、冲突检查仍为延后项。
- 阶段 2 增补“Matter 参与方写动作审计”非模型切片已完成（2026-09-07 计划）：`add/update/remove` 三个人工写动作在成功路径登记 TENANT_USER 结构化审计（action=`matter.party.add|update|remove`、reason_code=`added|updated|removed`、target_type=`matter_party`、target_id=party.id，`matter.` 前缀白名单已含）；审计与业务同 UoW 提交，仅标识符不泄漏正文；真实 MySQL+Redis 全栈验证 add 2→update 2→remove 1 对应审计行、按 `action=matter.` 过滤可见、租户 B 路径层 404/资源层无 matter.party 行；团队/角色、冲突检查仍为延后项。
- 阶段 2 增补“Matter 参与方拒绝路径审计”非模型切片已完成（2026-09-07 计划）：add/update/remove 的失败尝试也留痕——目标不存在（含跨租户资源层尝试）写 `result=denied`/reason_code=`matter_document_not_found`、422 非法请求写 `result=failure`，target 按可确定粒度记录（matter 或 party 引用）；审计在业务 UoW 回滚后经独立短事务提交；路径层 404（租户不匹配、未进入服务）不产生噪音；真实 MySQL+Redis 全栈验证 A 对已删 party 的 PATCH/DELETE 404 产生 denied 行且成功行计数不变、B 资源层尝试只进 B 租户（全为 denied `matter_document_not_found`）且 A 成功行永不到 B；团队/角色、冲突检查、permission code 化仍为延后项。
- 阶段 2 增补“Matter 参与方 Idempotency-Key”非模型切片已完成（2026-09-08 计划）：`POST .../parties`（add，指纹 = matter_id/display_name/kind）、`PATCH .../parties/{party_id}`（update，指纹含 party_id 与各字段）、`DELETE .../parties/{party_id}`（remove，指纹 = matter_id/party_id）接入幂等框架（membership scope；result_type=`matter_party`）；同 key 同体 replay 返回原 party 投影/204 且不重复插入/版本递增/删除、同 key 异体 → 409 `idempotency_conflict`、404/422 不 complete（修复后同 key 可重试）、无 key 路径向后兼容；真实 MySQL+Redis 全栈验证 add replay 同 id 仅 1 行、update replay 版本不重复递增、remove replay 204 仅删一次、异 key 对已删 party 404；团队/角色、冲突检查、permission code 化仍为延后项。
- 阶段 2 增补“Matter 列表（keyset 分页）”非模型切片已完成（2026-09-08 计划）：`GET /api/v1/tenants/{tenant_id}/matters?limit=&before_id=` 只读列出本租户 Matter（`created_at,id` keyset 稳定分页、limit 1–100、越权/未知游标 404、响应 `{items, next_before_id}`）；仓储 `list_matters` 租户范围 keyset 查询、服务编排校验；真实 MySQL+Redis 全栈验证 3 案件 limit=2 两页不重不漏、非法 limit 422、未知游标 404、租户 B path 层 404 且 B 列表永不含 A id；搜索过滤、permission code 化仍为延后项。
- 阶段 2 增补“Matter 列表搜索/过滤”非模型切片已完成（2026-09-10 计划）：`GET /api/v1/tenants/{tenant_id}/matters?limit=&before_id=&title=&status=&kind=` 增加只读过滤参数（title trim 后非空白子串≤512、status=open|active|closed|archived、kind 五类枚举；非法/空白/超长 422 `matter_document_invalid_request`）；服务校验并透传、仓储在租户范围 WHERE 之上追加过滤后仍按 `(created_at,id)` keyset 排序分页；真实 MySQL+Redis 全栈验证 title 子串/空白折行、status、kind、组合、过滤+分页不重不漏、非法值 422、租户 B path 层 404 且 B 过滤列表永不含 A id；permission code 化仍为延后项。
- 阶段 2 增补“Matter 状态迁移 HTTP API”非模型切片已完成（2026-09-09 计划）：`POST /api/v1/tenants/{tenant_id}/matters/{matter_id}/status`（body `{status}`、可选 If-Match 强 ETag）按领域纯函数白名单推进 `open→active/closed、active→closed、closed→archived`（archived 终态；回退/跳级 409 `matter_document_conflict`）；仓储先锁读再 `status+version` 条件 CAS、版本递增并返回新 ETag；成功登记 `matter.status` TENANT_USER 审计（reason=`changed`）；真实 MySQL+Redis 全栈验证 推进三连 version 1→4、过期 If-Match 409、终态冻结 409、跳级 409、不存在 matter 404、租户 B path/资源双层 404、审计三行；title/description 编辑、团队/角色、permission code 化仍为延后项。
- 阶段 2 增补“Matter 元数据编辑 HTTP API”非模型切片已完成（2026-09-09 计划）：`PATCH /api/v1/tenants/{tenant_id}/matters/{matter_id}`（body 可选 title/description 至少一项、可选 If-Match 强 ETag；title 非空白≤512、description≤4000 可置空）仅编辑文本元数据（kind/status/归属不变）；领域校验 `validate_matter_metadata`、仓储锁读+`version` CAS UPDATE 返回新 ETag；成功登记 `matter.update` 审计（reason=`changed`）；真实 MySQL+Redis 全栈验证 title+description 同改、description-only 保留 title、显式空 description 清空、过期 ETag 409、空白/超长/空 body 422、不存在 404、租户 B path/资源双层 404、审计三行；owner/团队/角色、permission code 化仍为延后项。
- 阶段 2 增补“Matter 只读投影补 description”非模型切片已完成（2026-09-10 计划）：此前元数据编辑仅写库不读回——`MatterSummary` 增可空 `description` 并由 `_matter_summary` 填充，create/get/list/status/owner 等所有返回 Matter 的响应均带出描述；真实 MySQL+Redis 全栈验证 创建带描述读回、PATCH title+description 读回新值、description-only 保 title、显式空描述读回 null、列表带 description、owner 指派后保留、租户 B path 层 404 且 B 列表永不含 A id；owner/团队角色 ABAC、permission code 化仍为延后项。
- 阶段 2 增补“Matter 参与方利益冲突只读检查”非模型切片已完成（2026-09-08 计划）：`POST /api/v1/tenants/{tenant_id}/matter-party-conflict-checks`（display_name 必填、kind 可选、exclude_matter_id 可选）只回答该当事方在**其它 Matter** 出现情况 `{conflict, other_matter_count}`（租户内 display_name/kind 精确匹配的 distinct matter 计数），响应永不含其它 Matter 的 ID/名称/内容（spec 8.4 最小信息）；仓储 `count_other_party_matters` 租户范围 COUNT、领域投影 `PartyConflictCheck` 校验 conflict 与计数一致；真实 MySQL+Redis 全栈验证 同名跨两 matter→count=2、kind 精确过滤、exclude 后计数变化、单 matter 排除后 clear、空 display_name/kind 422、响应无其它 matter 标识、租户 B 路径层 404；处置状态/Conflict Register/强制门槛仍为延后项。
- 阶段 2 增补“Matter owner 指派”非模型切片已完成（2026-09-10 计划）：`PUT /api/v1/tenants/{tenant_id}/matters/{matter_id}/owner`（body `{owner_membership_id}`、可选 If-Match 强 ETag）把同租户 active 的本所成员（member_type owner/internal，spec 8.4「自己的律师或法务」）指派为 Matter 负责人；领域纯函数 `matter_owner_assignable` 只放行该成员集（external_client/student 与 inactive 一律拒）；仓储锁读后 `version` CAS UPDATE 写 `owner_membership_id` 并返回新 ETag，foreign/不存在/挂起/客户端成员统一 422 防探测；`MatterSummary` 增可空 `owner_membership_id` 只读投影；成功登记 `matter.owner` 审计（reason=`assigned`）；真实 MySQL+Redis 全栈验证 指派→换人（If-Match）→过期 ETag 409→挂起/客户端/异租户/不存在 422→不存在 matter 404→租户 B path/资源双层 404→B 引用 A 成员 422→审计两行且 B 不可见；解锁指派、owner 详情/团队角色 ABAC、permission code 化仍为延后项。
- 当前增量遵循最新一份用户已批准的实施计划。项目进入后续阶段时，只更新本节的简短阶段说明，不得把临时任务进度复制到本文件。

## 多租户与授权不变量

租户隔离是安全边界，不是可选的查询约定。

- 一个自然人对应一个全局用户身份，并可拥有多个租户成员身份。
- 每个租户范围请求都必须携带显式租户上下文，并独立校验已认证身份和有效成员关系。
- 每条租户私有关系数据都包含 `tenant_id`。能够防止跨租户引用时，租户范围唯一约束和外键必须包含 `tenant_id`。
- Repository 和 Service 接口必须注入或要求租户上下文。不得为私有资源暴露不受限制的 `get_by_id(id)`；必须要求租户身份或经过显式审计的平台权限范围。
- OpenSearch 私有查询必须包含租户 Routing 和 Filter；Redis Key、锁、配额和 Stream 必须包含租户范围；MinIO Object Key 使用租户前缀和短时单对象访问；队列消息只携带标识符和签名上下文，Worker 必须从 MySQL 重新加载权威租户和权限状态。
- RBAC 决定角色可以执行哪些动作；ABAC 还要检查部门、Matter 团队、密级、创建人、委托关系、资源状态和审批状态。
- 外部客户和学生只能访问显式共享给他们的 Matter、咨询、班级、作业或资源。
- 超级管理员访问租户内容时，必须指定租户和资源范围、事由、工单、有效期，完成二次认证，取得短时特权授权，并产生完整审计事件；不得存在隐式的无限制数据通道。
- 每新增一个租户范围的 Repository、API、缓存、搜索、对象存储、队列或 Tool 路径，都要添加跨租户反向测试。只有同租户成功测试并不足够。

## 法律证据与 AI 安全不变量

- 每项重要法律结论都必须追溯到精确证据，包括文档身份、条文、制定机关、公布与施行日期、效力状态、法域或地域；有来源 URL 时记录 URL，没有来源 URL 时记录来源系统、来源文件或路径和文件哈希；可用时记录数据集/版本元数据，禁止猜测或拼造 URL。
- 检索结果必须形成显式 Evidence Bundle。生成的 Claim 必须引用允许的 `evidence_id`，并由 Citation Gate 在发布前验证引用。
- 证据缺失、冲突、失效、超出请求日期或地域范围，或者以其他方式不足时，必须拒答或明确缩小结论范围。不得把模型记忆变成无引用法律结论。
- 必须区分现行法、历史法、草案、司法材料、租户制度、客户文件、教学材料和模型生成内容，严禁把其中一种呈现为另一种。
- 正式法律意见、诉讼策略、时效结论和对外提交材料，必须由租户律师或法务人员审核批准。
- 用户上传内容、网页、租户知识、检索段落和 MCP 输出均为不可信内容。它们可以提供事实和证据，但不得提供可覆盖系统策略、授权、Tool 限制或输出 Schema 的指令。
- 不得存储或暴露 Chain-of-Thought、系统 Prompt、凭据、其他租户上下文或隐藏安全策略。

## 身份认证、隐私与密钥

- 支持账号密码、手机验证码、邮箱和微信登录；各 Provider 位于可替换 Adapter 之后。
- 密码使用 Argon2id 哈希；登录、验证码发送和验证码校验接口必须限流并防止账号枚举。
- 敏感个人字段加密存储；需要精确查询时使用用途隔离的 Blind Index。
- 使用短时 Access Token 和轮换 Refresh Token。租户 Service Account 与 API Key 只保存哈希，并具有显式 Scope、有效期、IP 策略和配额。
- 禁止提交 `.env`、API Key、密码、私钥、验证码、Access Token、Refresh Token、个人信息、客户文件、法律文档原件或生产导出数据。
- 测试使用合成数据；日志和 Fixture 必须脱敏标识符、文档内容、Prompt 和供应商 Payload。
- 不得为了让演示通过而削弱身份认证、租户过滤、审计、证据校验或人工复核控制。

## 法律语料与文档处理

- `F:\ai律师数据库` 是从国家法律法规数据库取得的外部只读来源语料。未经用户明确授权，不得重命名、改写、删除或重新组织该目录。
- 只检查当前任务所需的最小样本。没有明确需要时，不得把整个语料库递归读取到上下文，也不得执行高成本的全量统计。
- ZIP 与已解压的 Word 文档重复；除非用户明确要求校验压缩包，否则跳过 ZIP。
- 来源文件作为不可变原件保存；文件哈希、来源元数据、Parser 版本、导入批次和派生文档版本应单独记录。
- 不得假设通用固定长度 Chunk 适合所有文档。优先按标题、编、章、节、条、款、项、目等法律结构切分，同时允许 Parser 专用回退和后续评测。
- `.doc` 和 `.docx` 支持属于项目 Loader 接口之后的导入能力；必须用代表性样本验证真实 Parser 行为和抽取质量。

## Python 与 API 工程规范

- 可导入包位于 `backend/src/lawyer_agent`，测试位于 `backend/tests`。
- 优先使用职责单一的小模块和显式类型接口。领域代码不得直接依赖 FastAPI Request、ORM Session 或供应商 SDK Payload。
- 外部边界使用 Pydantic Model，应用代码采用严格类型检查。服务边界存在合适命名模型时，避免传递无类型 Dictionary。
- 公共 Endpoint 统一位于 `/api/v1`。租户资源使用 `/api/v1/tenants/{tenant_id}/...` 等显式租户路径，但服务端仍必须校验成员关系和资源范围。
- 保持稳定的 Problem Details 错误结构以及 Request/Trace ID。不得泄露堆栈、SQL、密钥、Prompt 或供应商内部响应。
- 异步 Worker 必须幂等。RabbitMQ 采用至少一次投递、有界退避重试和死信处理。
- Schema 变更使用 Alembic。不得修改已经发布的 Migration；应新增向前 Migration 并测试升级行为。破坏性数据迁移必须具有明确的发布与恢复设计。

## 开发流程

- 实现功能或行为变更前，确认已有获批设计和可执行计划覆盖当前工作；每个增量必须能够独立验收。
- 使用 TDD：先编写聚焦的失败测试并观察预期失败，再实现最小正确行为，最后执行聚焦测试和更广泛验证。
- 修改前先诊断失败原因。不得通过削弱断言、关闭安全控制、吞掉异常或删除必需行为掩盖失败。
- 保留脏工作树中的用户改动。不得重写无关文件或使用破坏性 Git 命令。
- 仓库搜索优先使用 `rg` 或 `rg --files`；有意的文本变更使用基于 Patch 的编辑。
- Commit 保持范围明确且便于审查。不得提交本地环境、缓存、生成凭据、运行时 Volume、语料数据或临时审查产物。
- 已批准接口或长期架构决策发生变化时，更新相关规格或计划；不得把临时进度记录复制到本文件。

## 标准命令

在 `backend/` 目录运行后端命令：

```powershell
uv sync --frozen
uv run pytest -v
uv run ruff check .
uv run mypy src
```

从仓库根目录准备本地环境，且不得提交生成的环境文件：

```powershell
if (-not (Test-Path -LiteralPath deploy\.env)) {
  Copy-Item deploy\compose.env.example deploy\.env
}
.\scripts\dev.ps1
```

验证 Compose 配置：

```powershell
docker compose --env-file deploy\.env -f deploy\compose.yaml config --quiet
```

从仓库根目录执行 `docker compose --env-file deploy\.env -f deploy\compose.yaml down` 清理；不带参数的 `docker compose down` 并不足够。除非用户明确授权删除命名开发数据卷，否则不得添加 `-v`。

## 完成定义

声称工作完成前：

- 在最终工作树中运行聚焦测试和完整的相关 pytest 测试集。
- 运行 Ruff 和 mypy，要求零错误。
- 修改部署配置时验证 Compose。
- 修改存储、身份认证、授权、缓存、搜索、对象存储、队列或 Tool 边界时，运行集成测试和跨租户反向隔离测试。
- 修改数据库 Schema 时，在要求的升级路径上验证 Migration。
- 确认没有 Secret 或 `.env` 被 Git 跟踪，并检查最终 Git Diff 中不存在无关变更。
- 报告精确验证证据、剩余警告和未验证的外部依赖。外部网络服务不可用不等于集成测试通过。
