import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from lawyer_agent.application.legal_dataset_publication import LegalDatasetPublicationService
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState
from lawyer_agent.domain.legal_dataset_publication import (
    DatasetPublication,
    PublicationError,
    PublicationState,
    validate_reviewed_candidate,
)
from lawyer_agent.domain.legal_navigation import navigation_index_name


def candidate():
    return DatasetSnapshot(
        id=new_uuid7(),
        dataset_name="laws",
        parser_version="parser-v1",
        state=DatasetState.PENDING,
        manifest={
            "alias": "laws",
            "index_name": "laws-new",
            "version_ids": [str(new_uuid7())],
            "model_ref": "synthetic",
            "dimension": 3,
            "indexed_documents": 2,
            "navigation_index": navigation_index_name("laws-new"),
            "navigation_schema_version": 1,
            "navigation_documents": 2,
        },
        quality_metrics={"indexed_documents": 2},
    )


def reviewed_candidate():
    value = candidate()
    value.manifest.update(quality_sha256="a" * 64, selection_sha256="b" * 64,
                          review_ref="synthetic-review", normalization="l2")
    value.quality_metrics["release_quality"] = {
        "schema_version": "release-quality-v1", "passed": True,
        "quality_sha256": "a" * 64, "source_text_coverage": "requires_source_review",
        "configuration": {
            "alias": value.dataset_name, "model_ref": value.manifest["model_ref"],
            "dimension": value.manifest["dimension"], "parser_version": value.parser_version,
            "selection_sha256": "b" * 64, "normalization": "l2",
        },
        "versions": [{"version_id": value.manifest["version_ids"][0], "blockers": []}],
    }
    return value


def test_reviewed_candidate_requires_bound_quality_report():
    validate_reviewed_candidate(reviewed_candidate())
    with pytest.raises(PublicationError, match="publication_quality_review_required"):
        validate_reviewed_candidate(candidate())


def test_release_report_provenance_must_match_reviewed_configuration():
    value = reviewed_candidate()
    members = [{
        "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
        "selection_start": 0, "selection_count": 1,
    }]
    value.manifest["release_reports"] = members
    value.quality_metrics["release_quality"]["configuration"]["report_members"] = deepcopy(members)
    validate_reviewed_candidate(value)
    value.manifest["release_reports"][0]["report_sha256"] = "d" * 64
    with pytest.raises(PublicationError, match="publication_invalid_candidate"):
        validate_reviewed_candidate(value)


@pytest.mark.parametrize(
    "members",
    [
        [{
            "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
            "selection_start": 0, "selection_count": 2,
        }],
        [{
            "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
            "selection_start": 1, "selection_count": 1,
        }],
        [
            {
                "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
                "selection_start": 0, "selection_count": 1,
            },
            {
                "report_path": "C:/reports/two.jsonl", "report_sha256": "d" * 64,
                "selection_start": 2, "selection_count": 1,
            },
        ],
        [
            {
                "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
                "selection_start": 0, "selection_count": 1,
            },
            {
                "report_path": "C:/reports/two.jsonl", "report_sha256": "d" * 64,
                "selection_start": 0, "selection_count": 1,
            },
        ],
        [{
            "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
            "selection_start": 0, "selection_count": True,
        }],
        [{
            "report_path": "relative/report.jsonl", "report_sha256": "c" * 64,
            "selection_start": 0, "selection_count": 1,
        }],
        [{
            "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
            "selection_start": 0, "selection_count": 1, "extra": "unexpected",
        }],
        [{
            "report_path": "C:/reports/one.jsonl", "report_sha256": "C" * 64,
            "selection_start": 0, "selection_count": 1,
        }],
        [{
            "report_path": "C:/reports/one.jsonl", "report_sha256": "c" * 64,
            "selection_start": 0,
        }],
    ],
)
def test_coordinated_malformed_release_report_provenance_is_rejected(members):
    value = reviewed_candidate()
    if len(members) == 2:
        version = str(new_uuid7())
        value.manifest["version_ids"].append(version)
        value.quality_metrics["release_quality"]["versions"].append(
            {"version_id": version, "blockers": []}
        )
    value.manifest["release_reports"] = deepcopy(members)
    value.quality_metrics["release_quality"]["configuration"]["report_members"] = (
        deepcopy(members)
    )
    with pytest.raises(PublicationError, match="publication_invalid_candidate"):
        validate_reviewed_candidate(value)


