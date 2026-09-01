# 多租户身份、认证与授权 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 FastAPI 底座上交付可供后续全部法律业务复用的 MySQL 身份、租户隔离、会话安全、RBAC + ABAC、邀请与平台审核能力。

**Architecture:** 采用模块化单体，领域类型和 Policy Engine 不依赖 FastAPI、SQLAlchemy 或 Redis；应用服务通过显式 Port 与 Unit of Work 编排事务；基础设施层使用 SQLAlchemy 2 异步 ORM、MySQL 8.4、Redis 和 Alembic。Access Token 仅携带身份与版本快照，每次请求以权威 Session/User/Membership 状态校验；租户私有查询同时由路径/Token、Repository 谓词和 MySQL 复合外键隔离。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.0、Alembic、asyncmy、MySQL 8.4、redis-py asyncio、Argon2id、AES-256-GCM、PyJWT EdDSA、pytest、Ruff、mypy、Docker Compose。

## Global Constraints

- 全部源代码、迁移、测试、配置和文档使用 UTF-8；产品结论只适用中国大陆。
- Python 版本固定为 `>=3.12,<3.14`；SQLAlchemy 固定 `>=2.0.52,<2.1`，Alembic 固定 `>=1.19.1,<2`。
- 使用 `mysql+asyncmy` 和显式 `AsyncSession`；禁止共享 Session、隐式 lazy I/O 和 SQLite 替代 MySQL 集成测试。
- 业务 ID 是 RFC 9562 UUIDv7；API 为小写标准 UUID 字符串，MySQL 为不交换字节序的 `BINARY(16)`。
- 时间统一为 UTC；MySQL 使用 `DATETIME(6)`、InnoDB 和 `utf8mb4`。
- 已验证身份全平台唯一；敏感 Subject 用 AES-256-GCM，加密检索用独立 HMAC-SHA-256 Blind Index。
- Access Token 只允许 Ed25519/EdDSA，账户、租户、平台和 Step-up Audience 不可互换；默认 10 分钟，平台与 Step-up 默认 5 分钟。
- Refresh Token 至少 256 bit，数据库只存用途隔离 HMAC；绝对有效期 30 天、空闲有效期 7 天、每次使用轮换，重放撤销整个 Family。
- Cookie 端点强制可信 Origin 与签名 Double-submit CSRF；认证、Refresh、Step-up 或限流所需 Redis 不可用时返回 503。
- RBAC + ABAC 默认拒绝；任何未知 Action、缺失上下文、异常、跨租户引用或无效成员均拒绝。
- 每个租户私有 Repository 方法必须接收 `TenantContext`；不得提供私有资源的裸 `get_by_id(id)`。
- 所有写端点使用 Idempotency-Key；更新使用 `If-Match`/ETag，版本冲突按已批准契约返回 409。
- Token、Cookie、密码、验证码、密文、Blind Index、内部版本和个人信息不得出现在响应 Schema、日志、审计或指标标签。
- Blind Index revision-02 保持 expand 兼容：`legacy-compatible` 时 active version 必须等于显式 legacy version；只有人工确认 revision-01 Writer 全部清退并配置 `rotation-ready + legacy_writers_drained` 后才可激活新版本，系统不得自动猜测清退状态。
- 开发 Provider 可以在测试 Fixture 捕获 OTP/邀请值；本增量不接入真实短信、邮件或微信供应商。
- 使用 TDD；每个任务先观察指定失败，再实现最小正确行为并提交；已发布 Migration 不得改写。

## Implementation Addendum

以下补正用于实现已确认的即时撤销、平台审核和默认拒绝要求，不扩大产品范围：

1. 增加 `auth_sessions`，保存 `user_id`、可空租户/成员、当前 Family、签发时版本、撤销和到期状态。Access Token 增加 `auth_version`；租户 Token 同时包含 `authz_version`。每次受保护请求校验权威状态，Redis 只作最长 60 秒且可主动删除的缓存。
2. 增加 `platform_roles` 与 `platform_role_permissions`，使 `platform_role_assignments` 有可审计的角色和权限来源。平台角色仍不获得租户内容读取权限。
3. 每张可被租户内复合外键引用的表增加 `UNIQUE(tenant_id, id)`。
4. Step-up Grant 是 Redis 中一次性、绑定 `session_id + tenant_id + action` 的 5 分钟记录，通过 `X-Step-Up-Grant` 传递并原子消费。
5. 本增量使用最小 Permission Catalog：`tenant.read`、`tenant.update`、`department.read`、`department.manage`、`membership.read`、`membership.invite`、`membership.update`、`membership.revoke`、`role.read`、`role.assign`、`external_service.enable`、`tenant_application.read`、`tenant_application.review`、`platform_admin.bootstrap`。角色矩阵采用最小权限，需在公网生产开放前由产品和安全负责人复核。
6. Blind Index 轮换发布顺序固定为 `expand -> legacy-compatible（active=legacy）-> 人工确认旧 Writer 清退 -> 显式 rotation-ready/drained -> 激活新 BI version并保留旧 Key -> 完成重索引 -> 后续前向 contract Migration`。应用以强类型 `BlindIndexRolloutPolicy` 在 `IdentityService` 构造期 Fail Closed；Staging/Production 的 phase 与 legacy version 不允许隐式默认。

## File Map

