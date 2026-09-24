"""Check authoritative corpus facts before any model or index work."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from lawyer_agent.application.legal_corpus_import import LegalProvisionDraft, _content_hash
from lawyer_agent.application.legal_corpus_publish import LegalCorpusQualityGate
from lawyer_agent.application.legal_index_chunks import LegalIndexChunkError, select_index_leaves
from lawyer_agent.domain.common import is_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalCategory,
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)
from lawyer_agent.domain.legal_dataset_quality import (
    DatasetQualityError,
    ReleaseConfiguration,
    ReleaseMetadataSummary,
    ReleaseQualityReport,
    ReleaseSelection,
    ReleaseSourceSummary,
    VersionQualitySummary,
    release_configuration_dict,
)
from lawyer_agent.domain.legal_parser_profiles import EXACT_TEXT_PARSER_VERSIONS
from lawyer_agent.domain.legal_release_provenance import (
    MixedReleaseConfiguration,
    MixedReleaseQualityReport,
    ReleaseReportIdentity,
    mixed_quality_digest,
)
from lawyer_agent.domain.legal_source_proof import (
    STATIC_CONVERTER_VERSION,
    LegalSourceProof,
    static_review_matches,
)


class ReleaseCorpusPort(Protocol):
    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

    async def provisions_for_version(self, version_id: UUID) -> tuple[Provision, ...]: ...


class ReleaseChunksPort(Protocol):
    async def chunks_for_version(self, version_id: UUID) -> tuple[LegalChunk, ...]: ...


class ReleaseProofsPort(Protocol):
    async def find_for_version(self, version_id: UUID) -> LegalSourceProof | None: ...


def _canonical(value: object) -> str:
    def encode(item: object) -> str:
        if isinstance(item, bytes):
            return item.hex()
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError("unsupported quality fact")

    return json.dumps(
        value,
        default=encode,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_review(
    report: ReleaseQualityReport, expected_quality_sha256: str, review_ref: str
) -> None:
    if not report.passed:
        raise DatasetQualityError("quality_checks_failed")
    if expected_quality_sha256 != report.digest:
        raise DatasetQualityError("quality_review_mismatch")
    if not isinstance(review_ref, str) or not review_ref.strip() or len(review_ref) > 512:
        raise DatasetQualityError("quality_review_required")


class ReleaseQualityService:
    """Caller supplies one repeatable-read view for checks and subsequent build."""

    def __init__(
        self, corpus: ReleaseCorpusPort, chunks: ReleaseChunksPort, proofs: ReleaseProofsPort
    ) -> None:
        self._corpus, self._chunks, self._proofs = corpus, chunks, proofs

    async def check(
        self, selections: tuple[ReleaseSelection, ...], configuration: ReleaseConfiguration
    ) -> ReleaseQualityReport:
        if (
            not isinstance(selections, tuple)
            or not selections
            or any(not isinstance(item, ReleaseSelection) for item in selections)
            or len({item.version_id for item in selections}) != len(selections)
        ):
            raise DatasetQualityError("quality_invalid_selection")
        if not isinstance(configuration, ReleaseConfiguration):
            raise DatasetQualityError("quality_invalid_configuration")
        member_reports: dict[UUID, ReleaseReportIdentity] = {}
        if isinstance(configuration, MixedReleaseConfiguration):
            provenance = configuration.provenance
            if selections != tuple(entry.selection for entry in provenance.entries):
                raise DatasetQualityError("quality_invalid_selection")
            reports_by_hash = {report.report_sha256: report for report in provenance.reports}
            member_reports = {
                entry.selection.version_id: reports_by_hash[entry.report_sha256]
                for entry in provenance.entries
            }
        summaries: list[VersionQualitySummary] = []
        version_digests: list[dict[str, str]] = []
        for selection in sorted(selections, key=lambda item: str(item.version_id)):
            member_report = member_reports.get(selection.version_id)
            member_configuration = configuration
            if member_report is not None:
                member_configuration = ReleaseConfiguration(
                    configuration.alias,
                    configuration.model_ref,
                    configuration.dimension,
                    member_report.chunk_parser_version,
                    configuration.selection_sha256,
                    configuration.normalization,
                )
            try:
                loaded = await self._corpus.version_with_instrument(selection.version_id)
                provisions = await self._corpus.provisions_for_version(selection.version_id)
                chunks = await self._chunks.chunks_for_version(selection.version_id)
                proof = await self._proofs.find_for_version(selection.version_id)
            except ValueError:
                raise DatasetQualityError("quality_source_invalid") from None
            blockers: list[str] = []
            if (
                len(provisions)
                != (selection.expected_provision_count or selection.expected_article_count)
                or sum(item.level == ProvisionLevel.ARTICLE for item in provisions)
                != selection.expected_article_count
                or len(chunks) != selection.expected_chunk_count
            ):
                blockers.append("selection_count_mismatch")
            if loaded is None:
                blockers.append("version_missing")
            else:
                version, instrument = loaded
                if member_report is not None:
                    if version.dataset_version != member_report.dataset_version:
                        blockers.append("dataset_version_mismatch")
                    if version.parser_version != member_report.parser_version:
                        blockers.append("parser_version_mismatch")
                _metadata(selection, version, instrument, blockers)
                _provisions(selection, version, provisions, blockers, proof)
            _proof(selection, member_configuration, loaded[0] if loaded else None, proof, blockers)
            _chunks(selection, member_configuration, provisions, chunks, blockers)
            degraded = sum(chunk.quality == ChunkQuality.DEGRADED for chunk in chunks)
            flags = proof.quality_flags if proof else ()
            manual = ["requires_source_review"]
            if selection.numbering_review is not None:
                manual.append("article_numbering_explicitly_reviewed")
            if loaded and _reviewed_unknown_dates(selection, loaded[0]):
                manual.append("date_unknown_user_reviewed")
            if degraded:
                manual.append("degraded_chunks_require_review")
            if flags:
                manual.append("source_quality_flags_require_review")
            summary = VersionQualitySummary(
                str(selection.version_id),
                tuple(sorted(set(blockers))),
                tuple(manual),
                flags,
                len(provisions),
                len(chunks),
                degraded,
                _metadata_summary(*loaded) if loaded else None,
                ReleaseSourceSummary(
                    selection.source_ref,
                    selection.source_sha256,
                    selection.input_sha256,
                    selection.structure_sha256,
                    selection.metadata_review_ref,
                    selection.expected_article_count,
                    selection.expected_chunk_count,
                    selection.date_review_ref,
                    selection.content_mode,
                    selection.expected_provision_count,
                    selection.static_review_sha256,
                    selection.numbering_review,
                ),
            )
            summaries.append(summary)
            version_facts: dict[str, Any] = {
                "selection": asdict(selection),
                "version": asdict(loaded[0]) if loaded else None,
                "instrument": asdict(loaded[1]) if loaded else None,
                "proof": asdict(proof) if proof else None,
                "provisions": sorted(
                    (_provision_semantic(item) for item in provisions), key=_canonical
                ),
                "chunks": _chunk_semantics(chunks),
                "summary": asdict(summary),
            }
            if selection.content_mode == "articles" and selection.expected_provision_count is None:
                for values in (version_facts["selection"], version_facts["summary"]["source"]):
                    values.pop("content_mode")
                    values.pop("expected_provision_count")
            if selection.static_review_sha256 is None:
                for values in (version_facts["selection"], version_facts["summary"]["source"]):
                    values.pop("static_review_sha256")
            if selection.numbering_review is None:
                for values in (version_facts["selection"], version_facts["summary"]["source"]):
                    values.pop("numbering_review")
            version_digests.append(
                {
                    "version_id": str(selection.version_id),
                    "facts_sha256": sha256(_canonical(version_facts).encode("utf-8")).hexdigest(),
                }
            )
            del version_facts
        if isinstance(configuration, MixedReleaseConfiguration):
            mixed_digest = mixed_quality_digest(
                configuration, tuple(version_digests), tuple(summaries)
            )
            return MixedReleaseQualityReport(
                not any(item.blockers for item in summaries),
                mixed_digest,
                tuple(summaries),
                configuration,
                version_digests=tuple(version_digests),
            )
        digest = sha256(
            _canonical(
                {
                    "schema_version": "release-quality-v1",
                    "configuration": release_configuration_dict(configuration),
                    "version_digests": version_digests,
                }
            ).encode("utf-8")
        ).hexdigest()
        return ReleaseQualityReport(
            not any(item.blockers for item in summaries), digest, tuple(summaries), configuration
        )


def _metadata_summary(version: LegalVersion, instrument: LegalInstrument) -> ReleaseMetadataSummary:
    return ReleaseMetadataSummary(
        instrument.title,
        instrument.issuing_authority,
        instrument.category.value,
        instrument.jurisdiction,
        instrument.region_code,
        version.version_label,
        version.status.value,
        version.published_on.isoformat() if version.published_on else None,
        version.effective_on.isoformat() if version.effective_on else None,
        version.repealed_on.isoformat() if version.repealed_on else None,
    )


def _provision_semantic(provision: Provision) -> dict[str, Any]:
    result = asdict(provision)
    result.pop("full_text")
    result["actual_text_sha256"] = content_sha256(provision.full_text).hex()
    return result


def _reviewed_unknown_dates(selection: ReleaseSelection, version: LegalVersion) -> bool:
    return (
        version.status == LegalVersionStatus.CURRENT
        and selection.date_review_ref is not None
        and (version.published_on is None or version.effective_on is None)
        and (version.published_on is None or isinstance(version.published_on, date))
        and (version.effective_on is None or isinstance(version.effective_on, date))
    )


def _metadata(
    selection: ReleaseSelection,
    version: LegalVersion,
    instrument: LegalInstrument,
    blockers: list[str],
) -> None:
    if (
        version.id != selection.version_id
        or version.instrument_id != instrument.id
        or version.instrument_id != selection.expected_instrument_id
    ):
        blockers.append("version_scope_invalid")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            instrument.title,
            instrument.issuing_authority,
            instrument.jurisdiction,
            version.version_label,
        )
    ):
        blockers.append("required_metadata_missing")
    if instrument.category == LegalCategory.UNKNOWN:
        blockers.append("category_unknown")
    # These are the explicit markers currently used by the mainland corpus.
    # Additional markers require reviewed support, never inferred jurisdiction mapping.
    if instrument.jurisdiction not in ("national", "CN"):
        blockers.append("jurisdiction_unsupported")
    if instrument.category == LegalCategory.LOCAL_REGULATION and not (
        isinstance(instrument.region_code, str) and instrument.region_code.strip()
    ):
        blockers.append("region_missing")
    if version.status in (LegalVersionStatus.STATUS_UNKNOWN, LegalVersionStatus.DRAFT):
        blockers.append("effect_status_unverified")
    if not isinstance(version.published_on, date) or not isinstance(version.effective_on, date):
        if not _reviewed_unknown_dates(selection, version):
            blockers.append("legal_dates_missing")
    if version.status == LegalVersionStatus.REPEALED and not isinstance(version.repealed_on, date):
        blockers.append("repealed_date_missing")


def _proof(
    selection: ReleaseSelection,
    configuration: ReleaseConfiguration,
    version: LegalVersion | None,
    proof: LegalSourceProof | None,
    blockers: list[str],
) -> None:
    if proof is None:
        blockers.append("source_proof_missing")
        return
    if (
        proof.source_ref != selection.source_ref
        or proof.source_sha256.hex() != selection.source_sha256
        or proof.input_sha256.hex() != selection.input_sha256
        or proof.structure_sha256.hex() != selection.structure_sha256
    ):
        blockers.append("source_proof_mismatch")
    if (
        version is None
        or proof.parser_version != version.parser_version
        or configuration.parser_version
        not in (
            f"{proof.parser_version}/hierarchical-v1",
            f"{proof.parser_version}/hierarchical-v2",
        )
    ):
        blockers.append("parser_version_mismatch")
    if (
        proof.converter_version == STATIC_CONVERTER_VERSION
        or proof.recovery_reason is not None
        or "legacy_binary_static_text_recovery" in proof.quality_flags
        or "original_office_validation_failed" in proof.quality_flags
    ):
        if not static_review_matches(proof, selection.static_review_sha256):
            blockers.append("static_recovery_blocked")


def _provisions(
    selection: ReleaseSelection,
    version: LegalVersion,
    provisions: tuple[Provision, ...],
    blockers: list[str],
    proof: LegalSourceProof | None = None,
) -> None:
    if not provisions:
        blockers.append("provisions_missing")
        return
    ordered = sorted(provisions, key=lambda item: (item.char_start, item.char_end, str(item.id)))
    if len({item.id for item in provisions}) != len(provisions):
        blockers.append("provision_duplicate")
    cursor = 0
    body_mode = selection.content_mode == "non_article_document"
    proof_body = proof is not None and "non_article_document" in proof.quality_flags
    if body_mode != proof_body:
        blockers.append("content_mode_mismatch")
    if body_mode and (
        len(provisions) != 1
        or provisions[0].provision_no != "正文"
        or provisions[0].structure_path != ("正文",)
    ):
        blockers.append("non_article_structure_invalid")
    for item in ordered:
        if (
            not is_uuid7(item.id)
            or item.version_id != selection.version_id
            or item.level != (ProvisionLevel.PARAGRAPH if body_mode else ProvisionLevel.ARTICLE)
            or not item.full_text.strip()
            or item.content_hash != content_sha256(item.full_text)
        ):
            blockers.append("provision_invalid")
        if (
            type(item.char_start) is not int
            or type(item.char_end) is not int
            or item.char_start != cursor
            or item.char_end != item.char_start + len(item.full_text)
        ):
            blockers.append("provision_bounds_invalid")
        cursor = item.char_end
    if not body_mode and LegalCorpusQualityGate()._sequence_break(
        tuple(item.provision_no for item in ordered),
        numbering_review=selection.numbering_review,
    ):
        blockers.append("article_sequence_invalid")
    expected_hash = (
        _content_hash(
            tuple(
                LegalProvisionDraft(
                    item.provision_no,
                    item.level,
                    item.structure_path,
                    item.title,
                    item.full_text,
                )
                for item in ordered
            ),
            parser_version=version.parser_version,
        )
        if version.parser_version in EXACT_TEXT_PARSER_VERSIONS
        else content_sha256("".join(item.full_text for item in ordered))
    )
    if version.content_hash != expected_hash:
        blockers.append("version_content_hash_mismatch")


def _chunks(
    selection: ReleaseSelection,
    configuration: ReleaseConfiguration,
    provisions: tuple[Provision, ...],
    chunks: tuple[LegalChunk, ...],
    blockers: list[str],
) -> None:
    if not chunks:
        blockers.append("chunks_missing")
        return
    by_provision = {item.id: item for item in provisions}
    by_id = {item.id: item for item in chunks}
    try:
        leaves = select_index_leaves(chunks)
    except LegalIndexChunkError:
        blockers.append("chunk_graph_invalid")
        return
    for item in chunks:
        if (
            not is_uuid7(item.id)
            or item.version_id != selection.version_id
            or item.provision_id not in by_provision
            or not item.content.strip()
            or item.content_hash != content_sha256(item.content)
            or item.parser_version != configuration.parser_version
        ):
            blockers.append("chunk_invalid")
        start, end = item.parent_relative_char_start, item.parent_relative_char_end
        if start is not None and end is not None:
            provision = by_provision.get(item.provision_id)
            if (
                provision is None
                or start < 0
                or end > len(provision.full_text)
                or provision.full_text[start:end] != item.content
            ):
                blockers.append("chunk_position_invalid")
        elif configuration.parser_version.endswith("/hierarchical-v2"):
            blockers.append("chunk_position_missing")
        if item.parent_chunk_id is not None:
            parent = by_id[item.parent_chunk_id]
            if item.content not in parent.content:
                blockers.append("child_text_outside_parent")
            if start is not None and end is not None:
                if (
                    parent.parent_relative_char_start is None
                    or parent.parent_relative_char_end is None
                    or start < parent.parent_relative_char_start
                    or end > parent.parent_relative_char_end
                ):
                    blockers.append("chunk_position_outside_parent")
    for provision in provisions:
        roots = [
            item
            for item in chunks
            if item.provision_id == provision.id and item.parent_chunk_id is None
        ]
        if (
            len(roots) != 1
            or roots[0].chunk_type != ChunkType.PROVISION
            or roots[0].content != provision.full_text
        ):
            blockers.append("article_parent_invalid")
            continue
        covered = bytearray(len(provision.full_text))
        ambiguous = False
        for leaf in leaves:
            if leaf.provision_id != provision.id:
                continue
            if (
                leaf.parent_relative_char_start is not None
                and leaf.parent_relative_char_end is not None
            ):
                start, end = leaf.parent_relative_char_start, leaf.parent_relative_char_end
                if (
                    start < 0
                    or end > len(provision.full_text)
                    or provision.full_text[start:end] != leaf.content
                ):
                    blockers.append("leaf_position_invalid")
                    continue
                covered[start:end] = b"\1" * (end - start)
                continue
            start = provision.full_text.find(leaf.content)
            if start < 0:
                blockers.append("leaf_text_outside_article")
                continue
            if provision.full_text.find(leaf.content, start + 1) >= 0:
                ambiguous = True
                continue
            covered[start : start + len(leaf.content)] = b"\1" * len(leaf.content)
        if any(
            not covered[i] and not character.isspace()
            for i, character in enumerate(provision.full_text)
        ):
            blockers.append("leaf_coverage_ambiguous" if ambiguous else "leaf_coverage_gap")


def _chunk_semantics(chunks: tuple[LegalChunk, ...]) -> list[dict[str, Any]]:
    """Bind multiplicity and parent semantic paths, excluding regenerated chunk IDs."""
    by_id = {item.id: item for item in chunks}

    def semantic(item: LegalChunk) -> dict[str, Any]:
        values = asdict(item)
        if item.parent_relative_char_start is None and item.parent_relative_char_end is None:
            values.pop("parent_relative_char_start")
            values.pop("parent_relative_char_end")
        values.pop("id")
        values.pop("parent_chunk_id")
        values.pop("content")
        values["actual_text_sha256"] = content_sha256(item.content).hex()
        return values

    children: dict[UUID, list[UUID]] = {item.id: [] for item in chunks}
    for item in chunks:
        if item.parent_chunk_id in children:
            children[item.parent_chunk_id].append(item.id)
    remaining = {identifier: len(values) for identifier, values in children.items()}
    pending = [identifier for identifier, count in remaining.items() if count == 0]
    subtree: dict[UUID, str] = {}
    while pending:
        identifier = pending.pop()
        item = by_id[identifier]
        subtree[identifier] = sha256(
            _canonical(
                {
                    "node": semantic(item),
                    "children": sorted(subtree[child] for child in children[identifier]),
                }
            ).encode("utf-8")
        ).hexdigest()
        parent_id = item.parent_chunk_id
        if parent_id in remaining:
            remaining[parent_id] -= 1
            if remaining[parent_id] == 0:
                pending.append(parent_id)

    result = []
    for item in chunks:
        path: list[object] = []
        seen = {item.id}
        parent_id = item.parent_chunk_id
        while parent_id is not None:
            if parent_id in seen:
                path.append("cycle")
                break
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                path.append("missing_parent")
                break
            path.append(semantic(parent))
            parent_id = parent.parent_chunk_id
        result.append(
            {
                "node": semantic(item),
                "ancestor_path": path,
                "subtree_sha256": subtree.get(item.id, "invalid_cycle"),
            }
        )
    return sorted(result, key=_canonical)
