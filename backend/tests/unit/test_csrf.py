from __future__ import annotations

from uuid import UUID

import pytest

from lawyer_agent.infrastructure.security.csrf import (
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    CsrfService,
    InvalidCsrfToken,
    UntrustedOrigin,
    cookie_options,
)

SESSION_ID = UUID("01990f00-0000-7000-8000-000000000201")
OTHER_SESSION_ID = UUID("01990f00-0000-7000-8000-000000000202")
ORIGIN = "https://app.lawyer-agent.test"


@pytest.fixture
def csrf() -> CsrfService:
    return CsrfService(key=b"c" * 32, trusted_origins=(ORIGIN,))


def test_signed_double_submit_token_is_session_bound(csrf: CsrfService) -> None:
    token = csrf.issue(SESSION_ID, nonce=b"n" * 32)

    csrf.verify(
        origin=ORIGIN,
        header_token=token,
        cookie_token=token,
        session_id=SESSION_ID,
    )
    with pytest.raises(InvalidCsrfToken):
        csrf.verify(
            origin=ORIGIN,
            header_token=token,
            cookie_token=token,
            session_id=OTHER_SESSION_ID,
        )


def test_csrf_rejects_origin_before_token_validation(csrf: CsrfService) -> None:
    with pytest.raises(UntrustedOrigin):
        csrf.verify(
            origin="https://attacker.invalid",
            header_token="malformed",  # noqa: S106 - synthetic CSRF fixture
            cookie_token="different",  # noqa: S106 - synthetic CSRF fixture
            session_id=SESSION_ID,
        )


@pytest.mark.parametrize(
    ("configured", "request_origin"),
    [
        ("HTTPS://APP.LAWYER-AGENT.TEST:443/", ORIGIN),
        ("http://app.lawyer-agent.test:80", "HTTP://APP.LAWYER-AGENT.TEST/"),
        ("HTTPS://[2001:DB8::1]:443/", "https://[2001:db8::1]"),
    ],
)
def test_csrf_normalizes_configured_and_request_origins(
    configured: str,
    request_origin: str,
) -> None:
    csrf = CsrfService(key=b"c" * 32, trusted_origins=(configured,))
    token = csrf.issue(SESSION_ID, nonce=b"n" * 32)

    csrf.verify(
        origin=request_origin,
        header_token=token,
        cookie_token=token,
        session_id=SESSION_ID,
    )


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "null",
        "*",
        "https://*.lawyer-agent.test",
        "ftp://app.lawyer-agent.test",
        "https://user@app.lawyer-agent.test",
        "https://app.lawyer-agent.test/path",
        "https://app.lawyer-agent.test?query=1",
        "https://app.lawyer-agent.test#fragment",
        "https://app.lawyer-agent.test\x00",
        "app.lawyer-agent.test",
    ],
)
def test_csrf_rejects_invalid_configured_origins(origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        CsrfService(key=b"c" * 32, trusted_origins=(origin,))


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "*",
        "https://*.lawyer-agent.test",
        "ftp://app.lawyer-agent.test",
        "https://user@app.lawyer-agent.test",
        "https://app.lawyer-agent.test/path",
        "https://app.lawyer-agent.test?query=1",
        "https://app.lawyer-agent.test#fragment",
    ],
)
def test_csrf_rejects_invalid_request_origin_before_token_validation(
    csrf: CsrfService,
    origin: str | None,
) -> None:
    with pytest.raises(UntrustedOrigin):
        csrf.verify(
            origin=origin,
            header_token="malformed",  # noqa: S106 - synthetic CSRF fixture
            cookie_token="different",  # noqa: S106 - synthetic CSRF fixture
            session_id=SESSION_ID,
        )


@pytest.mark.parametrize(
    ("header", "cookie"),
    [
        ("same", "different"),
        ("", ""),
        ("malformed", "malformed"),
        ("a.b.c", "a.b.c"),
    ],
)
def test_csrf_rejects_mismatch_empty_and_malformed_tokens(
    csrf: CsrfService,
    header: str,
    cookie: str,
) -> None:
    with pytest.raises(InvalidCsrfToken):
        csrf.verify(
            origin=ORIGIN,
            header_token=header,
            cookie_token=cookie,
            session_id=SESSION_ID,
        )


def test_cookie_contract_uses_host_prefix_and_correct_visibility() -> None:
    refresh = cookie_options(secure=True, http_only=True)
    csrf = cookie_options(secure=True, http_only=False)

    assert REFRESH_COOKIE_NAME == "__Host-lawyer_refresh"
    assert CSRF_COOKIE_NAME == "__Host-lawyer_csrf"
    assert refresh == {
        "secure": True,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
    }
    assert csrf["httponly"] is False
    assert "domain" not in refresh and "domain" not in csrf


def test_cookie_contract_rejects_insecure_production_configuration() -> None:
    with pytest.raises(ValueError, match="secure"):
        cookie_options(secure=False, http_only=True, production=True)
