from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_DNS_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*",
    re.ASCII,
)


@dataclass(frozen=True, slots=True, order=True)
class HttpOrigin:
    value: str

    @classmethod
    def parse(cls, value: str) -> HttpOrigin:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value == "null"
            or "*" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("origin is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            hostname = parsed.hostname
        except (UnicodeError, ValueError) as exc:
            raise ValueError("origin is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin must be an HTTP(S) origin without credentials or path")

        host = _canonical_host(hostname)
        scheme = parsed.scheme.lower()
        if port is None or (scheme == "http" and port == 80) or (
            scheme == "https" and port == 443
        ):
            return cls(f"{scheme}://{host}")
        return cls(f"{scheme}://{host}:{port}")

    def __str__(self) -> str:
        return self.value


def _canonical_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            host = value.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("origin host is invalid") from exc
        if _DNS_HOST_PATTERN.fullmatch(host) is None:
            raise ValueError("origin host is invalid") from None
        return host
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{address.compressed}]"
    return address.compressed
