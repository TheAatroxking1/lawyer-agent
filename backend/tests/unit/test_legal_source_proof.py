from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from lawyer_agent.application.conversion_provenance import (
    OFFICE_VALIDATION_FLAG,
    STATIC_CONVERSION_FLAGS,
    STATIC_CONVERTER_VERSION,
)
from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalImportResult,
)
from lawyer_agent.application.legal_source_proof import LegalSourceProofService
from lawyer_agent.domain.legal_source_proof import LegalSourceProof


def proof() -> LegalSourceProof:
    return LegalSourceProof(
        source_ref="file:///合成原件.docx", input_ref="file:///合成原件.docx",
        source_sha256=b"a" * 32, input_sha256=b"a" * 32, structure_sha256=b"b" * 32,
        loader_version="loader-v1", parser_version="parser-v1",
    )


@pytest.mark.parametrize("field", ["source_sha256", "input_sha256", "structure_sha256"])
@pytest.mark.parametrize("invalid", [b"", b"a" * 31, b"a" * 33, "a" * 32, bytearray(32), None])
def test_hashes_are_strict_bytes32(field: str, invalid: object) -> None:
    with pytest.raises(ValueError):
        replace(proof(), **{field: invalid})


@pytest.mark.parametrize("field,limit", [
    ("source_ref", 4096), ("input_ref", 4096),
    ("loader_version", 64), ("parser_version", 64), ("converter_version", 128),
])
@pytest.mark.parametrize("invalid", ["", "  ", None, 42])
def test_text_fields_require_nonempty_strings(field: str, limit: int, invalid: object) -> None:
    base = replace(proof(), converter_version="converter-v1", converter_fingerprint="a" * 64)
    with pytest.raises(ValueError):
        replace(base, **{field: invalid})


@pytest.mark.parametrize("field,limit", [
    ("source_ref", 4096), ("input_ref", 4096),
    ("loader_version", 64), ("parser_version", 64), ("converter_version", 128),
])
def test_text_field_limits(field: str, limit: int) -> None:
    base = replace(proof(), converter_version="converter-v1", converter_fingerprint="a" * 64)
    assert getattr(replace(base, **{field: "x" * limit}), field) == "x" * limit
    with pytest.raises(ValueError):
        replace(base, **{field: "x" * (limit + 1)})


