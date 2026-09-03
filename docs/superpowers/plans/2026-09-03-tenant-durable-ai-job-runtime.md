# 租户级持久 AI Job 与可靠执行内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在既有身份与多租户底座上交付租户级持久 AI Job、Transactional Outbox、RabbitMQ Publisher/Worker、租约与 Fencing、可靠恢复、版本化 HTTP API 和可重复故障验收内核。

**Architecture:** MySQL 是 Job、授权、状态、重试和效果的唯一事实源，RabbitMQ 只传递 Ed25519 签名的任务引用，Redis 只提供取消与缓存失效提示。API、Publisher、Worker、Maintenance、Topology Preflight 使用同一镜像但独立组合根、最小依赖和 Secret 边界；实施严格按 A→B→C→D 推进，前一里程碑的总门禁和独立审查结论为 Ready 后才可开始下一里程碑。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.0、Alembic、asyncmy、MySQL 8.4、aio-pika、RabbitMQ 4.x Quorum Queue、redis-py asyncio、Ed25519、pytest、Ruff、mypy、Docker Compose。

## Global Constraints

- 全部代码、迁移、测试、配置和文档使用 UTF-8；本增量不产生法律结论，只适用中国大陆产品边界。
- Python 固定 `>=3.12,<3.14`；沿用 SQLAlchemy `>=2.0.52,<2.1`、Alembic `>=1.19.1,<2`，新增 `aio-pika>=10.0.1,<11` 并用 `uv lock` 固化；Adapter 只调用 10.x 公共 API，并纳入 mypy 校验。
- MySQL 8.4/InnoDB/`utf8mb4`/UTC `DATETIME(6)` 是事实源；UUIDv7 以不交换字节序的 `BINARY(16)` 保存；SQLite、内存 Queue 或 Mock Confirm 不得替代正式集成门禁。
- Job 最多 4 次 Handler Attempt，Retry Bucket 固定 5/30/120 秒；Lease 默认 60 秒，Heartbeat 不超过 20 秒且必须小于 Lease 一半；Job 不可变寿命不超过 24 小时。
- RabbitMQ Envelope 恰有 `schema_version/message_id/job_id/tenant_id/correlation_id/issued_at/nonce/signature` 八个字段；Pydantic `extra='forbid'`，正文、对象地址、Actor、权限和 Secret 禁止进入消息。
- 消息使用独立用途 Ed25519：Publisher 只持 Private Seed Ring，Worker 只持 Public Verify Ring；API、Maintenance 和 Topology Init 都不得挂载消息私钥。
- Publisher Confirm 与 Consumer ACK 正交；Publisher 使用 `publisher_confirms=True`、`on_return_raises=True`、`mandatory=True`；Worker 使用 Manual ACK 和每 Consumer 默认 8、非零有限 Prefetch。
- Main/Retry/DLQ 均为 Durable Quorum Queue 与 Persistent Message；Retry 使用单 Queue 单固定 TTL，禁止 `nack(requeue=true)` 热循环；DLQ 不自动回放。
- 每个租户 Repository 方法显式接收 `TenantContext` 或已验证 `ExecutionContext`；所有租户 FK、唯一键、缓存键、消息和取消提示均包含 Tenant，且必须有跨租户反向测试。
- HTTP Job 路径不得信任当前 `tenant_actor.context.scope.allow_tenant_wide=True`；每次从 MySQL Role/Permission 重建 `JobAuthorizationResolver` 结果。Worker 不调用 HTTP `PolicyEngine`、不伪造 Session、不信任 ActorSnapshot。
- Production 在本增量永久保持 `catalog_only` 并禁止 Synthetic；只有 Development/Test/Staging 显式 Activated + Enabled 才可受理固定 `non_publishable` Synthetic Job。
- Audit 只做 nullable Expand；既有 `tenant_id/actor_user_id/actor_membership_id` 不重建，旧 Writer 可继续写 Legacy 行；新 Binary Writer 必须写非 NULL、矩阵合法的结构化 Actor Kind。
- 已发布 Migration 不得改写；新 Revision 固定从 `20260902_05` 向前。生产回滚保留新表，破坏性 Contract 与 Key/Queue 退役不属于本计划。
- 测试 Failpoint 只在 `environment=test` 可用，必须通过隔离子进程制造真实崩溃；不得提供远程切换 Endpoint。
- 使用 TDD：每个实现任务先写聚焦失败测试并观察预期失败，再写最小正确实现、运行聚焦与静态检查并形成独立 Commit。

## File Map

```text
backend/
  pyproject.toml
  uv.lock
  alembic/versions/20260903_06_tenant_durable_ai_job_runtime.py
  src/lawyer_agent/
    config.py
    main.py
    domain/ai_jobs.py
    application/audit.py
    application/ai_jobs.py
    application/ai_job_runtime.py
    application/permission_rollout.py
    application/security_locks.py
    application/{tenancy,invitations,sessions,platform}.py
    infrastructure/persistence/
      ai_jobs_uow.py
      permission_rollout_uow.py
      models/{__init__,audit,authorization,tenancy,sessions,ai_jobs}.py
      repositories/{audit,ai_jobs,message_security,permission_rollout,security_locks,tenant_workflows}.py
      seed_authz.py
    infrastructure/messaging/
      envelope.py
      signing.py
      rabbitmq.py
      topology.py
    infrastructure/redis/ai_job_hints.py
    runtime/
      settings.py
      composition.py
      health.py
      metrics.py
      failpoints.py
    workers/
      ai_job_publisher.py
      ai_job_worker.py
      ai_job_maintenance.py
      synthetic.py
    cli/redrive_ai_job.py
    api/dependencies.py
    api/errors.py
    api/v1/{router,ai_jobs}.py
  tests/
    conftest.py
    unit/
      test_ai_job_domain.py
      test_ai_job_authorization.py
      test_ai_job_envelope.py
      test_ai_job_settings.py
      test_synthetic_handler.py
    integration/mysql/
      test_ai_job_migration.py
      test_ai_job_constraints.py
      test_ai_job_repositories.py
      test_ai_job_authorization.py
      test_message_security_rejections.py
      test_permission_feature_rollout.py
      test_permission_rollout_concurrency.py
      test_audit_expand_compatibility.py
      test_ai_job_worker_transactions.py
      test_ai_job_maintenance.py
      test_ai_job_idempotency.py
    integration/rabbitmq/
      conftest.py
      network_fault_proxy.py
      test_ai_job_topology.py
      test_ai_job_publisher.py
      test_ai_job_consumer.py
      test_ai_job_failpoints.py
    integration/redis/test_ai_job_hints.py
    api/test_ai_jobs.py
    api/test_ai_job_cross_tenant.py
    contract/test_openapi_ai_jobs.py
    contract/test_ai_job_process_boundaries.py
deploy/
  compose.yaml
  compose.env.example
  compose.production.env.example
scripts/
  dev.ps1
  test-ai-job-runtime.ps1
```

职责边界固定如下：`domain/ai_jobs.py` 只保存纯状态和值对象；`application/ai_jobs.py` 只编排 HTTP 用例与 Job ABAC；`application/ai_job_runtime.py` 只编排 Worker/Publisher/Maintenance 事务端口；RabbitMQ、Redis、SQLAlchemy、FastAPI 分别停留在基础设施或 API 层。新增 `SqlAlchemyAIJobUnitOfWork`，不得把 Job Repository 塞入现有 `SqlAlchemyTenantWorkflowUnitOfWork`。

## 里程碑推进协议

每个里程碑最后一个任务是硬门禁。执行者必须保存命令、退出码、测试通过/跳过数量和独立审查结论；结论不是 Ready 时只允许修复当前里程碑，不得预写下一里程碑代码。A 不连接 RabbitMQ、不开放 HTTP；B 不执行 Handler；C 只通过内部 Harness 执行 Synthetic、不开放公共 API；D 才装配公共 API 与 Compose 全进程。

---

## 里程碑 A：Schema、状态机、Job 授权与 Permission Rollout

### Task 1: 纯 AI Job 领域状态、租约和安全上下文

**Files:**
- Create: `backend/src/lawyer_agent/domain/ai_jobs.py`
- Create: `backend/tests/unit/test_ai_job_domain.py`

**Interfaces:**
- Consumes: `lawyer_agent.domain.common.require_uuid7` 与 UTC-aware `datetime`。
- Produces: `AIJobStatus`、`PublicAIJobStatus`、`AIJobPermission`、`JobVisibility`、`FailureClass`、`ActorSnapshot`、`JobExecutionGrant`、`JobAuthorizationScope`、`JobExecutionPrincipal`、`ExecutionContext`、`require_transition()`、`to_public_status()`、`validate_runtime_timing()`。

- [ ] **Step 1: 写完整允许/拒绝迁移和时间不变量测试**

```python
@pytest.mark.parametrize(
    ("source", "target"),
    [
        (AIJobStatus.QUEUED, AIJobStatus.RUNNING),
        (AIJobStatus.QUEUED, AIJobStatus.CANCELLED),
        (AIJobStatus.QUEUED, AIJobStatus.FAILED),
        (AIJobStatus.RUNNING, AIJobStatus.SUCCEEDED),
        (AIJobStatus.RUNNING, AIJobStatus.RETRY_SCHEDULED),
        (AIJobStatus.RUNNING, AIJobStatus.FAILED),
        (AIJobStatus.RUNNING, AIJobStatus.CANCEL_REQUESTED),
        (AIJobStatus.CANCEL_REQUESTED, AIJobStatus.CANCELLED),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.RUNNING),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.CANCELLED),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.FAILED),
    ],
)
def test_allowed_job_transitions(source: AIJobStatus, target: AIJobStatus) -> None:
    require_transition(source, target)


@pytest.mark.parametrize("target", [AIJobStatus.SUCCEEDED, AIJobStatus.FAILED])
def test_cancel_requested_has_only_cancelled_successor(target: AIJobStatus) -> None:
    with pytest.raises(InvalidJobTransition):
        require_transition(AIJobStatus.CANCEL_REQUESTED, target)


def test_runtime_timing_requires_heartbeat_below_half_lease() -> None:
    with pytest.raises(ValueError, match="less than half"):
        validate_runtime_timing(lease=timedelta(seconds=60), heartbeat=timedelta(seconds=30))
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_ai_job_domain.py -v`

Expected: collection 因 `lawyer_agent.domain.ai_jobs` 不存在失败。

- [ ] **Step 3: 实现枚举、不可变对象和唯一迁移表**

```python
class AIJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: Final[frozenset[tuple[AIJobStatus, AIJobStatus]]] = frozenset(
    {
        (AIJobStatus.QUEUED, AIJobStatus.RUNNING),
        (AIJobStatus.QUEUED, AIJobStatus.CANCELLED),
        (AIJobStatus.QUEUED, AIJobStatus.FAILED),
        (AIJobStatus.RUNNING, AIJobStatus.SUCCEEDED),
        (AIJobStatus.RUNNING, AIJobStatus.RETRY_SCHEDULED),
        (AIJobStatus.RUNNING, AIJobStatus.FAILED),
        (AIJobStatus.RUNNING, AIJobStatus.CANCEL_REQUESTED),
        (AIJobStatus.CANCEL_REQUESTED, AIJobStatus.CANCELLED),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.RUNNING),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.CANCELLED),
        (AIJobStatus.RETRY_SCHEDULED, AIJobStatus.FAILED),
    }
)


def require_transition(source: AIJobStatus, target: AIJobStatus) -> None:
    if (source, target) not in ALLOWED_TRANSITIONS:
        raise InvalidJobTransition(source, target)


@dataclass(frozen=True, slots=True)
class JobAuthorizationScope:
    owner: bool
    shared: bool
    tenant_wide: bool
```

同文件实现公开六态映射、`owner_only/synthetic/non_publishable` 固定值、1–10 Attempt 上限、24 小时 Job 寿命、Lease 四字段成组规则。`ActorSnapshot` 的 Role/Permission 只提供审计访问器；`ExecutionContext` 必须接收本次重载的 `JobExecutionPrincipal`、Grant、Attempt/Fence 和三个 Protocol Port，不提供 Session、ORM、Broker 或任意网络对象。

- [ ] **Step 4: 运行领域测试与静态检查**

Run: `cd backend; uv run pytest tests/unit/test_ai_job_domain.py -v; uv run ruff check src/lawyer_agent/domain/ai_jobs.py tests/unit/test_ai_job_domain.py; uv run mypy src`

