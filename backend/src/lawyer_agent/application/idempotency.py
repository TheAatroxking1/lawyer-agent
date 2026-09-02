from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7, require_uuid7

_KEY_PATTERN = re.compile(r"[A-Za-z0-9._~:-]{16,128}\Z", re.ASCII)
_METHOD_PATTERN = re.compile(r"[A-Z]{1,16}\Z", re.ASCII)
_ROUTE_PATTERN = re.compile(r"/[A-Za-z0-9._~:/{}-]{1,511}\Z", re.ASCII)
_DEFAULT_TTL = timedelta(hours=24)
JsonPath = tuple[str, ...]
_OMIT = object()


class InvalidIdempotencyKey(ValueError):
    code = "invalid_idempotency_key"


class InvalidIdempotencyRequest(ValueError):
    code = "invalid_idempotency_request"


class IdempotencyConflictError(Exception):
    code = "idempotency_conflict"

    def __init__(self) -> None:
        super().__init__("idempotency key conflicts with another request")


class IdempotencyInProgressError(Exception):
    code = "idempotency_in_progress"

    def __init__(self) -> None:
        super().__init__("idempotent request is still in progress")


class IdempotencyStateError(RuntimeError):
    code = "idempotency_state_invalid"


class IdempotencyScopeType(StrEnum):
    USER = "user"
    MEMBERSHIP = "membership"
    PLATFORM = "platform"


class IdempotencyStatus(StrEnum):
    RESERVED = "reserved"
    COMPLETED = "completed"
    FAILED = "failed"


class IdempotencyMutationEffect(StrEnum):
    CHANGED = "changed"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    scope_type: IdempotencyScopeType
    scope_id: UUID
    tenant_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope_type, IdempotencyScopeType):
            raise ValueError("idempotency scope type must be strongly typed")
        require_uuid7(self.scope_id, field="idempotency scope_id")
        if self.scope_type is IdempotencyScopeType.MEMBERSHIP:
            require_uuid7(self.tenant_id, field="idempotency tenant_id")
        elif self.tenant_id is not None:
            raise ValueError("only membership idempotency scopes carry tenant_id")


@dataclass(frozen=True, slots=True)
class IdempotencyFingerprintPayload:
    values: Mapping[str, object] = field(repr=False)
    business_paths: frozenset[JsonPath]
    secret_paths: frozenset[JsonPath] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise InvalidIdempotencyRequest("fingerprint payload values must be a mapping")
        _require_paths(self.business_paths, field_name="business_paths")
        _require_paths(self.secret_paths, field_name="secret_paths")
        if self.business_paths & self.secret_paths:
            raise InvalidIdempotencyRequest("fingerprint paths must not overlap")
        for left in self.business_paths | self.secret_paths:
            for right in self.business_paths | self.secret_paths:
                if left != right and len(left) < len(right) and right[: len(left)] == left:
                    raise InvalidIdempotencyRequest("fingerprint paths must not overlap")

    def canonical_business_values(self) -> Mapping[str, object]:
        projected = _project_explicit_paths(
            self.values,
            path=(),
            business_paths=self.business_paths,
            secret_paths=self.secret_paths,
        )
        if not isinstance(projected, Mapping):
            raise InvalidIdempotencyRequest("fingerprint payload must remain an object")
        return projected


@dataclass(frozen=True, slots=True)
class IdempotencyRequest:
    key: str = field(repr=False)
    method: str
    canonical_route: str
    body: IdempotencyFingerprintPayload = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedIdempotencyRequest:
    key_hash: bytes
    request_fingerprint: bytes
    canonical_method: str
    canonical_route: str


@dataclass(frozen=True, slots=True)
class IdempotencyResultReference:
    result_type: str
    result_id: UUID
    mutation_effect: IdempotencyMutationEffect | None = None
    cache_authz_version: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.result_type, str)
            or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", self.result_type, re.ASCII)
            is None
        ):
            raise ValueError("idempotency result type is invalid")
        require_uuid7(self.result_id, field="idempotency result_id")
        if self.mutation_effect is not None and not isinstance(
            self.mutation_effect, IdempotencyMutationEffect
        ):
            raise ValueError("idempotency mutation effect must be strongly typed")
        if self.cache_authz_version is not None and (
            isinstance(self.cache_authz_version, bool)
            or not isinstance(self.cache_authz_version, int)
            or not 1 <= self.cache_authz_version <= (1 << 31) - 1
        ):
            raise ValueError("idempotency cache authz version is invalid")


