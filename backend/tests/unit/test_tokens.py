from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import lawyer_agent.application.sessions as session_application
import lawyer_agent.domain.sessions as session_domain
from lawyer_agent.domain.sessions import AccessTokenClaims, Audience, InvalidToken
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService

USER_ID = UUID("01990f00-0000-7000-8000-000000000101")
SESSION_ID = UUID("01990f00-0000-7000-8000-000000000102")
TOKEN_ID = UUID("01990f00-0000-7000-8000-000000000103")
TENANT_ID = UUID("01990f00-0000-7000-8000-000000000104")
MEMBERSHIP_ID = UUID("01990f00-0000-7000-8000-000000000105")
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
ISSUER = "https://identity.lawyer-agent.test"


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def tokens(signing_key: Ed25519PrivateKey) -> TokenService:
    return TokenService(
        issuer=ISSUER,
        active_kid="key-2026-09",
        signing_keys={"key-2026-09": signing_key},
        verification_keys={"key-2026-09": signing_key.public_key()},
        leeway_seconds=30,
    )


def account_claims(**changes: object) -> AccessTokenClaims:
    values: dict[str, object] = {
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "token_id": TOKEN_ID,
        "audience": Audience.ACCOUNT,
        "issued_at": NOW,
        "not_before": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "auth_version": 3,
        "tenant_id": None,
        "membership_id": None,
        "authz_version": None,
    }
    values.update(changes)
    return AccessTokenClaims(**values)  # type: ignore[arg-type]


def tenant_claims(**changes: object) -> AccessTokenClaims:
    values: dict[str, object] = {
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "token_id": TOKEN_ID,
        "audience": Audience.TENANT,
        "issued_at": NOW,
        "not_before": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "auth_version": 3,
        "tenant_id": TENANT_ID,
        "membership_id": MEMBERSHIP_ID,
        "authz_version": 7,
    }
    values.update(changes)
    return AccessTokenClaims(**values)  # type: ignore[arg-type]


def test_account_and_tenant_token_schemas_are_isolated(tokens: TokenService) -> None:
    account = tokens.issue_account(account_claims())
    tenant = tokens.issue_tenant(tenant_claims())

    assert tokens.verify(account, audience=Audience.ACCOUNT, now=NOW) == account_claims()
    assert tokens.verify(tenant, audience=Audience.TENANT, now=NOW) == tenant_claims()
    with pytest.raises(InvalidToken):
        tokens.verify(account, audience=Audience.TENANT, now=NOW)
    with pytest.raises(InvalidToken):
        tokens.verify(tenant, audience=Audience.ACCOUNT, now=NOW)


def test_token_verification_rejects_alg_confusion_and_unknown_kid(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
) -> None:
    payload = {
        "sub": str(USER_ID),
        "sid": str(SESSION_ID),
        "jti": str(TOKEN_ID),
        "auth_version": 3,
        "iss": ISSUER,
        "aud": Audience.ACCOUNT.value,
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
    }
    confused = jwt.encode(
        payload,
        "synthetic-hmac-secret-with-more-than-32-bytes",  # noqa: S106
        algorithm="HS256",
        headers={"kid": "key-2026-09"},
    )
    unknown = jwt.encode(
        payload,
        signing_key,
        algorithm="EdDSA",
        headers={"kid": "unknown"},
    )

    with pytest.raises(InvalidToken):
        tokens.verify(confused, audience=Audience.ACCOUNT, now=NOW)
    with pytest.raises(InvalidToken):
        tokens.verify(unknown, audience=Audience.ACCOUNT, now=NOW)


@pytest.mark.parametrize(
    "claims",
    [
        account_claims(
            issued_at=NOW - timedelta(minutes=20),
            not_before=NOW - timedelta(minutes=20),
            expires_at=NOW - timedelta(seconds=31),
        ),
        account_claims(not_before=NOW + timedelta(seconds=31)),
    ],
)
def test_token_verification_rejects_invalid_time_window(
    tokens: TokenService,
    claims: AccessTokenClaims,
) -> None:
    encoded = tokens.issue_account(claims)

    with pytest.raises(InvalidToken):
        tokens.verify(encoded, audience=Audience.ACCOUNT, now=NOW)


def test_token_verification_rejects_wrong_issuer(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
) -> None:
    other = TokenService(
        issuer="https://other-issuer.test",
        active_kid="key-2026-09",
        signing_keys={"key-2026-09": signing_key},
        verification_keys={"key-2026-09": signing_key.public_key()},
    )
    encoded = other.issue_account(account_claims())

    with pytest.raises(InvalidToken):
        tokens.verify(encoded, audience=Audience.ACCOUNT, now=NOW)


def test_claim_type_and_tenant_context_are_strict() -> None:
    with pytest.raises(ValueError, match="tenant context"):
        account_claims(tenant_id=TENANT_ID)
    with pytest.raises(ValueError, match="tenant context"):
        tenant_claims(authz_version=None)
    with pytest.raises(ValueError, match="auth_version"):
        account_claims(auth_version=True)
    with pytest.raises(ValueError, match="UUID"):
        account_claims(user_id=str(USER_ID))
    with pytest.raises(ValueError, match="timestamps"):
        account_claims(issued_at="2026-09-01T08:00:00Z")


def test_domain_sessions_has_no_framework_or_infrastructure_dependency() -> None:
    source = inspect.getsource(session_domain)

    assert "fastapi" not in source
    assert "sqlalchemy" not in source
    assert "lawyer_agent.infrastructure" not in source


def test_application_sessions_depends_on_ports_not_infrastructure() -> None:
    source = inspect.getsource(session_application)

    assert "fastapi" not in source
    assert "sqlalchemy" not in source
    assert "lawyer_agent.infrastructure" not in source
