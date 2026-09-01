from __future__ import annotations

from base64 import b64encode

import pytest
from argon2 import PasswordHasher

from lawyer_agent.domain.identity import (
    CiphertextAuthenticationError,
    IdentityKind,
    normalize_email,
    normalize_identifier,
    normalize_phone,
    normalize_username,
)
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.passwords import (
    Argon2PasswordHasher,
    InvalidPassword,
)


def test_username_nfkc_trim_casefold() -> None:
    assert normalize_username("  ＬＡＷＹＥＲ ") == "lawyer"


def test_mainland_phone_is_canonical_e164_and_foreign_numbers_are_rejected() -> None:
    assert normalize_phone("138 0013 8000") == "+8613800138000"

    with pytest.raises(ValueError, match="mainland China"):
        normalize_phone("+1 202 555 0123")
    with pytest.raises(ValueError, match="valid"):
        normalize_phone("12345")


@pytest.mark.parametrize(
    "value",
    [
        "01088886666",
        "4008001234",
        "8008101234",
        "110",
        "+85221234567",
        "+85328281234",
        "+886223456789",
        "+12025550123",
    ],
)
def test_phone_identity_accepts_only_explicit_mainland_mobile_numbers(value: str) -> None:
    with pytest.raises(ValueError, match="mobile"):
        normalize_phone(value)


def test_email_casefolds_local_part_and_uses_lower_idna_domain() -> None:
    assert normalize_email("  LAWYER@例子.公司  ") == "lawyer@xn--fsqu00a.xn--55qx5d"


def test_wechat_preserves_raw_subject_and_is_issuer_scoped() -> None:
    normalized = normalize_identifier(
        IdentityKind.WECHAT_OPENID,
        " Subject-With-Case ",
        issuer="wx-app-1",
    )

    assert normalized.subject == " Subject-With-Case "
    assert normalized.issuer == "wx-app-1"
    with pytest.raises(ValueError, match="issuer"):
        normalize_identifier(IdentityKind.WECHAT_OPENID, "openid")