Expected: 状态矩阵、公开映射、到期、Lease、取消唯一后继和 Context 类型测试全部通过；Ruff/mypy 零错误。

- [ ] **Step 5: 提交领域边界**

```powershell
git add backend/src/lawyer_agent/domain/ai_jobs.py backend/tests/unit/test_ai_job_domain.py
git commit -m "feat: define durable ai job domain"
```

### Task 2: AI Job/Audit Expand Migration 与 ORM 模型

**Files:**
- Create: `backend/alembic/versions/20260903_06_tenant_durable_ai_job_runtime.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/models/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/models/audit.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/models/tenancy.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/models/sessions.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/models/__init__.py`
- Create: `backend/tests/integration/mysql/test_ai_job_migration.py`
- Create: `backend/tests/integration/mysql/test_ai_job_constraints.py`
- Create: `backend/tests/integration/mysql/test_audit_expand_compatibility.py`

**Interfaces:**
- Consumes: Task 1 枚举值、现有 `Base/UuidBinary/UTC_DATETIME`、Revision `20260902_05`。
- Produces: `PermissionFeatureRolloutModel`、`PermissionFeatureTenantStateModel`、`AIJobModel`、`AIJobExecutionGrantModel`、`AIJobAccessGrantModel`、`AIJobAttemptModel`、`AIJobOutboxModel`、`AIJobInboxModel`、`AIJobStepEffectModel`、`MessageSecurityRejectionModel`，以及 Audit nullable Expand 列。

- [ ] **Step 1: 写从现有 Head 升级、往返和真实约束测试**

```python
def test_ai_job_revision_extends_current_head(mysql_url: URL) -> None:
    command.upgrade(alembic_config(mysql_url), "20260902_05")
    seed_legacy_audit_rows(mysql_url)
    command.upgrade(alembic_config(mysql_url), "head")
    assert set(AI_JOB_TABLES) <= set(inspect(sync_engine(mysql_url)).get_table_names())
    assert_legacy_audit_rows_unchanged(mysql_url)
    command.downgrade(alembic_config(mysql_url), "20260902_05")
    command.upgrade(alembic_config(mysql_url), "head")


@pytest.mark.mysql
async def test_audit_message_cannot_reference_other_job(session: AsyncSession) -> None:
    outbox = await insert_outbox(session, tenant_id=TENANT_A, job_id=JOB_A, message_id=MSG)
    session.add(structured_audit(tenant_id=TENANT_A, target_job_id=JOB_B, message_id=outbox.message_id))
    with pytest.raises(IntegrityError):
        await session.commit()
```

Migration 测试预置并逐行比对四类旧 Audit：Global User、Tenant Membership、Anonymous、`platform_admin.bootstrap` 与 `invitation.blind_index_legacy_reconcile`；升级后旧 Writer 仍能让所有真正新增列为 NULL，同时保留既有 Tenant/Actor 列。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_migration.py tests/integration/mysql/test_ai_job_constraints.py tests/integration/mysql/test_audit_expand_compatibility.py -v`

Expected: `20260903_06` Revision、模型和新表不存在，测试失败；不得以跳过报告代替失败。

- [ ] **Step 3: 编写单一向前 Revision 和模型**

```python
revision: str = "20260903_06"
down_revision: str | Sequence[str] | None = "20260902_05"


def upgrade() -> None:
    create_permission_feature_tables()
    create_ai_job_tables_in_fk_order()
    expand_audit_events()
    create_ai_job_permission_catalog_rows()


def downgrade() -> None:
    drop_audit_expand_constraints_and_columns()
    drop_ai_job_tables_in_reverse_fk_order()
    drop_ai_job_permission_catalog_rows()
```

Migration 按规格 7.1–7.10 逐字段创建所有表、状态 CHECK、Generated `active_marker`、复合 FK 和候选键。Feature Global/State 分别持久化单调 `rollout_generation` 与 `target_rollout_generation/applied_rollout_generation`；Outbox 分离 `dispatch_generation/envelope_generation/recovery_source_fence`。必须包含 `UNIQUE(tenant_id,job_id,message_id)`，Audit Message 用同三列 FK；Attempt/Inbox/Effect/Recovery 使用 Fence 复合键；Access Grant Supersedes 使用五列 FK。给 Membership 增加 `(tenant_id,id,user_id)` 候选键，给 Session 增加 `(tenant_id,id,user_id,membership_id)` 候选键。Audit Legacy 分支只约束新增列全 NULL；结构化分支实现完整 Actor Kind/Action 矩阵，V1 将 `platform_admin.bootstrap` 限定为 `system_identity_bootstrap`、`invitation.blind_index_legacy_reconcile` 限定为 `system_global_maintenance`。

- [ ] **Step 4: 验证所有复合关系负向矩阵**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_migration.py tests/integration/mysql/test_ai_job_constraints.py tests/integration/mysql/test_audit_expand_compatibility.py -v`

Expected: fresh upgrade、`20260902_05 -> head -> 20260902_05 -> head` 通过；Tenant NULL、错误 Tenant/Job、跨租户 Job/Actor/Membership/Grant/Attempt/Outbox/Inbox/Effect/Message、Attempt Fence、Supersedes Chain 和 Audit Kind 负向插入均由 MySQL 拒绝。

- [ ] **Step 5: 提交 Schema 增量**

```powershell
git add backend/alembic/versions/20260903_06_tenant_durable_ai_job_runtime.py backend/src/lawyer_agent/infrastructure/persistence/models backend/tests/integration/mysql/test_ai_job_migration.py backend/tests/integration/mysql/test_ai_job_constraints.py backend/tests/integration/mysql/test_audit_expand_compatibility.py
git commit -m "feat: add durable ai job schema"
```

### Task 3: Tenant-scoped Repository 与独立 AI Job UoW

**Files:**
- Create: `backend/src/lawyer_agent/application/ai_jobs.py`
- Create: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/ai_jobs_uow.py`
- Create: `backend/tests/integration/mysql/test_ai_job_repositories.py`
- Modify: `backend/tests/unit/test_tenant_repository_boundaries.py`

**Interfaces:**
- Consumes: Task 1 领域对象、Task 2 模型、现有 `SqlAlchemyIdempotencyRepository` 与 `AuditRepository`。
- Produces: `AIJobRepositoryPort`、`AIJobRuntimeRepositoryPort`、`AIJobUnitOfWork`、`SqlAlchemyAIJobRepository`、`SqlAlchemyAIJobUnitOfWork`。

- [ ] **Step 1: 写 Tenant 谓词、原子图写入和 Fence CAS 失败测试**

```python
@pytest.mark.mysql
async def test_repository_never_loads_job_from_other_tenant(ai_job_uow_factory) -> None:
    async with ai_job_uow_factory() as uow:
        assert await uow.jobs.get(TenantContext.for_test(TENANT_A), JOB_B) is None


@pytest.mark.mysql
async def test_stale_fence_cannot_finalize(ai_job_uow_factory, running_job) -> None:
    async with ai_job_uow_factory() as uow:
        changed = await uow.runtime.finalize_success(FencedFinalizeSuccess(
            tenant_id=running_job.tenant_id,
            job_id=running_job.id,
            lease_token=OLD_TOKEN,
            lease_fence=running_job.lease_fence - 1,
            expected_version=running_job.version,
            result=SYNTHETIC_RESULT,
            now=NOW,
        ))
    assert changed is False
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_repositories.py tests/unit/test_tenant_repository_boundaries.py -v`

Expected: Repository/UoW Port 未定义或边界扫描发现裸 ID 方法，测试失败。

- [ ] **Step 3: 定义 Port 和独立 UoW**

Port 签名固定为：

- `AIJobRepositoryPort.get(context: TenantContext, job_id: UUID, *, for_update: bool = False) -> AIJob | None`
- `AIJobRepositoryPort.add_job_graph(graph: NewAIJobGraph) -> None`
- `AIJobRepositoryPort.get_access(context: TenantContext, job_id: UUID, membership_id: UUID) -> JobAccess | None`
- `AIJobRuntimeRepositoryPort.claim_due_job(request: ClaimJobRequest) -> ClaimedExecution | ClaimRejected`
- `AIJobRuntimeRepositoryPort.heartbeat(request: FencedHeartbeat) -> bool`
- `AIJobRuntimeRepositoryPort.finalize_success(request: FencedFinalizeSuccess) -> bool`

`SqlAlchemyAIJobUnitOfWork` 暴露强类型 `jobs/runtime/idempotency/audit` 四个 Repository，进入时从同一个 `AsyncSession` 构造，正常退出 Commit，异常 Rollback 后 Close。

所有公开资源读取均要求 `TenantContext`；内部 Worker 方法要求已验签 `tenant_id + job_id` 和随后构造的 `ExecutionContext`。`add_job_graph()` 依次 Flush Job → Execution Grant → Outbox，并与 Idempotency/Audit 同 Session 提交。状态写均同时匹配 Tenant、Job、Version；运行写再匹配 Lease Token/Fence。

- [ ] **Step 4: 运行 Repository 和跨租户测试**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_repositories.py tests/integration/mysql/test_ai_job_constraints.py tests/unit/test_tenant_repository_boundaries.py -v; uv run ruff check src/lawyer_agent/application/ai_jobs.py src/lawyer_agent/application/ai_job_runtime.py src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py src/lawyer_agent/infrastructure/persistence/ai_jobs_uow.py; uv run mypy src`

Expected: 同租户原子图写入通过；裸 `get_by_id` 扫描、跨租户读取/写入和旧 Fence Finalize 全部按预期拒绝；静态检查零错误。

- [ ] **Step 5: 提交持久化边界**

```powershell
git add backend/src/lawyer_agent/application/ai_jobs.py backend/src/lawyer_agent/application/ai_job_runtime.py backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py backend/src/lawyer_agent/infrastructure/persistence/ai_jobs_uow.py backend/tests/integration/mysql/test_ai_job_repositories.py backend/tests/unit/test_tenant_repository_boundaries.py
git commit -m "feat: add tenant scoped ai job persistence"
```

### Task 4: JobAuthorizationResolver 与 DurableGrantPolicy

**Files:**
- Modify: `backend/src/lawyer_agent/application/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Create: `backend/tests/unit/test_ai_job_authorization.py`
- Create: `backend/tests/integration/mysql/test_ai_job_authorization.py`

**Interfaces:**
- Consumes: `TenantAuthorizationSnapshot` 的 MySQL Role/Permission 集合，但不消费其中硬编码 `context.scope`。
- Produces: `JobAuthorizationResolver.resolve(snapshot, permission) -> JobAuthorizationScope`、`DurableGrantPolicy.authorize(snapshot, job, grant, now) -> AuthorizationDecision`、`AIJobAuthorityLoader.load(tenant_id, job_id, for_update) -> JobAuthoritySnapshot`。

- [ ] **Step 1: 写角色 Scope、Session 差异和版本撤销测试**

```python
def test_resolver_ignores_generic_tenant_wide_scope() -> None:
    snapshot = authority(role_codes=frozenset({"assistant"}), generic_tenant_wide=True)
    scope = JobAuthorizationResolver().resolve(snapshot, AIJobPermission.READ)
    assert scope == JobAuthorizationScope(owner=True, shared=True, tenant_wide=False)


def test_custom_or_unknown_role_fails_closed() -> None:
    with pytest.raises(JobAuthorizationDenied, match="job_scope_unmapped"):
        JobAuthorizationResolver().resolve(authority(role_codes=frozenset({"custom_partner"})), AIJobPermission.READ)


async def test_logout_does_not_invalidate_grant_but_authz_bump_does(loader, policy) -> None:
    logged_out = await loader.load(TENANT, JOB, for_update=False)
    assert policy.authorize(logged_out.with_session_revoked(), NOW).allowed
    assert not policy.authorize(logged_out.with_authz_version(logged_out.authz_version + 1), NOW).allowed
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_ai_job_authorization.py tests/integration/mysql/test_ai_job_authorization.py -v`

Expected: Resolver、权威 Loader 和 Durable Grant Policy 不存在，测试失败。

- [ ] **Step 3: 实现固定 Manifest Resolver 与 Worker 专用授权**

```python
JOB_SCOPE_BY_ROLE: Final[Mapping[str, JobAuthorizationScope]] = MappingProxyType(
    {
        "tenant_owner": JobAuthorizationScope(True, True, True),
        "tenant_admin": JobAuthorizationScope(True, True, True),
        "department_admin": JobAuthorizationScope(True, True, False),
        "lawyer_or_legal": JobAuthorizationScope(True, True, False),
        "assistant": JobAuthorizationScope(True, True, False),
        "teacher": JobAuthorizationScope(True, True, False),
    }
)


