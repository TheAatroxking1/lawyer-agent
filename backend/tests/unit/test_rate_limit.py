from __future__ import annotations

import json
from ipaddress import IPv4Address, ip_address
from uuid import UUID

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ReadOnlyError as RedisReadOnlyError
from redis.exceptions import ResponseError as RedisResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

import lawyer_agent.infrastructure.redis.client as redis_client
import lawyer_agent.infrastructure.redis.rate_limit as rate_limit_module
import lawyer_agent.infrastructure.redis.step_up as step_up_module
from lawyer_agent.domain.identity import IdentityKind, NormalizedIdentity, normalize_identifier
from lawyer_agent.infrastructure.redis.authz_cache import (
    AuthorizationCache,
    AuthorizationCacheEntry,
    AuthorizationScope,
)
from lawyer_agent.infrastructure.redis.client import (
    RedisAsyncioAdapter,
    RedisDependencyUnavailable,
    RedisFailureKind,
)
from lawyer_agent.infrastructure.redis.rate_limit import RateLimiter, RateLimitRule
from lawyer_agent.infrastructure.redis.step_up import StepUpStore

USER_ID = UUID("01990f00-0000-7000-8000-000000000501")
SESSION_ID = UUID("01990f00-0000-7000-8000-000000000502")
TENANT_ID = UUID("01990f00-0000-7000-8000-000000000503")
MEMBERSHIP_ID = UUID("01990f00-0000-7000-8000-000000000504")
DEPARTMENT_ID = UUID("01990f00-0000-7000-8000-000000000505")


def test_fixed_rule_constants_are_public_and_strongly_typed() -> None:
    assert rate_limit_module.REGISTER_RULE is RateLimitRule.REGISTER
    assert rate_limit_module.LOGIN_RULE is RateLimitRule.LOGIN
    assert rate_limit_module.REFRESH_RULE is RateLimitRule.REFRESH
    assert rate_limit_module.REAUTH_RULE is RateLimitRule.REAUTH
    assert rate_limit_module.SWITCH_RULE is RateLimitRule.SWITCH_TENANT


class RecordingRedis:
    def __init__(self) -> None:
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.get_values: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, int | None, bool]] = []
        self.deleted: list[str] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        self.eval_calls.append((script, numkeys, keys_and_args))
        if "compare-and-delete" in script:
            return 1
        return [1, 4, 0]

    async def get(self, key: str) -> bytes | None:
        return self.get_values.get(key)

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self.set_calls.append((key, value, ex, nx))
        if nx and key in self.get_values:
            return False
        self.get_values[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
        return len(keys)

    async def aclose(self) -> None:
        return None


class FailingSdkClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def eval(self, script: str, numkeys: int, *values: object) -> object:
        del script, numkeys, values
        raise self.error

    async def get(self, key: str) -> None:
        del key
        raise self.error

    async def set(self, key: str, value: bytes, **kwargs: object) -> None:
        del key, value, kwargs
        raise self.error

    async def delete(self, *keys: str) -> None:
        del keys
        raise self.error

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_rate_limit_keys_never_contain_raw_dimensions() -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    raw_ip = "203.0.113.42"
    raw_identity = "+8613800138000"

    decision = await limiter.consume(
        RateLimitRule.LOGIN,
        {
            "ip": ip_address(raw_ip),
            "identity": normalize_identifier(IdentityKind.PHONE, raw_identity),
        },
    )

    assert decision.allowed is True
    assert decision.remaining == 4
    assert decision.retry_after_seconds == 0
    _, numkeys, payload = redis.eval_calls[0]
    assert numkeys == 2
    serialized = repr(payload)
    assert raw_ip not in serialized
    assert raw_identity not in serialized
    assert "13800138000" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "dimensions", "expected_buckets"),
    [
        (RateLimitRule.REGISTER, {"ip": ip_address("203.0.113.1")}, 1),
        (
            RateLimitRule.LOGIN,
            {
                "ip": ip_address("203.0.113.1"),
                "identity": normalize_identifier(
                    IdentityKind.EMAIL, "user@example.cn"
                ),
            },
            2,
        ),
        (RateLimitRule.REFRESH, {"session": SESSION_ID}, 1),
        (RateLimitRule.REAUTH, {"session": SESSION_ID}, 1),
        (RateLimitRule.SWITCH_TENANT, {"session": SESSION_ID}, 1),
    ],
)
async def test_fixed_rate_limit_rules_use_one_atomic_script(
    rule: RateLimitRule,
    dimensions: dict[str, object],
    expected_buckets: int,
) -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    await limiter.consume(rule, dimensions)

    assert len(redis.eval_calls) == 1
    assert redis.eval_calls[0][1] == expected_buckets


