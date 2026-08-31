from __future__ import annotations

import secrets
from collections.abc import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION_BYTES = 2
_NONCE_BYTES = 12
_TAG_BYTES = 16


class SensitiveValueCipher:
    def __init__(self, keys: Mapping[int, bytes], *, active_key_version: int) -> None:
        if not 0 <= active_key_version < 1 << (_VERSION_BYTES * 8):
            raise ValueError("active key version is out of range")
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
        plaintext = AESGCM(key).decrypt(
            envelope[nonce_start:nonce_end],
            envelope[nonce_end:],
            aad,
        )
        return plaintext.decode("utf-8")
