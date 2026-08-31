from __future__ import annotations

import hmac
from collections.abc import Mapping
from hashlib import sha256

_VERSION_BYTES = 2
_DOMAIN = b"lawyer-agent:blind-index:v1\x00"


class BlindIndexService:
    def __init__(self, keys: Mapping[int, bytes], *, active_key_version: int) -> None:
        if not 0 <= active_key_version < 1 << (_VERSION_BYTES * 8):
            raise ValueError("active key version is out of range")
        if active_key_version not in keys:
            raise ValueError("active key version is missing")
        if any(len(key) != 32 for key in keys.values()):
            raise ValueError("blind-index keys must contain exactly 32 bytes")
        self._keys = dict(keys)
        self._active_key_version = active_key_version

    @property
    def active_key_version(self) -> int:
        return self._active_key_version

    def digest(self, purpose: str, value: str, *, key_version: int | None = None) -> bytes:
        version = self._active_key_version if key_version is None else key_version
        key = self._keys.get(version)
        if key is None:
            raise ValueError("blind index uses an unknown key version")
        if not purpose or "\x00" in purpose:
            raise ValueError("blind-index purpose must be non-empty and contain no NUL")
        message = (
            _DOMAIN
            + version.to_bytes(_VERSION_BYTES, "big")
            + purpose.encode("utf-8")
            + b"\x00"
            + value.encode("utf-8")
        )
        return hmac.digest(key, message, sha256)

    def matches(
        self,
        expected: bytes,
        purpose: str,
        value: str,
        *,
        key_version: int,
    ) -> bool:
        actual = self.digest(purpose, value, key_version=key_version)
        return hmac.compare_digest(expected, actual)
