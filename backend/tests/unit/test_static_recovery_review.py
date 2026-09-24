import json
from dataclasses import replace
from hashlib import sha256

import pytest

from lawyer_agent.domain.legal_source_proof import LegalSourceProof


def static_proof():
    return LegalSourceProof(
        "file:///C:/source.doc", "file:///C:/derived.docx", b"a" * 32, b"b" * 32,
        b"c" * 32, "loader", "parser", "binary-word-static-text-v1", "d" * 64,
        "office_validation_failed",
    )


def test_static_review_is_bound_to_complete_proof_identity():
    from lawyer_agent.domain.legal_source_proof import (
        reviewed_static_proof,
        static_review_matches,
    )
    original = static_proof()
    reviewed = reviewed_static_proof(original, "e" * 64)
    assert static_review_matches(reviewed, "e" * 64)
    assert set(original.quality_flags) <= set(reviewed.quality_flags)
    assert not static_review_matches(reviewed, "f" * 64)
    assert not static_review_matches(replace(reviewed, structure_sha256=b"x" * 32), "e" * 64)
    assert not static_review_matches(replace(reviewed, source_sha256=b"x" * 32), "e" * 64)
    assert not static_review_matches(original, "e" * 64)


@pytest.mark.parametrize("digest", [None, " ", "approved", "e" * 63])
def test_informal_static_review_does_not_approve(digest):
    from lawyer_agent.domain.legal_source_proof import static_review_matches
    assert not static_review_matches(static_proof(), digest)


def certificate(tmp_path, changes=None, review_changes=None):
    verification = {
        "source_path": "C:/source.doc", "converted_path": "C:/derived.docx",
        "source_sha256": (b"a" * 32).hex(), "converted_sha256": (b"b" * 32).hex(),
        "converter_version": "binary-word-static-text-v1", "converter_fingerprint": "d" * 64,
        "recovery_reason": "office_validation_failed",
        "exact_main_paragraph_sequence_equal": True,
        "exact_header_paragraph_sequence_equal": True, "exact_article_sequence_equal": True,
        "different_main_ordinals": [], "expected_main_paragraphs": 2,
        "actual_main_paragraphs": 2, "expected_header_paragraphs": 0,
        "actual_header_paragraphs": 0, "actual_article_count": 1,
        "expected_main_sha256": "e" * 64, "actual_main_sha256": "e" * 64,
        **(changes or {}),
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(verification), encoding="utf-8")
    review = {"schema_version": "static-recovery-review-v1", "complete": True, "issues": [],
              "verification_path": str(evidence),
              "verification_sha256": sha256(evidence.read_bytes()).hexdigest(),
              **(review_changes or {})}
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    return path, sha256(path.read_bytes()).hexdigest()


def test_exact_review_snapshot_approves_only_matching_source(tmp_path):
    from lawyer_agent.domain.legal_source_proof import static_review_matches
    from lawyer_agent.infrastructure.documents.static_recovery_review import (
        verify_static_recovery_review,
    )
    path, digest = certificate(tmp_path)
    proof = verify_static_recovery_review(path, digest, static_proof(), body_paragraph_count=2,
                                         auxiliary_paragraph_count=0, article_count=1)
    assert static_review_matches(proof, digest)


@pytest.mark.parametrize("change", [
    {"source_sha256": "f" * 64}, {"converter_fingerprint": "f" * 64},
    {"actual_main_paragraphs": True}, {"exact_article_sequence_equal": 1},
    {"different_main_ordinals": [1]}, {"actual_main_sha256": "f" * 64},
    {"source_path": "C:/other.doc"}, {"actual_header_paragraphs": 1},
])
def test_invalid_independent_verification_rejected(tmp_path, change):
    from lawyer_agent.infrastructure.documents.static_recovery_review import (
        StaticRecoveryReviewError,
        verify_static_recovery_review,
    )
    path, digest = certificate(tmp_path, change)
    with pytest.raises(StaticRecoveryReviewError):
        verify_static_recovery_review(path, digest, static_proof(), body_paragraph_count=2,
                                     auxiliary_paragraph_count=0, article_count=1)


@pytest.mark.parametrize("change", [{"complete": False}, {"complete": 1},
                                   {"issues": ["difference"]}, {"verification_sha256": "a" * 64}])
def test_incomplete_review_rejected(tmp_path, change):
    from lawyer_agent.infrastructure.documents.static_recovery_review import (
        StaticRecoveryReviewError,
        verify_static_recovery_review,
    )
    path, digest = certificate(tmp_path, review_changes=change)
    with pytest.raises(StaticRecoveryReviewError):
        verify_static_recovery_review(path, digest, static_proof(), body_paragraph_count=2,
                                     auxiliary_paragraph_count=0, article_count=1)


async def test_service_requires_explicit_matching_review_digest():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from lawyer_agent.application.legal_corpus_import import (
        LegalCorpusImportConflict,
        LegalImportResult,
    )
    from lawyer_agent.application.legal_source_proof import LegalSourceProofService
    from lawyer_agent.domain.common import new_uuid7
    from lawyer_agent.domain.legal_source_proof import reviewed_static_proof
    repository = SimpleNamespace(find_for_version=AsyncMock(return_value=None),
                                 create_for_version=AsyncMock())
    service = LegalSourceProofService(repository)
    imported = LegalImportResult(new_uuid7(), new_uuid7(), False)
    proof = reviewed_static_proof(static_proof(), "e" * 64)
    with pytest.raises(LegalCorpusImportConflict):
        await service.ensure(imported, proof)
    await service.ensure(imported, proof, static_review_sha256="e" * 64)
    repository.create_for_version.assert_awaited_once()