@pytest.mark.asyncio
async def test_rate_limit_rejects_missing_or_extra_dimensions_before_redis() -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    with pytest.raises(ValueError, match="dimensions"):
        await limiter.consume(
            RateLimitRule.LOGIN, {"ip": ip_address("203.0.113.1")}
        )
    with pytest.raises(ValueError, match="dimensions"):
        await limiter.consume(
            RateLimitRule.REGISTER,
            {"ip": ip_address("203.0.113.1"), "identity": "unexpected"},
        )

    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_rate_limit_hmac_accepts_unicode_identity_without_exposing_it() -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    identity = "ＷＡＮＧＬＵＳＨＩ"
    normalized = normalize_identifier(IdentityKind.USERNAME, identity)

    await limiter.consume(
        RateLimitRule.LOGIN,
        {"ip": ip_address("2001:0db8::1"), "identity": normalized},
    )

    assert identity not in repr(redis.eval_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        "arbitrary-raw-string",
        "line\nbreak",
        "\ud800",
        NormalizedIdentity(
            IdentityKind.WECHAT_OPENID,
            "wechat-provider",
            "line\nbreak",
            None,
        ),
    ],
)
async def test_rate_limit_rejects_unsafe_dimension_values(identity: object) -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    with pytest.raises(ValueError, match="dimension"):
        await limiter.consume(
            RateLimitRule.LOGIN,
            {"ip": ip_address("203.0.113.1"), "identity": identity},
        )

    assert redis.eval_calls == []


@pytest.mark.asyncio
async def test_equivalent_ip_identity_and_uuid_objects_use_canonical_keys() -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    await limiter.consume(
        RateLimitRule.REGISTER,
        {"ip": ip_address("2001:0DB8:0:0:0:0:0:1")},
    )
    await limiter.consume(
        RateLimitRule.REGISTER,
        {"ip": ip_address("2001:db8::1")},
    )
    first_ip_key = redis.eval_calls[-2][2][0]
    second_ip_key = redis.eval_calls[-1][2][0]
    assert first_ip_key == second_ip_key

    await limiter.consume(
        RateLimitRule.REGISTER,
        {"ip": IPv4Address(int(ip_address("203.0.113.9")))},
    )
    await limiter.consume(
        RateLimitRule.REGISTER,
        {"ip": ip_address("203.0.113.9")},
    )
    assert redis.eval_calls[-2][2][0] == redis.eval_calls[-1][2][0]

    first_identity = normalize_identifier(IdentityKind.USERNAME, "  ＷＡＮＧ  ")
    second_identity = normalize_identifier(IdentityKind.USERNAME, "wang")
    for identity in (first_identity, second_identity):
        await limiter.consume(
            RateLimitRule.LOGIN,
            {"ip": ip_address("203.0.113.10"), "identity": identity},
        )
    assert redis.eval_calls[-2][2][1] == redis.eval_calls[-1][2][1]

    for session_id in (SESSION_ID, UUID(str(SESSION_ID).upper())):
        await limiter.consume(RateLimitRule.REFRESH, {"session": session_id})
    assert redis.eval_calls[-2][2][0] == redis.eval_calls[-1][2][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_value",
    [str(SESSION_ID), UUID("00000000-0000-4000-8000-000000000001"), True],
)
async def test_session_limit_rejects_raw_text_non_v7_and_bool_before_redis(
    session_value: object,
) -> None:
    redis = RecordingRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)

    with pytest.raises(ValueError, match="dimension"):
        await limiter.consume(RateLimitRule.REFRESH, {"session": session_value})

    assert redis.eval_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (RedisTimeoutError("synthetic"), RedisFailureKind.TIMEOUT),
        (RedisConnectionError("synthetic"), RedisFailureKind.CONNECTION),
    ],
)
async def test_redis_adapter_classifies_sdk_connection_failures(
    error: BaseException,
    kind: RedisFailureKind,
) -> None:
    adapter = RedisAsyncioAdapter(FailingSdkClient(error))  # type: ignore[arg-type]

    with pytest.raises(RedisDependencyUnavailable) as captured:
        await adapter.get("opaque-key")

    assert captured.value.kind is kind
    assert "opaque-key" not in repr(captured.value)


