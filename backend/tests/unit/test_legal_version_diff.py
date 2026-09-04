from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_diff import (
    LegalVersionDiffError,
    LegalVersionDiffQueryPort,
    LegalVersionDiffService,
    version_diff,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)

VERSION_OLD = UUID("01a06ae2-6100-7000-8000-0000000000c2")
VERSION_NEW = UUID("01a06ae2-6200-7000-8000-0000000000c3")
INSTRUMENT = UUID("01a06ae2-6000-7000-8000-0000000000c1")


def _instrument() -> LegalInstrument:
    return LegalInstrument(
        id=INSTRUMENT,
        title="中华人民共和国民法典",
        issuing_authority="全国人民代表大会",
        jurisdiction="national",
    )


def _version(version_id: UUID) -> LegalVersion:
    return LegalVersion(
        id=version_id,
        instrument_id=INSTRUMENT,
        version_label=f"版本 {version_id}",
        status=LegalVersionStatus.CURRENT,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        content_hash=bytes(32),
    )


def _provision(
    *,
    version_id: UUID,
    provision_no: str,
    text: str,
) -> Provision:
    return Provision(
        id=new_uuid7(),
        version_id=version_id,
        provision_no=provision_no,
        level=ProvisionLevel.ARTICLE,
        structure_path=(provision_no,),
        title=None,
        full_text=text,
        content_hash=content_sha256(text),
        char_start=0,
        char_end=len(text),
    )


class _Query(LegalVersionDiffQueryPort):
    def __init__(
        self,
        versions: dict[UUID, tuple[LegalVersion, tuple[Provision, ...]]],
    ) -> None:
        self._versions = dict(versions)

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None:
        row = self._versions.get(version_id)
        return None if row is None else (row[0], _instrument())

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]:
        row = self._versions.get(version_id)
        return () if row is None else row[1]


async def test_diff_groups_added_removed_modified_unchanged() -> None:
    old_provisions = (
        _provision(
            version_id=VERSION_OLD,
            provision_no="第一条",
            text="第一条 旧版原样条文。",
        ),
        _provision(
            version_id=VERSION_OLD,
            provision_no="第二条",
            text="第二条 旧版随后被修改。",
        ),
        _provision(
            version_id=VERSION_OLD,
            provision_no="第三条",
            text="第三条 旧版随后被删除。",
        ),
    )
    new_provisions = (
        _provision(
            version_id=VERSION_NEW,
            provision_no="第一条",
            text="第一条 旧版原样条文。",
        ),
        _provision(
            version_id=VERSION_NEW,
            provision_no="第二条",
            text="第二条 新版已修改条文。",
        ),
        _provision(
            version_id=VERSION_NEW,
            provision_no="第四条",
            text="第四条 新版新增条文。",
        ),
    )
    result = version_diff(
        old_provisions,
        new_provisions,
        instrument_id=INSTRUMENT,
        from_version_id=VERSION_OLD,
        to_version_id=VERSION_NEW,
    )
    assert [p.provision_no for p in result.unchanged] == ["第一条"]
    assert [p.provision_no for p in result.removed] == ["第三条"]
    assert [p.provision_no for p in result.added] == ["第四条"]
    assert [m.provision_no for m in result.modified] == ["第二条"]
    modified = result.modified[0]
    assert modified.previous.provision_no == "第二条"
    assert "旧版随后被修改" in modified.previous.full_text
    assert "新版已修改条文" in modified.current.full_text


async def test_diff_empty_versions_is_safe() -> None:
    result = version_diff(
        (),
        (),
        instrument_id=INSTRUMENT,
        from_version_id=VERSION_OLD,
        to_version_id=VERSION_NEW,
    )
    assert result.unchanged == ()
    assert result.removed == ()
    assert result.added == ()
    assert result.modified == ()


async def test_diff_keeps_stable_provision_order() -> None:
    new_provisions = (
        _provision(version_id=VERSION_NEW, provision_no="第二条", text="第二条 乙。"),
        _provision(version_id=VERSION_NEW, provision_no="第一条", text="第一条 甲。"),
    )
    result = version_diff(
        (),
        new_provisions,
        instrument_id=INSTRUMENT,
        from_version_id=VERSION_OLD,
        to_version_id=VERSION_NEW,
    )
    assert [p.provision_no for p in result.added] == ["第一条", "第二条"]


async def test_diff_service_rejects_cross_instrument_versions() -> None:
    foreign = LegalVersion(
        id=new_uuid7(),
        instrument_id=new_uuid7(),
        version_label="外国法版本",
        status=LegalVersionStatus.CURRENT,
        published_on=None,
        effective_on=None,
        repealed_on=None,
    )
    query = _Query(
        {
            VERSION_OLD: (_version(VERSION_OLD), ()),
            VERSION_NEW: (foreign, ()),
        }
    )
    with pytest.raises(LegalVersionDiffError, match="instrument"):
        await LegalVersionDiffService(query).diff(
            from_version_id=VERSION_OLD, to_version_id=VERSION_NEW
        )


async def test_diff_service_rejects_unknown_version() -> None:
    query = _Query({VERSION_OLD: (_version(VERSION_OLD), ())})
    with pytest.raises(LegalVersionDiffError, match="version"):
        await LegalVersionDiffService(query).diff(
            from_version_id=VERSION_OLD, to_version_id=new_uuid7()
        )


async def test_diff_service_reads_both_versions_and_returns_result() -> None:
    query = _Query(
        {
            VERSION_OLD: (
                _version(VERSION_OLD),
                (_provision(version_id=VERSION_OLD, provision_no="第一条", text="第一条 原文。"),),
            ),
            VERSION_NEW: (
                _version(VERSION_NEW),
                (_provision(version_id=VERSION_NEW, provision_no="第一条", text="第一条 新版。"),),
            ),
        }
    )
    result = await LegalVersionDiffService(query).diff(
        from_version_id=VERSION_OLD, to_version_id=VERSION_NEW
    )
    assert result.instrument_id == INSTRUMENT
    assert result.from_version_id == VERSION_OLD
    assert result.to_version_id == VERSION_NEW
    assert [m.provision_no for m in result.modified] == ["第一条"]
