from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from lawyer_agent.domain.sessions import AccessTokenClaims, Audience, InvalidToken

_KID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}", re.ASCII)
_COMMON_CLAIMS = frozenset(
    {"sub", "sid", "jti", "iat", "nbf", "exp", "iss", "aud", "auth_version"}
)
_TENANT_CLAIMS = frozenset({"tenant_id", "membership_id", "authz_version"})


class TokenService:
    def __init__(
        self,
        *,
        issuer: str,
        active_kid: str,
        signing_keys: Mapping[str, Ed25519PrivateKey],
        verification_keys: Mapping[str, Ed25519PublicKey],
        leeway_seconds: int = 30,
    ) -> None:
        if not issuer or len(issuer) > 512:
            raise ValueError("JWT issuer is invalid")
        if _KID_PATTERN.fullmatch(active_kid) is None:
            raise ValueError("active JWT kid is invalid")
        if active_kid not in signing_keys or active_kid not in verification_keys:
            raise ValueError("active JWT kid must have signing and verification keys")
        if set(signing_keys) - set(verification_keys):
            raise ValueError("every JWT signing key must have a verification key")
        if isinstance(leeway_seconds, bool) or not 0 <= leeway_seconds <= 60:
            raise ValueError("JWT leeway must be between 0 and 60 seconds")
        if any(_KID_PATTERN.fullmatch(kid) is None for kid in verification_keys):
            raise ValueError("JWT key ring contains an invalid kid")
        self._issuer = issuer
        self._active_kid = active_kid
        self._signing_keys = dict(signing_keys)
        self._verification_keys = dict(verification_keys)
        self._leeway = timedelta(seconds=leeway_seconds)

    def issue_account(self, claims: AccessTokenClaims) -> str:
        if claims.audience is not Audience.ACCOUNT:
            raise ValueError("account token requires account claims")
        return self._issue(claims)

    def issue_tenant(self, claims: AccessTokenClaims) -> str:
        if claims.audience is not Audience.TENANT:
            raise ValueError("tenant token requires tenant claims")
        return self._issue(claims)

    def issue_platform(self, claims: AccessTokenClaims) -> str:
        if claims.audience is not Audience.PLATFORM:
            raise ValueError("platform token requires platform claims")
        return self._issue(claims)

    def issue_step_up(self, claims: AccessTokenClaims) -> str:
        if claims.audience is not Audience.STEP_UP:
            raise ValueError("step-up token requires step-up claims")
        return self._issue(claims)

    def verify(
        self,
        encoded: str,
        *,
        audience: Audience,
        now: datetime | None = None,
    ) -> AccessTokenClaims:
        if not isinstance(audience, Audience) or not encoded or len(encoded) > 8192:
            raise InvalidToken
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
            raise ValueError("verification time must be UTC-aware")
        try:
            header = jwt.get_unverified_header(encoded)
            kid = header.get("kid")
            if (
                header.get("alg") != "EdDSA"
                or header.get("typ") != "at+jwt"
                or not isinstance(kid, str)
                or _KID_PATTERN.fullmatch(kid) is None
            ):
                raise InvalidToken
            public_key = self._verification_keys.get(kid)
            if public_key is None:
                raise InvalidToken
            decoded = jwt.decode(
                encoded,
                public_key,
                algorithms=["EdDSA"],
                audience=audience.value,
                issuer=self._issuer,
                options={
                    "require": [
                        "sub",
                        "sid",
                        "jti",
                        "iat",
                        "nbf",
                        "exp",
                        "iss",
                        "aud",
                        "auth_version",
                    ],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "strict_aud": True,
                },
            )
            claims = self._claims_from_payload(decoded, audience)
            if claims.issued_at > current + self._leeway:
                raise InvalidToken
            if claims.not_before > current + self._leeway:
                raise InvalidToken
            if claims.expires_at <= current - self._leeway:
                raise InvalidToken
            return claims
        except InvalidToken:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, OverflowError):
            raise InvalidToken from None

    def _issue(self, claims: AccessTokenClaims) -> str:
        payload: dict[str, object] = {
            "sub": str(claims.user_id),
            "sid": str(claims.session_id),
            "jti": str(claims.token_id),
            "iat": _timestamp(claims.issued_at),
            "nbf": _timestamp(claims.not_before),
            "exp": _timestamp(claims.expires_at),
            "iss": self._issuer,
            "aud": claims.audience.value,
            "auth_version": claims.auth_version,
        }
        if claims.audience is Audience.TENANT:
            payload.update(
                tenant_id=str(claims.tenant_id),
                membership_id=str(claims.membership_id),
                authz_version=claims.authz_version,
            )
        return jwt.encode(
            payload,
            self._signing_keys[self._active_kid],
            algorithm="EdDSA",
            headers={"kid": self._active_kid, "typ": "at+jwt"},
        )

    def _claims_from_payload(
        self,
        payload: dict[str, Any],
        audience: Audience,
    ) -> AccessTokenClaims:
        expected = _COMMON_CLAIMS | (_TENANT_CLAIMS if audience is Audience.TENANT else set())
        if set(payload) != expected:
            raise InvalidToken
        if payload["aud"] != audience.value or payload["iss"] != self._issuer:
            raise InvalidToken
        return AccessTokenClaims(
            user_id=_strict_uuid(payload["sub"]),
            session_id=_strict_uuid(payload["sid"]),
            token_id=_strict_uuid(payload["jti"]),
            audience=audience,
            issued_at=_strict_timestamp(payload["iat"]),
            not_before=_strict_timestamp(payload["nbf"]),
            expires_at=_strict_timestamp(payload["exp"]),
            auth_version=_strict_positive_int(payload["auth_version"]),
            tenant_id=(
                _strict_uuid(payload["tenant_id"]) if audience is Audience.TENANT else None
            ),
            membership_id=(
                _strict_uuid(payload["membership_id"])
                if audience is Audience.TENANT
                else None
            ),
            authz_version=(
                _strict_positive_int(payload["authz_version"])
                if audience is Audience.TENANT
                else None
            ),
        )


def _timestamp(value: datetime) -> int:
    return int(value.timestamp())


def _strict_timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidToken
    return datetime.fromtimestamp(value, UTC)


def _strict_positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidToken
    return value


def _strict_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise InvalidToken
    parsed = UUID(value)
    if str(parsed) != value:
        raise InvalidToken
    return parsed
