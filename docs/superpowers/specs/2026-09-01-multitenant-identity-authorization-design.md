# 多租户身份、认证与授权设计

- 日期：2026-09-01
- 状态：设计已口头确认，待书面规格审阅
- 所属阶段：阶段 0——工程与安全底座
- 适用法域：中国大陆

## 1. 目标与范围

本增量为公网多租户 SaaS 建立可投入后续业务开发的身份与隔离底座，交付：

- MySQL 异步访问层和 Alembic 迁移；
- 一个自然人对应一个全局 `User` 的身份模型；
- 用户名、手机号、邮箱和微信身份的统一 Provider 接口；
- 租户申请、审核、部门、成员关系和成员邀请；
- 租户绑定 Access Token、轮换 Refresh Token、重放检测和会话撤销；
- 数据库驱动的 Permission Catalog、内置角色和强类型 RBAC + ABAC Policy Engine；
- 审计事件、限流、就绪检查、OpenAPI 契约和跨租户反向测试。

以下内容不属于本增量：

- 真实短信、邮件和微信开放平台接入；
- 租户自定义角色管理 API；
- 超级管理员读取租户业务内容；
- Service Account/API Key；
- Matter、合同、知识库、LangChain、LangGraph 和 MCP 业务能力；
- Vue 页面。

## 2. 已确认决策

1. 已验证的手机号、邮箱、微信 UnionID/OpenID 等身份标识全平台唯一绑定一个全局 `User`。
2. Access Token 使用短时 Bearer Token；Refresh Token 使用 HttpOnly Cookie、服务端哈希、Token Family 轮换和重放检测。
3. 用户可公开注册并申请创建租户；租户初始为 `pending_verification`，正式对外服务前必须经平台审核。
4. Permission Catalog 与内置角色存入数据库；Schema 支持未来租户自定义角色，本增量不开放管理 API。
5. 授权使用项目自有、强类型、默认拒绝的 Policy Engine，不引入 Casbin 或独立 OPA 服务。
6. 主键使用 UUIDv7；API 使用标准 UUID 字符串，MySQL 使用 `BINARY(16)`。
7. 整体采用模块化单体、SQLAlchemy 2 异步 ORM、Alembic、MySQL 和 Redis。
8. 此前在企业架构设计中标记为暂定的相关方向均转为确认状态。

## 3. 模块边界

### 3.1 Identity

负责全局用户、登录身份、密码凭据、身份验证挑战、标识规范化、敏感字段加密和 Blind Index。

模块输出稳定接口：

- `IdentityService`
- `PasswordHasher`
- `SensitiveValueCipher`
- `BlindIndexService`
- `IdentityProvider`

手机号、邮箱和微信 Provider 位于 Adapter 后面。账号密码完整实现；手机号验证码、邮箱验证码和微信使用测试开发 Provider，不连接真实供应商。

### 3.2 Tenancy

负责租户申请、审核、部门、成员关系、成员邀请、租户状态和成员有效期。

模块输出：

- `TenantService`
- `MembershipService`
- `InvitationService`
- `TenantContext`

创建租户、实例化内置角色和创建所有者成员关系必须位于同一个 MySQL 事务中。

### 3.3 Authorization

负责 Permission Catalog、角色、角色权限、成员角色分配和 RBAC + ABAC 决策。

模块输出：

- `Principal`
- `Action`
- `ResourceAttributes`
- `AuthorizationDecision`
- `PolicyEngine`

Policy Engine 不依赖 FastAPI Request、SQLAlchemy Session 或供应商 SDK 类型。

### 3.4 Sessions

负责 Access Token、Refresh Token、Token Family、重新认证、租户切换、会话撤销、CSRF 和重放检测。

模块输出：

- `TokenService`
- `SessionService`
- `AccessTokenClaims`
- `RefreshTokenRecord`
- `StepUpGrant`

### 3.5 Infrastructure

负责 SQLAlchemy Model、Repository、Unit of Work、Redis、Alembic、JWT Key Provider、审计写入和 FastAPI Adapter。领域与应用模块不得直接读取环境变量或构造基础设施客户端。

## 4. 标识符、时间与数据库约定