@pytest.mark.asyncio
async def test_redis_adapter_maps_invalid_sdk_response_to_project_error() -> None:
    class TextSdkClient(FailingSdkClient):
        async def get(self, key: str) -> str:
            del key
            return "decode-responses-must-be-disabled"

    adapter = RedisAsyncioAdapter(TextSdkClient(RuntimeError("unused")))  # type: ignore[arg-type]

    with pytest.raises(redis_client.RedisDependencyInvalidResponse):
        await adapter.get("opaque-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["eval", "get", "set", "delete"])
@pytest.mark.parametrize(
    ("sdk_error", "expected_error", "expected_kind"),
    [
        (
            RedisResponseError("WRONGTYPE leaked-command secret-grant"),
            redis_client.RedisDependencyInvalidResponse,
            None,
        ),
        (
            RedisReadOnlyError("READONLY leaked-command secret-grant"),
            RedisDependencyUnavailable,
            RedisFailureKind.SERVER,
        ),
    ],
)
async def test_redis_adapter_sanitizes_all_sdk_error_replies(
    method: str,
    sdk_error: BaseException,
    expected_error: type[redis_client.RedisDependencyError],
    expected_kind: RedisFailureKind | None,
) -> None:
    adapter = RedisAsyncioAdapter(FailingSdkClient(sdk_error))  # type: ignore[arg-type]

    async def invoke() -> object:
        if method == "eval":
            return await adapter.eval("return redis.error_reply('secret')", 1, "secret-key")
        if method == "get":
            return await adapter.get("secret-key")
        if method == "set":
            return await adapter.set("secret-key", b"secret-value")
        return await adapter.delete("secret-key")

    with pytest.raises(expected_error) as captured:
        await invoke()

    assert captured.value.code == "security_dependency_unavailable"
    assert "secret" not in str(captured.value).lower()
    assert "secret" not in repr(captured.value).lower()
    assert captured.value.__cause__ is None
    if expected_kind is not None:
        assert isinstance(captured.value, RedisDependencyUnavailable)
        assert captured.value.kind is expected_kind


@pytest.mark.asyncio
async def test_step_up_stores_only_hmac_key_and_bound_typed_value() -> None:
    redis = RecordingRedis()
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)

    grant = await store.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action="tenant_application.review",
    )

    assert len(grant.value) >= 43
    assert grant.value not in repr(grant)
    key, value, ttl, only_if_missing = redis.set_calls[0]
    assert grant.value not in key
    assert grant.value.encode() not in value
    assert ttl == 300
    assert only_if_missing is True
    assert json.loads(value) == {
        "action": "tenant_application.review",
        "session_id": str(SESSION_ID),
        "tenant_id": str(TENANT_ID),
        "user_id": str(USER_ID),
        "v": 1,
    }


@pytest.mark.asyncio
async def test_step_up_compare_and_delete_receives_no_raw_grant() -> None:
    redis = RecordingRedis()
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)
    grant = await store.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action="tenant_application.review",
    )

    consumed = await store.consume(
        grant,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action="tenant_application.review",
    )

    assert consumed is True
    script, numkeys, payload = redis.eval_calls[-1]
    assert "compare-and-delete" in script
    assert numkeys == 1
    assert grant.value not in repr(payload)