class DurableGrantPolicy:
    def authorize(self, snapshot: JobAuthoritySnapshot, *, now: datetime) -> AuthorizationDecision:
        checks = (
            snapshot.job.expires_at > now,
            snapshot.grant.revoked_at is None,
            snapshot.user_active,
            snapshot.tenant_active,
            snapshot.membership_active_at(now),
            snapshot.grant.auth_version_at_submit == snapshot.auth_version,
            snapshot.grant.authz_version_at_submit == snapshot.authz_version,
            AIJobPermission.CREATE.value in snapshot.permission_codes,
        )
        return decision_from_fixed_order(checks, snapshot)
```

Loader 必须在每个 Claim/Heartbeat/Effect/Finalize 边界从 MySQL 重读 User/Tenant/Membership/Role/Permission/Grant/Job；不查询浏览器 Session，不读 ActorSnapshot 权限。Resolver 遇学生、外部客户、Custom/未知 Role、未知 Permission/Scope、混合未知 Role 或不匹配 Manifest 一律拒绝。

- [ ] **Step 4: 运行授权矩阵和真实撤销测试**

Run: `cd backend; uv run pytest tests/unit/test_ai_job_authorization.py tests/integration/mysql/test_ai_job_authorization.py -v; uv run ruff check src/lawyer_agent/application/ai_jobs.py src/lawyer_agent/application/ai_job_runtime.py; uv run mypy src`

Expected: Owner/Shared/Tenant-wide 与 Shared 不可 Cancel 全矩阵通过；自然 Session 到期/Logout 不停止，User/Tenant/Membership/Permission/Role/Grant/安全版本变化均在下一边界拒绝。

- [ ] **Step 5: 提交授权内核**

```powershell
git add backend/src/lawyer_agent/application/ai_jobs.py backend/src/lawyer_agent/application/ai_job_runtime.py backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py backend/tests/unit/test_ai_job_authorization.py backend/tests/integration/mysql/test_ai_job_authorization.py
git commit -m "feat: enforce durable ai job authorization"
```

### Task 5: Permission Feature Manifest、Activation 与 Deactivation

**Files:**
- Create: `backend/src/lawyer_agent/application/permission_rollout.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/permission_rollout.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/permission_rollout_uow.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/seed_authz.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/tenant_workflows.py`
- Create: `backend/tests/integration/mysql/test_permission_feature_rollout.py`
- Create: `backend/tests/integration/mysql/test_permission_rollout_concurrency.py`

**Interfaces:**
- Consumes: Task 2 Feature 表、现有 Role Template/Role/Permission/Idempotency/Authz Cache Outbox。
- Produces: `AI_JOB_PERMISSION_MANIFEST`、`PermissionFeatureRolloutService.reconcile_expand_window/activate_batch/deactivate_batch/finalize`、`PermissionRolloutRepository`、`SqlAlchemyPermissionRolloutUnitOfWork`。

- [ ] **Step 1: 写重复启停、崩溃恢复和新租户并发测试**

```python
@pytest.mark.mysql
async def test_same_manifest_can_activate_deactivate_and_activate_again(service) -> None:
    assert await service.begin_activation(DRAIN_EVIDENCE) == 1
    await service.finish_activation(1)
    assert await service.begin_deactivation() == 2
    await service.finish_deactivation(2)
    assert await service.begin_activation(DRAIN_EVIDENCE_2) == 3


@pytest.mark.mysql
async def test_claim_never_changes_applied_fact(service, tenant_state) -> None:
    claimed = await service.claim_tenant(tenant_state.tenant_id, generation=1)
    assert claimed.applied_manifest_version == BASE_MANIFEST.version
    assert claimed.phase == TenantFeaturePhase.CATALOG_ONLY
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_permission_feature_rollout.py tests/integration/mysql/test_permission_rollout_concurrency.py -v`

Expected: Feature Manifest、Repository 和 Service 不存在，测试失败。

- [ ] **Step 3: 实现精确 Manifest、Generation 与 Expand 对账**

```python
AI_JOB_PERMISSION_MANIFEST = PermissionFeatureManifest(
    feature_code="ai_job_runtime_v1",
    base_version="ai-job-base-v1",
    feature_version="ai-job-feature-v1",
    permission_codes=frozenset({"ai_job.create", "ai_job.read", "ai_job.cancel"}),
    target_role_codes=frozenset(
        {"tenant_owner", "tenant_admin", "department_admin", "lawyer_or_legal", "assistant", "teacher"}
    ),
)
```

Service 签名固定为 `reconcile_expand_window(evidence: WritersDrainedEvidence) -> ReconciliationProof`、`begin_activation(evidence: WritersDrainedEvidence) -> int`、`activate_batch(*, generation: int, limit: int) -> RolloutBatchResult`、`begin_deactivation() -> int`、`deactivate_batch(*, generation: int, limit: int) -> RolloutBatchResult` 和 `finalize(*, direction: RolloutDirection, generation: int) -> None`。

`reconcile_expand_window()` 在全局 Feature Row `FOR UPDATE` 下用 anti-join `INSERT-SELECT` 补齐 State，再以 Tenant/State count 和排序 UUID SHA-256 对账；仅证明旧 Writer 全清退后可进入 Activation。稳定 Phase 开始每次方向迁移将 unsigned Generation 原子加一，溢出、不连续、回退或跨代 Claim Fail Closed。Claim 只写 Target/Claim 字段；每租户业务事务才原子提交 Role Permission、Membership-scoped 幂等记录、全 Tenant Session 撤销、Authz Version、现有 Cache Outbox、结构化 Audit 和 Applied 二元组。`PermissionFeatureRolloutService` 必须绑定部署环境，Production 对 `begin_activation()` 和 Activated State 均 Fail Closed，且 Migration/Seed 永不隐式切换 Phase。

- [ ] **Step 4: 实现 Phase-aware Seed/租户克隆和反向收敛**

Seed 只保证三个 Catalog Code 语义不漂移，不在 `catalog_only` 给模板授权。Tenant 创建在 Global Feature Row Share Lock 下按 Phase 克隆：Activating/Activated 克隆 Feature；Catalog-only/Deactivating 显式过滤 Feature 并写 Base State。Deactivating 期间模板保留 Feature，全部 Tenant 到当前 Generation Base 后的最终全局事务才删除模板权限并 CAS `catalog_only`。每轮 Idempotency Key 必须含 Feature + Generation + Manifest + Membership；Cache Outbox 不加 Generation 列，只引用新 Record ID 与递增 Authz Version。

- [ ] **Step 5: 验证并提交 Rollout**

Run: `cd backend; uv run pytest tests/integration/mysql/test_permission_feature_rollout.py tests/integration/mysql/test_permission_rollout_concurrency.py tests/unit/test_authz_seed.py -v; uv run ruff check src/lawyer_agent/application/permission_rollout.py src/lawyer_agent/infrastructure/persistence/repositories/permission_rollout.py src/lawyer_agent/infrastructure/persistence/permission_rollout_uow.py src/lawyer_agent/infrastructure/persistence/seed_authz.py; uv run mypy src`

Expected: Expand 补齐、count/hash、同 Manifest 三代启停、崩溃同代恢复、旧 Fence 0 行、Custom/漂移 Role 阻断、新租户所有 Phase 精确集合和 Deactivation 最终模板收敛全部通过。

Commit: `git commit -m "feat: add ai job permission rollout"`

### Task 6: 身份 Writer 统一锁序与结构化 Audit Writer

**Files:**
- Create: `backend/src/lawyer_agent/application/audit.py`
- Modify: `backend/src/lawyer_agent/application/security_locks.py`
- Modify: `backend/src/lawyer_agent/application/identity.py`
- Modify: `backend/src/lawyer_agent/application/tenancy.py`
- Modify: `backend/src/lawyer_agent/application/invitations.py`
- Modify: `backend/src/lawyer_agent/application/sessions.py`
- Modify: `backend/src/lawyer_agent/application/platform.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/audit.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/security_locks.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/tenant_workflows.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/tenancy_uow.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/invitations_uow.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/platform_uow.py`
- Modify: `backend/tests/integration/mysql/test_permission_rollout_concurrency.py`
- Modify: `backend/tests/integration/mysql/test_audit_expand_compatibility.py`

**Interfaces:**
- Consumes: Task 5 Tenant Feature State 锁与 Applied Manifest；现有安全写事务。
- Produces: `AuditActorKind`、`StructuredAuditEvent`、`AuditRepositoryPort`、扩展后的 `SecurityWriteLockRepositoryPort`。

- [ ] **Step 1: 写旧 Writer 并发、Kind Allowlist 和统一锁序测试**

```python
@pytest.mark.mysql
async def test_membership_writer_waits_for_rollout_and_reloads_generation(concurrent_uows) -> None:
    rollout, invitation = concurrent_uows
    await rollout.lock_tenant_feature_exclusive(TENANT)
    pending = asyncio.create_task(invitation.accept(INVITATION_COMMAND))
    assert not pending.done()
    await rollout.commit_feature_generation(2)
    membership = await pending
    assert membership.role_manifest_generation == 2


def test_global_maintenance_writer_cannot_claim_bootstrap_kind() -> None:
    with pytest.raises(ValueError, match="actor kind/action mismatch"):
        StructuredAuditEvent(actor_kind=AuditActorKind.SYSTEM_IDENTITY_BOOTSTRAP, action="invitation.blind_index_legacy_reconcile", **GLOBAL_FIELDS)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_permission_rollout_concurrency.py tests/integration/mysql/test_audit_expand_compatibility.py -v`

Expected: 现有 Writer 未锁 Tenant Feature State，且现有 Audit 类型没有 Actor Kind，测试失败。

- [ ] **Step 3: 实现结构化 Audit 闭集和现有 Writer 映射**

```python
class AuditActorKind(StrEnum):
    TENANT_USER = "tenant_user"
    GLOBAL_USER = "global_user"
    ANONYMOUS = "anonymous"
    SYSTEM_IDENTITY_BOOTSTRAP = "system_identity_bootstrap"
    SYSTEM_GLOBAL_MAINTENANCE = "system_global_maintenance"
    SYSTEM_WORKER = "system_worker"
    SYSTEM_PUBLISHER = "system_publisher"
    SYSTEM_JOB_MAINTENANCE = "system_job_maintenance"
    SYSTEM_FEATURE_ROLLOUT = "system_feature_rollout"
    SYSTEM_GLOBAL_FEATURE_ROLLOUT = "system_global_feature_rollout"
    PLATFORM_OPERATOR = "platform_operator"