- 所有业务主键为 UUIDv7。
- API 使用小写带连字符 UUID 字符串。
- MySQL 使用标准 UUID 16-byte 表示的 `BINARY(16)`；不得使用会改变字节顺序的非标准 swap 转换。
- 时间统一保存为 UTC `DATETIME(6)`，应用层使用带时区 `datetime`，API 输出 ISO 8601 UTC 时间。
- 表使用 InnoDB 和 `utf8mb4`。
- Schema 使用统一 Constraint Naming Convention，保证 Alembic Diff 可复现。
- 所有可并发修改的聚合包含单调递增 `version`。
- 删除默认采用软删除或状态撤销；物理清理由后续受审计的保留策略处理。

## 5. 数据模型

### 5.1 `users`

主要字段：

- `id BINARY(16)`：UUIDv7；
- `status`：`active`、`locked`、`disabled`、`pending_deletion`；
- `display_name`；
- `auth_version`：密码、身份或全局安全状态变化时递增；
- `created_at`、`updated_at`、`deleted_at`；
- `version`。

### 5.2 `auth_identities`

主要字段：

- `id`、`user_id`；
- `kind`：`username`、`phone`、`email`、`wechat_unionid`、`wechat_openid`；
- `provider` 与 `issuer`；
- `display_value`：仅允许保存非敏感或已脱敏显示值；
- `subject_ciphertext`：手机号、邮箱或微信 Subject 的密文；
- `subject_blind_index`；
- `key_version`：密文字段使用的加密密钥版本；
- `blind_index_key_version`：Blind Index 使用的独立密钥版本；
- `verified_at`、`status`、时间戳。

唯一约束为 `(kind, issuer, blind_index_key_version, subject_blind_index)`。注册和认证必须对当前 Key Ring 中全部有效 Blind Index 版本查询并按稳定版本顺序取得用途隔离的 MySQL Advisory Lock；单一版本唯一约束只负责数据库最终防线，不能代替跨版本冲突检查。用户名在创建账号时形成有效身份；手机号、邮箱和微信只有完成证明后才进入 `auth_identities`，未验证 Claim 保存在有期限的验证挑战中，避免未验证占位长期阻塞真实用户。

用户名采用 NFKC、去除首尾空白和 Casefold 后生成索引；手机号规范化为 E.164；邮箱规范化域名后生成用途隔离的 Blind Index；微信按 Provider Issuer + UnionID/OpenID 判断唯一性。

### 5.3 `identity_verification_challenges`

保存挑战类型、目标 Blind Index、Challenge Hash、尝试次数、过期时间、消费时间和限流上下文。不得保存明文验证码。真实 Provider 接入前由开发 Provider 在测试进程中捕获一次性值。

### 5.4 `password_credentials`

与 `users` 一对一，保存 Argon2id Hash、算法参数、密码修改时间、失败计数、安全状态和版本。密码长度为 12–128 个字符，允许空格及密码管理器生成的特殊字符。不得保存可逆密码、密码提示或日志副本。

### 5.5 `tenants`

主要字段：

- `id`、`name`、规范化名称；
- `tenant_type`：`law_firm`、`enterprise`、`university`；
- `status`：`pending_verification`、`active`、`suspended`、`closed`；
- `created_by_user_id`；
- 审核状态、审核时间和不包含敏感正文的原因代码；
- `version` 与时间戳。

租户显示名称允许同名；规范化名称仅用于一致的检索和展示规范化，不是全局唯一身份，
不得据此新增唯一约束或把任意数据库完整性错误映射成名称冲突。

待审核租户可配置组织和内部成员，但不得启用外部客户服务。

### 5.6 `departments`

包含 `id`、`tenant_id`、`parent_id`、名称、状态和版本。父部门使用 `(tenant_id, parent_id)` 到本表的复合外键，禁止跨租户部门树。

### 5.7 `tenant_memberships`

包含 `id`、`tenant_id`、`user_id`、`department_id`、成员类型、状态、`valid_from`、`valid_until`、`authz_version`、版本和时间戳。

- `(tenant_id, user_id)` 唯一；
- 部门外键包含 `tenant_id`；
- 状态至少包括 `invited`、`active`、`suspended`、`revoked`；
- 撤销成员必须递增授权版本并撤销相关租户 Session。

### 5.8 权限与角色表

- `permission_catalog`：稳定 Permission Code、资源、动作、风险级别和状态；
- `role_templates`：平台维护的内置角色模板；
- `role_template_permissions`：模板权限；
- `tenant_roles`：全部包含 `tenant_id`，租户创建时从模板实例化；
- `tenant_role_permissions`：使用包含 `tenant_id` 的复合外键；
- `membership_role_assignments`：同时包含 `tenant_id`、`membership_id` 和 `tenant_role_id`；
- `platform_role_assignments`：平台角色单独管理，不复用租户角色分配表。