```text
backend/
  pyproject.toml
  uv.lock
  alembic.ini
  alembic/env.py
  alembic/script.py.mako
  alembic/versions/20260901_01_identity_authz_baseline.py
  src/lawyer_agent/
    config.py
    main.py
    api/errors.py
    api/dependencies.py
    api/v1/router.py
    api/v1/auth.py
    api/v1/accounts.py
    api/v1/tenants.py
    api/v1/invitations.py
    api/v1/platform.py
    application/errors.py
    application/identity.py
    application/sessions.py
    application/tenancy.py
    application/invitations.py
    application/platform.py
    application/idempotency.py
    domain/common.py
    domain/identity.py
    domain/sessions.py
    domain/tenancy.py
    domain/authorization.py
    infrastructure/persistence/base.py
    infrastructure/persistence/types.py
    infrastructure/persistence/engine.py
    infrastructure/persistence/uow.py
    infrastructure/persistence/models/{identity,tenancy,authorization,sessions,audit}.py
    infrastructure/persistence/repositories/{identity,tenancy,authorization,sessions,audit}.py
    infrastructure/persistence/seed_authz.py
    infrastructure/security/{passwords,cipher,blind_index,jwt_tokens,csrf}.py
    infrastructure/redis/{client,rate_limit,authz_cache,step_up}.py
    infrastructure/providers/development.py
    cli/bootstrap_platform_admin.py
  tests/
    conftest.py
    unit/{test_ids,test_identity_security,test_policy,test_tokens,test_csrf,test_rate_limit}.py
    integration/mysql/{conftest,test_migrations,test_identity,test_tenant_constraints,test_sessions,test_idempotency}.py
    integration/redis/{test_rate_limit,test_step_up}.py
    api/{test_auth,test_tenants,test_invitations,test_platform,test_cross_tenant_isolation}.py
    contract/test_openapi_identity_authz.py
deploy/compose.yaml
deploy/compose.env.example
scripts/migrate.ps1
docs/project-decisions/pending-production-reviews.md
```

---

### Task 1: 依赖、严格配置与异步基础设施

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `backend/src/lawyer_agent/config.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/base.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/types.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/engine.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/uow.py`
- Create: `backend/tests/unit/test_ids.py`
- Create: `backend/tests/unit/test_settings_security.py`

**Interfaces:**
- Consumes: 现有 `Settings` 和 Python 3.12。
- Produces: `new_uuid7() -> UUID`、`UuidBinary`、`Base`、`create_engine(settings) -> AsyncEngine`、`create_session_factory(engine) -> async_sessionmaker[AsyncSession]`、`SqlAlchemyUnitOfWork`。

- [ ] **Step 1: 写 UUID 与生产配置失败测试**

```python
def test_uuid7_uses_standard_network_bytes() -> None:
    value = new_uuid7()
    assert value.version == 7
    assert value.variant == RFC_4122
    assert UUID(bytes=value.bytes) == value


def test_production_rejects_shared_or_missing_security_keys() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", secret_key="x" * 32)
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_ids.py tests/unit/test_settings_security.py -v`

Expected: collection 因 `domain.common` 或新增 Settings 字段不存在而失败。

- [ ] **Step 3: 增加依赖与完整配置边界**

在 `dependencies` 中加入：

```toml
"SQLAlchemy[asyncio]>=2.0.52,<2.1",
"alembic>=1.19.1,<2",
"asyncmy>=0.2.11,<1",
"redis>=8.1,<9",
"argon2-cffi>=25.1,<26",
"cryptography>=48,<50",
"PyJWT[crypto]>=2.13,<3",
"email-validator>=2.2,<3",
"phonenumbers>=9,<10",
```

`Settings` 增加 `database_url`、`redis_url`、池参数、可信 Origin、Cookie 开关，以及分别 Base64 编码的 32-byte data/blind-index/refresh/csrf key 和 Ed25519 key ring。Production validator 必须拒绝缺失、重复 key、非 HTTPS Origin、`cookie_secure=False` 和通配 Origin；test/development 可使用显式测试值。

- [ ] **Step 4: 实现 UUIDv7、类型、Engine 与 UoW**

```python
def new_uuid7(*, now_ms: int | None = None) -> UUID:
    timestamp = int(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    if not 0 <= timestamp < 1 << 48:
        raise ValueError("UUIDv7 timestamp out of range")
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return UUID(int=value)


class UuidBinary(TypeDecorator[UUID]):
    impl = BINARY(16)
    cache_ok = True

    def process_bind_param(self, value: UUID | None, dialect: Dialect) -> bytes | None:
        return None if value is None else value.bytes

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> UUID | None:
        return None if value is None else UUID(bytes=value)
```

Metadata naming convention固定为 `pk/fk/uq/ck/ix`；Engine 使用 `pool_pre_ping=True`、有界 pool timeout，连接 hook 设置 `time_zone='+00:00'`；sessionmaker 使用 `expire_on_commit=False, autoflush=False`。UoW 在异常时 rollback，Repository 不得 commit。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv lock; uv sync --frozen; uv run pytest tests/unit/test_ids.py tests/unit/test_settings_security.py -v; uv run ruff check .; uv run mypy src`

Expected: 聚焦测试全部通过，Ruff/mypy 为零错误。

Commit: `git commit -m "feat: add async persistence foundation"`

### Task 2: 完整基线 Schema、Alembic 与确定性 Seed

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/alembic/versions/20260901_01_identity_authz_baseline.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/models/*.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/seed_authz.py`
- Create: `backend/tests/integration/mysql/conftest.py`
- Create: `backend/tests/integration/mysql/test_migrations.py`
- Create: `backend/tests/unit/test_authz_seed.py`