@pytest.mark.asyncio
async def test_step_up_generation_exhaustion_is_a_sanitized_redis_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CollisionRedis(RecordingRedis):
        attempts = 0

        async def set(
            self,
            key: str,
            value: bytes,
            *,
            ex: int | None = None,
            nx: bool = False,
        ) -> bool:
            del key, value, ex, nx
            self.attempts += 1
            return False

    redis = CollisionRedis()
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)
    leaked_grant = "A" * 43
    monkeypatch.setattr(step_up_module.secrets, "token_urlsafe", lambda size: leaked_grant)

    with pytest.raises(step_up_module.StepUpGrantGenerationExhausted) as captured:
        await store.issue(
            user_id=USER_ID,
            session_id=SESSION_ID,
            tenant_id=TENANT_ID,
            action="tenant_application.review",
        )

    assert redis.attempts == 3
    assert isinstance(captured.value, redis_client.RedisDependencyError)
    assert captured.value.code == "security_dependency_unavailable"
    assert leaked_grant not in repr(captured.value)


@pytest.mark.asyncio
async def test_rate_limit_and_step_up_fail_closed_on_invalid_redis_response() -> None:
    class InvalidEvalRedis(RecordingRedis):
        async def eval(
            self,
            script: str,
            numkeys: int,
            *keys_and_args: object,
        ) -> object:
            del script, numkeys, keys_and_args
            return b"invalid"

    redis = InvalidEvalRedis()
    limiter = RateLimiter(redis=redis, hmac_key=b"r" * 32)
    store = StepUpStore(redis=redis, hmac_key=b"s" * 32)
    grant = await store.issue(
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        action="tenant_application.review",
    )

    with pytest.raises(redis_client.RedisDependencyInvalidResponse):
        await limiter.consume(
            RateLimitRule.REGISTER, {"ip": ip_address("203.0.113.1")}
        )
    with pytest.raises(redis_client.RedisDependencyInvalidResponse):
        await store.consume(
            grant,
            user_id=USER_ID,
            session_id=SESSION_ID,
            tenant_id=TENANT_ID,
            action="tenant_application.review",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forged",
    [
        [1.9, 4, 0],
        [1, 4.9, 0],
        [1, 4, 0.0],
        [b"1", 4, 0],
        ["1", 4, 0],
        [True, 4, 0],
        [1, 4, 1],
        [0, 0, 0],
        [0, 1, 30],
        [1, 5, 0],
        [0, 0, 721],
    ],
)
async def test_rate_limit_rejects_forged_decision_shapes_and_invariants(
    forged: object,
) -> None:
    class ForgedDecisionRedis(RecordingRedis):
        async def eval(
            self,
            script: str,
            numkeys: int,
            *keys_and_args: object,
        ) -> object:
            del script, numkeys, keys_and_args
            return forged

    limiter = RateLimiter(redis=ForgedDecisionRedis(), hmac_key=b"r" * 32)

    with pytest.raises(redis_client.RedisDependencyInvalidResponse):
        await limiter.consume(
            RateLimitRule.REGISTER, {"ip": ip_address("203.0.113.1")}
        )