@pytest.mark.parametrize("malformed", [False, 0, "", {}, None])
def test_falsy_malformed_report_members_are_not_treated_as_legacy_missing(malformed):
    value = reviewed_candidate()
    value.quality_metrics["release_quality"]["configuration"]["report_members"] = malformed
    with pytest.raises(PublicationError, match="publication_invalid_candidate"):
        validate_reviewed_candidate(value)


def test_legacy_report_members_may_be_missing_or_an_explicit_empty_list():
    missing = reviewed_candidate()
    validate_reviewed_candidate(missing)
    empty = reviewed_candidate()
    empty.quality_metrics["release_quality"]["configuration"]["report_members"] = []
    validate_reviewed_candidate(empty)


@pytest.mark.parametrize("defect", [
    "digest", "failed", "scope", "blocker", "ref", "missing", "model", "boolean_dimension",
])
def test_partial_or_contradictory_review_metadata_cannot_enter_publication(defect):
    value = reviewed_candidate()
    report = value.quality_metrics["release_quality"]
    if defect == "digest":
        report["quality_sha256"] = "b" * 64
    elif defect == "failed":
        report["passed"] = False
    elif defect == "scope":
        report["versions"][0]["version_id"] = str(new_uuid7())
    elif defect == "blocker":
        report["versions"][0]["blockers"] = ["source_proof_missing"]
    elif defect == "ref":
        value.manifest["review_ref"] = ""
    elif defect == "model":
        value.manifest["model_ref"] = "different-unreviewed-model"
    elif defect == "boolean_dimension":
        value.manifest["dimension"] = 1
        report["configuration"]["dimension"] = True
    else:
        del value.quality_metrics["release_quality"]
    with pytest.raises(PublicationError, match="publication_invalid_candidate"):
        DatasetPublication(new_uuid7(), value, None, PublicationState.READY)


class Store:
    def __init__(self):
        self.rows = {}
        self.fail_complete = False
        self.fail_ack = False
        self.completions = 0

    async def create(self, candidate, previous_target):
        if any(r.state != PublicationState.COMPLETED for r in self.rows.values()):
            raise PublicationError("publication_conflict")
        row = DatasetPublication(new_uuid7(), candidate, previous_target, PublicationState.READY)
        self.rows[row.id] = row
        return row

    async def get(self, run_id):
        return self.rows.get(run_id)

    async def transition(self, run_id, expected, target):
        await asyncio.sleep(0)
        row = self.rows[run_id]
        if row.state != expected:
            raise PublicationError("publication_conflict")
        if target == PublicationState.ACKNOWLEDGED and self.fail_ack:
            raise RuntimeError("database unavailable")
        row = DatasetPublication(row.id, row.candidate, row.previous_target, target)
        self.rows[row.id] = row
        return row

    async def complete(self, run_id):
        if self.fail_complete:
            raise RuntimeError("database unavailable")
        self.completions += 1
        return await self.transition(
            run_id, PublicationState.ACKNOWLEDGED, PublicationState.COMPLETED
        )


class Alias:
    def __init__(self):
        self.active = "laws-old"
        self.calls = 0
        self.failure = None
        self.returned_previous = "laws-old"

    async def active_dataset_index(self, alias):
        return self.active

    async def publish_dataset(self, alias, index_name):
        self.calls += 1
        self.active = index_name
        if self.failure:
            raise self.failure
        return self.returned_previous


@pytest.mark.asyncio
async def test_publish_and_completed_resume_are_idempotent():
    store, alias = Store(), Alias()
    service = LegalDatasetPublicationService(store, alias)
    row = await service.publish(candidate())
    assert row.state == PublicationState.COMPLETED
    alias.active = "future-index"
    assert await service.resume(row.id) == row
    assert alias.calls == 1
    assert store.completions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), asyncio.CancelledError()])
async def test_ambiguous_outcome_never_resends_or_completes(failure):
    store, alias = Store(), Alias()
    alias.failure = failure
    service = LegalDatasetPublicationService(store, alias)
    with pytest.raises(type(failure)):
        await service.publish(candidate())
    row = next(iter(store.rows.values()))
    assert row.state == PublicationState.SWITCHING
    with pytest.raises(PublicationError, match="publication_outcome_unknown"):
        await service.resume(row.id)
    with pytest.raises(PublicationError, match="publication_conflict"):
        await service.publish(candidate())
    assert alias.calls == 1
    assert store.completions == 0