**Interfaces:**
- Consumes: Task 1 的 `Base`、`UuidBinary`、Engine。
- Produces: 规格第 5 节全部表，另含 `auth_sessions`、`platform_roles`、`platform_role_permissions`；`seed_authorization_catalog(session) -> None`。

- [ ] **Step 1: 写真实 MySQL Migration 与 Seed 漂移测试**

```python
@pytest.mark.mysql
def test_baseline_round_trip(mysql_url: str) -> None:
    command.upgrade(alembic_config(mysql_url), "head")
    assert REQUIRED_TABLES <= set(inspect(sync_engine(mysql_url)).get_table_names())
    command.downgrade(alembic_config(mysql_url), "base")
    command.upgrade(alembic_config(mysql_url), "head")


async def test_seed_is_idempotent_and_rejects_semantic_drift(session: AsyncSession) -> None:
    await seed_authorization_catalog(session)
    await seed_authorization_catalog(session)
    await session.execute(update(PermissionModel).where(PermissionModel.code == "tenant.read").values(action="delete"))
    with pytest.raises(SeedDriftError):
        await seed_authorization_catalog(session)
```

- [ ] **Step 2: 确认空项目不能迁移**

Run: `cd backend; uv run pytest tests/integration/mysql/test_migrations.py tests/unit/test_authz_seed.py -v`

Expected: 因 Alembic 环境和模型不存在失败。

- [ ] **Step 3: 建立全部模型和约束**

Migration 必须显式创建规格中的全部表；状态用 `VARCHAR + CHECK`，不用 MySQL native ENUM。每个可租户引用父表都同时具备：

```python
UniqueConstraint("tenant_id", "id"),
ForeignKeyConstraint(
    ["tenant_id", "membership_id"],
    ["tenant_memberships.tenant_id", "tenant_memberships.id"],
),
```

`auth_sessions` 字段固定为 `id,user_id,tenant_id,membership_id,current_family_id,auth_version_at_issue,authz_version_at_issue,revoked_at,revocation_reason,last_seen_at,expires_at,version,created_at,updated_at`。`refresh_token_records` 以复合 FK 绑定租户成员，并具有 `token_hash` 唯一键、`family_id` 和 `replaced_by_id` 索引。

- [ ] **Step 4: 实现确定性 Permission/Role Seed**

```python
PERMISSIONS = (
    PermissionSeed("tenant.read", "tenant", "read", "medium"),
    PermissionSeed("tenant.update", "tenant", "update", "high"),
    PermissionSeed("department.read", "department", "read", "low"),
    PermissionSeed("department.manage", "department", "manage", "medium"),
    PermissionSeed("membership.read", "membership", "read", "medium"),
    PermissionSeed("membership.invite", "membership", "invite", "high"),
    PermissionSeed("membership.update", "membership", "update", "high"),
    PermissionSeed("membership.revoke", "membership", "revoke", "high"),
    PermissionSeed("role.read", "role", "read", "medium"),
    PermissionSeed("role.assign", "role", "assign", "high"),
    PermissionSeed("external_service.enable", "external_service", "enable", "high"),
    PermissionSeed("tenant_application.read", "tenant_application", "read", "high"),
    PermissionSeed("tenant_application.review", "tenant_application", "review", "critical"),
    PermissionSeed("platform_admin.bootstrap", "platform_admin", "bootstrap", "critical"),
)
```

Seed 只插入缺失项；同 Code 语义或权限集合不同立即抛 `SeedDriftError`，不得覆盖。Migration 不创建默认管理员。

- [ ] **Step 5: 验证 Migration 往返与提交**

Run: `cd backend; uv run pytest tests/integration/mysql/test_migrations.py tests/unit/test_authz_seed.py -v`

Expected: 在 MySQL 8.4 上 `upgrade -> downgrade -> upgrade` 通过，Seed 重复运行不产生重复行。

Commit: `git commit -m "feat: add identity authorization database baseline"`

### Task 3: 身份规范化、密码与敏感字段保护

**Files:**
- Create: `backend/src/lawyer_agent/domain/identity.py`
- Create: `backend/src/lawyer_agent/infrastructure/security/passwords.py`
- Create: `backend/src/lawyer_agent/infrastructure/security/cipher.py`
- Create: `backend/src/lawyer_agent/infrastructure/security/blind_index.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/identity.py`
- Create: `backend/src/lawyer_agent/application/identity.py`
- Create: `backend/tests/unit/test_identity_security.py`
- Create: `backend/tests/integration/mysql/test_identity.py`
- Modify: `backend/src/lawyer_agent/config.py`
- Create: `backend/alembic/versions/20260901_02_blind_index_key_version.py`
- Create: `backend/tests/integration/mysql/test_identity_locks.py`
- Create: `backend/tests/integration/mysql/test_identity_migration.py`
- Modify: `deploy/compose.env.example`
- Modify: `docs/superpowers/specs/2026-09-01-multitenant-identity-authorization-design.md`

**Interfaces:**
- Produces: `normalize_identifier(kind, value) -> NormalizedIdentity`、`Argon2PasswordHasher`、`SensitiveValueCipher.encrypt/decrypt`、`BlindIndexService.digest`、强类型 `BlindIndexRolloutPolicy`、`IdentityService.register`、`IdentityService.authenticate`。