Schema 允许以后创建 `tenant_roles.is_custom = true` 的角色，本增量不提供相应 API。

### 5.9 `tenant_invitations`

包含 `id`、`tenant_id`、目标类型、目标 Blind Index、Token Hash、预分配角色、邀请人、过期时间、接受/撤销时间、状态和版本。接受邀请的用户必须具有与目标 Blind Index 匹配的已验证身份。

邀请原始 Token 具有高熵、只发送一次，数据库只保存 HMAC-SHA-256 Hash。Token 通过前端链接 Fragment 接收，再放入 POST Body；不得放入 URL Path、Query、日志或审计元数据。

邀请预分配角色使用 `tenant_invitation_role_assignments` 关联表，字段同时包含 `tenant_id`、`invitation_id` 和 `tenant_role_id`，并通过复合外键阻止跨租户角色注入。

### 5.10 Session 与审计表

`refresh_token_records` 保存：

- Token Hash、`family_id`、`session_id`；
- `user_id`、可空的 `tenant_id` 和 `membership_id`；
- 签发、过期、空闲过期、使用、撤销时间；
- 替代 Token ID、撤销原因和脱敏设备信息。

`audit_events` 为追加写入事件，保存 Actor、Tenant、Action、Result、Reason Code、Target Type/ID、Trace ID、时间和脱敏客户端信息。不得保存 Token、密码、验证码、完整手机号、完整邮箱、邀请原文或请求正文。

### 5.11 `idempotency_records`

保存租户或用户范围、`Idempotency-Key` Hash、请求指纹、操作类型、执行状态、结果引用、过期时间和时间戳。唯一约束防止同一主体和操作重复执行；相同 Key 携带不同请求指纹时返回 409。记录不得直接缓存 Token、Cookie、密码或完整业务响应正文。

## 6. 加密与密钥

- 手机号、邮箱和微信 Subject 使用 AES-256-GCM 加密。
- 每个密文使用唯一 Nonce，并把字段用途与实体 ID 作为 AAD。
- 精确查询使用独立密钥的 HMAC-SHA-256 Blind Index。
- 数据加密密钥、Blind Index 密钥、Refresh Token Hash Key 和 JWT Signing Key 必须相互分离。
- 本地开发从未提交的环境变量或只读 Secret 文件加载密钥。
- 生产环境由中国大陆部署的 Vault/KMS 提供密钥和轮换能力。
- 密文和 Blind Index 设计包含 `key_version`，为以后轮换和重加密保留路径。

Blind Index 轮换采用不可跳步的两阶段发布门禁：

1. `expand`：新增可空 `blind_index_key_version`，已有 revision-01 行按本行 `key_version` 回填；滚动期保留 `BEFORE INSERT` 兼容 Trigger，使旧 Writer 省略新列时仍写入本行旧版本，并由 `BEFORE UPDATE` Trigger 拒绝 NULL 或 digest/version 非原子变化。
2. `legacy-compatible`：显式配置 legacy BI version，所有新 Worker 的 active BI version 必须与其相同。revision-01 Writer 不取得新 Advisory Lock，因此只要它仍可能运行，就禁止激活新 BI version；应用在构造身份服务或启动门禁时 Fail Closed。
3. 运维通过实例清单、部署状态和流量证据确认所有 revision-01 Writer 已清退。系统不得从数据库行、时间窗口或“看起来没有旧流量”自动推断已清退；必须写入显式 `legacy_writers_drained` 确认。
4. `rotation-ready`：只有 phase 与 drained acknowledgement 同时显式配置后才允许 active BI version 前进。Key Ring 必须继续保留 legacy version，以便查询旧行；新服务同时查询/锁定所有有效版本，并在认证成功事务中懒重索引。
5. `contract`：待旧 Writer 清退、NULL 行为零、持久化版本均有可用 Key、旧版本行完成重索引且回退方案验证后，才允许新增前向 Migration 删除兼容 Trigger、收紧列非空并按计划退役旧 Key。不得在当前 expand revision 中提前 contract。

Staging 与 Production 必须显式提供 `LAWYER_BLIND_INDEX_ROLLOUT_PHASE` 和 `LAWYER_BLIND_INDEX_LEGACY_KEY_VERSION`；进入 rotation-ready 时还必须显式设置 `LAWYER_BLIND_INDEX_LEGACY_WRITERS_DRAINED=true`。Development/Test 可以安全默认 `legacy-compatible + v1`，但仍执行同一强类型验证。Cipher Key 与 Blind Index Key 独立轮换，Cipher active version 不参与上述 BI phase 判断。

