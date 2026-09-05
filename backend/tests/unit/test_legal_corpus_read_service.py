from __future__ import annotations

from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_read import (
    LegalCorpusInstrumentCursorInvalid,
    LegalCorpusInvalidRequest,
    LegalCorpusQueryService,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalInstrument

_TITLE = "中华人民共和国民法典"
_AUTHORITY = "全国人民代表大会"
_JURISDICTION = "national"


class _FakeCorpus:
    def __init__(self, owner: _FakeUow) -> None:
        self._owner = owner

    async def list_instruments(
        self,
        *,
        limit: int,
        before_id: UUID | None = None,
        title: str | None = None,
        issuing_authority: str | None = None,
        jurisdiction: str | None = None,
        region_code: str | None = None,
    ) -> tuple[LegalInstrument, ...]:
        owner = self._owner
        if owner.fail_cursor:
            from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
                LegalCorpusInstrumentListCursorInvalid,
            )

            raise LegalCorpusInstrumentListCursorInvalid("cursor missing")
        owner.calls.append(
            {
                "limit": limit,
                "before_id": before_id,
                "title": title,
                "issuing_authority": issuing_authority,
                "jurisdiction": jurisdiction,
                "region_code": region_code,
            }
        )
        if owner.empty:
            return ()
        return (
            LegalInstrument(
                id=new_uuid7(),
                title=_TITLE,
                issuing_authority=_AUTHORITY,
                jurisdiction=_JURISDICTION,
            ),
        )


class _FakeUow:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_cursor = False
        self.empty = False
        self.corpus = _FakeCorpus(self)

    async def __aenter__(self) -> _FakeUow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc


def _service(uow: _FakeUow) -> LegalCorpusQueryService:
    return LegalCorpusQueryService(lambda: uow)


async def test_instruments_passes_validated_filters_to_uow() -> None:
    uow = _FakeUow()
    cursor = new_uuid7()
    rows = await _service(uow).instruments(
        limit=5,
        before_id=cursor,
        title="  中华人民共和国民法典  ",
        issuing_authority="  全国人大  ",
        jurisdiction="  national ",
        region_code=None,
    )
    assert len(rows) == 1
    assert uow.calls == [
        {
            "limit": 5,
            "before_id": cursor,
            "title": "中华人民共和国民法典",
            "issuing_authority": "全国人大",
            "jurisdiction": "national",
            "region_code": None,
        }
    ]


async def test_instruments_rejects_blank_or_overlong_filters() -> None:
    service = _service(_FakeUow())
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, title="   ")
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, issuing_authority="x" * 257)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, jurisdiction="x" * 65)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, region_code="x" * 17)


async def test_instruments_rejects_invalid_limit_and_cursor() -> None:
    service = _service(_FakeUow())
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=0)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=101)
    with pytest.raises(LegalCorpusInvalidRequest):
        await service.instruments(limit=5, before_id=UUID(int=0))  # not uuid7


async def test_instruments_maps_cursor_missing_to_stable_error() -> None:
    uow = _FakeUow()
    uow.fail_cursor = True
    with pytest.raises(LegalCorpusInstrumentCursorInvalid):
        await _service(uow).instruments(limit=5, before_id=new_uuid7())


async def test_instruments_empty_result_is_returned() -> None:
    uow = _FakeUow()
    uow.empty = True
    rows = await _service(uow).instruments(limit=5)
    assert rows == ()
