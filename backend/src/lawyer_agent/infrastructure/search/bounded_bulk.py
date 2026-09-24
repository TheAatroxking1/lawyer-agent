"""Prevalidate a small input batch, then serialize at most one HTTP payload at a time."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

MAX_BULK_BYTES = 8 * 1024 * 1024
MAX_BULK_DOCUMENTS = 256


class BoundedBulkError(ValueError):
    """Safe boundary error without documents or serializer details."""


@dataclass(frozen=True, slots=True)
class BulkPayload:
    content: bytes
    document_ids: tuple[str, ...]


def _document_bytes(index_name: str, document: dict[str, Any], identifier: str) -> bytes:
    try:
        action = json.dumps({"index": {"_index": index_name, "_id": identifier}},
                            ensure_ascii=False, allow_nan=False)
        source = json.dumps(document, ensure_ascii=False, allow_nan=False)
        encoded = (action + "\n" + source + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BoundedBulkError("bulk document is not valid JSON") from None
    if len(encoded) > MAX_BULK_BYTES:
        raise BoundedBulkError("bulk document exceeds the request size limit")
    return encoded


def bounded_bulk_payloads(
    index_name: str, documents: tuple[dict[str, Any], ...], *, id_field: str,
) -> Iterator[BulkPayload]:
    """All inputs are checked eagerly; returned iterator holds only the current request.

    Serialize twice instead of caching every serialized document in the input batch.
    A caller must keep its documents unchanged while consuming the iterator.
    """
    if (not isinstance(documents, tuple) or len(documents) > MAX_BULK_DOCUMENTS
            or not isinstance(index_name, str) or not index_name
            or not isinstance(id_field, str) or not id_field):
        raise BoundedBulkError("bulk batch is invalid")
    identifiers: list[str] = []
    seen: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            raise BoundedBulkError("bulk document is invalid")
        identifier = document.get(id_field)
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise BoundedBulkError("bulk document ids must be nonempty and unique")
        _document_bytes(index_name, document, identifier)
        identifiers.append(identifier)
        seen.add(identifier)
    return _payloads(index_name, documents, tuple(identifiers))


def _payloads(
    index_name: str, documents: tuple[dict[str, Any], ...], identifiers: tuple[str, ...],
) -> Iterator[BulkPayload]:
    current = bytearray()
    current_ids: list[str] = []
    for document, identifier in zip(documents, identifiers, strict=True):
        encoded = _document_bytes(index_name, document, identifier)
        if current and len(current) + len(encoded) > MAX_BULK_BYTES:
            yield BulkPayload(bytes(current), tuple(current_ids))
            current.clear()
            current_ids.clear()
        current.extend(encoded)
        current_ids.append(identifier)
    if current:
        yield BulkPayload(bytes(current), tuple(current_ids))