## 7. 认证与 Session 流程

### 7.1 注册与登录

`POST /api/v1/auth/register` 创建全局用户、用户名身份和密码凭据。`POST /api/v1/auth/login` 使用统一模糊错误，防止账号枚举。

登录成功返回无租户上下文的 Access Token，只允许读取个人信息、列出可用租户和选择租户；同时设置 Refresh Cookie。

### 7.2 Access Token

- 默认有效期 10 分钟；
- 平台特权 Token 默认有效期 5 分钟；
- 使用非对称 Ed25519/EdDSA 签名，包含 `kid` 并支持 Key Rotation；
- Claim 包含 `sub`、`tenant_id`、`membership_id`、`session_id`、`authz_version`、`jti`、`iat`、`nbf`、`exp`、`iss`、`aud`；
- 无租户个人 Token 使用独立 Account Audience；租户 Token 使用 Tenant Audience；平台审核和 Step-up Grant 使用不同 Purpose/Audience，三者不能互换；
- 不在 Token 中长期固化完整 Permission 列表；
- 通过 `Authorization: Bearer` 传递。

### 7.3 Refresh Token

- 原始值为至少 256-bit CSPRNG 随机数；
- 数据库保存 HMAC-SHA-256 Hash；
- Cookie 名为 `__Host-lawyer_refresh`，生产环境强制 `HttpOnly`、`Secure`、`SameSite=Lax`、`Path=/` 且不设置 Domain；
- 默认绝对有效期 30 天，空闲有效期 7 天；
- 每次刷新在行锁事务中消费旧 Token 并创建替代 Token；
- 已使用 Token 再次出现时撤销整个 Token Family，写审计并要求重新登录；
- 前端必须对刷新采用 Single-flight，不并发使用同一 Refresh Token。

Cookie 认证端点同时校验 Origin 和签名 Double-submit CSRF Token。Redis 不可用时，登录、刷新、重新认证和其他依赖限流或重放状态的端点返回 503。

### 7.4 租户切换

`POST /api/v1/auth/switch-tenant` 重新验证用户、租户、成员状态、成员有效期和租户状态。成功后撤销旧 Token Family，创建绑定目标租户与成员的新 Family，并返回租户绑定 Access Token。

路径 `tenant_id`、Token `tenant_id` 和当前成员关系必须一致。

### 7.5 重新认证和撤销

密码重新认证生成单用途、5 分钟有效、绑定 Session、租户和目标操作的 `StepUpGrant`。Redis 不可用时 Step-up 默认拒绝。

修改密码、禁用用户、撤销成员、改变高风险角色或主动注销时，递增相应安全版本并撤销相关 Session。

## 8. 租户创建、邀请与审核

### 8.1 创建租户

用户公开注册后可申请创建租户。事务同时完成：

1. 创建 `pending_verification` 租户；
2. 从内置模板实例化租户角色；
3. 创建所有者成员关系；
4. 分配所有者角色；
5. 写入审计事件。

任何一步失败都整体回滚。

### 8.2 成员邀请

管理员按手机号或邮箱创建邀请。系统保存目标 Blind Index、角色、期限和 Token Hash；开发 Provider 在测试中捕获一次性邀请值。

接受端点为 `POST /api/v1/invitations/accept`，原始 Token 放在 Request Body。服务端验证 Token、期限、状态、目标身份和目标租户后，原子创建或激活成员关系并消费邀请。

### 8.3 平台审核

平台审核员必须具有平台 Permission，并先完成密码重新认证。批准或拒绝租户使用 Step-up Grant，写完整审计事件。该能力只允许处理租户状态，不授予租户业务内容读取权。

### 8.4 首个平台管理员引导

系统不通过公开 API、Migration 或固定默认密码创建超级管理员。部署环境提供一次性管理命令，把一个已经完成注册的现有 `user_id` 赋予首个平台超级管理员角色。命令只能在当前不存在任何超级管理员时执行，必须读取独立部署 Secret、写审计事件并在事务中原子完成；Secret 不得通过命令行参数、代码或 Git 保存。已有超级管理员后，后续平台角色变化只能走受控管理流程和 Step-up 授权。

## 9. RBAC + ABAC

### 9.1 内置角色

