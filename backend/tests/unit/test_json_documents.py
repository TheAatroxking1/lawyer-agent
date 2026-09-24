from __future__ import annotations

import hashlib
from typing import Any

import pytest
from sqlalchemy import literal, select

from lawyer_agent.infrastructure.persistence import json_documents


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.closed = False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for row in self.rows:
            yield row

    async def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result = _Rows(rows)

    async def stream(self, statement: Any) -> _Rows:
        return self.result


async def test_reads_utf8_across_binary_boundaries_and_checks_digest() -> None:
    data = ('{"正文":"' + '中' * 50000 + '"}').encode()
    size = 128 * 1024
    session = _Session([(i // size, len(data), data[i:i + size])
                        for i in range(0, len(data), size)])
    result = await json_documents.read_json_document(
        session, select(literal('ignored')),  # type: ignore[arg-type]
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )
    assert result == {"正文": "中" * 50000}
    assert session.result.closed


async def test_missing_document_returns_none() -> None:
    session = _Session([])
    assert await json_documents.read_json_document(
        session, select(literal('ignored')),  # type: ignore[arg-type]
    ) is None
    assert session.result.closed


@pytest.mark.parametrize("rows", [
    [(1, 2, b'{}')], [(0, 3, b'{}')], [(0, 2, b'{}'), (1, 2, b'{}')],
    [(0, 64 * 1024 * 1024 + 1, b'x')], [(0, 2, '{}')], [(True, 2, b'{}')],
    [(0, 0, b'')], [(0, 2, b'[]')], [(0, 3, b'NaN')],
    [(0, 13, b'{"a":1,"a":2}')], [(0, 8, b'{"a":1}\xff')],
    [(0, 2, b'{}'), (1, 2, b'')],
    [(0, 12, b'{"a":1e9999}')], [(0, 2)],
])
async def test_rejects_incomplete_oversized_or_non_strict_json(rows: list[tuple[Any, ...]]) -> None:
    session = _Session(rows)
    with pytest.raises(ValueError, match='^json_document_invalid$'):
        await json_documents.read_json_document(
            session, select(literal('ignored')),  # type: ignore[arg-type]
        )
    assert session.result.closed


async def test_digest_mismatch_rejects_concurrent_change() -> None:
    session = _Session([(0, 2, b'{}')])
    with pytest.raises(ValueError, match='^json_document_changed$'):
        await json_documents.read_json_document(
            session, select(literal('ignored')),  # type: ignore[arg-type]
            expected_sha256='0' * 64,
        )


async def test_digest_wrong_type_is_stable_error() -> None:
    with pytest.raises(ValueError, match='^json_document_invalid$'):
        await json_documents.read_json_document(
            _Session([]), select(literal('ignored')), expected_sha256=123,  # type: ignore[arg-type]
        )
