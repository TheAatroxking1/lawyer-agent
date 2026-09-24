"""Source-derived legal title and structure navigation values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,254}")
_ID = re.compile(r"[a-f0-9]{64}")


class NavigationDocumentKind(StrEnum):
    INSTRUMENT = "instrument"
    STRUCTURE = "structure"


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"navigation {field} must be non-empty text")


@dataclass(frozen=True, slots=True)
class NavigationDocument:
    navigation_id: str
    version_id: UUID
    kind: NavigationDocumentKind
    locator: str
    content: str
    parser_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.navigation_id, str) or not _ID.fullmatch(self.navigation_id):
            raise ValueError("navigation id must be a lowercase SHA256 digest")
        require_uuid7(self.version_id, field="navigation version_id")
        if not isinstance(self.kind, NavigationDocumentKind):
            raise ValueError("navigation kind must be strongly typed")
        _require_text(self.locator, "locator")
        _require_text(self.content, "content")
        _require_text(self.parser_version, "parser_version")


@dataclass(frozen=True, slots=True)
class NavigationSearchHit:
    version_id: UUID
    locator: str
    score: float

    def __post_init__(self) -> None:
        require_uuid7(self.version_id, field="navigation hit version_id")
        _require_text(self.locator, "locator")
        if not isinstance(self.score, float) or not isfinite(self.score):
            raise ValueError("navigation score must be a finite float")


def navigation_index_name(main_index_name: str) -> str:
    """Name the paired sidecar without expanding the accepted index namespace."""
    if not isinstance(main_index_name, str) or not _NAME.fullmatch(main_index_name):
        raise ValueError("main_index_name is invalid")
    return "lawyer-nav-" + sha256(main_index_name.encode("utf-8")).hexdigest()[:32]
