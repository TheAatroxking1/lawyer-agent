from __future__ import annotations

import hmac
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from typing import TypedDict
from uuid import UUID

from lawyer_agent.domain.origins import HttpOrigin

REFRESH_COOKIE_NAME = "__Host-lawyer_refresh"
CSRF_COOKIE_NAME = "__Host-lawyer_csrf"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{22,128}\.[A-Za-z0-9_-]{43}", re.ASCII)


class UntrustedOrigin(Exception):
    code = "csrf_origin_rejected"


class InvalidCsrfToken(Exception):
    code = "csrf_validation_failed"


class CookieOptions(TypedDict):
    secure: bool
    httponly: bool
    samesite: str
    path: str


def cookie_options(
    *,
    secure: bool,
    http_only: bool,
    production: bool = False,
) -> CookieOptions:
    if production and not secure:
        raise ValueError("production cookies must be secure")
    return {
        "secure": secure,
        "httponly": http_only,
        "samesite": "lax",
        "path": "/",
    }


class CsrfService:
    def __init__(
        self,
        *,
        key: bytes,
        trusted_origins: tuple[str | HttpOrigin, ...],
    ) -> None:
        if len(key) != 32:
            raise ValueError("CSRF key must contain exactly 32 bytes")
        if not trusted_origins:
            raise ValueError("trusted origins must not be empty")
        self._key = key
        self._trusted_origins = frozenset(
            origin if isinstance(origin, HttpOrigin) else HttpOrigin.parse(origin)
            for origin in trusted_origins
        )

    def issue(self, session_id: UUID, *, nonce: bytes | None = None) -> str:
        nonce_value = secrets.token_bytes(32) if nonce is None else nonce
        if len(nonce_value) < 16:
            raise ValueError("CSRF nonce must contain at least 16 bytes")
        encoded_nonce = _encode(nonce_value)
        signature = self._mac(session_id, encoded_nonce)
        return f"{encoded_nonce}.{_encode(signature)}"

    def verify(
        self,
        *,
        origin: str | None,
        header_token: str | None,
        cookie_token: str | None,
        session_id: UUID,
    ) -> None:
        try:
            request_origin = None if origin is None else HttpOrigin.parse(origin)
        except ValueError:
            raise UntrustedOrigin from None
        if request_origin is None or request_origin not in self._trusted_origins:
            raise UntrustedOrigin
        header = "" if header_token is None else header_token
        cookie = "" if cookie_token is None else cookie_token
        if not hmac.compare_digest(header.encode("utf-8"), cookie.encode("utf-8")):
            raise InvalidCsrfToken
        if _TOKEN_PATTERN.fullmatch(header) is None:
            raise InvalidCsrfToken
        encoded_nonce, encoded_signature = header.split(".", 1)
        try:
            nonce = _decode(encoded_nonce)
            signature = _decode(encoded_signature)
        except ValueError:
            raise InvalidCsrfToken from None
        if len(nonce) < 16 or len(signature) != sha256().digest_size:
            raise InvalidCsrfToken
        expected = self._mac(session_id, encoded_nonce)
        if not hmac.compare_digest(signature, expected):
            raise InvalidCsrfToken

    def _mac(self, session_id: UUID, encoded_nonce: str) -> bytes:
        message = f"csrf:v1:{session_id}:{encoded_nonce}".encode("ascii")
        return hmac.digest(self._key, message, sha256)


def _encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = urlsafe_b64decode(value + padding)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("invalid base64url") from exc
    if not hmac.compare_digest(_encode(decoded), value):
        raise ValueError("non-canonical base64url")
    return decoded
