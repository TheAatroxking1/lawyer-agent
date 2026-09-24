from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lawyer_agent.application.legal_dataset_quality import ReleaseQualityService
from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError
from lawyer_agent.domain.legal_release_provenance import (
    MIXED_PARSER_VERSION,
    MixedReleaseConfiguration,
    ReleaseEntry,
    ReleaseReportIdentity,
    ReleaseSetProvenance,
    mixed_quality_payload_digest,
    selection_digest,
)
from tests.unit.test_legal_dataset_quality import _imported_quality_fixture, fixture


async def mixed_fixture():
    first, second = fixture(), await _imported_quality_fixture()
    first.version = replace(first.version, parser_version="corpus-docx-v3", dataset_version="ds")
    first.proof = replace(first.proof, parser_version="corpus-docx-v3")
    first.chunks = tuple(
        replace(c, parser_version="corpus-docx-v3/hierarchical-v1") for c in first.chunks
    )
    second.version = replace(second.version, dataset_version="ds")
    second.proof = replace(
        second.proof, source_ref="file:///C:/second.docx", input_ref="file:///C:/second.docx"
    )
    second.selection = replace(second.selection, source_ref=second.proof.source_ref)
    reports = tuple(
        ReleaseReportIdentity(
            f"C:/report{i}.json",
            str(i) * 64,
            1,
            "ds",
            d.version.parser_version,
            d.chunks[0].parser_version,
        )
        for i, d in enumerate((first, second), 1)
    )
    entries = tuple(
        ReleaseEntry(r.report_sha256, 1, d.selection)
        for r, d in zip(reports, (first, second), strict=True)
    )
    proof = ReleaseSetProvenance(
        "a" * 64, selection_digest(reports, entries, ()), reports, entries, ()
    )
    configuration = MixedReleaseConfiguration(
        "laws", "fake", 3, MIXED_PARSER_VERSION, "a" * 64, provenance=proof
    )
    data = {d.version.id: d for d in (first, second)}
    corpus = SimpleNamespace(
        version_with_instrument=AsyncMock(
            side_effect=lambda v: (data[v].version, data[v].instrument)
        ),
        provisions_for_version=AsyncMock(side_effect=lambda v: data[v].provisions),
    )
    service = ReleaseQualityService(
        corpus,
        SimpleNamespace(chunks_for_version=AsyncMock(side_effect=lambda v: data[v].chunks)),
        SimpleNamespace(find_for_version=AsyncMock(side_effect=lambda v: data[v].proof)),
    )
    return (first, second), configuration, service, corpus


async def test_mixed_quality_checks_real_v4_and_legacy_with_distinct_parser_identity():
    data, config, service, _ = await mixed_fixture()
    result = await service.check(tuple(d.selection for d in data), config)
    assert result.passed, [v.blockers for v in result.versions]
    payload = result.to_dict()
    assert payload["schema_version"] == "release-quality-v2"
    assert mixed_quality_payload_digest(payload) == result.digest
    assert payload["configuration"]["provenance"] == config.provenance.to_dict()


@pytest.mark.parametrize("mutation", ["order", "metadata", "omission"])
async def test_mixed_quality_requires_exact_selected_values_before_database_reads(mutation):
    data, config, service, corpus = await mixed_fixture()
    selections = tuple(d.selection for d in data)
    if mutation == "order":
        selections = tuple(reversed(selections))
    elif mutation == "metadata":
        selections = (replace(selections[0], metadata_review_ref="changed"), selections[1])
    else:
        selections = selections[:1]
    with pytest.raises(DatasetQualityError):
        await service.check(selections, config)
    corpus.version_with_instrument.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation,blocker",
    [
        ("dataset", "dataset_version_mismatch"),
        ("proof", "parser_version_mismatch"),
        ("chunk", "chunk_invalid"),
    ],
)
async def test_mixed_quality_rejects_member_identity_drift(mutation, blocker):
    data, config, service, _ = await mixed_fixture()
    if mutation == "dataset":
        data[0].version = replace(data[0].version, dataset_version="other")
    elif mutation == "proof":
        data[0].version = replace(data[0].version, parser_version="other")
        data[0].proof = replace(data[0].proof, parser_version="other")
    else:
        data[0].chunks = tuple(
            replace(c, parser_version="other/hierarchical-v1") for c in data[0].chunks
        )
    result = await service.check(tuple(d.selection for d in data), config)
    assert not result.passed
    assert (
        blocker
        in next(v for v in result.versions if v.version_id == str(data[0].version.id)).blockers
    )