租户角色：

- 租户所有者；
- 租户管理员；
- 部门管理员；
- 律师/法务；
- 助理；
- 教师；
- 学生；
- 外部客户。

平台角色：

- 超级管理员；
- 安全审计员；
- 运维支持；
- 内容运营。

平台角色不能自动读取租户内容。

### 9.2 Policy 输入输出

输入：

- `Principal`：用户、平台角色、认证方式、认证时间、Session 和安全版本；
- `TenantContext`：租户、成员、部门、租户状态和成员有效期；
- `Action`：稳定 Permission Code；
- `ResourceAttributes`：资源租户、部门、创建人、Matter 团队、密级、状态和审批状态。

输出 `AuthorizationDecision`：

- `allowed: bool`；
- `reason_code: str`；
- `policy_version: str`；
- `audit_required: bool`。

### 9.3 固定判断顺序

身份有效 → Session 有效 → 租户匹配 → 租户状态允许 → 成员有效 → RBAC Permission 允许 → ABAC 资源范围允许 → 资源状态允许。

任何缺失信息、未知 Action、未知角色或 Policy 异常都默认拒绝。

### 9.4 隔离防线

1. API 层验证路径租户与 Token 租户一致；
2. Service/Repository 层强制传入 `TenantContext`；
3. MySQL 使用包含 `tenant_id` 的复合唯一键和复合外键阻止跨租户关联。

禁止为租户私有资源提供不受限制的 `get_by_id(id)`。跨租户读取返回 404；已知资源上的操作权限不足返回 403；未认证或 Session 失效返回 401。

### 9.5 权限缓存

Redis Key 包含租户、成员和 `authz_version`。缓存未命中时回源 MySQL。Redis 故障时普通已认证请求可回源数据库，高风险平台操作默认拒绝。角色、成员或安全状态变更先提交 MySQL，再递增授权版本并删除缓存。

## 10. API 契约

