from __future__ import annotations

import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lawyer_agent.domain.identity import CiphertextAuthenticationError

_VERSION_BYTES = 2
_MYSQL_SIGNED_SMALLINT_MAX = 32767
_NONCE_BYTES = 12
_TAG_BYTES = 16


class SensitiveValueCipher:
    def __init__(self, keys: Mapping[int, bytes], *, active_key_version: int) -> None:
        if any(
            isinstance(version, bool)
            or not isinstance(version, int)
            or not 1 <= version <= _MYSQL_SIGNED_SMALLINT_MAX
            for version in keys
        ):
            raise ValueError("cipher key versions must be integers from 1 to 32767")
        if (
            isinstance(active_key_version, bool)
            or not isinstance(active_key_version, int)
            or not 1 <= active_key_version <= _MYSQL_SIGNED_SMALLINT_MAX
        ):
            raise ValueError("cipher key versions must be integers from 1 to 32767")
        if active_key_version not in keys:
            raise ValueError("active key version is missing")
        if any(len(key) != 32 for key in keys.values()):
            raise ValueError("AES-256-GCM keys must contain exactly 32 bytes")
        self._keys = dict(keys)
        self._active_key_version = active_key_version

    @property
    def active_key_version(self) -> int:
        return self._active_key_version

    def encrypt(self, value: str, *, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext_and_tag = AESGCM(self._keys[self._active_key_version]).encrypt(
            nonce,
            value.encode("utf-8"),
            aad,
        )
        return (
            self._active_key_version.to_bytes(_VERSION_BYTES, "big")
            + nonce
            + ciphertext_and_tag
        )

    def decrypt(self, envelope: bytes, *, aad: bytes) -> str:
        if len(envelope) < _VERSION_BYTES + _NONCE_BYTES + _TAG_BYTES:
            raise ValueError("ciphertext envelope is truncated")
        key_version = int.from_bytes(envelope[:_VERSION_BYTES], "big")
        key = self._keys.get(key_version)
        if key is None:
            raise ValueError("ciphertext uses an unknown key version")
        nonce_start = _VERSION_BYTES
        nonce_end = nonce_start + _NONCE_BYTES
        try:
            plaintext = AESGCM(key).decrypt(
                envelope[nonce_start:nonce_end],
                envelope[nonce_end:],
                aad,
            )
        except InvalidTag:
            raise CiphertextAuthenticationError("ciphertext authentication failed") from None
        return plaintext.decode("utf-8")