```

新 Binary 的所有 Identity/Tenant/Invitation/Session/Platform/Job/Rollout Writer 必须构造 `StructuredAuditEvent`；Repository 不再为新调用隐式写 Legacy Null。`platform_admin.bootstrap` 只允许 Identity Bootstrap，`invitation.blind_index_legacy_reconcile` 只允许 Global Maintenance；Action Allowlist 与 Task 2 CHECK 一致。旧部署 Binary 仍可在滚动窗口直接插入新增列全 NULL 的 Legacy 行。

- [ ] **Step 4: 将现有身份 Writer 改为 Feature State 优先锁序**

安全锁 Port 签名固定为 `lock_global_feature_for_tenant_create() -> PermissionFeatureSnapshot`、`lock_tenant_feature_shared(tenant_id: UUID) -> TenantFeatureSnapshot`、`acquire_tenant_write(request: TenantSecurityWriteLockRequest) -> bool` 和 `acquire_session_family(request: SessionFamilyWriteLockRequest) -> bool`。

Membership 创建、邀请接受、角色调整/分配、Tenant Session 签发/切换必须先锁 Tenant Feature State Share，再按 Role → Membership → Session → Idempotency/Cache Outbox/Audit 排序锁定并重载 Applied Generation；新 Tenant 先锁 Global Feature Row Share。角色调整无论是否影响 AI Job 权限，都撤销全部 Tenant Session、递增 Authz Version并写现有 Cache Outbox/Audit。Feature Activation/Deactivation 对每个受影响 Membership 撤销全部 Tenant Session，稳定 Reason 分别为 `permission_feature_activation` 和 `permission_feature_deactivation`；重试依赖含 Generation 的幂等记录，不重复递增版本或撤销。事务未提交 Deadlock 只做有界 Jitter 重试。

- [ ] **Step 5: 运行现有身份回归与并发矩阵并提交**

Run: `cd backend; uv run pytest tests/integration/mysql/test_permission_rollout_concurrency.py tests/integration/mysql/test_audit_expand_compatibility.py tests/integration/mysql/test_invitations_platform.py tests/integration/mysql/test_sessions.py tests/api/test_tenants.py tests/api/test_invitations.py tests/api/test_platform.py -v; uv run ruff check .; uv run mypy src`

Expected: 普通 Writer 先/后 Rollout 两种调度结果均精确、无漏撤 Session；所有新 Writer Actor Kind 合法，旧 Writer/旧数据仍兼容；现有身份 API 回归通过。

Commit: `git commit -m "feat: coordinate identity writers with ai job rollout"`

### Task 7: 里程碑 A 总门禁与独立审查

**Files:**
- Test only: `backend/tests/unit/test_ai_job_domain.py`
- Test only: `backend/tests/unit/test_ai_job_authorization.py`
- Test only: `backend/tests/integration/mysql/`

**Interfaces:**
- Consumes: Task 1–6 的领域、Schema、Repository、授权、Rollout 和身份 Writer。
- Produces: 里程碑 A 的可复核测试证据与 `Ready/Not Ready` 审查结论；不产生 RabbitMQ 连接或 HTTP Job Route。

- [ ] **Step 1: 运行 A 聚焦与完整身份回归**

```powershell
cd backend
uv run pytest tests/unit/test_ai_job_domain.py tests/unit/test_ai_job_authorization.py -v
uv run pytest tests/integration/mysql -v
uv run pytest tests/api/test_auth.py tests/api/test_tenants.py tests/api/test_invitations.py tests/api/test_platform.py tests/api/test_cross_tenant_isolation.py -v
uv run ruff check .
uv run mypy src
```

Expected: 全部通过；Migration 从 `20260902_05` 往返、Audit Rolling Compatibility、全部复合 FK 负向、三代 Rollout、身份 Writer 并发均有真实 MySQL 证据；测试收集或运行期间没有 RabbitMQ 连接，也没有 `/api/v1/tenants/{tenant_id}/ai-jobs` Route。

- [ ] **Step 2: 证明里程碑边界**

Run: `cd backend; uv run pytest tests/contract/test_openapi_identity_authz.py -v; rg -n "aio_pika|connect_robust" src/lawyer_agent/application src/lawyer_agent/domain`

Expected: OpenAPI 仍只有既有身份接口；第二条命令无输出，证明领域/应用层未连接 RabbitMQ。

- [ ] **Step 3: 请求独立审查并记录结论**

审查输入固定为 Task 1–6 Commit Range、最终规格第 1–8、17.1–17.5、18.1、19.1–19.2、20.6–20.10/20.14/20.16 节，以及上述命令原始输出。审查必须明确回答 Schema/Audit Rolling、Tenant Isolation、Durable Grant、Rollout Generation、Writer Lock Order 五项是否 Ready。

Expected: 只有五项均为 `Ready` 才可进入 Task 8；任一项为 `Not Ready` 时修复 A 并重跑本任务全部命令。

- [ ] **Step 4: 提交只针对审查发现的 A 修复**

若审查要求代码修复，每个独立问题按“失败测试 → 最小修复 → 聚焦通过”单独 Commit，最后重新执行 Step 1–3。若无需修复，本门禁不制造空 Commit。

## 里程碑 B：Ed25519、Topology Preflight 与 Outbox Publisher

### Task 8: 进程专用 Settings、严格 Envelope 与安全拒绝存储

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/src/lawyer_agent/runtime/settings.py`
- Create: `backend/src/lawyer_agent/infrastructure/messaging/envelope.py`
- Create: `backend/src/lawyer_agent/infrastructure/messaging/signing.py`
- Create: `backend/src/lawyer_agent/infrastructure/persistence/repositories/message_security.py`
- Create: `backend/tests/unit/test_ai_job_settings.py`
- Create: `backend/tests/unit/test_ai_job_envelope.py`
- Create: `backend/tests/integration/mysql/test_message_security_rejections.py`

**Interfaces:**
- Consumes: Task 2 `MessageSecurityRejectionModel`、现有 Secret File 严格 UTF-8/大小/普通文件校验模式。
- Produces: `AIJobPublisherSettings`、`AIJobWorkerSettings`、`AIJobTopologySettings`、`AIJobMaintenanceSettings`、`AIJobEnvelope`、`EnvelopeSigner`、`EnvelopeVerifier`、`MessageSecurityRejectionRepository`。

- [ ] **Step 1: 写八字段、规范编码、篡改和 Secret 隔离测试**

```python
def test_envelope_forbids_payload_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AIJobEnvelope.model_validate({**VALID_ENVELOPE, "prompt": "secret"})


def test_signature_binds_tenant_job_and_message(signer: EnvelopeSigner, verifier: EnvelopeVerifier) -> None:
    signed = signer.sign(unsigned_envelope())
    verifier.verify(signed)
    with pytest.raises(EnvelopeSignatureError):
        verifier.verify(signed.model_copy(update={"job_id": OTHER_JOB}))


def test_process_settings_never_expose_foreign_credentials() -> None:
    assert "message_private_seed_ring" in AIJobPublisherSettings.model_fields
    assert "message_private_seed_ring" not in AIJobWorkerSettings.model_fields
    assert "rabbit_management_credential" not in AIJobPublisherSettings.model_fields
    assert "rabbit_management_credential" in AIJobTopologySettings.model_fields
```

- [ ] **Step 2: 加依赖并确认测试失败**

在 `dependencies` 加入 `"aio-pika>=10.0.1,<11"`，运行 `cd backend; uv lock; uv sync --frozen; uv run pytest tests/unit/test_ai_job_settings.py tests/unit/test_ai_job_envelope.py tests/integration/mysql/test_message_security_rejections.py -v`。

Expected: Settings、Envelope、签名和 Repository 尚不存在，测试失败；`uv.lock` 已确定 `aio-pika` 版本。

- [ ] **Step 3: 实现互不包含的进程 Settings 和 Ed25519 Codec**

```python
Environment = Literal["development", "test", "staging", "production"]


class AIJobPublisherSettings(BaseSettings):
    environment: Environment
    database_url: SecretStr
    rabbit_amqp_url_file: Path
    message_private_seed_ring_file: Path
    message_active_kid: str
    confirm_timeout_seconds: float = 5.0
    claim_ttl_seconds: int = 30
    batch_size: int = 50


class AIJobWorkerSettings(BaseSettings):
    environment: Environment
    database_url: SecretStr
    rabbit_amqp_url_file: Path
    message_public_verify_ring_file: Path
    observability_reference_key_file: Path
    prefetch: int = 8
    lease_seconds: int = 60
    heartbeat_seconds: int = 20


class AIJobEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    message_id: UUID
    job_id: UUID
    tenant_id: UUID
    correlation_id: UUID
    issued_at: datetime
    nonce: str
    signature: str
```

所有 Secret Path Validator 必须拒绝相对路径、非普通文件、符号链接、超限文件和非 UTF-8；字段值只在对应进程组合根读取。Private Ring JSON 值严格解码为 32-byte Seed；Public Ring 严格解码为 32-byte Public Key。Canonical bytes 按固定八字段中的前七字段顺序，以字段名和内容的 4-byte big-endian 长度前缀 + UTF-8 内容连接；UUID 要求小写标准形式，时间要求 UTC 微秒 `Z`，Nonce Validator 要求无 Padding Base64URL 128 bit。签名 Validator 固定接受 `ed25519.<kid>.<base64url-no-padding>`。Key Ring 不得复用 JWT Ring。

- [ ] **Step 4: 实现有界安全拒绝遥测**

```python
class MessageSecurityRejectionRepository:
    async def record(self, rejection: SecurityRejection, *, capacity: RejectionCapacity) -> RejectionWriteResult:
        raise NotImplementedError

    async def delete_expired(self, *, before: datetime, limit: int) -> int:
        raise NotImplementedError


def observability_ref(key: bytes, raw_message: bytes) -> bytes:
    return hmac.digest(key, b"ai-message-observability:v1\x00" + raw_message, sha256)
```

Hint 只接受范围受限整数 Schema 与 1–64 ASCII Kid；不合规存 NULL。Repository 以 `(observability_ref,rejection_code,retention_bucket)` 去重，按全局行/日容量和确定性采样决定 `persisted/deduplicated/sampled/dropped_capacity`，容量满不抛给消息放行；清理固定 7 天且每批 1000 行以内。未验签字段不得写 Tenant/Job/User/Membership/Audit。

- [ ] **Step 5: 验证并提交消息安全边界**

Run: `cd backend; uv run pytest tests/unit/test_ai_job_settings.py tests/unit/test_ai_job_envelope.py tests/integration/mysql/test_message_security_rejections.py -v; uv run ruff check .; uv run mypy src`

Expected: Canonicalization、未知 Algorithm/Kid、签名篡改、Active/Previous Public Key 轮换、已证明无在途旧消息后的 Key 退役、重复 Key Material、错误 Secret 文件、Hint 清洗、去重/采样/容量/7 天清理全部通过；各进程 Settings 不含越权 Secret；aio-pika 10.x 公共 API 通过 mypy。

Commit: `git commit -m "feat: add signed ai job envelopes"`

### Task 9: RabbitMQ 隔离 Fixture、Topology Init 与 Passive Readiness

**Files:**
- Create: `backend/src/lawyer_agent/infrastructure/messaging/rabbitmq.py`
- Create: `backend/src/lawyer_agent/infrastructure/messaging/topology.py`
- Create: `backend/tests/integration/rabbitmq/conftest.py`
- Create: `backend/tests/integration/rabbitmq/test_ai_job_topology.py`

**Interfaces:**
- Consumes: Task 8 Topology Settings 与 `aio-pika`。
- Produces: `RabbitTopologyV1`、`RabbitManagementClient`、`TopologyInitializer.apply_and_verify()`、`AioPikaRuntimeTopology.passive_check()`、`rabbit_vhost` 测试 Fixture。

- [ ] **Step 1: 写真实 VHost 生命周期和拓扑等价测试**

```python
_VHOST_PATTERN = re.compile(r"lawyer_test_[a-f0-9]{32}\Z", re.ASCII)


@pytest.mark.integration
async def test_topology_is_quorum_durable_and_runtime_passive(rabbit_vhost) -> None:
    initializer = TopologyInitializer(rabbit_vhost.management, rabbit_vhost.amqp_url)
    await initializer.apply_and_verify(RabbitTopologyV1())
    await AioPikaRuntimeTopology(rabbit_vhost.amqp_url).passive_check(RabbitTopologyV1())
    actual = await rabbit_vhost.management.snapshot()
    assert all(queue.durable and queue.arguments["x-queue-type"] == "quorum" for queue in actual.queues)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/rabbitmq/test_ai_job_topology.py -v`

Expected: Rabbit Fixture/Adapter 不存在，测试失败；RabbitMQ 不可用时当前开发步骤可报告外部阻断，但里程碑 B 门禁不得跳过。

- [ ] **Step 3: 实现一次性严格 VHost Fixture 和精确清理**

```python
@pytest_asyncio.fixture
async def rabbit_vhost() -> AsyncIterator[RabbitTestVhost]:
    suffix = uuid4().hex
    name = f"lawyer_test_{suffix}"
    if not _VHOST_PATTERN.fullmatch(name) or name != f"lawyer_test_{suffix}":
        raise RuntimeError("refusing unsafe RabbitMQ vhost name")
    resource = await create_isolated_vhost(name)
    try:
        yield resource
    finally:
        await resource.close_connections_for_exact_vhost(name)
        await resource.delete_test_queues_exchanges_bindings_and_policies(name)
        await resource.delete_exact_vhost(name)
```

Fixture 从 `LAWYER_TEST_RABBITMQ_MANAGEMENT_URL/ADMIN_USER/ADMIN_PASSWORD` 或未跟踪 `deploy/.env` 读取测试管理员凭据；创建当前随机 VHost 和只限该 VHost 的运行用户。每次删除前再次验证正则和原 suffix 相等，只枚举/删除该 VHost 当前测试创建的资源，最后删除精确 URL-encoded VHost。禁止访问 `/`、Development VHost、其他测试 VHost 或任何 Volume；清理失败使测试失败并输出脱敏资源名。

- [ ] **Step 4: 实现 Topology Preflight 与 Runtime 被动检查**