@dataclass(frozen=True, slots=True)
class NewIdempotencyRecord:
    id: UUID
    scope: IdempotencyScope
    operation: str
    key_hash: bytes
    request_fingerprint: bytes
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredIdempotencyRecord:
    id: UUID
    scope: IdempotencyScope
    operation: str
    key_hash: bytes
    request_fingerprint: bytes
    status: IdempotencyStatus
    result: IdempotencyResultReference | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AcquiredIdempotencyRecord:
    record: StoredIdempotencyRecord
    inserted: bool


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    record_id: UUID
    request_fingerprint: bytes
    replay: IdempotencyResultReference | None

    @property
    def is_replay(self) -> bool:
        return self.replay is not None


class IdempotencyRepositoryPort(Protocol):
    async def acquire(
        self, candidate: NewIdempotencyRecord
    ) -> AcquiredIdempotencyRecord: ...

    async def restart(
        self,
        *,
        record_id: UUID,
        old_request_fingerprint: bytes,
        new_request_fingerprint: bytes,
        allowed_statuses: frozenset[IdempotencyStatus],
        expired_before: datetime | None,
        expires_at: datetime,
    ) -> bool: ...

    async def complete(
        self,
        *,
        record_id: UUID,
        request_fingerprint: bytes,
        result: IdempotencyResultReference,
        now: datetime,
    ) -> bool: ...

    async def fail(
        self,
        *,
        record_id: UUID,
        request_fingerprint: bytes,
        now: datetime,
    ) -> bool: ...