- [ ] **Step 1: 写规范化、密码和 AAD 反向测试**

```python
def test_username_nfkc_trim_casefold() -> None:
    assert normalize_username("  ＬＡＷＹＥＲ ") == "lawyer"


def test_ciphertext_cannot_move_between_rows(cipher: SensitiveValueCipher) -> None:
    envelope = cipher.encrypt("13800138000", aad=b"auth_identity:id-a:subject")
    with pytest.raises(InvalidTag):
        cipher.decrypt(envelope, aad=b"auth_identity:id-b:subject")


def test_blind_indexes_are_purpose_isolated(blind: BlindIndexService) -> None:
    assert blind.digest("identity:phone", "+8613800138000") != blind.digest("invite:phone", "+8613800138000")
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_identity_security.py tests/integration/mysql/test_identity.py -v`

Expected: 新模块不存在。

- [ ] **Step 3: 实现规范化与安全 Adapter**

用户名执行 NFKC/trim/casefold；手机号经 `phonenumbers` 解析为中国大陆 `+86` E.164 且必须 valid；邮箱 trim、local-part casefold、域名 IDNA lower；微信只按 issuer + 原始 subject。密码接受 12–128 字符且 UTF-8 不超过 1024 bytes，不进行 Unicode 规范化。Argon2 固定 `memory_cost=65536,time_cost=3,parallelism=1,hash_len=32,salt_len=16`，成功验证后才按 `check_needs_rehash` 更新。

AESGCM envelope 为 `key_version(2 bytes) + nonce(12 bytes) + ciphertext_and_tag`，每次随机 nonce；Blind Index 使用用途前缀、key version 与 `hmac.compare_digest`。

Blind Index 的 cipher version 与 index version 分列保存。revision-02 只做 expand：列保持 nullable 且无固定 default；旧行以本行 `key_version` 回填，INSERT Trigger 为旧 Writer 补同一版本，UPDATE Trigger 要求 digest/version 成对变化。Staging/Production 必须显式配置 rollout phase 与 legacy version；Development/Test 安全默认 `legacy-compatible + v1`。

- [ ] **Step 4: 实现注册/认证事务**

```python
@dataclass(frozen=True, slots=True)
class RegisterCommand:
    username: str
    password: str
    display_name: str


class IdentityService:
    async def register(self, command: RegisterCommand) -> RegisteredUser:
        async with self._uow_factory() as uow:
            return await self._register_in_transaction(uow, command)

    async def authenticate(
        self,
        identifier: LoginIdentifier,
        password: str,
    ) -> AuthenticatedUser | None:
        normalized = normalize_identifier(identifier.kind, identifier.value)
        return await self._authenticate_normalized(normalized, password)
```

`register` 在一个 UoW 写 User、已验证 local username identity、Argon2 credential 和审计。数据库唯一冲突映射为不泄露具体标识的 `identity_conflict`。`authenticate` 在账号不存在时仍验证固定 dummy hash；任何不存在、密码错误、锁定或禁用均返回同一失败结果。

注册和认证对 Key Ring 全部有效 BI version 查询；注册按稳定版本顺序获取 Advisory Lock。`IdentityService` 构造期先验证 `BlindIndexRolloutPolicy`：`legacy-compatible` 只能 active=legacy；`rotation-ready` 必须带人工清退确认并保留 legacy Key。真实 MySQL 测试必须覆盖旧 v7 Writer 与新 v7 Worker 并发只有一条身份、旧 Writer 存续时 v8 配置启动失败，以及明确 rotation-ready 后 v8 查询并重索引旧 v7 行。后续 contract 必须另建 Migration，且只能在旧 Writer 清退、NULL 清零、旧行重索引和回退验证均完成后执行。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/unit/test_identity_security.py tests/unit/test_identity_application.py tests/unit/test_settings_security.py tests/integration/mysql/test_identity.py tests/integration/mysql/test_identity_locks.py tests/integration/mysql/test_identity_migration.py -v; uv run ruff check .; uv run mypy src`

Expected: 全局身份唯一、密文和索引隔离、真实混合版本并发、显式 rollout 启动门禁与 expand/trigger 生命周期测试通过。

Commit: `git commit -m "feat: add protected global identities"`

### Task 4: JWT、权威 Session、Refresh Rotation 与 CSRF

**Files:**
- Create: `backend/src/lawyer_agent/domain/sessions.py`
- Create: `backend/src/lawyer_agent/infrastructure/security/jwt_tokens.py`
- Create: `backend/src/lawyer_agent/infrastructure/security/csrf.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/sessions.py`
- Create: `backend/src/lawyer_agent/application/sessions.py`
- Create: `backend/tests/unit/test_tokens.py`
- Create: `backend/tests/unit/test_csrf.py`
- Create: `backend/tests/integration/mysql/test_sessions.py`

**Interfaces:**
- Produces: `AccessTokenClaims`、`TokenService.issue_account/issue_tenant/verify`、`CsrfService`、`SessionService.start/refresh/revoke/switch_tenant`。

- [ ] **Step 1: 写算法/Audience/重放/并发失败测试**

```python
def test_account_token_is_not_tenant_token(tokens: TokenService) -> None:
    encoded = tokens.issue_account(account_claims())
    with pytest.raises(InvalidToken):
        tokens.verify(encoded, audience=Audience.TENANT)