拓扑常量精确声明 `lawyer.ai.jobs.v1`、Retry/DLX Exchange、Main、5/30/120 秒 Retry、DLQ Queue 和固定 Routing Key。Management Preflight 应用/验证 Quorum、Durable、Binding、Operator Policy、`stream_queue`、At-least-once DLX、`overflow=reject-publish`、有界长度；Runtime 只通过 AMQP passive declare 比对 Exchange/Queue 类型与 Durable 属性，不读取 Policy/Feature Flag，不持 Management Credential，也不自动删除重建资源。

- [ ] **Step 5: 运行真实拓扑测试并提交**

Run: `cd backend; uv run pytest tests/integration/rabbitmq/test_ai_job_topology.py -v; uv run ruff check src/lawyer_agent/infrastructure/messaging tests/integration/rabbitmq; uv run mypy src`

Expected: Preflight 幂等，缺 Feature Flag/Policy/DLX/Binding/Queue 或类型漂移均 Fail Closed；Runtime 无 Management 凭据且 passive 缺失即 Not Ready；测试 VHost 精确清理后不存在。

Commit: `git commit -m "feat: provision reliable ai job topology"`

### Task 10: Transactional Outbox Publisher 与 Confirm/Mandatory

**Files:**
- Modify: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/ai_jobs_uow.py`
- Modify: `backend/src/lawyer_agent/infrastructure/messaging/rabbitmq.py`
- Create: `backend/src/lawyer_agent/runtime/composition.py`
- Create: `backend/src/lawyer_agent/runtime/failpoints.py`
- Create: `backend/src/lawyer_agent/workers/ai_job_publisher.py`
- Create: `backend/tests/integration/rabbitmq/test_ai_job_publisher.py`
- Modify: `backend/tests/integration/mysql/test_ai_job_repositories.py`

**Interfaces:**
- Consumes: Task 3 Outbox 表/UoW、Task 8 Signer/Publisher Settings、Task 9 Runtime Topology。
- Produces: `OutboxPublisherService.publish_batch(now) -> PublishBatchResult`、`AioPikaPublisher.publish(envelope, routing_key) -> PublishReceipt`、Publisher CLI `python -m lawyer_agent.workers.ai_job_publisher`。

- [ ] **Step 1: 写 Confirm、Return、不确定发布和崩溃重取测试**

```python
@pytest.mark.integration
async def test_unroutable_confirm_never_marks_published(publisher, outbox_row) -> None:
    result = await publisher.publish_one(outbox_row.id, routing_key="ai.job.unbound")
    assert result.error_code == "rabbit_unroutable"
    assert await load_outbox(outbox_row.id).status == OutboxStatus.PENDING


@pytest.mark.mysql
async def test_expired_publisher_claim_reuses_same_envelope(uow_factory) -> None:
    first = await claim_and_persist_envelope(uow_factory, OUTBOX, NOW)
    second = await reclaim_after_expiry(uow_factory, OUTBOX, NOW + CLAIM_TTL)
    assert second.message_id == first.message_id
    assert second.envelope_digest == first.envelope_digest
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/rabbitmq/test_ai_job_publisher.py tests/integration/mysql/test_ai_job_repositories.py -v`

Expected: Publisher Service/Adapter 和 Claim Fence 方法不存在，测试失败。

- [ ] **Step 3: 实现无网络事务的 Outbox Claim/Fence**

```python
class OutboxPublisherService:
    async def claim_batch(self, *, now: datetime, limit: int) -> tuple[ClaimedOutbox, ...]:
        raise NotImplementedError

    async def mark_published(self, claim: ClaimedOutbox, receipt: PublishReceipt, *, now: datetime) -> bool:
        raise NotImplementedError

    async def release_or_block(self, claim: ClaimedOutbox, error_code: PublisherErrorCode, *, now: datetime) -> bool:
        raise NotImplementedError
```

Claim 事务使用 Due `pending` 或 Claim 过期 `publishing` + `FOR UPDATE SKIP LOCKED`；首次 Claim 生成 Message ID/Nonce/Issued At/Signature/Digest，后续 Claim 复用完全相同 Envelope；递增 Claim Fence、设置 Token/TTL 后先 Commit。不得在 MySQL 事务或行锁内等待 Rabbit 网络。

- [ ] **Step 4: 实现 aio-pika Confirm + Mandatory 发布循环**

按 aio-pika 10.x 公共 API 使用 `aio_pika.connect_robust()`，再创建 `publisher_confirms=True, on_return_raises=True` Channel；每次 `exchange.publish(message, routing_key, mandatory=True, timeout=confirm_timeout)`。只有 Positive Confirm 且无 Return 才以 Claim Token/Fence/Version 标记 Published；Nack、Timeout、Channel Close、Connection Loss、Return 分别写稳定码并释放 Pending，有界耗尽写 Blocked/Audit。进程停机先停止 Claim，再在期限内等待在途 Confirm；不得调用 9.x 私有实现或未类型化内部属性。

`FailpointRegistry` 只在 `environment=test` 接受固定 `publisher.after_claim_commit`、`publisher.after_confirm_before_mark`，触发时调用进程退出钩子而不是抛出可被业务捕获的异常。真实测试由父进程等候数据库/Confirm 探针后终止子进程，再启动全新 Publisher 验证同 Envelope 恢复；非 Test 或未知名称在进程连接数据库/RabbitMQ 前失败。

- [ ] **Step 5: 验证并提交 Publisher**

Run: `cd backend; uv run pytest tests/integration/rabbitmq/test_ai_job_publisher.py tests/integration/mysql/test_ai_job_repositories.py -v; uv run ruff check .; uv run mypy src`

Expected: Positive Confirm、Mandatory Return、Nack、Timeout、断线、同 Envelope 重发、并发 Publisher 唯一 Claim、迟到结果 Fence 0 行全部通过；本任务不消费消息或执行 Handler。

Commit: `git commit -m "feat: publish fenced ai job outbox"`

### Task 11: 里程碑 B 总门禁与独立审查

**Files:**
- Test only: `backend/tests/unit/test_ai_job_{settings,envelope}.py`
- Test only: `backend/tests/integration/mysql/test_message_security_rejections.py`
- Test only: `backend/tests/integration/rabbitmq/test_ai_job_{topology,publisher}.py`

**Interfaces:**
- Consumes: Task 8–10。
- Produces: B 的签名、拓扑和发布可靠性证据；不执行 Handler。

- [ ] **Step 1: 运行 B 全门禁**

```powershell
cd backend
uv run pytest tests/unit/test_ai_job_settings.py tests/unit/test_ai_job_envelope.py -v
uv run pytest tests/integration/mysql/test_message_security_rejections.py tests/integration/rabbitmq/test_ai_job_topology.py tests/integration/rabbitmq/test_ai_job_publisher.py -v
uv run ruff check .
uv run mypy src
```

Expected: 零失败、零跳过；每个 Rabbit VHost 都是 `lawyer_test_<32 lowercase hex>` 并已精确清理。Confirm 与 Mandatory、Private/Public/Management Secret 隔离、Security Rejection 容量边界均有证据。

- [ ] **Step 2: 证明 B 边界未执行 Handler**

Run: `cd backend; rg -n "SyntheticHandler|handler\.execute|message\.ack|message\.reject" src/lawyer_agent/workers/ai_job_publisher.py src/lawyer_agent/infrastructure/messaging`

Expected: 无 Handler、Consumer ACK 或 Reject 调用；Publisher 只 Claim/Sign/Publish/Confirm/Mark。

- [ ] **Step 3: 请求独立审查并执行结论**

审查输入固定为 Task 8–10 Commit Range、规格第 7.7/7.10、10–11、12.1(8–10)、15–16、18.2、19.3、20.2–20.4/20.12/20.15 节和门禁原始输出。只有 Envelope、Key Boundary、Topology Preflight、Confirm/Mandatory、Security Telemetry 五项均为 `Ready` 才进入 Task 12；否则只修复 B、补失败测试并重跑本门禁。

## 里程碑 C：Worker、Inbox、Lease/Fence、Effect 与恢复

### Task 12: Worker 验签顺序、Inbox 去重与权威 Claim

**Files:**
- Modify: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/ai_jobs_uow.py`
- Modify: `backend/src/lawyer_agent/infrastructure/messaging/rabbitmq.py`
- Create: `backend/src/lawyer_agent/workers/ai_job_worker.py`
- Create: `backend/tests/integration/mysql/test_ai_job_worker_transactions.py`
- Create: `backend/tests/integration/rabbitmq/test_ai_job_consumer.py`

**Interfaces:**
- Consumes: Task 4 `AIJobAuthorityLoader/DurableGrantPolicy`、Task 8 Verifier、Task 9 Consumer Channel。
- Produces: `AIJobWorkerService.accept_delivery(delivery) -> DeliveryDecision`、`AioPikaJobConsumer.run()`、`ClaimedExecution`。

- [ ] **Step 1: 写验签/Inbox/Clock 固定顺序和无 Attempt 授权失败测试**

```python
@pytest.mark.integration
async def test_exact_digest_duplicate_may_cross_first_clock_window(worker, delivered) -> None:
    await worker.accept_delivery(delivered)
    duplicate = delivered.with_redelivery_at(NOW + timedelta(hours=1))
    assert await worker.accept_delivery(duplicate) is DeliveryDecision.ACK_DUPLICATE


@pytest.mark.mysql
async def test_revoked_before_first_claim_fails_without_attempt(worker, revoked_grant_delivery) -> None:
    decision = await worker.accept_delivery(revoked_grant_delivery)
    assert decision is DeliveryDecision.ACK_TERMINAL
    assert await count_attempts(revoked_grant_delivery.job_id) == 0
    assert await inbox_status(revoked_grant_delivery.message_id) is InboxStatus.COMPLETED
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_worker_transactions.py tests/integration/rabbitmq/test_ai_job_consumer.py -v`

Expected: Worker Service/Consumer、Inbox 和 Claim 方法不存在，测试失败。

- [ ] **Step 3: 实现固定消息验证和 Inbox 顺序**

```python
async def accept_delivery(self, raw: RawDelivery) -> DeliveryDecision:
    bounded = self._codec.require_size_and_content_type(raw)
    parsed = self._codec.parse_and_require_canonical(bounded)
    verified = self._verifier.verify(parsed)
    existing = await self._lookup_inbox(verified.tenant_id, verified.message_id)
    if existing is not None:
        return await self._decide_existing_digest(existing, verified)
    self._clock.require_first_seen_window(verified.issued_at)
    return await self._insert_and_claim(verified)
```

任何结构/规范/签名失败先写有界 Platform Security Rejection，再返回 `REJECT_NO_REQUEUE`；只有验签后才使用 Tenant/Message 查 Inbox。Exact Digest Duplicate 可越过 Clock；Digest/Nonce 冲突 Fail Closed。未验签值不进入 Tenant Audit。

- [ ] **Step 4: 实现 Inbox(NULL) → 授权 → Attempt/Fence 的事务**

在单事务先插 `Inbox(handling_attempt_id=NULL)` 并 Flush，锁同 Tenant Job；终态只完成 Inbox。首次 Claim 前重载 Grant/User/Tenant/Membership/Role/Permission/Scope；失效时 `queued/retry_scheduled -> failed(authorization_revoked|job_expired)` + Audit + Inbox Completed，不建 Lease/Attempt。授权有效才原子递增 Fence、设 Lease、建 Attempt，再以四列 `(tenant,job,attempt,fence)` 更新 Inbox 为 Processing。并发 Delivery 只有一个 Claim，其余按 Inbox/Lease 对账。

- [ ] **Step 5: 验证并提交 Worker Claim**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_worker_transactions.py tests/integration/rabbitmq/test_ai_job_consumer.py -v; uv run ruff check .; uv run mypy src`

Expected: 校验顺序、Clock Window、Digest/Nonce 冲突、终态消息、首次授权失效无 Attempt、两 Worker 单 Fence和跨租户 Envelope 全部通过；尚不执行 Handler。

Commit: `git commit -m "feat: claim verified ai job deliveries"`

### Task 13: Step Effect、Heartbeat 与受控 Synthetic Handler

**Files:**
- Modify: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Create: `backend/src/lawyer_agent/workers/synthetic.py`
- Create: `backend/tests/unit/test_synthetic_handler.py`
- Modify: `backend/tests/integration/mysql/test_ai_job_worker_transactions.py`

**Interfaces:**
- Consumes: Task 12 `ClaimedExecution`。
- Produces: `FencedCancellationProbe`、`FencedLeaseHeartbeat`、`FencedEffectStore`、`SyntheticHandler.execute(context) -> SyntheticResult`。

- [ ] **Step 1: 写幂等 Effect、迟到 Fence 和 Synthetic 非发布测试**

```python
async def test_same_effect_key_applies_once(effect_store) -> None:
    first = await effect_store.apply("unit", "stable-1", {"counter": 1})
    second = await effect_store.apply("unit", "stable-1", {"counter": 1})
    assert first == second
    assert await count_applied_effects("stable-1") == 1


