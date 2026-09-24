"""Validated source identities and structural provenance for a public legal version."""

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from re import fullmatch

STATIC_CONVERTER_VERSION = "binary-word-static-text-v1"
# Keep these domain limitations aligned with the offline conversion contract.
_STATIC_FLAGS = frozenset({
    "legacy_binary_static_text_recovery",
    "original_format_not_preserved",
    "field_results_static_not_recalculated",
    "header_section_layout_not_preserved",
    "original_layout_and_visibility_not_preserved",
    "images_objects_and_automatic_numbering_not_recovered",
})


def _validate_text(value: object, name: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid_source_proof_{name}")


@dataclass(frozen=True, slots=True)
class LegalSourceProof:
    source_ref: str
    input_ref: str
    source_sha256: bytes
    input_sha256: bytes
    structure_sha256: bytes
    loader_version: str
    parser_version: str
    converter_version: str | None = None
    converter_fingerprint: str | None = None
    recovery_reason: str | None = None
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source_ref", "input_ref"):
            _validate_text(getattr(self, name), name, 4096)
        for name in ("source_sha256", "input_sha256", "structure_sha256"):
            value = getattr(self, name)
            if not isinstance(value, bytes) or len(value) != 32:
                raise ValueError(f"invalid_source_proof_{name}")
        for name in ("loader_version", "parser_version"):
            _validate_text(getattr(self, name), name, 64)
        if (self.converter_version is None) != (self.converter_fingerprint is None):
            raise ValueError("invalid_source_proof_conversion_pair")
        if self.converter_version is not None:
            _validate_text(self.converter_version, "converter_version", 128)
            if not isinstance(self.converter_fingerprint, str) or not fullmatch(
                r"[0-9a-f]{64}", self.converter_fingerprint
            ):
                raise ValueError("invalid_source_proof_converter_fingerprint")
        if (
            self.source_ref != self.input_ref or self.source_sha256 != self.input_sha256
        ) and self.converter_version is None:
            raise ValueError("source_proof_conversion_required")
        if self.recovery_reason is not None and (
            self.recovery_reason != "office_validation_failed"
            or self.converter_version != STATIC_CONVERTER_VERSION
        ):
            raise ValueError("invalid_source_proof_recovery_reason")
        if not isinstance(self.quality_flags, tuple) or len(self.quality_flags) > 64:
            raise ValueError("invalid_source_proof_quality_flags")
        for flag in self.quality_flags:
            _validate_text(flag, "quality_flag", 128)
        review_flags = [flag for flag in self.quality_flags if flag.startswith("static_review_")]
        if len(review_flags) != len(set(review_flags)):
            raise ValueError("invalid_source_proof_quality_flags")
        flags = set(self.quality_flags)
        if self.converter_version == STATIC_CONVERTER_VERSION:
            flags.update(_STATIC_FLAGS)
        if self.recovery_reason is not None:
            flags.add("original_office_validation_failed")
        if len(flags) > 64:
            raise ValueError("invalid_source_proof_quality_flags")
        object.__setattr__(self, "quality_flags", tuple(sorted(flags)))


def _review_binding(proof: LegalSourceProof, digest: str) -> str:
    values = asdict(proof)
    values["quality_flags"] = tuple(
        flag for flag in proof.quality_flags if not flag.startswith("static_review_")
    )
    values["review_sha256"] = digest
    for key in ("source_sha256", "input_sha256", "structure_sha256"):
        values[key] = values[key].hex()
    return sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def reviewed_static_proof(proof: LegalSourceProof, digest: str) -> LegalSourceProof:
    if (proof.converter_version != STATIC_CONVERTER_VERSION
            or not isinstance(digest, str) or fullmatch(r"[0-9a-f]{64}", digest) is None
            or any(flag.startswith("static_review_") for flag in proof.quality_flags)):
        raise ValueError("invalid_static_review")
    return replace(proof, quality_flags=(*proof.quality_flags,
        "static_review_sha256:" + digest,
        "static_review_binding:" + _review_binding(proof, digest),
    ))


def static_review_matches(proof: LegalSourceProof, digest: str | None) -> bool:
    if (proof.converter_version != STATIC_CONVERTER_VERSION
            or not isinstance(digest, str) or fullmatch(r"[0-9a-f]{64}", digest) is None):
        return False
    flags = {flag for flag in proof.quality_flags if flag.startswith("static_review_")}
    return flags == {"static_review_sha256:" + digest,
                     "static_review_binding:" + _review_binding(proof, digest)}