@pytest.mark.mysql
async def test_parallel_refresh_revokes_family(session_service_factory) -> None:
    raw = await create_refresh_token(session_service_factory)
    first, second = await asyncio.gather(
        session_service_factory().refresh(raw),
        session_service_factory().refresh(raw),
        return_exceptions=True,
    )
    assert sum(isinstance(value, RefreshResult) for value in (first, second)) == 1
    assert await family_is_revoked(first.family_id if isinstance(first, RefreshResult) else second.family_id)
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_tokens.py tests/unit/test_csrf.py tests/integration/mysql/test_sessions.py -v`

Expected: Token/Session 模块不存在。

- [ ] **Step 3: 实现 EdDSA Token 与 Session 校验**

```python
decoded = jwt.decode(
    encoded,
    public_key,
    algorithms=["EdDSA"],
    audience=expected_audience.value,
    issuer=settings.jwt_issuer,
    options={"require": ["sub", "sid", "jti", "iat", "nbf", "exp", "auth_version"]},
    leeway=30,
)
```

必须先读取未验证 Header 选择允许的 `kid`，但 `alg` 永远由配置固定为 EdDSA。Session validator 比对未撤销、未到期、User `auth_version`，租户 Token 还比对 Tenant/Membership 状态与 `authz_version`；缓存最长 60 秒，缓存故障回源 MySQL。

- [ ] **Step 4: 实现 Refresh 行锁、重放与 CSRF**

Refresh 原始值 `secrets.token_urlsafe(32)`，Hash 为用途隔离 HMAC-SHA-256。事务固定为 `SELECT FOR UPDATE -> 校验 -> 标记 used -> 插入 replacement -> 更新 session -> audit -> commit`。已使用 Token 再现时在同一事务撤销整个 Family 与 Session；服务端不自动重试 Refresh。

CSRF 格式为 `b64url(nonce).b64url(HMAC(csrf_key, "csrf:v1:{sid}:{nonce}"))`；验证顺序是可信 Origin、header/cookie constant-time equality、基于 Session 的 MAC。Cookie 名为 `__Host-lawyer_refresh` 和 `__Host-lawyer_csrf`，生产强制 Secure、Path `/`、无 Domain；Refresh 为 HttpOnly，CSRF 可被前端读取。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/unit/test_tokens.py tests/unit/test_csrf.py tests/integration/mysql/test_sessions.py -v`

Expected: 不可信算法/Audience/KID/过期 Token 拒绝；并发刷新一个成功后整个 Family 因重放被撤销。

Commit: `git commit -m "feat: add revocable token sessions"`

### Task 5: Redis 限流、Step-up 与授权缓存

**Files:**
- Create: `backend/src/lawyer_agent/infrastructure/redis/client.py`
- Create: `backend/src/lawyer_agent/infrastructure/redis/rate_limit.py`
- Create: `backend/src/lawyer_agent/infrastructure/redis/step_up.py`
- Create: `backend/src/lawyer_agent/infrastructure/redis/authz_cache.py`
- Create: `backend/tests/unit/test_rate_limit.py`
- Create: `backend/tests/integration/redis/test_rate_limit.py`
- Create: `backend/tests/integration/redis/test_step_up.py`

**Interfaces:**
- Produces: `RateLimiter.consume(rule, dimensions) -> RateLimitDecision`、`StepUpStore.issue/consume`、`AuthorizationCache.get/set/invalidate`。

- [ ] **Step 1: 写原子限流与一次性 Grant 测试**

```python
async def test_step_up_is_action_bound_and_single_use(store: StepUpStore) -> None:
    grant = await store.issue(session_id=SID, tenant_id=TID, action="tenant_application.review")
    assert await store.consume(grant, SID, TID, "tenant_application.review") is True
    assert await store.consume(grant, SID, TID, "tenant_application.review") is False


async def test_rate_limit_keys_never_contain_raw_identity(limiter: RateLimiter, redis_spy) -> None:
    await limiter.consume(LOGIN_RULE, {"identity": "+8613800138000"})
    assert all("13800138000" not in key for key in redis_spy.keys)
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_rate_limit.py tests/integration/redis -v`

Expected: Redis adapters 不存在。

- [ ] **Step 3: 实现 Lua Token Bucket 和故障分类**

限流 Key 的各维度先 HMAC，规则固定为 register IP 5/h；login IP 20/15m + identity 5/15m；refresh session 30/15m；reauth session 5/15m；switch session 10/15m。Lua 在单命令中 refill、扣减、设置 TTL 并返回剩余数与 Retry-After。Redis connection/timeout 在认证、安全写和 Step-up 路径映射为 503；普通 Bearer 读授权缓存故障回源 MySQL。安全控制 Redis 通过强类型 `SecurityRedisTopology` 在 Settings 与 Adapter 创建前门禁，只允许单一主 Key 空间的 Standalone 或 Sentinel/HA Primary；Staging/Production 必须显式配置，Cluster/Sharded 与未知值 Fail Closed。当前 login 的 IP Bucket 必须跨 Identity 共享、Identity Bucket 必须跨 IP 共享，因此不以改变 Hash Tag 语义规避 CROSSSLOT；未来若支持 Redis Cluster，需重新批准并设计组合限流原子协议。

- [ ] **Step 4: 实现一次性 Step-up 和版本化授权缓存**

