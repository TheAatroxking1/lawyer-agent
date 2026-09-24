import json
from copy import deepcopy
from dataclasses import replace

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_dataset_publication import (
    DatasetPublication,
    PublicationError,
    PublicationState,
    validate_candidate,
)
from lawyer_agent.domain.legal_release_provenance import provenance_digest
from tests.unit.test_legal_dataset_publication import candidate
from tests.unit.test_mixed_release_quality import mixed_fixture


async def mixed_candidate():
    data, config, service, _ = await mixed_fixture()
    report = await service.check(tuple(d.selection for d in data), config)
    assert report.passed
    value = replace(candidate(), parser_version=config.parser_version)
    value.manifest.update(
        model_ref=config.model_ref,
        dimension=config.dimension,
        version_ids=[str(d.selection.version_id) for d in data],
        quality_sha256=report.digest,
        selection_sha256=config.selection_sha256,
        review_ref="review:synthetic-mixed",
        normalization="l2",
        release_selection_provenance=dict(
            schema_version="release-provenance-ref-v1",
            manifest_sha256=config.provenance.manifest_sha256,
            selection_sha256=config.provenance.selection_sha256,
            provenance_sha256=provenance_digest(config.provenance),
        ),
    )
    value.quality_metrics["release_quality"] = report.to_dict()
    return value


async def test_mixed_candidate_survives_json_journal_roundtrip_without_duplicate_proof():
    value = await mixed_candidate()
    validate_candidate(value)
    recovered = replace(
        value,
        manifest=json.loads(json.dumps(value.manifest)),
        quality_metrics=json.loads(json.dumps(value.quality_metrics)),
    )
    record = DatasetPublication(new_uuid7(), recovered, None, PublicationState.READY)
    assert record.candidate == recovered
    assert "entries" not in record.candidate.manifest["release_selection_provenance"]


@pytest.mark.parametrize(
    "mutation",
    [
        "summary",
        "facts",
        "ref",
        "proof",
        "order",
        "schema",
        "remove_quality",
        "remove_ref",
        "legacy_ranges",
        "dimension",
        "review",
        "source",
        "duplicate_facts",
    ],
)
async def test_mixed_candidate_rejects_tampering_and_downgrade(mutation):
    value = await mixed_candidate()
    report = value.quality_metrics["release_quality"]
    if mutation == "summary":
        report["versions"][0]["manual_review"] += ("changed",)
    elif mutation == "facts":
        report["version_digests"][0]["facts_sha256"] = "0" * 64
    elif mutation == "ref":
        value.manifest["release_selection_provenance"]["provenance_sha256"] = "0" * 64
    elif mutation == "proof":
        report["configuration"]["provenance"]["entries"][0]["row"] = 2
    elif mutation == "order":
        value.manifest["version_ids"].reverse()
    elif mutation == "schema":
        report["schema_version"] = "release-quality-v1"
    elif mutation == "remove_quality":
        value.quality_metrics.pop("release_quality")
        for field in ("quality_sha256", "selection_sha256", "review_ref"):
            value.manifest.pop(field)
    elif mutation == "remove_ref":
        value.manifest.pop("release_selection_provenance")
    elif mutation == "legacy_ranges":
        value.manifest["release_reports"] = []
    elif mutation == "dimension":
        report["configuration"]["dimension"] = True
    elif mutation == "review":
        value.manifest["review_ref"] = ""
    elif mutation == "source":
        report["versions"][0]["source"]["source_sha256"] = "0" * 64
    else:
        report["version_digests"][1] = deepcopy(report["version_digests"][0])
    with pytest.raises(PublicationError, match="publication_invalid_candidate"):
        validate_candidate(value)
