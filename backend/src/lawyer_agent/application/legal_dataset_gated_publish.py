"""Quality-gated dataset index publish orchestration (non-model glue).

spec 6.6: a version may only be turned into a new dataset release after the
automatic quality check passes. The raw index-publish orchestration builds an
index, re-points the dataset alias and records a PUBLISHED snapshot, but it
never validates the authoritative DB provisions first, so an empty or broken
version could still reach the alias. This composition runs the shared
`LegalCorpusQualityGate` over the DB version's provisions before touching the
indexer; when the gate refuses, nothing is indexed, aliased or recorded.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lawyer_agent.application.legal_corpus_publish import (
    LegalCorpusQualityGate,
    QualityReport,
)
from lawyer_agent.application.legal_index_publish import (
    DatasetPublishResult,
    LegalDatasetPublishError,
)
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    Provision,
)


class LegalVersionReadPort(Protocol):
    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

    async def provisions_for_version(
        self, version_id: UUID
    ) -> tuple[Provision, ...]: ...


class _PublishCallable(Protocol):
    async def publish_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        alias: str,
        model_ref: str,
        dimension: int,
        batch_size: int,
    ) -> DatasetPublishResult: ...


class LegalDatasetGatePublishService:
    """Runs the quality gate, then delegates to the raw index publisher."""

    def __init__(
        self,
        gate: LegalCorpusQualityGate,
        corpus: LegalVersionReadPort,
        publish: _PublishCallable,
    ) -> None:
        if not isinstance(gate, LegalCorpusQualityGate):
            raise ValueError("gated dataset publish requires a quality gate")
        if not hasattr(corpus, "version_with_instrument"):
            raise ValueError("gated dataset publish requires a corpus read port")
        if not hasattr(publish, "publish_version"):
            raise ValueError("gated dataset publish requires an index publish service")
        self._gate = gate
        self._corpus = corpus
        self._publish = publish

    async def publish_version(
        self,
        *,
        version_id: UUID,
        index_name: str,
        alias: str,
        model_ref: str,
        dimension: int,
        batch_size: int = 64,
    ) -> DatasetPublishResult:
        report = await self._quality_report(version_id)
        if not report.passed:
            issues = " ".join(report.issues)
            raise LegalDatasetPublishError(
                f"quality gate refused dataset publish: {issues}"
            )
        return await self._publish.publish_version(
            version_id=version_id,
            index_name=index_name,
            alias=alias,
            model_ref=model_ref,
            dimension=dimension,
            batch_size=batch_size,
        )

    async def _quality_report(self, version_id: UUID) -> QualityReport:
        loaded = await self._corpus.version_with_instrument(version_id)
        if loaded is None:
            raise LegalDatasetPublishError(
                f"no legal version exists for dataset publish: {version_id}"
            )
        provisions = await self._corpus.provisions_for_version(version_id)
        article_numbers = tuple(provision.provision_no for provision in provisions)
        return self._gate.evaluate(
            article_count=len(provisions),
            article_numbers=article_numbers,
            required_field_missing=(),
            parse_failures=0,
        )
