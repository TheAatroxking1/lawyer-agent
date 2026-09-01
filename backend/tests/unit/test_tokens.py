from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid1, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import lawyer_agent.application.sessions as session_application
import lawyer_agent.domain.sessions as session_domain
from lawyer_agent.application.sessions import (
    LockedRefreshToken,
    NewRefreshToken,
    RefreshResult,
    SessionResult,
)
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


def platform_claims(**changes: object) -> AccessTokenClaims:
    changes.setdefault("expires_at", NOW + timedelta(minutes=5))
    return account_claims(audience=Audience.PLATFORM, **changes)


def token_payload(claims: AccessTokenClaims) -> dict[str, object]:
    payload: dict[str, object] = {
        "sub": str(claims.user_id),
        "sid": str(claims.session_id),
        "jti": str(claims.token_id),
        "auth_version": claims.auth_version,
        "iss": ISSUER,
        "aud": claims.audience.value,
        "iat": int(claims.issued_at.timestamp()),
        "nbf": int(claims.not_before.timestamp()),
        "exp": int(claims.expires_at.timestamp()),
    }
    if claims.audience is Audience.TENANT:
        payload.update(
            tenant_id=str(claims.tenant_id),
            membership_id=str(claims.membership_id),
            authz_version=claims.authz_version,
        )
    return payload


def sign_payload(
    payload: dict[str, object],
    signing_key: Ed25519PrivateKey,
    *,
    kid: str = "key-2026-09",
    typ: str = "at+jwt",
) -> str:
    return jwt.encode(
        payload,
        signing_key,
        algorithm="EdDSA",
        headers={"kid": kid, "typ": typ},
    )


def test_account_and_tenant_token_schemas_are_isolated(tokens: TokenService) -> None:
    account = tokens.issue_account(account_claims())
    tenant = tokens.issue_tenant(tenant_claims())

    assert tokens.verify(account, audience=Audience.ACCOUNT, now=NOW) == account_claims()
    assert tokens.verify(tenant, audience=Audience.TENANT, now=NOW) == tenant_claims()
    with pytest.raises(InvalidToken):
        tokens.verify(account, audience=Audience.TENANT, now=NOW)
    with pytest.raises(InvalidToken):
        tokens.verify(tenant, audience=Audience.ACCOUNT, now=NOW)


def test_access_token_audience_schemas_are_pairwise_isolated(tokens: TokenService) -> None:
    claims_by_audience = {
        Audience.ACCOUNT: account_claims(),
        Audience.TENANT: tenant_claims(),
        Audience.PLATFORM: platform_claims(),
    }
    encoded_by_audience = {
        Audience.ACCOUNT: tokens.issue_account(claims_by_audience[Audience.ACCOUNT]),
        Audience.TENANT: tokens.issue_tenant(claims_by_audience[Audience.TENANT]),
        Audience.PLATFORM: tokens.issue_platform(claims_by_audience[Audience.PLATFORM]),
    }

    for issued_audience, encoded in encoded_by_audience.items():
        assert (
            tokens.verify(encoded, audience=issued_audience, now=NOW)
            == claims_by_audience[issued_audience]
        )
        for requested_audience in Audience:
            if requested_audience is issued_audience:
                continue
            with pytest.raises(InvalidToken):
                tokens.verify(encoded, audience=requested_audience, now=NOW)


def test_step_up_is_reserved_without_an_access_token_signing_path(
    tokens: TokenService,
) -> None:
    assert not hasattr(tokens, "issue_step_up")


def test_access_token_claims_reject_reserved_step_up_audience() -> None:
    with pytest.raises(ValueError, match="reserved"):
        account_claims(
            audience=Audience.STEP_UP,
            expires_at=NOW + timedelta(minutes=5),
        )


def test_access_token_verifier_rejects_reserved_step_up_even_when_audience_matches(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
) -> None:
    payload = token_payload(account_claims(expires_at=NOW + timedelta(minutes=5)))
    payload["aud"] = Audience.STEP_UP.value
    encoded = sign_payload(payload, signing_key)

    for audience in Audience:
        with pytest.raises(InvalidToken):
            tokens.verify(encoded, audience=audience, now=NOW)


def test_token_verification_rejects_alg_confusion_and_unknown_kid(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
) -> None:
    payload = token_payload(account_claims())
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


