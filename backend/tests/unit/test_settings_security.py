from base64 import b64encode

import pytest
from pydantic import ValidationError

from lawyer_agent.config import Settings


def encoded_key(byte: int) -> str:
    return b64encode(bytes([byte]) * 32).decode("ascii")


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": "p" * 32,
        "data_encryption_key_b64": encoded_key(11),
        "blind_index_key_b64": encoded_key(12),
        "refresh_token_key_b64": encoded_key(13),
        "csrf_key_b64": encoded_key(14),
        "jwt_ed25519_key_ring": {"active": "configured-outside-source-control"},
        "trusted_origins": ("https://app.example.cn",),
        "cookie_secure": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_rejects_shared_or_missing_security_keys() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", secret_key="x" * 32)

    with pytest.raises(ValidationError, match="must be distinct"):
        production_settings(refresh_token_key_b64=encoded_key(11))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trusted_origins", ("http://app.example.cn",)),
        ("trusted_origins", ("https://*.example.cn",)),
        ("cookie_secure", False),
    ],
)
def test_production_rejects_insecure_cookie_or_origin(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        production_settings(**{field: value})


def test_production_accepts_explicit_distinct_security_configuration() -> None:
    settings = production_settings()

    assert settings.cookie_secure is True
    assert settings.trusted_origins == ("https://app.example.cn",)