@pytest.mark.asyncio
async def test_ack_persist_failure_is_unknown_even_if_alias_is_target():
    store, alias = Store(), Alias()
    store.fail_ack = True
    service = LegalDatasetPublicationService(store, alias)
    with pytest.raises(RuntimeError):
        await service.publish(candidate())
    row = next(iter(store.rows.values()))
    with pytest.raises(PublicationError, match="publication_outcome_unknown"):
        await service.resume(row.id)
    assert alias.calls == 1


@pytest.mark.asyncio
async def test_snapshot_failure_can_resume_without_alias_retry():
    store, alias = Store(), Alias()
    store.fail_complete = True
    service = LegalDatasetPublicationService(store, alias)
    with pytest.raises(RuntimeError):
        await service.publish(candidate())
    row = next(iter(store.rows.values()))
    assert row.state == PublicationState.ACKNOWLEDGED
    store.fail_complete = False
    assert (await service.resume(row.id)).state == PublicationState.COMPLETED
    assert alias.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [PublicationState.READY, PublicationState.ACKNOWLEDGED])
async def test_alias_drift_blocks_write_and_completion(state):
    store, alias = Store(), Alias()
    row = await store.create(candidate(), "laws-old")
    store.rows[row.id] = DatasetPublication(row.id, row.candidate, row.previous_target, state)
    alias.active = "unexpected"
    with pytest.raises(PublicationError, match="publication_alias_conflict"):
        await LegalDatasetPublicationService(store, alias).resume(row.id)
    assert alias.calls == 0
    assert store.completions == 0


@pytest.mark.asyncio
async def test_returned_previous_mismatch_stays_switching():
    store, alias = Store(), Alias()
    alias.returned_previous = "unexpected"
    with pytest.raises(PublicationError, match="publication_alias_conflict"):
        await LegalDatasetPublicationService(store, alias).publish(candidate())
    assert next(iter(store.rows.values())).state == PublicationState.SWITCHING


@pytest.mark.asyncio
async def test_competing_resumes_send_once():
    store, alias = Store(), Alias()
    row = await store.create(candidate(), "laws-old")
    service = LegalDatasetPublicationService(store, alias)
    results = await asyncio.gather(
        service.resume(row.id), service.resume(row.id), return_exceptions=True
    )
    assert sum(isinstance(r, PublicationError) for r in results) == 1
    assert alias.calls == 1


@pytest.mark.asyncio
async def test_missing_run_is_stable_error():
    with pytest.raises(PublicationError, match="publication_not_found"):
        await LegalDatasetPublicationService(Store(), Alias()).resume(new_uuid7())


@pytest.mark.parametrize(
    "field,value",
    [
        ("dimension", True),
        ("indexed_documents", 0),
        ("navigation_documents", -1),
        ("navigation_schema_version", True),
        ("navigation_schema_version", 2),
        ("navigation_index", "unrelated"),
        ("model_ref", " "),
        ("version_ids", []),
        ("version_ids", ["invalid"]),
        ("index_name", "laws"),
        ("alias", "invalid*"),
    ],
)
def test_invalid_manifest_rejected(field, value):
    snapshot = candidate()
    snapshot.manifest[field] = value
    with pytest.raises(PublicationError, match="publication_invalid_candidate"):
        DatasetPublication(new_uuid7(), snapshot, "laws-old", PublicationState.READY)


def test_duplicate_versions_rejected():
    snapshot = candidate()
    snapshot.manifest["version_ids"] *= 2
    with pytest.raises(PublicationError):
        DatasetPublication(new_uuid7(), snapshot, None, PublicationState.READY)


def test_candidate_is_defensively_copied_on_input_and_access():
    snapshot = candidate()
    row = DatasetPublication(new_uuid7(), snapshot, "laws-old", PublicationState.READY)
    snapshot.manifest["version_ids"].clear()
    exposed = row.candidate
    exposed.manifest["version_ids"].clear()
    exposed.quality_metrics.clear()
    assert len(row.candidate.manifest["version_ids"]) == 1
    assert row.candidate.quality_metrics == {"indexed_documents": 2}


@pytest.mark.parametrize(
    "changes",
    [
        {"state": DatasetState.PUBLISHED},
        {"dataset_name": "a" * 65},
        {"parser_version": "p" * 65},
    ],
)
def test_candidate_snapshot_limits(changes):
    with pytest.raises(PublicationError):
        DatasetPublication(
            new_uuid7(), replace(candidate(), **changes), None, PublicationState.READY
        )