def test_token_verification_rejects_wrong_typ(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
) -> None:
    encoded = sign_payload(token_payload(account_claims()), signing_key, typ="JWT")

    with pytest.raises(InvalidToken):
        tokens.verify(encoded, audience=Audience.ACCOUNT, now=NOW)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_token_verification_rejects_missing_and_extra_claims(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
    mutation: str,
) -> None:
    payload = token_payload(account_claims())
    if mutation == "missing":
        del payload["auth_version"]
    else:
        payload["unexpected"] = "must-not-be-accepted"
    encoded = sign_payload(payload, signing_key)

    with pytest.raises(InvalidToken):
        tokens.verify(encoded, audience=Audience.ACCOUNT, now=NOW)


def test_token_verification_rejects_non_v7_identifier(
    tokens: TokenService,
    signing_key: Ed25519PrivateKey,
) -> None:
    payload = token_payload(account_claims())
    payload["sub"] = str(uuid4())
    encoded = sign_payload(payload, signing_key)

    with pytest.raises(InvalidToken):
        tokens.verify(encoded, audience=Audience.ACCOUNT, now=NOW)


@pytest.mark.parametrize(
    "claims",
    [
        account_claims(
            issued_at=NOW - timedelta(minutes=5),
            not_before=NOW - timedelta(minutes=5),
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


@pytest.mark.parametrize("invalid_id", [UUID(int=0), uuid1(), uuid4()])
@pytest.mark.parametrize("field", ["user_id", "session_id", "token_id"])
def test_common_token_identifiers_must_be_uuidv7(
    field: str,
    invalid_id: UUID,
) -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        account_claims(**{field: invalid_id})


@pytest.mark.parametrize("invalid_id", [UUID(int=0), uuid1(), uuid4()])
@pytest.mark.parametrize("field", ["tenant_id", "membership_id"])
def test_tenant_token_identifiers_must_be_uuidv7(
    field: str,
    invalid_id: UUID,
) -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        tenant_claims(**{field: invalid_id})


@pytest.mark.parametrize(
    ("claims_factory", "maximum"),
    [
        (account_claims, timedelta(minutes=10)),
        (tenant_claims, timedelta(minutes=10)),
        (platform_claims, timedelta(minutes=5)),
    ],
)
def test_token_lifetime_accepts_exact_audience_maximum(
    claims_factory: object,
    maximum: timedelta,
) -> None:
    claims = claims_factory(expires_at=NOW + maximum)  # type: ignore[operator]

    assert claims.expires_at - claims.issued_at == maximum


@pytest.mark.parametrize(
    ("claims_factory", "maximum"),
    [
        (account_claims, timedelta(minutes=10)),
        (tenant_claims, timedelta(minutes=10)),
        (platform_claims, timedelta(minutes=5)),
    ],
)
def test_token_lifetime_rejects_value_above_audience_maximum(
    claims_factory: object,
    maximum: timedelta,
) -> None:
    with pytest.raises(ValueError, match="lifetime"):
        claims_factory(expires_at=NOW + maximum + timedelta(seconds=1))  # type: ignore[operator]


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


@pytest.mark.parametrize(
    ("result_type", "secret_fields"),
    [
        (SessionResult, {"access_token", "refresh_token"}),
        (RefreshResult, {"access_token", "refresh_token"}),
        (session_application._SessionMaterial, {"raw_refresh"}),
        (LockedRefreshToken, {"token_hash"}),
        (NewRefreshToken, {"token_hash"}),
    ],
)
def test_token_secrets_are_excluded_from_dataclass_repr(
    result_type: type[object],
    secret_fields: set[str],
) -> None:
    repr_by_name = {field.name: field.repr for field in fields(result_type)}

    assert all(repr_by_name[name] is False for name in secret_fields)


def test_public_session_results_do_not_repr_raw_tokens() -> None:
    access = "synthetic.access.secret"
    refresh = "synthetic-refresh-secret"

    assert access not in repr(SessionResult(SESSION_ID, TOKEN_ID, access, refresh))
    assert refresh not in repr(SessionResult(SESSION_ID, TOKEN_ID, access, refresh))
    assert access not in repr(RefreshResult(SESSION_ID, TOKEN_ID, access, refresh))
    assert refresh not in repr(RefreshResult(SESSION_ID, TOKEN_ID, access, refresh))