async def test_synthetic_result_is_never_publishable(handler, execution_context) -> None:
    result = await handler.execute(execution_context)
    assert result.release_state is ReleaseState.NON_PUBLISHABLE
    assert set(result.model_dump()) == {"result_code", "completed_units", "release_state"}
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/unit/test_synthetic_handler.py tests/integration/mysql/test_ai_job_worker_transactions.py -v`

Expected: 三个 Fenced Port 和 Synthetic Handler 不存在，测试失败。

- [ ] **Step 3: 实现每边界授权/Fence 校验的三个 Port**

```python
class FencedEffectStore:
    async def apply(self, step_code: str, effect_key: str, result: SyntheticEffectResult) -> SyntheticEffectResult:
        authority = await self._loader.load(self._context.tenant_id, self._context.job_id, for_update=True)
        self._policy.require(authority, now=self._clock.now())
        return await self._repository.apply_effect(self._context.fence, step_code, effect_key, result)
```

Cancellation Probe 每个 Work Unit 前读 MySQL；Heartbeat 在不超过 20 秒间隔、Lease 一半前延长同 Fence，`running/cancel_requested` 均可续租但不能更改权限/状态；Effect Insert/Complete 用 `(tenant,job,step,effect_key)` 唯一键和当前 Fence CAS，输入 Digest 不同视为安全状态错误。

- [ ] **Step 4: 实现四场景 Synthetic Handler 和启动 Guard**

Handler 只接受 `kind=synthetic.v1`、`scenario=success|retry_once|permanent_failure|wait_for_cancel`、`work_units=1..10`。每 Unit 调 Cancellation Probe → Effect Store → Heartbeat；输出只含稳定 Result Code/计数/`non_publishable`。`AIJobWorkerSettings` 在 Production 或非显式 Enabled 时拒绝注册 Handler；未知 Handler/Version/Input Schema 永久失败。

- [ ] **Step 5: 验证并提交执行端口**

Run: `cd backend; uv run pytest tests/unit/test_synthetic_handler.py tests/integration/mysql/test_ai_job_worker_transactions.py -v; uv run ruff check .; uv run mypy src`

Expected: Effect 并发唯一、旧 Fence 0 行、每边界权限撤销、取消观察、Heartbeat 时间约束、四场景结果和 Production Guard 全部通过。

Commit: `git commit -m "feat: execute fenced synthetic ai jobs"`

### Task 14: Manual ACK、Retry/DLQ 与隔离进程崩溃恢复

**Files:**
- Modify: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Modify: `backend/src/lawyer_agent/infrastructure/messaging/rabbitmq.py`
- Modify: `backend/src/lawyer_agent/runtime/failpoints.py`
- Modify: `backend/src/lawyer_agent/workers/ai_job_worker.py`
- Create: `backend/tests/integration/rabbitmq/test_ai_job_failpoints.py`
- Modify: `backend/tests/integration/rabbitmq/test_ai_job_consumer.py`
- Modify: `backend/tests/integration/mysql/test_ai_job_worker_transactions.py`

**Interfaces:**
- Consumes: Task 13 Handler/Effect Port 与 Task 9 Retry/DLQ 拓扑。
- Produces: `AIJobWorkerService.finalize()`、`RetrySchedule.for_attempt()`、Worker 优雅停机与 Crash Failpoint 协议。

- [ ] **Step 1: 写 ACK 时序、Retry Bucket 和 Prefetch 测试**

```python
@pytest.mark.integration
async def test_commit_after_effect_before_ack_redelivery_is_idempotent(crash_worker) -> None:
    await crash_worker.run_until("worker.after_final_commit_before_ack")
    crash_worker.kill()
    recovered = await start_fresh_worker()
    await recovered.wait_for_ack(MESSAGE_ID)
    assert await count_effects(JOB_ID) == 1


def test_prefetch_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AIJobWorkerSettings(prefetch=0, **VALID_WORKER_SETTINGS)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/rabbitmq/test_ai_job_failpoints.py tests/integration/rabbitmq/test_ai_job_consumer.py -v`

Expected: Manual ACK 时序、Retry Finalize 和 Worker Failpoint 尚未实现，测试失败。

- [ ] **Step 3: 实现最终事务与 Broker 决策**

成功事务原子写 Effect/Attempt Succeeded/Job Succeeded/Inbox Completed/Audit；瞬时失败写 Attempt Retryable、Job `retry_scheduled`、`next_attempt_at` 和新 Outbox 后完成 Inbox；永久失败写终态。5/30/120 秒由 `attempt_no` 精确选桶，最多 4 Attempt。业务瞬时失败 Commit 后 ACK 当前 Delivery，绝不 `requeue=true`；结构/签名 Poison 使用 `reject(requeue=False)`；业务永久失败 Commit 后 ACK。

- [ ] **Step 4: 实现真实子进程 Failpoint 与 ACK 后置**

Registry 增加 `worker.after_inbox_commit`、`worker.after_effect_commit`、`worker.after_final_commit_before_ack`。父测试进程通过单向管道收到边界探针，使用 `subprocess.Popen.kill()` 终止 Worker，再以新 Connection/Channel 启动全新 Worker。Consumer 回调只在应用返回 `ACK` 后调用 `message.ack()`；Reject 只用于 Poison；Prefetch 通过 `channel.set_qos(prefetch_count=settings.prefetch, global_=False)` 按 Consumer 有界设置。

- [ ] **Step 5: 验证并提交可靠消费**

Run: `cd backend; uv run pytest tests/integration/rabbitmq/test_ai_job_consumer.py tests/integration/rabbitmq/test_ai_job_failpoints.py tests/integration/mysql/test_ai_job_worker_transactions.py -v; uv run ruff check .; uv run mypy src`

Expected: Commit 前崩溃重投、Commit 后 ACK 前只 ACK、Effect 一次、Retry 三桶允许抖动、无热 Requeue、Poison DLQ、业务失败 ACK、每 Consumer Prefetch=8 且 0 拒绝全部通过。

Commit: `git commit -m "feat: recover ai jobs with manual acknowledgements"`

### Task 15: Lease Recovery、Envelope Refresh、Maintenance 与 Redis 取消提示

**Files:**
- Modify: `backend/src/lawyer_agent/application/ai_job_runtime.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Create: `backend/src/lawyer_agent/infrastructure/redis/ai_job_hints.py`
- Modify: `backend/src/lawyer_agent/runtime/composition.py`
- Modify: `backend/src/lawyer_agent/runtime/failpoints.py`
- Create: `backend/src/lawyer_agent/workers/ai_job_maintenance.py`
- Create: `backend/src/lawyer_agent/cli/redrive_ai_job.py`
- Create: `backend/tests/integration/mysql/test_ai_job_maintenance.py`
- Create: `backend/tests/integration/redis/test_ai_job_hints.py`
- Modify: `backend/tests/integration/rabbitmq/test_ai_job_failpoints.py`

**Interfaces:**
- Consumes: Task 10 Publisher Outbox、Task 12 Lease/Inbox、Task 14 Retry。
- Produces: `AIJobMaintenanceService.run_batch(now) -> MaintenanceBatchResult`、`AIJobRedriveService.request(command) -> RedriveResult`、`AIJobCancellationHint.publish/poll`、Maintenance/受控 Redrive CLI。

- [ ] **Step 1: 写 Lease 三分支、Envelope 二代和 Redis 降级测试**

```python
@pytest.mark.mysql
async def test_expired_cancel_requested_becomes_cancelled(maintenance, leased_job) -> None:
    await mark_cancel_requested(leased_job)
    await maintenance.run_batch(now=leased_job.lease_expires_at)
    assert await job_status(leased_job.id) is AIJobStatus.CANCELLED
    assert await recovery_outbox_count(leased_job.lease_fence) == 0


@pytest.mark.mysql
async def test_retry_and_recovery_dispatch_can_refresh_second_envelope(maintenance) -> None:
    retry, recovery = await stale_retry_and_recovery_outboxes()
    await maintenance.run_batch(now=STALE_AT)
    assert await envelope_generations(retry.dispatch_generation) == (1, 2)
    assert await envelope_generations(recovery.dispatch_generation) == (1, 2)
    assert await recovery_source_fences(recovery.dispatch_generation) == (recovery.recovery_source_fence,) * 2
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_maintenance.py tests/integration/redis/test_ai_job_hints.py tests/integration/rabbitmq/test_ai_job_failpoints.py -v`

Expected: Maintenance Service、Redis Hint 和维护 Failpoint 不存在，测试失败。

- [ ] **Step 3: 实现 Lease 过期、Job 到期和授权失效收敛**

Maintenance 用有界 `SKIP LOCKED` Claim + Job Version/Fence。`running` 有预算且有效时写 Attempt Lease Expired、Job Retry Scheduled、清 Lease并以 Source Fence 创建一个逻辑 Recovery Dispatch；预算耗尽/到期/授权失效写 Failed；`cancel_requested` 只写 Cancelled。无 Lease 的 Queued/Retry Scheduled 到期或授权失效写 Failed/Audit并 Supersede 未完成 Outbox。活跃 Publishing Claim 不抢占，过期后处理。

- [ ] **Step 4: 实现 Envelope Refresh、Inbox 对账和 Redis Hint**

Refresh 只看当前 Outbox `message_id` 无 Inbox且 Job 无有效 Running Lease，不看历史 Attempt/Inbox。Pending 未生成 Message 不刷新；过期 Publishing、Published、Blocked 超窗时在同事务 Supersede 原行并保持 Logical Dispatch/Recovery Source、增加 `envelope_generation`，上限 4。迟到 Confirm 因 Claim Fence 更新 0 行。Redis Key 固定 `ai-job-cancel:{tenant_id}:{job_id}` 并对标识做现有前缀隔离；发布/轮询失败只记 Degraded，Worker 始终重读 MySQL。

Maintenance 还对 Processing TTL 已过期的 Inbox 做有界对账：若对应租约仍有效则保持等待；若 Effect/终态已提交则 Fenced Complete；否则交给 Lease Recovery，绝不凭 Broker 状态猜测业务结果。它同时按每批最多 1000 行清理 7 天前 Security Rejection。Registry 增加 `maintenance.after_recovery_commit`；多 Maintenance 并发败者由 Claim/Job/唯一键退出。

`redrive_ai_job.py` 是非 HTTP 的受控运维入口：命令必须带工单号、原因和已验证 Platform Operator Workload Identity。`AIJobRedriveService` 只允许 Queued/Retry Scheduled、无有效 Lease、无终态 Effect、Job 未过期且当前 Durable Grant 仍有效的 Job；终态、消息安全拒绝和权限失效一律拒绝。它写 Request/Completed/Rejected 结构化 Audit，并请求 Publisher 为新 Logical Dispatch 签名发布；Maintenance/API 仍不读取私钥。DLQ 不自动回放，旧 Broker Delivery 保留用于取证后由精确 VHost/Queue 管理流程清理。

- [ ] **Step 5: 验证并提交 Maintenance**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_maintenance.py tests/integration/redis/test_ai_job_hints.py tests/integration/rabbitmq/test_ai_job_failpoints.py -v; uv run ruff check .; uv run mypy src`

Expected: Lease Retry/Failed/Cancelled 三分支、Source Fence 单逻辑 Recovery、Retry/Recovery Envelope 1→2、活跃 Claim 等待、过期 Processing Inbox 对账、迟到 Confirm 0 行、Job Expiry、Redis 故障回源、跨租户 Hint、多实例恢复，以及受控 Redrive 的允许/拒绝/Audit 边界全部通过。

Commit: `git add backend/src/lawyer_agent/application/ai_job_runtime.py backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py backend/src/lawyer_agent/infrastructure/redis/ai_job_hints.py backend/src/lawyer_agent/runtime/composition.py backend/src/lawyer_agent/runtime/failpoints.py backend/src/lawyer_agent/workers/ai_job_maintenance.py backend/src/lawyer_agent/cli/redrive_ai_job.py backend/tests/integration/mysql/test_ai_job_maintenance.py backend/tests/integration/redis/test_ai_job_hints.py backend/tests/integration/rabbitmq/test_ai_job_failpoints.py; git commit -m "feat: maintain and recover durable ai jobs"`

### Task 16: 里程碑 C 内部 Harness 与总门禁

**Files:**
- Modify: `backend/src/lawyer_agent/runtime/composition.py`
- Modify: `backend/src/lawyer_agent/workers/synthetic.py`
- Test only: `backend/tests/integration/mysql/test_ai_job_worker_transactions.py`
- Test only: `backend/tests/integration/rabbitmq/test_ai_job_{consumer,failpoints}.py`
- Test only: `backend/tests/integration/redis/test_ai_job_hints.py`

**Interfaces:**
- Consumes: Task 12–15。
- Produces: 非 HTTP `SyntheticJobHarness.submit(command) -> UUID` 和里程碑 C Ready 证据。

- [ ] **Step 1: 增加环境受限内部 Harness 测试**

```python
async def test_internal_harness_runs_full_non_publishable_job(synthetic_harness) -> None:
    job_id = await synthetic_harness.submit(SyntheticSubmitCommand("success", 2))
    await synthetic_harness.wait_terminal(job_id)
    result = await synthetic_harness.load_result(job_id)
    assert result.release_state is ReleaseState.NON_PUBLISHABLE