@pytest.mark.asyncio
async def test_authorization_cache_uses_exact_versioned_key_and_typed_value() -> None:
    redis = RecordingRedis()
    cache = AuthorizationCache(redis=redis, ttl_seconds=60)
    entry = AuthorizationCacheEntry(
        permissions=frozenset({"tenant.read", "membership.read"}),
        scope=AuthorizationScope(
            department_ids=frozenset({DEPARTMENT_ID}),
            allow_owned=True,
            allow_shared=False,
        ),
    )

    await cache.set(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=7,
        entry=entry,
    )

    key, encoded, ttl, only_if_missing = redis.set_calls[0]
    assert key == f"authz:{TENANT_ID}:{MEMBERSHIP_ID}:7"
    assert ttl == 60
    assert only_if_missing is False
    payload = json.loads(encoded)
    assert payload == {
        "authz_version": 7,
        "permissions": ["membership.read", "tenant.read"],
        "scope": {
            "allow_owned": True,
            "allow_shared": False,
            "department_ids": [str(DEPARTMENT_ID)],
        },
        "v": 1,
    }
    assert set(payload) == {"v", "authz_version", "permissions", "scope"}

    redis.get_values[key] = encoded
    assert (
        await cache.get(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
        )
        == entry
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"v":1,"authz_version":8,"permissions":[],"scope":{}}',
        b'{"v":1,"authz_version":7,"permissions":["tenant.read","tenant.read"],"scope":{}}',
        b'{"v":1,"authz_version":7,"permissions":[],"scope":{"roles":["owner"]}}',
    ],
)
async def test_authorization_cache_treats_malformed_or_mismatched_value_as_miss(
    payload: bytes,
) -> None:
    redis = RecordingRedis()
    cache = AuthorizationCache(redis=redis)
    key = f"authz:{TENANT_ID}:{MEMBERSHIP_ID}:7"
    redis.get_values[key] = payload

    assert (
        await cache.get(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
        )
        is None
    )


@pytest.mark.asyncio
async def test_authorization_cache_rejects_boolean_schema_and_version_values() -> None:
    redis = RecordingRedis()
    cache = AuthorizationCache(redis=redis)
    key = f"authz:{TENANT_ID}:{MEMBERSHIP_ID}:1"
    redis.get_values[key] = (
        b'{"v":true,"authz_version":true,"permissions":[],"scope":'
        b'{"allow_owned":false,"allow_shared":false,"department_ids":[]}}'
    )

    assert (
        await cache.get(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=1,
        )
        is None
    )


@pytest.mark.asyncio
async def test_authorization_cache_read_failure_is_miss_but_writes_fail_closed() -> None:
    class FailingRedis(RecordingRedis):
        async def get(self, key: str) -> bytes | None:
            del key
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION)

        async def set(
            self,
            key: str,
            value: bytes,
            *,
            ex: int | None = None,
            nx: bool = False,
        ) -> bool:
            del key, value, ex, nx
            raise RedisDependencyUnavailable(RedisFailureKind.TIMEOUT)

        async def delete(self, *keys: str) -> int:
            del keys
            raise RedisDependencyUnavailable(RedisFailureKind.CONNECTION)

    cache = AuthorizationCache(redis=FailingRedis())
    entry = AuthorizationCacheEntry(frozenset({"tenant.read"}), AuthorizationScope())
    assert (
        await cache.get(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
        )
        is None
    )
    with pytest.raises(RedisDependencyUnavailable):
        await cache.set(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
            entry=entry,
        )
    with pytest.raises(RedisDependencyUnavailable):
        await cache.invalidate(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
        )


@pytest.mark.asyncio
async def test_authorization_cache_rejects_unsuccessful_set_response() -> None:
    class RejectingSetRedis(RecordingRedis):
        async def set(
            self,
            key: str,
            value: bytes,
            *,
            ex: int | None = None,
            nx: bool = False,
        ) -> bool:
            del key, value, ex, nx
            return False

    cache = AuthorizationCache(redis=RejectingSetRedis())
    entry = AuthorizationCacheEntry(frozenset({"tenant.read"}), AuthorizationScope())

    with pytest.raises(redis_client.RedisDependencyInvalidResponse):
        await cache.set(
            tenant_id=TENANT_ID,
            membership_id=MEMBERSHIP_ID,
            authz_version=7,
            entry=entry,
        )


@pytest.mark.asyncio
async def test_authorization_cache_invalidation_deletes_only_exact_old_version() -> None:
    redis = RecordingRedis()
    cache = AuthorizationCache(redis=redis)

    await cache.invalidate(
        tenant_id=TENANT_ID,
        membership_id=MEMBERSHIP_ID,
        authz_version=7,
    )

    assert redis.deleted == [f"authz:{TENANT_ID}:{MEMBERSHIP_ID}:7"]
