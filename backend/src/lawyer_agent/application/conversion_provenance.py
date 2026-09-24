"""Typed, non-executable provenance for converted offline corpus inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

STATIC_CONVERTER_VERSION = "binary-word-static-text-v1"
STATIC_CONVERSION_FLAGS = frozenset({
    "legacy_binary_static_text_recovery", "original_format_not_preserved",
    "field_results_static_not_recalculated", "header_section_layout_not_preserved",
    "original_layout_and_visibility_not_preserved",
    "images_objects_and_automatic_numbering_not_recovered",
})
OFFICE_VALIDATION_FLAG = "original_office_validation_failed"
CONVERSION_QUALITY_FLAGS = STATIC_CONVERSION_FLAGS | {OFFICE_VALIDATION_FLAG}


@dataclass(frozen=True, slots=True)
class ConversionProvenance:
    converter_version: str | None
    converter_fingerprint: str | None
    recovery_reason: Literal["office_validation_failed"] | None

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> ConversionProvenance:
        version = record.get("converter_version")
        fingerprint = record.get("converter_fingerprint")
        reason = record.get("recovery_reason")
        for value in (version, fingerprint):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("invalid_conversion_provenance")
        if version == STATIC_CONVERTER_VERSION and fingerprint is None:
            raise ValueError("invalid_conversion_provenance")
        if reason is not None and (
            reason != "office_validation_failed" or version != STATIC_CONVERTER_VERSION
        ):
            raise ValueError("invalid_conversion_provenance")
        assert version is None or isinstance(version, str)
        assert fingerprint is None or isinstance(fingerprint, str)
        return cls(version, fingerprint, "office_validation_failed" if reason else None)

    @property
    def quality_flags(self) -> frozenset[str]:
        flags = (STATIC_CONVERSION_FLAGS if self.converter_version == STATIC_CONVERTER_VERSION
                 else frozenset())
        return flags | {OFFICE_VALIDATION_FLAG} if self.recovery_reason else flags