@pytest.mark.parametrize(
    ("issuer", "subject"),
    [
        ("wx-app", "   "),
        ("wx-app", "open\x00id"),
        ("wx-app", "x" * 256),
        ("x" * 256, "openid"),
        ("wx\x00app", "openid"),
    ],
)
def test_wechat_issuer_and_subject_have_bounded_safe_storage(
    issuer: str,
    subject: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_identifier(IdentityKind.WECHAT_OPENID, subject, issuer=issuer)


@pytest.mark.parametrize(
    "password",
    [
        "short",
        "x" * 129,
        "法" * 342,
        "\ud800" * 12,
    ],
)
def test_password_policy_rejects_invalid_character_or_utf8_lengths(password: str) -> None:
    hasher = Argon2PasswordHasher()

    with pytest.raises(InvalidPassword):
        hasher.hash(password)


def test_password_hash_uses_fixed_argon2id_parameters_without_unicode_normalization() -> None:
    hasher = Argon2PasswordHasher()
    composed = "Pássword-long-value"
    decomposed = "Pa\u0301ssword-long-value"

    encoded = hasher.hash(composed)

    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert hasher.verify(encoded, composed).valid is True
    assert hasher.verify(encoded, decomposed).valid is False


def test_rehash_is_reported_only_after_successful_verification() -> None:
    old_hasher = PasswordHasher(memory_cost=8192, time_cost=1, parallelism=1)
    encoded = old_hasher.hash("correct horse battery staple")
    hasher = Argon2PasswordHasher()

    failed = hasher.verify(encoded, "wrong password value")
    succeeded = hasher.verify(encoded, "correct horse battery staple")

    assert failed.valid is False
    assert failed.needs_rehash is False
    assert succeeded.valid is True
    assert succeeded.needs_rehash is True


@pytest.mark.parametrize("password", ["x" * 129, "法" * 400, "\ud800"])
def test_dummy_verification_never_passes_oversized_login_input_to_argon2(
    monkeypatch: pytest.MonkeyPatch,
    password: str,
) -> None:
    hasher = Argon2PasswordHasher()
    seen: list[str] = []

    class SpyArgon2:
        def verify(self, encoded_hash: str, candidate: str) -> bool:
            del encoded_hash
            seen.append(candidate)
            return False

    monkeypatch.setattr(hasher, "_hasher", SpyArgon2())

    hasher.verify_dummy(password)

    assert seen == ["lawyer-agent-bounded-dummy"]


def test_cipher_envelope_is_versioned_random_and_aad_bound() -> None:
    cipher = SensitiveValueCipher({7: b"d" * 32}, active_key_version=7)
    aad = b"auth_identity:id-a:subject"

    first = cipher.encrypt("13800138000", aad=aad)
    second = cipher.encrypt("13800138000", aad=aad)

    assert first[:2] == (7).to_bytes(2, "big")
    assert len(first) >= 2 + 12 + 16
    assert first != second
    assert cipher.decrypt(first, aad=aad) == "13800138000"
    with pytest.raises(CiphertextAuthenticationError):
        cipher.decrypt(first, aad=b"auth_identity:id-b:subject")


def test_cipher_rejects_invalid_key_material_and_unknown_versions() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        SensitiveValueCipher({1: b"short"}, active_key_version=1)

    cipher = SensitiveValueCipher({1: b"a" * 32}, active_key_version=1)
    envelope = (2).to_bytes(2, "big") + b"x" * 28
    with pytest.raises(ValueError, match="unknown key version"):
        cipher.decrypt(envelope, aad=b"row")


def test_cipher_versions_match_mysql_signed_smallint_boundaries() -> None:
    cipher = SensitiveValueCipher(
        {1: b"a" * 32, 32767: b"b" * 32},
        active_key_version=32767,
    )

    assert cipher.active_key_version == 32767


@pytest.mark.parametrize(
    ("keys", "active_version"),
    [
        ({0: b"a" * 32}, 0),
        ({-1: b"a" * 32, 1: b"b" * 32}, 1),
        ({1: b"a" * 32, 32768: b"b" * 32}, 1),
        ({True: b"a" * 32}, True),
    ],
)
def test_cipher_rejects_invalid_active_or_inactive_key_versions(
    keys: dict[int, bytes],
    active_version: int,
) -> None:
    with pytest.raises(ValueError, match="1 to 32767"):
        SensitiveValueCipher(keys, active_key_version=active_version)


def test_blind_indexes_are_purpose_and_key_version_isolated() -> None:
    blind = BlindIndexService({1: b"b" * 32, 2: b"c" * 32}, active_key_version=2)

    identity = blind.digest("identity:phone", "+8613800138000")
    invitation = blind.digest("invite:phone", "+8613800138000")
    old_version = blind.digest("identity:phone", "+8613800138000", key_version=1)

    assert identity != invitation
    assert identity != old_version
    assert blind.matches(
        identity,
        "identity:phone",
        "+8613800138000",
        key_version=2,
    )
    assert not blind.matches(
        identity,
        "invite:phone",
        "+8613800138000",
        key_version=2,
    )


def test_blind_index_versions_match_mysql_signed_smallint_boundaries() -> None:
    service = BlindIndexService(
        {1: b"a" * 32, 32767: b"b" * 32},
        active_key_version=32767,
    )

    assert service.key_versions == (1, 32767)


@pytest.mark.parametrize(
    ("keys", "active_version"),
    [
        ({0: b"a" * 32}, 0),
        ({-1: b"a" * 32, 1: b"b" * 32}, 1),
        ({1: b"a" * 32, 32768: b"b" * 32}, 1),
        ({True: b"a" * 32}, True),
    ],
)
def test_blind_index_rejects_invalid_active_or_inactive_key_versions(
    keys: dict[int, bytes],
    active_version: int,
) -> None:
    with pytest.raises(ValueError, match="1 to 32767"):
        BlindIndexService(keys, active_key_version=active_version)


def test_security_keys_are_binary_not_base64_text() -> None:
    encoded = b64encode(b"k" * 32)

    with pytest.raises(ValueError, match="32 bytes"):
        BlindIndexService({1: encoded}, active_key_version=1)