Step-up Key 保存 HMAC 后的 grant id，TTL 300 秒，value 绑定 user/session/tenant/action；消费使用 Lua compare-and-delete。授权 Key 固定 `authz:{tenant_id}:{membership_id}:{authz_version}`，只缓存 permission code 集合和低敏 ABAC scope，角色/成员变更提交后删除旧版本 Key。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/unit/test_rate_limit.py tests/integration/redis -v; uv run ruff check .; uv run mypy src`

Expected: 并发限流不超额、Grant 第二次消费失败、Redis 故障策略符合规格。

Commit: `git commit -m "feat: add security redis controls"`

### Task 6: 纯 Policy Engine 与租户隔离 Repository

**Files:**
- Create: `backend/src/lawyer_agent/domain/authorization.py`
- Create: `backend/src/lawyer_agent/domain/tenancy.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/tenancy.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/authorization.py`
- Create: `backend/tests/unit/test_policy.py`
- Create: `backend/tests/integration/mysql/test_tenant_constraints.py`

**Interfaces:**
- Produces: `Principal`、`TenantContext`、`Action`、`ResourceAttributes`、`AuthorizationDecision`、`PolicyEngine.decide(principal, context, action, resource, now)` 和 tenant-scoped repositories。

- [ ] **Step 1: 写固定决策顺序和跨租户反向测试**

```python
def test_policy_fails_closed_in_fixed_order(policy: PolicyEngine) -> None:
    decision = policy.decide(invalid_principal(), mismatched_context(), Action.TENANT_READ, resource(), NOW)
    assert decision == AuthorizationDecision(False, "identity_invalid", POLICY_VERSION, True)


@pytest.mark.mysql
async def test_cross_tenant_role_assignment_is_rejected(session: AsyncSession) -> None:
    session.add(MembershipRoleAssignment(tenant_id=TENANT_A, membership_id=MEMBER_A, tenant_role_id=ROLE_B))
    with pytest.raises(IntegrityError):
        await session.commit()
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_policy.py tests/integration/mysql/test_tenant_constraints.py -v`

Expected: Policy 和 Repository 不存在。

- [ ] **Step 3: 实现强类型默认拒绝 Policy**

```python
class Action(StrEnum):
    TENANT_READ = "tenant.read"
    TENANT_UPDATE = "tenant.update"
    MEMBERSHIP_INVITE = "membership.invite"
    MEMBERSHIP_REVOKE = "membership.revoke"
    TENANT_APPLICATION_REVIEW = "tenant_application.review"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason_code: str
    policy_version: str
    audit_required: bool
```

判断顺序严格为 identity → session → tenant match/status → membership status/time → permission → department/owner/shared scope → resource state。未知 Action/角色/字段和任何异常返回拒绝。领域包不得 import FastAPI、SQLAlchemy 或 Redis。

- [ ] **Step 4: 实现显式 TenantContext Repository**

```python
class MembershipRepository:
    async def get(self, context: TenantContext, membership_id: UUID) -> Membership | None:
        stmt = select(TenantMembershipModel).where(
            TenantMembershipModel.tenant_id == context.tenant_id,
            TenantMembershipModel.id == membership_id,
        )
        return to_domain(await self._session.scalar(stmt))
```

所有 tenant 表读写都重复 tenant predicate；跨租户猜测返回无结果。Department parent、Membership department、Role assignment、Invitation role 均由包含 tenant 的复合 FK 阻断。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/unit/test_policy.py tests/integration/mysql/test_tenant_constraints.py -v`

Expected: 全角色允许/拒绝矩阵、状态/部门限制和全部复合 FK 反向测试通过。

Commit: `git commit -m "feat: enforce tenant authorization policy"`

### Task 7: 租户创建、成员管理、审计与幂等

**Files:**
- Create: `backend/src/lawyer_agent/application/tenancy.py`
- Create: `backend/src/lawyer_agent/application/idempotency.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/audit.py`
- Create: `backend/tests/integration/mysql/test_idempotency.py`
- Create: `backend/tests/api/test_tenants.py`

**Interfaces:**
- Produces: `TenantService.create_application/get/update/list_members/update_member/revoke_member`、`IdempotencyService.reserve/complete`、append-only `AuditRepository`。

- [ ] **Step 1: 写事务全有或全无、ETag 和幂等测试**

```python
async def test_tenant_bootstrap_rolls_back_when_role_clone_fails(service, db) -> None:
    service.role_templates.fail_on_clone = True
    with pytest.raises(RoleTemplateUnavailable):
        await service.create_application(command())
    assert await db.scalar(select(func.count()).select_from(TenantModel)) == 0


async def test_same_idempotency_key_with_other_fingerprint_is_conflict(client) -> None:
    first = await client.post("/api/v1/tenants", headers={"Idempotency-Key": KEY}, json=PAYLOAD)
    second = await client.post("/api/v1/tenants", headers={"Idempotency-Key": KEY}, json=OTHER_PAYLOAD)
    assert first.status_code == 201
    assert second.status_code == 409
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_idempotency.py tests/api/test_tenants.py -v`

Expected: 应用服务/API 不存在。

- [ ] **Step 3: 实现租户原子创建与成员变更**

同一 UoW 内依次 reserve idempotency、创建 `pending_verification` tenant、克隆角色模板、创建 active owner membership、分配 owner、写审计、完成 idempotency。Pending 租户允许内部部门/成员配置但拒绝 `external_service.enable`。成员/角色变化必须在事务中递增 `authz_version`、撤销相关 Session；提交后删除缓存。