### 10.1 认证与账户

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`
- `POST /api/v1/auth/reauth`
- `POST /api/v1/auth/switch-tenant`
- `GET /api/v1/me`
- `GET /api/v1/me/tenants`

### 10.2 租户与成员

- `POST /api/v1/tenants`
- `GET /api/v1/tenants/{tenant_id}`
- `PATCH /api/v1/tenants/{tenant_id}`
- `GET /api/v1/tenants/{tenant_id}/members`
- `POST /api/v1/tenants/{tenant_id}/invitations`
- `POST /api/v1/invitations/accept`
- `PATCH /api/v1/tenants/{tenant_id}/members/{membership_id}`
- `DELETE /api/v1/tenants/{tenant_id}/members/{membership_id}`

### 10.3 平台审核

- `GET /api/v1/platform/tenant-applications`
- `POST /api/v1/platform/tenant-applications/{tenant_id}/approve`
- `POST /api/v1/platform/tenant-applications/{tenant_id}/reject`

写操作支持 `Idempotency-Key`。成员列表使用游标分页。租户和成员更新使用 `version`/ETag 与条件请求，冲突返回稳定的 409 Problem Details。

Refresh Cookie、Token、Hash、加密值、Blind Index 和内部安全版本不得出现在 OpenAPI Response Schema、日志或错误详情中。

## 11. 错误、事务与并发

- 保持现有 Problem Details 风格与 `trace_id`；
- 认证失败使用统一错误代码和文案，不区分用户不存在、密码错误或身份未绑定；
- 唯一键冲突转换为稳定业务错误，不回显敏感标识；
- Refresh Rotation 使用 `SELECT ... FOR UPDATE` 或等价行锁；
- 邀请接受、租户创建、角色变更和成员撤销使用明确事务边界；
- 外部 Provider 调用不得位于长数据库事务内；
- 乐观锁版本不匹配返回 409；
- MySQL、Redis 或密码 Hash 服务超时使用有界超时，不无限重试；
- 不向客户端暴露 SQL、Constraint Name、堆栈或密码算法内部错误。

## 12. 限流、就绪与可观测性

新增 `/health/ready`，检查 MySQL 和 Redis。`/health/live` 不依赖外部服务。

Redis 限流维度至少包括：

- IP；
- 身份 Blind Index；
- Session；
- 租户；
- Endpoint 风险级别。

认证端点在 Redis 不可用时 Fail Closed 并返回 503；普通已认证读取可回源 MySQL。

认证、限流、Step-up 和授权缓存使用的安全控制 Redis 必须保持单一主 Key 空间，只允许
Standalone 或由 Sentinel/HA 解析出的 Primary，不支持分片 Redis Cluster。原因是 login 同时扣减
跨 Identity 共享的 IP Bucket 和跨 IP 共享的 Identity Bucket，两者无法通过固定 Hash Tag 保持业务语义并
天然落在同一 Cluster Slot；当前多 Key Lua 依赖单一主 Key 空间才能原子执行。Development/Test 安全默认
Standalone，Staging/Production 必须显式声明拓扑；Cluster、未识别值或非强类型值在创建 Redis Adapter
之前 Fail Closed。若未来支持 Cluster，必须单独批准并重设计组合限流原子协议。

指标至少包括认证结果、Refresh 重放、Policy 决策原因、数据库延迟、Redis 延迟、邀请状态和租户审核结果。Metric Label 不得包含手机号、邮箱、User ID 原文、Tenant Name、Token 或高基数字段。

## 13. 迁移与种子数据

- Alembic 创建全部表、约束和索引；
- Permission Catalog 与内置角色模板以确定性种子写入；
- Seed 重复运行必须幂等并校验 Code 含义未被静默改变；
- 在一次性 MySQL 中验证 `upgrade head`、`downgrade base` 和再次 `upgrade head`；
- 生产只执行已验证向前迁移；
- 已发布 Migration 不得修改；
- 破坏性迁移必须另行设计发布和恢复流程。
- 身份 revision-02 处于 expand 兼容期；后续 contract 必须新增前向 Migration，并以第 6 节的 Writer 清退、显式确认、NULL/Key/重索引检查作为发布门禁。

## 14. 测试策略

1. 单元测试：身份规范化、Blind Index、Argon2id、UUIDv7、Token Claim、Policy Engine、错误映射。
2. MySQL Repository 集成测试：事务、复合外键、唯一约束、软删除和乐观锁。
3. API 测试：注册、登录、刷新、注销、重新认证、租户切换、租户创建、邀请、成员修改和审核。
4. 跨租户反向测试：路径与 Token 租户不一致、跨租户角色/成员/部门引用、猜测 UUID、失效成员访问。
5. Session 测试：单次轮换、并发刷新、重放撤销 Family、密码或成员变化后失效。
6. 权限矩阵测试：每个内置角色的允许与拒绝动作，租户状态、部门和资源状态限制。
7. Migration 测试：真实 MySQL 上升级、降级和再次升级。
8. OpenAPI Snapshot：防止 Cookie、Secret 和内部字段泄露。
9. Blind Index 混合版本测试：revision-01 v7 Writer 与新 Worker 并存时，`legacy-compatible` 拒绝 active v8；active v7 并发注册只能留下一条身份；显式 `rotation-ready + drained` 后 v8 仍能查询并重索引 v7 行。

不得用 SQLite 替代 MySQL 集成测试，因为 `BINARY(16)`、复合外键、唯一约束、事务锁和并发语义不同。

## 15. 验收标准

1. Alembic 可在全新 MySQL 上完整升级，并在一次性数据库通过降级与再次升级验证。
2. 同一规范化用户名或已验证手机号、邮箱、微信身份不能绑定多个用户。
3. 同一用户可以拥有多个租户成员身份，并只能通过显式租户切换取得租户绑定 Token。
4. 任意跨租户访问均由 API、Policy/Repository 或数据库约束阻止。
5. Refresh Token 重放会撤销整个 Token Family。
6. 待审核租户不能启用外部客户服务。
7. 平台审核需要平台 Permission 和有效 Step-up Grant，且不授予租户内容访问权。
8. 审计事件覆盖关键身份、成员、授权和审核操作，且不含敏感原文。
9. pytest、Ruff、mypy、MySQL 集成测试、迁移测试和 OpenAPI 契约全部通过。
10. Docker Compose 仍可一键启动，所有 Secret 保持未提交状态。
11. Blind Index 轮换未取得显式旧 Writer 清退确认时不能激活新版本，且门禁发生在任何注册或认证写路径之前。

## 16. 规格自审时的安全修正

口头设计阶段曾使用 `POST /api/v1/invitations/{token}/accept` 表示邀请接受。书面规格将其修正为 `POST /api/v1/invitations/accept`，Token 只放入 Request Body。原因是 URL Path 可能被反向代理、浏览器历史和访问日志记录；该修正不改变邀请业务能力，但消除不必要的凭据泄露面。