def test_production_cannot_construct_synthetic_harness() -> None:
    with pytest.raises(ValueError, match="synthetic disabled"):
        SyntheticJobHarness(environment="production", enabled=True, services=TEST_SERVICES)
```

- [ ] **Step 2: 实现 Harness 并运行 C 全门禁**

Harness 仅从 Development/Test/Staging 组合根直接调用应用 Service 创建 Job/Grant/Outbox，不注册 FastAPI Route，不接受网络输入；三个环境都必须显式 Enabled，Production 构造失败。随后运行：

```powershell
cd backend
uv run pytest tests/unit/test_synthetic_handler.py -v
uv run pytest tests/integration/mysql/test_ai_job_worker_transactions.py tests/integration/mysql/test_ai_job_maintenance.py -v
uv run pytest tests/integration/rabbitmq/test_ai_job_consumer.py tests/integration/rabbitmq/test_ai_job_failpoints.py -v
uv run pytest tests/integration/redis/test_ai_job_hints.py -v
uv run ruff check .
uv run mypy src
```

Expected: 零失败、零跳过；成功、Retry、永久失败、等待取消、授权撤销、所有 Crash Point、Redis 降级与跨租户消息全部通过。

- [ ] **Step 3: 证明 C 没有公共 Job API**

Run: `cd backend; uv run python -c "from lawyer_agent.main import create_app; assert all('ai-jobs' not in route.path for route in create_app().routes)"`

Expected: 退出码 0。

- [ ] **Step 4: 请求独立审查并执行结论**

审查 Task 12–15 Commit Range与规格第 6、7.6–7.10、8、10.3、12.2–13、18.3、19.2–19.5、20.3/20.5–20.11。只有 Worker Principal、Inbox/ACK、Fence/Effect、Retry/DLQ、Maintenance/Redis 五项均为 `Ready` 才进入 Task 17；否则只修复 C 并重跑本门禁。

- [ ] **Step 5: 提交 Harness**

```powershell
git add backend/src/lawyer_agent/runtime/composition.py backend/src/lawyer_agent/workers/synthetic.py
git commit -m "test: verify durable ai job worker runtime"
```

## 里程碑 D：HTTP API、Compose、健康、指标与全量门禁

### Task 17: 原子 Idempotency、Admission 与 AI Job 应用服务

**Files:**
- Modify: `backend/src/lawyer_agent/application/idempotency.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/idempotency.py`
- Modify: `backend/src/lawyer_agent/application/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/repositories/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/infrastructure/persistence/ai_jobs_uow.py`
- Create: `backend/tests/integration/mysql/test_ai_job_idempotency.py`

**Interfaces:**
- Consumes: Task 3 AI Job UoW、Task 4 Resolver、Task 5 Feature 状态、现有 Idempotency 类型。
- Produces: `IdempotencyService.lookup_completed()`、`IdempotencyService.acquire_in_business_transaction()`、`AIJobService.create/get/cancel`。

- [ ] **Step 1: 写 Replay 当前授权、背压绕过和 Deactivation 竞态测试**

```python
@pytest.mark.mysql
async def test_completed_create_replay_rechecks_current_permission(service, completed_create) -> None:
    await revoke_permission(completed_create.membership_id, "ai_job.create")
    with pytest.raises(JobAuthorizationDenied):
        await service.create(completed_create.command)


@pytest.mark.mysql
async def test_completed_replay_bypasses_current_admission_but_returns_current_etag(service, completed_create) -> None:
    service.admission.force_unavailable()
    replay = await service.create(completed_create.command)
    assert replay.job_id == completed_create.job_id
    assert replay.etag == await current_job_etag(completed_create.job_id)


@pytest.mark.mysql
async def test_deactivation_update_lock_closes_new_create(service, rollout) -> None:
    await rollout.lock_global_for_deactivation()
    request = asyncio.create_task(service.create(NEW_COMMAND))
    await rollout.commit_deactivating()
    with pytest.raises(JobHandlerUnavailable):
        await request
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_idempotency.py -v`

Expected: Idempotency 短 Lookup/原子模式和 Job Service 未实现，测试失败。

- [ ] **Step 3: 扩展 Idempotency 原子模式并保持既有调用兼容**

```python
class IdempotencyService:
    async def lookup_completed(
        self,
        repository: IdempotencyRepositoryPort,
        *,
        scope: IdempotencyScope,
        operation: str,
        request: IdempotencyRequest,
    ) -> CompletedIdempotency | IdempotencyMiss:
        raise NotImplementedError

    async def acquire_in_business_transaction(
        self,
        repository: IdempotencyRepositoryPort,
        *,
        scope: IdempotencyScope,
        operation: str,
        request: IdempotencyRequest,
        now: datetime,
    ) -> AtomicIdempotencyAcquire:
        raise NotImplementedError
```

短 Lookup 只读、不 Reserve、不跨 Redis 持锁；Completed 比对 Fingerprint 后返回 Result Reference。只有新请求执行 Redis 限流/配额、Outbox Backpressure、Handler/Environment Admission。最终单 MySQL 事务唯一 Acquire：Winner 写 Reserved + Job/Grant/Audit/Outbox + Completed；Loser 唯一冲突回滚后新短事务重读。不得产生对外可见孤立 Reservation；Admission 失败不插 Idempotency 行。保持现有身份用例 `reserve/complete` 行为不变。

- [ ] **Step 4: 实现 Create/Get/Cancel 授权与锁顺序**

```python
class AIJobService:
    async def create(self, command: CreateAIJobCommand) -> AcceptedAIJob:
        raise NotImplementedError

    async def get(self, query: GetAIJobQuery) -> AIJobProjection:
        raise NotImplementedError

    async def cancel(self, command: CancelAIJobCommand) -> CancelAIJobResult:
        raise NotImplementedError
```

每次调用先 HTTP 已验证 Actor 所指 User/Session/Tenant/Membership → 当前 Permission → Resolver；绝不采用传入 `allow_tenant_wide`。Completed Create/Cancel Replay 仍做当前 Permission + Job ABAC，之后才返回当前安全投影/当前 ETag；只绕过 Admission、旧 If-Match 与写入。全新 Create 最终事务按 Global Feature `FOR SHARE` → Tenant State `FOR SHARE` → Membership/Authz/Permission 重验 → Idempotency Acquire → Job Graph 写入；Deactivation 用 Global `FOR UPDATE` 串行关闭。Create 固定 Owner-only。Cancel 新请求校验当前 If-Match 后 CAS：Queued/Retry→Cancelled，Running→Cancel-requested且保留 Lease，终态 409；Redis Hint 仅 Commit 后 best effort。

- [ ] **Step 5: 验证并提交应用服务**

Run: `cd backend; uv run pytest tests/integration/mysql/test_ai_job_idempotency.py tests/integration/mysql/test_idempotency.py tests/integration/mysql/test_permission_rollout_concurrency.py -v; uv run ruff check .; uv run mypy src`

Expected: Completed Replay 当前无权拒绝、当前 ETag、背压绕过、Fingerprint 冲突、并发 Winner/Loser、Admission 无记录、Create/Deactivation 双向调度、Cancel/Success 竞争全部通过。

Commit: `git commit -m "feat: add atomic ai job application service"`

### Task 18: 版本化 AI Job API、错误映射与 OpenAPI 契约

**Files:**
- Create: `backend/src/lawyer_agent/api/v1/ai_jobs.py`
- Modify: `backend/src/lawyer_agent/api/v1/router.py`
- Modify: `backend/src/lawyer_agent/api/dependencies.py`
- Modify: `backend/src/lawyer_agent/api/errors.py`
- Modify: `backend/src/lawyer_agent/main.py`
- Create: `backend/tests/api/test_ai_jobs.py`
- Create: `backend/tests/api/test_ai_job_cross_tenant.py`
- Create: `backend/tests/contract/test_openapi_ai_jobs.py`

**Interfaces:**
- Consumes: Task 17 `AIJobService` 与现有 `TenantActorDependency/require_path_tenant`。
- Produces: `POST /api/v1/tenants/{tenant_id}/ai-jobs`、`GET /api/v1/tenants/{tenant_id}/ai-jobs/{job_id}`、`POST /api/v1/tenants/{tenant_id}/ai-jobs/{job_id}/cancel`。

- [ ] **Step 1: 写 202/ETag/304/Cancel/404 和泄露扫描测试**

```python
async def test_create_returns_202_location_and_strong_etag(client, tenant_headers) -> None:
    response = await client.post(
        f"/api/v1/tenants/{TENANT}/ai-jobs",
        headers={**tenant_headers, "Idempotency-Key": IDEMPOTENCY_KEY},
        json={"kind": "synthetic.v1", "scenario": "success", "work_units": 2},
    )
    assert response.status_code == 202
    assert response.headers["Location"].endswith(f"/ai-jobs/{response.json()['job_id']}")
    assert re.fullmatch(r'"[0-9]+"', response.headers["ETag"])


async def test_tenant_a_cannot_probe_tenant_b_job(client, tenant_a_headers) -> None:
    response = await client.get(f"/api/v1/tenants/{TENANT_A}/ai-jobs/{JOB_B}", headers=tenant_a_headers)
    assert response.status_code == 404
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/api/test_ai_jobs.py tests/api/test_ai_job_cross_tenant.py tests/contract/test_openapi_ai_jobs.py -v`

Expected: Route 未注册，返回 404；OpenAPI 缺少契约。

- [ ] **Step 3: 装配专用依赖而不复用通用 Tenant-wide Scope**

`ApplicationServices` 增加 `ai_jobs: AIJobService`，组合根注入独立 `SqlAlchemyAIJobUnitOfWork`。Route 可复用 Tenant Audience/Session 验证与 Path/Token Tenant equality，但 Service 必须立即从 MySQL 重载 Role/Permission并调用 Job Resolver；不得读取 `TenantActor.context.scope.allow_tenant_wide`。Production 或 Handler 不可用映射 503 `ai_job_handler_unavailable`，跨租户 404，同租户 ABAC 403，认证失效 401。

- [ ] **Step 4: 实现严格 Pydantic 契约与 HTTP 状态**

```python
class CreateAIJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["synthetic.v1"]
    scenario: Literal["success", "retry_once", "permanent_failure", "wait_for_cancel"]
    work_units: int = Field(ge=1, le=10)


class AIJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    status: PublicAIJobStatus
    submitted_at: datetime
    completed_at: datetime | None
    retry_scheduled: bool
    cancellation_requested: bool
    failure_code: str | None
    result: SyntheticPublicResult | None