- [ ] **Step 4: 实现幂等、游标与 ETag**

Idempotency scope 固定：账户写为 `user_id`，租户写为 `membership_id`，平台审核为 platform `user_id`，邀请接受为当前 `user_id`。Key 只接受 16–128 ASCII；Fingerprint 为 method + canonical route + 去除秘密字段后的规范 JSON。相同 key/fingerprint 重读权威对象；不同 fingerprint 返回 409。成员游标编码 `(created_at,id)`；PATCH/DELETE 必须 `If-Match: "<version>"`，版本不匹配返回 409。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/integration/mysql/test_idempotency.py tests/api/test_tenants.py -v`

Expected: tenant bootstrap 原子、幂等无重复副作用、乐观锁和分页稳定。

Commit: `git commit -m "feat: add tenant membership workflows"`

### Task 8: 邀请、平台审核与首管理员引导

**Files:**
- Create: `backend/src/lawyer_agent/application/invitations.py`
- Create: `backend/src/lawyer_agent/application/platform.py`
- Create: `backend/src/lawyer_agent/infrastructure/providers/development.py`
- Create: `backend/src/lawyer_agent/cli/bootstrap_platform_admin.py`
- Create: `backend/tests/api/test_invitations.py`
- Create: `backend/tests/api/test_platform.py`

**Interfaces:**
- Produces: `InvitationService.create/accept`、`PlatformReviewService.list/approve/reject`、CLI `python -m lawyer_agent.cli.bootstrap_platform_admin`。

- [ ] **Step 1: 写 body-only Token、身份匹配和 Step-up 测试**

```python
async def test_invitation_token_is_only_accepted_in_body(client, invitation_token) -> None:
    assert (await client.post(f"/api/v1/invitations/{invitation_token}/accept")).status_code == 404
    response = await client.post("/api/v1/invitations/accept", json={"token": invitation_token})
    assert response.status_code == 200


async def test_platform_review_requires_matching_single_use_step_up(client, reviewer_headers) -> None:
    response = await client.post(f"/api/v1/platform/tenant-applications/{TENANT_ID}/approve", headers=reviewer_headers)
    assert response.status_code == 403
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/api/test_invitations.py tests/api/test_platform.py -v`

Expected: 路由和服务不存在。

- [ ] **Step 3: 实现邀请原子接受与安全交付**

创建时规范化目标并使用 `invite:<kind>` Blind Index，生成至少 256-bit Token，数据库只保存 HMAC；开发 Delivery Adapter 仅在 `environment=test` 捕获一次性值，HTTP Response 永不返回。接受时锁 Invitation，校验期限/状态/已验证身份，在一个事务创建或激活 Membership、复制预分配角色、消费邀请和写审计；Token 不进入日志、错误、URL 或指标。

- [ ] **Step 4: 实现平台审核与一次性 Bootstrap CLI**

平台列表只返回申请投影，不 join 租户内容。Approve/Reject 必须 platform audience、对应 permission、目标 tenant/action 绑定的未消费 Step-up。CLI 从 stdin 或 Secret 文件读取独立 bootstrap secret，禁止 argv；获取 MySQL advisory lock，只在不存在 super admin 时给现有 user_id 赋权并写审计，第二次运行失败。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/api/test_invitations.py tests/api/test_platform.py -v`

Expected: 邀请目标和跨租户角色注入被拒绝；平台审核不授予租户内容读取；Grant 和 Bootstrap 均只能使用一次。

Commit: `git commit -m "feat: add invitations and platform review"`

### Task 9: HTTP 认证契约、依赖注入与错误映射

**Files:**
- Modify: `backend/src/lawyer_agent/api/errors.py`
- Create: `backend/src/lawyer_agent/api/dependencies.py`
- Create: `backend/src/lawyer_agent/api/v1/router.py`
- Create: `backend/src/lawyer_agent/api/v1/auth.py`
- Create: `backend/src/lawyer_agent/api/v1/accounts.py`
- Create: `backend/src/lawyer_agent/api/v1/tenants.py`
- Create: `backend/src/lawyer_agent/api/v1/invitations.py`
- Create: `backend/src/lawyer_agent/api/v1/platform.py`
- Modify: `backend/src/lawyer_agent/main.py`
- Create: `backend/tests/api/test_auth.py`
- Create: `backend/tests/api/test_cross_tenant_isolation.py`

**Interfaces:**
- Produces: 规格第 10 节全部 `/api/v1` Endpoint、Bearer/tenant/CSRF/Step-up dependencies 和稳定 Problem Details。

- [ ] **Step 1: 写完整认证流程与跨租户测试**

```python
async def test_register_login_refresh_logout_contract(client) -> None:
    registered = await client.post("/api/v1/auth/register", json=REGISTER, headers={"Origin": ORIGIN})
    assert registered.status_code == 201
    assert "access_token" in registered.json()
    assert "__Host-lawyer_refresh" in registered.headers["set-cookie"]
    refreshed = await refresh_with_csrf(client)
    assert refreshed.status_code == 200
    assert (await logout_with_csrf(client)).status_code == 204


async def test_tenant_a_token_cannot_read_tenant_b(client, tenant_a_headers) -> None:
    response = await client.get(f"/api/v1/tenants/{TENANT_B}", headers=tenant_a_headers)
    assert response.status_code == 404
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/api/test_auth.py tests/api/test_cross_tenant_isolation.py -v`