@pytest.mark.parametrize("change", [
    {"input_ref": "file:///derived.docx"}, {"input_sha256": b"c" * 32},
    {"converter_version": "converter-v1"}, {"converter_fingerprint": "a" * 64},
])
def test_derivation_requires_complete_conversion_identity(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(proof(), **change)


@pytest.mark.parametrize("fingerprint", ["A" * 64, "z" * 64, "a" * 63, "a" * 65, 42, ""])
def test_converter_fingerprint_is_lowercase_sha256(fingerprint: object) -> None:
    with pytest.raises(ValueError):
        replace(proof(), converter_version="converter-v1", converter_fingerprint=fingerprint)


def test_converted_proof_retains_both_identities() -> None:
    converted = replace(proof(), input_ref="file:///derived.docx", input_sha256=b"c" * 32,
                        converter_version="converter-v1", converter_fingerprint="a" * 64)
    assert converted.source_sha256 != converted.input_sha256


@pytest.mark.parametrize("version,reason", [
    (None, "office_validation_failed"), ("word-v1", "office_validation_failed"),
    (STATIC_CONVERTER_VERSION, "unknown"),
])
def test_recovery_reason_only_valid_for_static_version(version: str | None, reason: str) -> None:
    with pytest.raises(ValueError):
        replace(proof(), converter_version=version,
                converter_fingerprint="a" * 64 if version else None, recovery_reason=reason)


@pytest.mark.parametrize("reason", [None, "office_validation_failed"])
def test_static_proof_preserves_limitations(reason: str | None) -> None:
    static = replace(proof(), converter_version=STATIC_CONVERTER_VERSION,
                     converter_fingerprint="a" * 64, recovery_reason=reason)
    expected = STATIC_CONVERSION_FLAGS | ({OFFICE_VALIDATION_FLAG} if reason else set())
    assert set(static.quality_flags) == expected


@pytest.mark.parametrize("flags", [["flag"], ("",), ("  ",), (42,), ("x" * 129,),
                                  tuple(f"flag-{i}" for i in range(65))])
def test_quality_flags_are_bounded_nonempty_text_tuple(flags: object) -> None:
    with pytest.raises(ValueError):
        replace(proof(), quality_flags=flags)


def test_quality_flags_are_canonical_and_proof_is_frozen() -> None:
    item = replace(proof(), quality_flags=("z", "a", "z"))
    assert item.quality_flags == ("a", "z")
    assert not hasattr(item, "__dict__")
    with pytest.raises(FrozenInstanceError):
        item.loader_version = "changed"


def test_static_limitations_count_toward_quality_flag_limit() -> None:
    with pytest.raises(ValueError, match="invalid_source_proof_quality_flags"):
        replace(proof(), converter_version=STATIC_CONVERTER_VERSION,
                converter_fingerprint="a" * 64,
                quality_flags=tuple(f"flag-{i}" for i in range(64)))


class Repository:
    def __init__(self, existing: LegalSourceProof | None = None) -> None:
        self.existing = existing
        self.reads: list[UUID] = []
        self.writes: list[tuple[UUID, LegalSourceProof]] = []

    async def find_for_version(self, version_id: UUID) -> LegalSourceProof | None:
        self.reads.append(version_id)
        return self.existing

    async def create_for_version(self, version_id: UUID, item: LegalSourceProof) -> None:
        self.writes.append((version_id, item))


async def test_new_version_creates_proof_once() -> None:
    repository = Repository()
    imported = LegalImportResult(uuid4(), uuid4(), False)
    await LegalSourceProofService(repository).ensure(imported, proof())
    assert repository.reads == [imported.version_id]
    assert repository.writes == [(imported.version_id, proof())]


async def test_identical_replay_only_reads() -> None:
    repository = Repository(proof())
    imported = LegalImportResult(uuid4(), uuid4(), True)
    await LegalSourceProofService(repository).ensure(imported, proof())
    assert repository.reads == [imported.version_id]
    assert repository.writes == []


async def test_missing_replay_requires_review_without_backfill() -> None:
    repository = Repository()
    with pytest.raises(LegalCorpusImportConflict, match="^source_proof_missing_requires_review$"):
        await LegalSourceProofService(repository).ensure(
            LegalImportResult(uuid4(), uuid4(), True), proof())
    assert repository.writes == []


@pytest.mark.parametrize("change", [
    {"source_ref": "file:///other.docx"}, {"input_ref": "file:///other.docx"},
    {"source_sha256": b"d" * 32}, {"input_sha256": b"d" * 32},
    {"structure_sha256": b"d" * 32}, {"loader_version": "loader-v2"},
    {"parser_version": "parser-v2"}, {"converter_version": "converter-v2"},
    {"converter_fingerprint": "b" * 64}, {"quality_flags": ("needs_review",)},
])
async def test_every_proof_identity_change_rejects_replay(change: dict[str, object]) -> None:
    original = replace(proof(), converter_version="converter-v1", converter_fingerprint="a" * 64)
    repository = Repository(original)
    with pytest.raises(LegalCorpusImportConflict, match="^source_proof_conflict$"):
        await LegalSourceProofService(repository).ensure(
            LegalImportResult(uuid4(), uuid4(), True), replace(original, **change))
    assert repository.existing == original
    assert repository.writes == []


@pytest.mark.parametrize("existing", [proof(), replace(proof(), structure_sha256=b"z" * 32)])
async def test_new_version_with_existing_proof_is_inconsistent(existing: LegalSourceProof) -> None:
    repository = Repository(existing)
    with pytest.raises(LegalCorpusImportConflict, match="^source_proof_conflict$"):
        await LegalSourceProofService(repository).ensure(
            LegalImportResult(uuid4(), uuid4(), False), proof())
    assert repository.writes == []


@pytest.mark.parametrize("replayed", [True, False])
@pytest.mark.parametrize("reason", [None, "office_validation_failed"])
async def test_static_recovery_rejected_before_any_repository_call(
    replayed: bool, reason: str | None,
) -> None:
    static = replace(proof(), converter_version=STATIC_CONVERTER_VERSION,
                     converter_fingerprint="a" * 64, recovery_reason=reason)
    repository = Repository(static)
    with pytest.raises(LegalCorpusImportConflict, match="static.*requires.*review"):
        await LegalSourceProofService(repository).ensure(
            LegalImportResult(uuid4(), uuid4(), replayed), static)
    assert repository.reads == repository.writes == []