```

Create 仅 Dev/Test/Staging Activated+Enabled 返回 202，含相对 Location 与强 ETag，不返回 Input/Snapshot。GET 支持 If-None-Match→304。Cancel 要求 Idempotency-Key + If-Match：Queued/Retry 首次 200、Running/Cancel-requested 202、Cancelled Replay 200、其他终态 409。429/503 带整数 Retry-After；Problem Details 不含 Broker/Constraint/SQL/签名。

- [ ] **Step 5: 验证并提交 HTTP 契约**

Run: `cd backend; uv run pytest tests/api/test_ai_jobs.py tests/api/test_ai_job_cross_tenant.py tests/contract/test_openapi_ai_jobs.py tests/api/test_composition_security.py -v; uv run ruff check .; uv run mypy src`

Expected: 创建/查询/304/取消/Replay 状态契约、Owner/Shared/Tenant-wide、Shared 不可取消、跨租户 404、Production 503 和 OpenAPI 禁止字段全部通过。

Commit: `git commit -m "feat: expose tenant ai job api"`

### Task 19: Compose 四进程、Health、Metrics 与 Secret Mount 边界

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/src/lawyer_agent/runtime/health.py`
- Create: `backend/src/lawyer_agent/runtime/metrics.py`
- Modify: `backend/src/lawyer_agent/runtime/settings.py`
- Modify: `backend/src/lawyer_agent/runtime/composition.py`
- Modify: `backend/src/lawyer_agent/workers/ai_job_publisher.py`
- Modify: `backend/src/lawyer_agent/workers/ai_job_worker.py`
- Modify: `backend/src/lawyer_agent/workers/ai_job_maintenance.py`
- Modify: `backend/src/lawyer_agent/config.py`
- Modify: `backend/src/lawyer_agent/main.py`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/compose.env.example`
- Modify: `deploy/compose.production.env.example`
- Modify: `scripts/dev.ps1`
- Create: `backend/tests/contract/test_ai_job_process_boundaries.py`
- Modify: `backend/tests/unit/test_compose_contract.py`
- Modify: `backend/tests/api/test_health.py`

**Interfaces:**
- Consumes: Task 8 进程 Settings、Task 9 Passive Topology、Task 10/14/15 三后台循环。
- Produces: `ProcessHealthState`、`create_process_probe_app()`、`AIJobMetricsPort`、Compose `ai-job-topology-init/publisher/worker/maintenance`。

- [ ] **Step 1: 写依赖健康、Secret 文件与低基数指标测试**

```python
def test_worker_service_mounts_public_key_but_not_private_or_management(compose_config) -> None:
    worker = compose_config["services"]["ai-job-worker"]
    assert "lawyer_message_public_verify_ring" in worker["secrets"]
    assert "lawyer_message_private_seed_ring" not in worker["secrets"]
    assert "lawyer_rabbit_management_credential" not in worker["secrets"]


def test_metrics_reject_identifier_labels(metrics: AIJobMetrics) -> None:
    with pytest.raises(ValueError):
        metrics.increment("job_transition", {"tenant_id": str(TENANT)})
```

- [ ] **Step 2: 加指标依赖并确认测试失败**

在 dependencies 加 `"prometheus-client>=0.21,<1"`，执行 `cd backend; uv lock; uv sync --frozen; uv run pytest tests/contract/test_ai_job_process_boundaries.py tests/unit/test_compose_contract.py tests/api/test_health.py -v`。

Expected: 后台服务、Health/Metrics 和 Secret 边界尚未进入 Compose，测试失败。

- [ ] **Step 3: 实现进程 Probe、低基数 Metrics 和优雅停机**

```python
ALLOWED_METRIC_LABELS = frozenset({"handler_code", "outcome", "failure_class", "queue_role", "schema_version", "rejection_code", "source_channel", "capacity_state"})


class ProcessHealthState:
    async def live(self) -> bool:
        raise NotImplementedError

    async def ready(self) -> ReadinessResult:
        raise NotImplementedError


def create_process_probe_app(state: ProcessHealthState, metrics: AIJobMetricsPort) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.process_health = state
    app.state.metrics = metrics
    return app
```

Publisher Ready=MySQL + AMQP + Private Ring + passive topology；Worker Ready=MySQL + AMQP + Public Ring + passive topology，Redis 故障仅 Degraded；Maintenance Ready=MySQL；API Ready=MySQL+安全 Redis且不连接 Rabbit。Live 只检查事件循环。`/metrics` 禁止 Tenant/Job/User/Message/Error 文本 Label。Publisher 停 Claim 后有界等待 Confirm；Worker 停消费后有界完成/释放 Lease，未 ACK 由 Broker 重投。

- [ ] **Step 4: 实现同镜像不同命令与最小 Secret Mount**

Compose 新增一次性 `ai-job-topology-init` 与三个后台服务，均复用 Backend Image、`read_only: true`、`tmpfs: [/tmp]`、非 Root、`no-new-privileges`。Topology Init 独占 Management Credential；Publisher 独占 Private Seed Ring；Worker 只挂 Public Verify Ring + Observability Key；Maintenance/API 不挂消息或 Management Key。API 移除 RabbitMQ/OpenSearch/MinIO Ready 依赖。Publisher/Worker 等待 Topology Init `service_completed_successfully`；Maintenance 只等 MySQL。示例 env 只列文件路径/非秘密参数，不提交真实 Key。

- [ ] **Step 5: 验证 Compose 并提交运行边界**

Run: `cd backend; uv run pytest tests/contract/test_ai_job_process_boundaries.py tests/unit/test_compose_contract.py tests/api/test_health.py -v; uv run ruff check .; uv run mypy src; cd ..; docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet`

Expected: 测试和静态检查通过，Compose config 退出码 0；Runtime 无 Management Credential，Worker/API/Maintenance 无 Private Seed，API 不因 Rabbit 停止而 Not Ready。

Commit: `git commit -m "feat: deploy durable ai job processes"`

### Task 20: 全故障矩阵、零跳过门禁与最终独立审查

**Files:**
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/integration/rabbitmq/network_fault_proxy.py`
- Modify: `backend/src/lawyer_agent/runtime/failpoints.py`
- Modify: `backend/tests/integration/rabbitmq/test_ai_job_failpoints.py`
- Modify: `backend/tests/integration/mysql/test_ai_job_idempotency.py`
- Modify: `backend/tests/api/test_ai_jobs.py`
- Create: `scripts/test-ai-job-runtime.ps1`

**Interfaces:**
- Consumes: Task 1–19 全部产物。
- Produces: 可重复全量门禁、Integration Skip 强制失败、完整 Crash/Restart 证据与最终 Ready 结论。

- [ ] **Step 1: 写 API Crash、Confirm 故障与 Integration Skip Gate**

```python
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.getenv("LAWYER_REQUIRE_INTEGRATION") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = () if reporter is None else tuple(reporter.stats.get("skipped", ()))
    if skipped:
        session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)


@pytest.mark.integration
async def test_api_commit_before_response_replays_same_job(crash_api_client) -> None:
    crash_api_client.enable("api.after_job_commit_before_response")
    await crash_api_client.post_create_and_kill()
    replay = await crash_api_client.restart_and_replay()
    assert replay.job_id == await only_job_id_for_key(IDEMPOTENCY_KEY)
```

Registry 最终固定且只允许八个名称：`api.before_job_commit`、`api.after_job_commit_before_response`、`publisher.after_claim_commit`、`publisher.after_confirm_before_mark`、`worker.after_inbox_commit`、`worker.after_effect_commit`、`worker.after_final_commit_before_ack`、`maintenance.after_recovery_commit`。

- [ ] **Step 2: 实现可重复基础设施故障编排脚本**

`scripts/test-ai-job-runtime.ps1` 必须：验证当前目录；要求未跟踪 `deploy/.env`；启动 MySQL/Redis/RabbitMQ；运行 Migration；设置 `LAWYER_REQUIRE_INTEGRATION=1` 后执行全部 Integration；调用 `network_fault_proxy.py` 在测试独占监听端口上以确定性规则制造 Confirm Timeout/Channel Close，用一次性 `reject-publish` Quorum Queue 制造 Nack，用无 Binding Key 制造 Mandatory Return；发布 Persistent 消息后执行 `docker compose --env-file deploy/.env -f deploy/compose.yaml restart rabbitmq` 并验证恢复。Proxy 只接受回环地址、父进程生成的随机控制令牌，并在测试退出时关闭 Socket/子进程。脚本清理只运行不带 `-v` 的精确 `docker compose --env-file deploy/.env -f deploy/compose.yaml down`，绝不删除命名 Volume。

- [ ] **Step 3: 运行聚焦故障矩阵**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-ai-job-runtime.ps1`

Expected: 八个 Failpoint 都在隔离子进程触发并由新进程恢复；Confirm Nack/Timeout/Close/Return、Rabbit 重启持久消息、Redis Timeout、MySQL Deadlock、取消竞争、权限三边界撤销、Retry/Recovery Envelope 1→2、Security Telemetry 容量和跨租户路径全部通过；Integration 汇总为 0 skipped。

- [ ] **Step 4: 运行完整仓库与部署门禁**

Run from `backend/`:

```powershell
uv sync --frozen
uv run pytest -m "not integration" -v
$env:LAWYER_REQUIRE_INTEGRATION = "1"
uv run pytest -m integration -v -ra
Remove-Item Env:LAWYER_REQUIRE_INTEGRATION
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

Expected: 所有测试、Ruff、mypy、Compose 和 Migration 门禁退出码 0；Integration 明确 0 skipped；Secret 扫描无命中；工作树只含当前任务预期文件。不得将单节点 Compose 结果表述为 Quorum 多数派、Kubernetes 或生产容量证明。

- [ ] **Step 5: 请求最终独立审查**

审查输入为 A–D 全 Commit Range、最终规格全文、四个里程碑门禁原始输出、OpenAPI、Compose 展开配置、Migration 往返和故障脚本输出。审查必须逐项回答规格 20.1–20.18 是否有实际证据，并核对 21 节未验证项仍被明确保留。

Expected: 只有全部已实现条目均为 `Ready` 且未把 LangChain、DeepSeek、RAG、MinIO、SSE、法律成果、三节点 RabbitMQ、Kubernetes 或 10,000 RPS 宣称为已完成，才可称本基础设施增量验收完成。

- [ ] **Step 6: 提交最终门禁**

```powershell
git add backend/tests/conftest.py backend/tests/integration/rabbitmq/network_fault_proxy.py backend/src/lawyer_agent/runtime/failpoints.py backend/tests/integration/rabbitmq/test_ai_job_failpoints.py backend/tests/integration/mysql/test_ai_job_idempotency.py backend/tests/api/test_ai_jobs.py scripts/test-ai-job-runtime.ps1
git commit -m "test: verify durable ai job runtime gates"
```

## Plan Self-Review

- Spec coverage: Task 1–7 覆盖 Schema/状态/授权/Rollout/Audit/身份 Writer；Task 8–11 覆盖 Envelope/Ed25519/安全拒绝/Topology/Publisher；Task 12–16 覆盖 Worker/Inbox/Lease/Fence/Effect/Retry/Refresh/Maintenance/Redis/Synthetic Harness；Task 17–20 覆盖 Idempotency Replay/API/Compose/Health/Metrics/故障与全量门禁。
- Placeholder scan: 每个实现任务都包含失败测试、预期失败、具体接口或算法、通过命令和 Commit；没有引用未命名的后续实现步骤。
- Type consistency: `AIJobStatus`、`JobAuthorizationScope`、`JobAuthoritySnapshot`、`SqlAlchemyAIJobUnitOfWork`、`AIJobEnvelope`、`ClaimedExecution`、`OutboxPublisherService`、`AIJobWorkerService`、`AIJobMaintenanceService` 与 `AIJobService` 在首次定义后保持同名复用。
- Security review: 当前 HTTP 硬编码 Tenant-wide Scope 未进入 Job 授权；Worker 无 Session；消息 Private/Public/Management Key 分离；所有复合 FK、Audit Actor 矩阵、Redis Key、Rabbit VHost 和跨租户反向路径都有真实依赖测试。
- Reliability review: Confirm/Mandatory 与 Manual ACK 分离，MySQL Commit 在 ACK 前，Outbox/Inbox/Effect 幂等、Lease Fence、Envelope Generation、Source Fence Recovery、固定 TTL Retry 与有界 DLQ 均有 Crash/竞态测试。
- Release review: Production 始终 Catalog-only 且禁止 Synthetic；Development/Test/Staging 的结果固定 Non-publishable；未把任何法律或后续 AI 能力列为本计划产物。

## Exit Criteria

本计划只有在 Task 7、11、16、20 四个门禁依次通过且每次独立审查为 Ready 后才完成。最终证据必须包含零 Integration Skip、真实 MySQL/RabbitMQ/Redis、Migration 往返、严格随机 VHost 清理、八个隔离进程 Failpoint、跨租户反向矩阵、Ruff/mypy、无秘密 OpenAPI、Compose 展开和 Secret 扫描。三节点 RabbitMQ、Kubernetes、Vault/KMS、10,000 RPS、LangChain、DeepSeek、RAG、Evidence Bundle、Citation Gate、SSE、MinIO、MCP、文档与法律成果仍是明确未验证项。