Expected: `/api/v1` 路由未装配。

- [ ] **Step 3: 装配应用生命周期和安全依赖**

FastAPI lifespan 创建/关闭 Engine 与 Redis；每请求创建独立 UoW。依赖顺序：解析 Bearer → 固定 Audience 验签 → Session/User 状态 → 可选 TenantContext → path/token tenant equality → Policy。跨租户资源不存在统一 404；同租户已知资源但无权限 403；无认证或撤销 Session 401。

- [ ] **Step 4: 实现全部 Endpoint 与稳定错误**

认证失败统一 `authentication_failed`；限流 429 带整数 `Retry-After`；Redis security dependency 故障 503 `security_dependency_unavailable`；唯一/版本/幂等冲突映射稳定 409。所有 Response Schema `extra="forbid"`，禁止返回内部版本和安全字段。

- [ ] **Step 5: 验证并提交**

Run: `cd backend; uv run pytest tests/api/test_auth.py tests/api/test_tenants.py tests/api/test_invitations.py tests/api/test_platform.py tests/api/test_cross_tenant_isolation.py -v`

Expected: 认证、租户、邀请、审核和全部反向隔离 API 测试通过。

Commit: `git commit -m "feat: expose identity authorization api"`

### Task 10: Ready、OpenAPI、部署与全量门禁

**Files:**
- Modify: `backend/src/lawyer_agent/api/router.py`
- Modify: `backend/src/lawyer_agent/main.py`
- Create: `backend/tests/contract/test_openapi_identity_authz.py`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/compose.env.example`
- Create: `scripts/migrate.ps1`
- Create: `docs/project-decisions/pending-production-reviews.md`

**Interfaces:**
- Produces: `GET /health/ready`、无秘密 OpenAPI 契约、显式 Migration 命令、上线前复核清单。

- [ ] **Step 1: 写就绪与 OpenAPI 泄露扫描测试**

```python
FORBIDDEN = {"refresh_token", "password_hash", "blind_index", "ciphertext", "auth_version", "authz_version", "key_version"}


def test_openapi_has_no_internal_security_fields(app) -> None:
    serialized = json.dumps(app.openapi(), ensure_ascii=False).lower()
    assert all(name not in serialized for name in FORBIDDEN)


async def test_ready_requires_mysql_and_redis(client, redis_down) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert "redis" not in json.dumps(response.json()).lower()
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd backend; uv run pytest tests/contract/test_openapi_identity_authz.py tests/api/test_health.py -v`

Expected: ready 不存在或 OpenAPI 暴露内部字段。

- [ ] **Step 3: 实现 Ready、部署配置和迁移脚本**

Ready 以有界超时并发执行 MySQL `SELECT 1` 与 Redis `PING`，只返回 `{"status":"ready"}` 或无依赖名称的 Problem Details。Compose API 环境加入 app DSN、Redis URL 和 Secret 文件路径；迁移为显式一次性命令，不由 API 自动执行。`scripts/migrate.ps1` 只运行 `uv run alembic upgrade head`，不打印 DSN/Secret。

- [ ] **Step 4: 记录不阻塞开发的上线前复核项**

`pending-production-reviews.md` 明确记录：Permission/角色矩阵业务复核、Argon2 在生产硬件的 p95 压测与参数确认、真实短信/邮件/微信 Provider、安全密钥 Vault/KMS 与轮换演练、审计保留期限。这些项目不削弱当前安全默认值，也不被声明为已经验证。

- [ ] **Step 5: 执行完整验收门禁**

Run from `backend/`:

```powershell
uv sync --frozen
uv run pytest -m "not integration" -v
uv run pytest -m integration -v
uv run ruff check .
uv run mypy src
```

Run from repository root:

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
git diff --check
git status --short
git ls-files | Select-String -Pattern '(^|/)(\.env|.*private.*key|.*secret.*)$'
```

Expected: 全部测试/静态检查/Compose 验证通过；迁移在一次性 MySQL 完成 upgrade/downgrade/upgrade；Secret 扫描无命中；最终 Diff 无无关修改。

- [ ] **Step 6: 提交**

Commit: `git commit -m "test: verify identity authorization release gate"`

## Plan Self-Review

- Spec coverage: 规格第 1–16 节均映射到 Task 1–10；即时撤销、平台角色主表、复合 FK 与 Step-up 一次性语义已在 Addendum 补正。
- Placeholder scan: 已扫描并移除占位词、空实现和缺少验收命令的泛化步骤。
- Type consistency: `TenantContext`、`Principal`、`Action`、`AuthorizationDecision`、`TokenService`、`SessionService` 和 Repository 签名在各任务中保持一致；API 只依赖应用服务。
- Security review: Account/Tenant/Platform/Step-up audience 分离，Refresh 重放撤销 Family，Redis 安全路径 Fail Closed，租户隔离有三层反向测试。
- Operational review: Migration 独立于 API 启动；真实 MySQL 8.4 验证；Compose 保留命名卷且不提交 `.env`。

## Exit Criteria

本计划只有在规格第 15 节全部验收标准、Task 10 全量门禁、跨租户反向矩阵、Refresh 并发重放、Migration 往返和 OpenAPI 泄露扫描均有实际通过证据时才可标记完成。外部短信、邮件、微信、Vault/KMS 或公网负载未验证时，必须明确报告，不得用开发 Adapter 的结果代替生产集成结论。
