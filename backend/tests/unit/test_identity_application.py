from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

import lawyer_agent.application.identity as identity_application
from lawyer_agent.application.identity import (
    AuditContext,
    AuditEvent,
    AuthenticationRecord,
    IdentityService,
    LoginIdentifier,
    RegisterCommand,
)
from lawyer_agent.domain.identity import IdentityKind, PasswordVerification, VersionedBlindIndex

USER_ID = UUID("01990f00-0000-7000-8000-000000000001")
IDENTITY_ID = UUID("01990f00-0000-7000-8000-000000000002")


@dataclass
class FakePasswordHasher:
    valid: bool = True
    needs_rehash: bool = False
    dummy_inputs: list[str] | None = None

    @property
    def parameters(self) -> dict[str, int | str]:
        return {"algorithm": "fake"}

    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, encoded_hash: str, password: str) -> PasswordVerification:
        del encoded_hash, password
        return PasswordVerification(self.valid, self.needs_rehash if self.valid else False)

    def verify_dummy(self, password: str) -> None:
        if self.dummy_inputs is not None:
            self.dummy_inputs.append(password)

    def is_verification_input_within_limits(self, password: str) -> bool:
        return len(password) <= 128 and len(password.encode("utf-8")) <= 1024


class FakeCipher:
    active_key_version = 9

    def encrypt(self, value: str, *, aad: bytes) -> bytes:
        del aad
        return value.encode("utf-8")

    def decrypt(self, envelope: bytes, *, aad: bytes) -> str:
        del aad
        return envelope.decode("utf-8")


class FakeBlindIndex:
    active_key_version = 2
    key_versions = (1, 2)

    def digest(self, purpose: str, value: str, *, key_version: int | None = None) -> bytes:
        version = self.active_key_version if key_version is None else key_version
        return f"{version}:{purpose}:{value}".encode().ljust(32, b".")[:32]

    def digests(self, purpose: str, value: str) -> tuple[VersionedBlindIndex, ...]:
        return tuple(
            VersionedBlindIndex(version, self.digest(purpose, value, key_version=version))
            for version in self.key_versions
        )


class FakeIdentityRepository:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.records: tuple[AuthenticationRecord, ...] = ()
        self.unsupported_versions = False
        self.fail_flush: Exception | None = None

    async def has_unsupported_blind_index_versions(self, supported: tuple[int, ...]) -> bool:
        self.calls.append(f"unsupported:{supported}")
        return self.unsupported_versions

    async def find_by_blind_indexes(
        self,
        *,
        kind: str,
        issuer: str,
        indexes: tuple[VersionedBlindIndex, ...],
        for_update: bool = False,
    ) -> tuple[AuthenticationRecord, ...]:
        del kind, issuer, indexes
        self.calls.append(f"find:{for_update}")
        return self.records

    async def add_user(self, user: object) -> None:
        del user
        self.calls.append("add_user")

    async def add_identity(self, identity: object) -> None:
        del identity
        self.calls.append("add_identity")

    async def add_credential(self, credential: object) -> None:
        del credential
        self.calls.append("add_credential")

    async def flush(self) -> None:
        self.calls.append("flush")
        if self.fail_flush is not None:
            raise self.fail_flush

    async def update_password_hash(
        self,
        *,
        user_id: UUID,
        password_hash: str,
        parameters: dict[str, int | str],
        now: object,
    ) -> None:
        del user_id, password_hash, parameters, now
        self.calls.append("rehash")

    async def update_blind_index(
        self,
        *,
        identity_id: UUID,
        blind_index: VersionedBlindIndex,
        now: object,
    ) -> None:
        del identity_id, blind_index, now
        self.calls.append("reindex")


