"""Read an explicitly selected single-source review, not an approval signature."""

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

from lawyer_agent.domain.legal_source_proof import LegalSourceProof, reviewed_static_proof
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path


class StaticRecoveryReviewError(ValueError):
    def __init__(self) -> None:
        self.code = "static_recovery_review_invalid"
        super().__init__(self.code)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _read(path: Path, digest: str) -> dict[str, Any]:
    if (not path.is_absolute() or ".." in path.parts or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise ValueError
    with unredirected_path(path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or sha256(raw).hexdigest() != digest:
        raise ValueError
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(result, dict):
        raise ValueError
    return result


def verify_static_recovery_review(
    path: Path, digest: str, proof: LegalSourceProof, *, body_paragraph_count: int,
    auxiliary_paragraph_count: int, article_count: int,
) -> LegalSourceProof:
    try:
        certificate = _read(path, digest)
        if set(certificate) != {"schema_version", "complete", "issues", "verification_path",
                               "verification_sha256"}:
            raise ValueError
        if (certificate["schema_version"] != "static-recovery-review-v1"
                or certificate["complete"] is not True or certificate["issues"] != []):
            raise ValueError
        verification = _read(Path(certificate["verification_path"]),
                             certificate["verification_sha256"])
        expected = {
            "source_path": proof.source_ref,
            "converted_path": proof.input_ref,
            "source_sha256": proof.source_sha256.hex(),
            "converted_sha256": proof.input_sha256.hex(),
            "converter_version": proof.converter_version,
            "converter_fingerprint": proof.converter_fingerprint,
            "recovery_reason": proof.recovery_reason,
        }
        for key, value in expected.items():
            actual = verification[key]
            if key.endswith("_path"):
                source_path = Path(actual)
                if not source_path.is_absolute() or ".." in source_path.parts:
                    raise ValueError
                actual = source_path.as_uri()
            if actual != value:
                raise ValueError
        for key in ("exact_main_paragraph_sequence_equal", "exact_header_paragraph_sequence_equal",
                    "exact_article_sequence_equal"):
            if verification[key] is not True:
                raise ValueError
        if verification["different_main_ordinals"] != []:
            raise ValueError
        for key, expected_count in {
            "expected_main_paragraphs": body_paragraph_count,
            "actual_main_paragraphs": body_paragraph_count,
            "expected_header_paragraphs": auxiliary_paragraph_count,
            "actual_header_paragraphs": auxiliary_paragraph_count,
            "actual_article_count": article_count,
        }.items():
            if type(verification[key]) is not int or verification[key] != expected_count:
                raise ValueError
        if body_paragraph_count <= 0 or article_count <= 0:
            raise ValueError
        if (not isinstance(verification["actual_main_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", verification["actual_main_sha256"]) is None
                or verification["actual_main_sha256"] != verification["expected_main_sha256"]):
            raise ValueError
        return reviewed_static_proof(proof, digest)
    except (OSError, ValueError, TypeError, KeyError):
        raise StaticRecoveryReviewError() from None
