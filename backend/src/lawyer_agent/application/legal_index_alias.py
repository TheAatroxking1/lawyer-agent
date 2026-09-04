"""Dataset index alias management for the legal corpus (non-model).

spec 6.6 publishes a fresh OpenSearch index and atomically re-points the
dataset alias so readers keep a stable, current target while older indexes stay
available for history and fast rollback. This service only manages the alias:
it never builds indexes (the vector indexing service does that) and never moves
data. Alias and index names are validated against a strict whitelist.
"""

from __future__ import annotations

import re
from typing import Protocol

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,254}$")


class LegalIndexAliasError(ValueError):
    """Stable error for dataset alias operations."""


class _IndexAliasClientPort(Protocol):
    async def index_exists(self, index_name: str) -> bool: ...

    async def resolve_alias(self, alias: str) -> str | None: ...

    async def point_alias(self, alias: str, index_name: str) -> str | None: ...


class LegalDatasetAliasService:
    """Publishes a dataset alias to one existing index, switching atomically."""

    def __init__(self, client: _IndexAliasClientPort) -> None:
        if not hasattr(client, "point_alias"):
            raise ValueError("dataset alias service requires an OpenSearch client")
        self._client = client

    async def publish_dataset(self, alias: str, index_name: str) -> str | None:
        """Point the alias at one built index; returns the previous target."""
        _require_name(alias, "alias")
        _require_name(index_name, "index_name")
        if not await self._client.index_exists(index_name):
            raise LegalIndexAliasError(
                f"target index does not exist: {index_name}"
            )
        return await self._client.point_alias(alias, index_name)

    async def active_dataset_index(self, alias: str) -> str | None:
        """Resolve the current alias target for retrieval callers."""
        _require_name(alias, "alias")
        return await self._client.resolve_alias(alias)


def _require_name(value: str, field: str) -> None:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise LegalIndexAliasError(
            f"{field} must be 1-255 chars of [a-z0-9_.-] starting alphanumeric"
        )