class FakeAuditRepository:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.events: list[AuditEvent] = []
        self.failure: Exception | None = None

    async def append(self, event: AuditEvent) -> None:
        self.calls.append(f"audit:{event.action}")
        if self.failure is not None:
            raise self.failure
        self.events.append(event)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock_digests: list[bytes] = []
        self.identities = FakeIdentityRepository(self.calls)
        self.audit = FakeAuditRepository(self.calls)

    async def __aenter__(self) -> Self:
        self.calls.append("enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self.calls.append("rollback" if exc_type is not None else "commit")

    async def lock_identity(self, lock_digest: bytes) -> None:
        self.lock_digests.append(lock_digest)
        self.calls.append("lock")


def _audit_context() -> AuditContext:
    return AuditContext(
        trace_id="trace-123",
        client_ip_hash=b"i" * 32,
        user_agent_hash=b"u" * 32,
    )


def _service(
    uow: FakeUnitOfWork,
    *,
    hasher: FakePasswordHasher | None = None,
) -> IdentityService:
    def factory() -> FakeUnitOfWork:
        return uow

    return IdentityService(
        uow_factory=factory,
        password_hasher=hasher or FakePasswordHasher(),
        cipher=FakeCipher(),
        blind_index=FakeBlindIndex(),
    )


def test_application_identity_has_no_sqlalchemy_or_infrastructure_dependency() -> None:
    source = inspect.getsource(identity_application)

    assert "sqlalchemy" not in source
    assert "cryptography" not in source
    assert "lawyer_agent.infrastructure" not in source


@pytest.mark.asyncio
async def test_register_uses_ports_in_transactional_order_with_real_audit_context() -> None:
    uow = FakeUnitOfWork()

    await _service(uow).register(
        RegisterCommand("Lawyer", "correct horse battery staple", "Lawyer"),
        audit_context=_audit_context(),
    )

    assert uow.calls == [
        "enter",
        "lock",
        "lock",
        "unsupported:(1, 2)",
        "find:True",
        "add_user",
        "flush",
        "add_identity",
        "add_credential",
        "audit:identity.register",
        "flush",
        "commit",
    ]
    assert uow.lock_digests == [
        item.digest
        for item in FakeBlindIndex().digests("identity:username", "lawyer")
    ]
    event = uow.audit.events[0]
    assert event.trace_id == "trace-123"
    assert event.client_ip_hash == b"i" * 32
    assert event.user_agent_hash == b"u" * 32
    assert event.metadata is None


@pytest.mark.asyncio
async def test_non_identity_integrity_failure_propagates_and_rolls_back() -> None:
    uow = FakeUnitOfWork()
    database_failure = RuntimeError("foreign key failure")
    uow.identities.fail_flush = database_failure

    with pytest.raises(RuntimeError, match="foreign key failure"):
        await _service(uow).register(
            RegisterCommand("Lawyer", "correct horse battery staple", "Lawyer"),
            audit_context=_audit_context(),
        )

    assert uow.calls[-1] == "rollback"


def _authentication_record(*, blind_version: int = 2) -> AuthenticationRecord:
    return AuthenticationRecord(
        identity_id=IDENTITY_ID,
        user_id=USER_ID,
        user_status="active",
        identity_status="active",
        subject_ciphertext=b"lawyer",
        cipher_key_version=9,
        blind_index_key_version=blind_version,
        password_hash="old-hash",  # noqa: S106 - synthetic credential fixture
        credential_status="active",
        locked_until=None,
    )


@pytest.mark.asyncio
async def test_successful_rehash_and_audits_share_one_atomic_uow() -> None:
    uow = FakeUnitOfWork()
    uow.identities.records = (_authentication_record(),)
    hasher = FakePasswordHasher(valid=True, needs_rehash=True)

    authenticated = await _service(uow, hasher=hasher).authenticate(
        LoginIdentifier(IdentityKind.USERNAME, "lawyer"),
        "correct horse battery staple",
        audit_context=_audit_context(),
    )

    assert authenticated is not None
    assert "rehash" in uow.calls
    assert "audit:credential.rehash" in uow.calls
    assert "audit:identity.authenticate" in uow.calls
    assert uow.calls[-1] == "commit"


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_rehash_and_wrong_password_never_rehashes() -> None:
    failing_uow = FakeUnitOfWork()
    failing_uow.identities.records = (_authentication_record(),)
    failing_uow.audit.failure = RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _service(
            failing_uow,
            hasher=FakePasswordHasher(valid=True, needs_rehash=True),
        ).authenticate(
            LoginIdentifier(IdentityKind.USERNAME, "lawyer"),
            "correct horse battery staple",
            audit_context=_audit_context(),
        )
    assert "rehash" in failing_uow.calls
    assert failing_uow.calls[-1] == "rollback"

    wrong_uow = FakeUnitOfWork()
    wrong_uow.identities.records = (_authentication_record(),)
    result = await _service(wrong_uow, hasher=FakePasswordHasher(valid=False)).authenticate(
        LoginIdentifier(IdentityKind.USERNAME, "lawyer"),
        "wrong password value",
        audit_context=_audit_context(),
    )
    assert result is None
    assert "rehash" not in wrong_uow.calls
    assert "audit:credential.rehash" not in wrong_uow.calls
    assert "audit:identity.authenticate" in wrong_uow.calls


@pytest.mark.parametrize("bad_hash", [b"short", b"x" * 31, b"x" * 33])
def test_audit_context_accepts_only_fixed_length_client_hashes(bad_hash: bytes) -> None:
    with pytest.raises(ValueError, match="32-byte"):
        AuditContext("trace", bad_hash, None)


@pytest.mark.parametrize(
    "trace_id",
    [
        "   ",
        "trace\x00id",
        "trace\rid",
        "trace\nid",
        "trace\tid",
        "追踪-id",
        "trace/id",
        "x" * 65,
    ],
)
def test_audit_context_rejects_unsafe_trace_ids(trace_id: str) -> None:
    with pytest.raises(ValueError, match="trace_id"):
        AuditContext(trace_id, None, None)


def test_audit_context_accepts_explicit_safe_ascii_trace_charset() -> None:
    context = AuditContext("trace-01_ab.cd:ef", None, None)

    assert context.trace_id == "trace-01_ab.cd:ef"
