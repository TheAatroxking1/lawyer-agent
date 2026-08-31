from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

import phonenumbers
from email_validator import EmailNotValidError, validate_email


class IdentityKind(StrEnum):
    USERNAME = "username"
    PHONE = "phone"
    EMAIL = "email"
    WECHAT_UNIONID = "wechat_unionid"
    WECHAT_OPENID = "wechat_openid"


@dataclass(frozen=True, slots=True)
class NormalizedIdentity:
    kind: IdentityKind
    issuer: str
    subject: str
    display_value: str | None


def normalize_username(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized:
        raise ValueError("username must not be blank")
    return normalized


def normalize_phone(value: str) -> str:
    try:
        parsed = phonenumbers.parse(value.strip(), "CN")
    except phonenumbers.NumberParseException as exc:
        raise ValueError("phone number must be valid") from exc
    if parsed.country_code != 86:
        raise ValueError("phone number must be in mainland China")
    if not phonenumbers.is_valid_number_for_region(parsed, "CN"):
        raise ValueError("phone number must be valid")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(value: str) -> str:
    candidate = value.strip()
    try:
        validated = validate_email(
            candidate,
            check_deliverability=False,
            allow_smtputf8=True,
        )
    except EmailNotValidError as exc:
        raise ValueError("email address must be valid") from exc
    local_part = validated.local_part.casefold()
    ascii_domain = validated.ascii_domain
    if ascii_domain is None:
        ascii_domain = validated.domain.encode("idna").decode("ascii")
    return f"{local_part}@{ascii_domain.lower()}"


def normalize_identifier(
    kind: IdentityKind | str,
    value: str,
    *,
    issuer: str | None = None,
) -> NormalizedIdentity:
    identity_kind = IdentityKind(kind)
    if identity_kind is IdentityKind.USERNAME:
        subject = normalize_username(value)
        return NormalizedIdentity(identity_kind, "local", subject, subject)
    if identity_kind is IdentityKind.PHONE:
        subject = normalize_phone(value)
        return NormalizedIdentity(identity_kind, "phone", subject, None)
    if identity_kind is IdentityKind.EMAIL:
        subject = normalize_email(value)
        return NormalizedIdentity(identity_kind, "email", subject, None)

    if issuer is None or not issuer.strip():
        raise ValueError("wechat identity requires an issuer")
    if not value:
        raise ValueError("wechat subject must not be empty")
    return NormalizedIdentity(identity_kind, issuer.strip(), value, None)