class IdempotencyService:
    def __init__(
        self,
        *,
        key_hash_secret: bytes,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> None:
        if not isinstance(key_hash_secret, bytes) or len(key_hash_secret) < 32:
            raise ValueError("idempotency key hash secret must contain at least 32 bytes")
        if not isinstance(ttl, timedelta) or not timedelta(minutes=1) <= ttl <= timedelta(days=7):
            raise ValueError("idempotency TTL must be between one minute and seven days")
        self._key_hash_secret = key_hash_secret
        self._ttl = ttl

    def prepare(self, request: IdempotencyRequest) -> PreparedIdempotencyRequest:
        if not isinstance(request, IdempotencyRequest):
            raise InvalidIdempotencyRequest("idempotency request must be strongly typed")
        if not isinstance(request.key, str) or _KEY_PATTERN.fullmatch(request.key) is None:
            raise InvalidIdempotencyKey("idempotency key must be 16-128 safe ASCII characters")
        method = _canonical_method(request.method)
        route = _canonical_route(request.canonical_route)
        if not isinstance(request.body, IdempotencyFingerprintPayload):
            raise InvalidIdempotencyRequest(
                "idempotency body must declare explicit business and secret paths"
            )
        canonical_body = _canonical_json(request.body.canonical_business_values())
        fingerprint_input = b"\n".join(
            (method.encode("ascii"), route.encode("ascii"), canonical_body)
        )
        return PreparedIdempotencyRequest(
            key_hash=hmac.digest(
                self._key_hash_secret,
                b"idempotency-key:v1:" + request.key.encode("ascii"),
                sha256,
            ),
            request_fingerprint=sha256(
                b"idempotency-request:v1:" + fingerprint_input
            ).digest(),
            canonical_method=method,
            canonical_route=route,
        )

    async def reserve(
        self,
        repository: IdempotencyRepositoryPort,
        *,
        scope: IdempotencyScope,
        operation: str,
        request: IdempotencyRequest,
        now: datetime,
    ) -> IdempotencyReservation:
        prepared = self.prepare(request)
        effective_now = _require_utc(now)
        _require_operation(operation)
        candidate = NewIdempotencyRecord(
            id=new_uuid7(),
            scope=scope,
            operation=operation,
            key_hash=prepared.key_hash,
            request_fingerprint=prepared.request_fingerprint,
            expires_at=effective_now + self._ttl,
        )
        acquired = await repository.acquire(candidate)
        record = acquired.record
        if acquired.inserted:
            return IdempotencyReservation(record.id, prepared.request_fingerprint, None)
        if not hmac.compare_digest(
            record.request_fingerprint,
            prepared.request_fingerprint,
        ):
            raise IdempotencyConflictError
        if record.status is IdempotencyStatus.COMPLETED:
            if record.result is None:
                raise IdempotencyStateError("completed idempotency record has no result")
            return IdempotencyReservation(
                record.id,
                prepared.request_fingerprint,
                record.result,
            )
        if record.status is IdempotencyStatus.FAILED:
            restarted = await repository.restart(
                record_id=record.id,
                old_request_fingerprint=record.request_fingerprint,
                new_request_fingerprint=prepared.request_fingerprint,
                allowed_statuses=frozenset({IdempotencyStatus.FAILED}),
                expired_before=None,
                expires_at=effective_now + self._ttl,
            )
            if not restarted:
                raise IdempotencyStateError("failed idempotency record could not be recovered")
            return IdempotencyReservation(record.id, prepared.request_fingerprint, None)
        if record.status is IdempotencyStatus.RESERVED and record.expires_at <= effective_now:
            restarted = await repository.restart(
                record_id=record.id,
                old_request_fingerprint=record.request_fingerprint,
                new_request_fingerprint=prepared.request_fingerprint,
                allowed_statuses=frozenset({IdempotencyStatus.RESERVED}),
                expired_before=effective_now,
                expires_at=effective_now + self._ttl,
            )
            if not restarted:
                raise IdempotencyStateError("expired idempotency record could not be recovered")
            return IdempotencyReservation(record.id, prepared.request_fingerprint, None)
        raise IdempotencyInProgressError

    async def complete(
        self,
        repository: IdempotencyRepositoryPort,
        reservation: IdempotencyReservation,
        result: IdempotencyResultReference,
        *,
        now: datetime,
    ) -> None:
        if reservation.is_replay:
            raise IdempotencyStateError("a replay reservation cannot be completed again")
        if not await repository.complete(
            record_id=reservation.record_id,
            request_fingerprint=reservation.request_fingerprint,
            result=result,
            now=_require_utc(now),
        ):
            raise IdempotencyStateError("idempotency reservation is no longer completable")

    async def fail(
        self,
        repository: IdempotencyRepositoryPort,
        reservation: IdempotencyReservation,
        *,
        now: datetime,
    ) -> None:
        if reservation.is_replay:
            raise IdempotencyStateError("a replay reservation cannot fail")
        if not await repository.fail(
            record_id=reservation.record_id,
            request_fingerprint=reservation.request_fingerprint,
            now=_require_utc(now),
        ):
            raise IdempotencyStateError("idempotency reservation is no longer fail-able")


def _canonical_method(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidIdempotencyRequest("HTTP method is invalid")
    method = value.upper()
    if _METHOD_PATTERN.fullmatch(method) is None:
        raise InvalidIdempotencyRequest("HTTP method is invalid")
    return method


def _canonical_route(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidIdempotencyRequest("canonical route is invalid")
    split = urlsplit(value)
    if split.scheme or split.netloc or split.query or split.fragment:
        raise InvalidIdempotencyRequest("canonical route must be a path without query data")
    route = split.path.rstrip("/") or "/"
    if _ROUTE_PATTERN.fullmatch(route) is None:
        raise InvalidIdempotencyRequest("canonical route is invalid")
    return route


def _project_explicit_paths(
    value: object,
    *,
    path: JsonPath,
    business_paths: frozenset[JsonPath],
    secret_paths: frozenset[JsonPath],
) -> object:
    if path in secret_paths:
        _require_explicit_leaf(value, classification="secret")
        return _OMIT
    if path in business_paths:
        return _canonical_business_leaf(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise InvalidIdempotencyRequest("JSON object keys must be strings")
            projected = _project_explicit_paths(
                child,
                path=(*path, raw_key),
                business_paths=business_paths,
                secret_paths=secret_paths,
            )
            if projected is not _OMIT:
                result[raw_key] = projected
        return result
    raise InvalidIdempotencyRequest(
        "request body contains an unclassified business or secret field"
    )


def _canonical_business_leaf(value: object) -> object:
    if isinstance(value, Mapping):
        raise InvalidIdempotencyRequest(
            "fingerprint paths must terminate at an exact scalar leaf"
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if any(
            isinstance(child, Mapping)
            or (
                isinstance(child, Sequence)
                and not isinstance(child, (str, bytes, bytearray))
            )
            for child in value
        ):
            raise InvalidIdempotencyRequest(
                "fingerprint sequence values require an explicit safe scalar schema"
            )
        return [_canonical_business_leaf(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise InvalidIdempotencyRequest("request body must contain canonical JSON values")


def _require_explicit_leaf(value: object, *, classification: str) -> None:
    try:
        _canonical_business_leaf(value)
    except InvalidIdempotencyRequest as exc:
        raise InvalidIdempotencyRequest(
            f"{classification} paths must classify exact scalar leaves"
        ) from exc


def _require_paths(value: object, *, field_name: str) -> None:
    if not isinstance(value, frozenset):
        raise InvalidIdempotencyRequest(f"{field_name} must be a frozenset")
    for path in value:
        if (
            not isinstance(path, tuple)
            or not path
            or any(not isinstance(part, str) or not part for part in path)
        ):
            raise InvalidIdempotencyRequest(f"{field_name} contains an invalid JSON path")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidIdempotencyRequest("request body is not canonical JSON") from exc


def _require_operation(value: object) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", value, re.ASCII) is None
    ):
        raise ValueError("idempotency operation is invalid")


def _require_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("idempotency time must be UTC-aware")
    return value.astimezone(UTC)
