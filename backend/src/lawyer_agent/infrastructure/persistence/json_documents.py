"""Read one MySQL JSON value without large protocol fields or multi-query tearing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any
from typing import cast as type_cast

from sqlalchemy import Select, case, cast, func, literal, select, true
from sqlalchemy.dialects.mysql import BINARY
from sqlalchemy.ext.asyncio import AsyncSession

_PART_BYTES = 128 * 1024
_MAX_BYTES = 64 * 1024 * 1024


def _parts_statement(statement: Select[Any]) -> Select[Any]:
    columns = list(statement.selected_columns)
    if len(columns) != 1:
        raise ValueError("json_document_invalid")
    # LIMIT prevents MySQL from merging this derived table: serialize the JSON once.
    payload = statement.with_only_columns(
        cast(columns[0], BINARY).label("data"), maintain_column_froms=True,
    ).limit(1).cte("json_payload")
    parts = select(literal(0).label("part_index")).cte("json_parts", recursive=True)
    parts = parts.union_all(
        select((parts.c.part_index + 1).label("part_index"))
        .where(parts.c.part_index < _MAX_BYTES // _PART_BYTES - 1)
    )
    total = func.octet_length(payload.c.data)
    return (
        select(
            parts.c.part_index,
            total.label("total_bytes"),
            case(
                (total <= _MAX_BYTES, func.substring(
                    payload.c.data, parts.c.part_index * _PART_BYTES + 1, _PART_BYTES,
                )),
                else_=cast(literal(b""), BINARY),
            ).label("part_data"),
        )
        .select_from(payload.join(parts, true()))
        .where((parts.c.part_index == 0) | (parts.c.part_index * _PART_BYTES < total))
        .order_by(parts.c.part_index)
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("json_document_invalid")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("json_document_invalid")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("json_document_invalid")
    return result


async def read_json_document(
    session: AsyncSession, statement: Select[Any], *, expected_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Read a single-column, uniquely scoped JSON SELECT; cap one document at 64 MiB.

    All parts come from one SQL statement. Callers combining separate metadata reads
    should pass its SHA2(CAST(json_column AS BINARY), 256) to detect concurrent changes.
    Neither driver settings nor the stored JSON representation are changed.
    """
    if expected_sha256 is not None and (
        type(expected_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise ValueError("json_document_invalid")
    result = await session.stream(_parts_statement(statement))
    data = bytearray()
    expected_total: int | None = None
    index = 0
    try:
        async for row in result:
            if len(row) != 3:
                raise ValueError("json_document_invalid")
            ordinal, total, part = row
            if (
                type(ordinal) is not int or ordinal != index
                or index >= _MAX_BYTES // _PART_BYTES
                or type(total) is not int or not 0 < total <= _MAX_BYTES
                or len(data) >= total
                or not isinstance(part, bytes)
                or (expected_total is not None and total != expected_total)
                or len(part) != min(_PART_BYTES, total - len(data))
            ):
                raise ValueError("json_document_invalid")
            expected_total = total
            data.extend(part)
            index += 1
    finally:
        await result.close()
    if expected_total is None:
        return None
    if len(data) != expected_total:
        raise ValueError("json_document_invalid")
    if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("json_document_changed")
    try:
        decoded = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant, parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("json_document_invalid") from None
    if not isinstance(decoded, dict):
        raise ValueError("json_document_invalid")
    return type_cast(dict[str, Any], decoded)
