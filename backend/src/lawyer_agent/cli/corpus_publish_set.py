"""Check and publish a reviewed selection from a controlled import report."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from lawyer_agent.application.legal_dataset_set_publish import MAX_EMBED_BATCH_SIZE
from lawyer_agent.config import Settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from lawyer_agent.domain.legal_corpus import DatasetSnapshot
    from lawyer_agent.domain.legal_dataset_quality import ReleaseConfiguration, ReleaseSelection
    from lawyer_agent.infrastructure.documents.release_manifest import LoadedReleaseManifest
    from lawyer_agent.infrastructure.documents.release_set_manifest import LoadedReleaseSetManifest
    from lawyer_agent.infrastructure.documents.release_set_v3 import LoadedMixedReleaseSet


class ReleaseInputError(ValueError):
    pass


def require_review_arguments(args: argparse.Namespace, *, check: bool) -> None:
    if check:
        return
    if (
        not isinstance(args.review_ref, str)
        or not args.review_ref.strip()
        or len(args.review_ref) > 512
    ):
        raise ReleaseInputError("review_ref_required")
    if (
        not isinstance(args.expected_quality_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", args.expected_quality_sha256) is None
    ):
        raise ReleaseInputError("expected_quality_sha256_required")


def require_build_arguments(*, index_name: str | None, alias: str, batch_size: int) -> None:
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_EMBED_BATCH_SIZE:
        raise ReleaseInputError("batch_size_invalid")
    if not isinstance(alias, str) or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", alias) is None:
        raise ReleaseInputError("alias_invalid")
    if index_name is not None and (
        not isinstance(index_name, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,254}", index_name) is None
        or index_name == alias
    ):
        raise ReleaseInputError("index_name_invalid")


def require_cache_directory(directory: Path | None) -> None:
    if directory is None:
        return
    from lawyer_agent.infrastructure.providers.embedding_cache import reject_reparse_path

    allowed = Path(__file__).resolve().parents[4] / "artifacts" / "legal-corpus" / "vectors"
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise ReleaseInputError("embedding_cache_directory_invalid")
    try:
        reject_reparse_path(directory)
        if not directory.resolve().is_relative_to(allowed):
            raise ValueError("outside cache subtree")
    except (OSError, ValueError) as from_exception:
        raise ReleaseInputError("embedding_cache_directory_invalid") from from_exception


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lawyer_agent.cli.corpus_publish_set")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--import-report", type=Path)
    source.add_argument("--release-set", type=Path)
    parser.add_argument("--alias", default="dataset_v1")
    parser.add_argument("--model-ref")
    parser.add_argument("--dimension", type=int)
    parser.add_argument("--index-name")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--embedding-cache-directory", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--review-ref")
    parser.add_argument("--expected-quality-sha256")
    return parser


def load_release_input(
    args: argparse.Namespace,
) -> LoadedReleaseManifest | LoadedReleaseSetManifest | LoadedMixedReleaseSet:
    """Load either one legacy report or one ordered report set before Settings/DB."""
    if args.release_set is not None:
        from lawyer_agent.infrastructure.documents.release_set_manifest import (
            read_release_set_manifest,
        )

        return read_release_set_manifest(args.release_set)
    from lawyer_agent.infrastructure.documents.release_manifest import read_release_manifest

    return read_release_manifest(args.import_report)


def release_configuration(
    loaded: LoadedReleaseManifest | LoadedReleaseSetManifest | LoadedMixedReleaseSet,
    *,
    alias: str,
    model_ref: str,
    dimension: int,
) -> ReleaseConfiguration:
    from lawyer_agent.domain.legal_dataset_quality import ReleaseConfiguration
    from lawyer_agent.domain.legal_release_provenance import MixedReleaseConfiguration
    from lawyer_agent.infrastructure.documents.release_set_v3 import LoadedMixedReleaseSet

    if isinstance(loaded, LoadedMixedReleaseSet):
        return MixedReleaseConfiguration(
            alias,
            model_ref,
            dimension,
            loaded.chunk_parser_version,
            loaded.sha256,
            provenance=loaded.provenance,
        )
    return ReleaseConfiguration(
        alias,
        model_ref,
        dimension,
        loaded.chunk_parser_version or f"{loaded.parser_version}/hierarchical-v1",
        loaded.sha256,
        report_members=getattr(loaded, "members", ()),
    )


def release_provenance_fields(configuration: ReleaseConfiguration) -> dict[str, object]:
    from dataclasses import asdict

    from lawyer_agent.domain.legal_mixed_release_review import provenance_reference
    from lawyer_agent.domain.legal_release_provenance import MixedReleaseConfiguration

    if isinstance(configuration, MixedReleaseConfiguration):
        return {"release_selection_provenance": provenance_reference(configuration.provenance)}
    if configuration.report_members:
        return {"release_reports": [asdict(item) for item in configuration.report_members]}
    return {}


async def check_and_build(
    engine: AsyncEngine,
    settings: Settings,
    selections: tuple[ReleaseSelection, ...],
    configuration: ReleaseConfiguration,
    *,
    check: bool,
    review_ref: str | None,
    expected_quality_sha256: str | None,
    index_name: str,
    batch_size: int,
    embedding_cache_directory: Path | None = None,
) -> DatasetSnapshot | None:
    """Check and build against one explicit repeatable-read fact view."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from lawyer_agent.application.legal_dataset_quality import (
        ReleaseQualityService,
        validate_review,
    )
    from lawyer_agent.application.legal_dataset_set_publish import LegalDatasetSetPublishService
    from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
    from lawyer_agent.application.legal_navigation_index import LegalNavigationIndexService
    from lawyer_agent.application.model_gateway import ModelGateway
    from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError
    from lawyer_agent.domain.model_gateway import CallLimits
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
        SqlAlchemyLegalCorpusChunkRepository,
        SqlAlchemyLegalCorpusRepository,
    )
    from lawyer_agent.infrastructure.persistence.repositories.legal_source_proof import (
        SqlAlchemyLegalSourceProofRepository,
    )
    from lawyer_agent.infrastructure.providers.embedding import (
        LocalSentenceTransformerEmbeddingProvider,
    )
    from lawyer_agent.infrastructure.providers.embedding_cache import LocalEmbeddingCacheProvider
    from lawyer_agent.infrastructure.providers.lifecycle import close_embedding_provider
    from lawyer_agent.infrastructure.providers.recorder import LoggingModelCallRecorder
    from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    require_build_arguments(index_name=index_name, alias=configuration.alias, batch_size=batch_size)
    require_cache_directory(embedding_cache_directory)
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin(), AsyncSession(bind=connection, autoflush=False) as session:
            corpus = SqlAlchemyLegalCorpusRepository(session)
            chunks = SqlAlchemyLegalCorpusChunkRepository(session)
            report = await ReleaseQualityService(
                corpus,
                chunks,
                SqlAlchemyLegalSourceProofRepository(session),
            ).check(selections, configuration)
            print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
            if check:
                if not report.passed:
                    raise DatasetQualityError("quality_checks_failed")
                return None
            if expected_quality_sha256 is None or review_ref is None:
                raise DatasetQualityError("quality_review_required")
            validate_review(report, expected_quality_sha256, review_ref)
            search = OpenSearchRestClient(base_url=settings.opensearch_url)
            provider: LocalSentenceTransformerEmbeddingProvider | LocalEmbeddingCacheProvider
            provider = LocalSentenceTransformerEmbeddingProvider(
                model_name_or_path=configuration.model_ref,
                normalize_embeddings=True,
                device=getattr(settings, "embedding_device", "cpu"),
                local_files_only=getattr(settings, "embedding_local_files_only", True),
            )
            cache: LocalEmbeddingCacheProvider | None = None
            try:
                if embedding_cache_directory is not None:
                    cache = LocalEmbeddingCacheProvider(
                        provider,
                        cache_directory=embedding_cache_directory,
                        model_ref=configuration.model_ref,
                    )
                    provider = cache
                gateway = ModelGateway(
                    provider=provider,
                    recorder=LoggingModelCallRecorder(),
                    limits=CallLimits(timeout_seconds=180.0, max_attempts=1),
                )
                candidate = await LegalDatasetSetPublishService(
                    chunks,
                    gateway,
                    search,
                    LegalDatasetAliasService(search),
                    navigation=LegalNavigationIndexService(
                        corpus,
                        OpenSearchNavigationClient(base_url=settings.opensearch_url),
                    ),
                    dataset_parser_version=configuration.parser_version,
                ).build_set(
                    version_ids=tuple(item.version_id for item in selections),
                    index_name=index_name,
                    alias=configuration.alias,
                    model_ref=configuration.model_ref,
                    dimension=configuration.dimension,
                    batch_size=batch_size,
                )
                return replace(
                    candidate,
                    manifest={
                        **candidate.manifest,
                        "quality_sha256": report.digest,
                        "review_ref": review_ref,
                        "selection_sha256": configuration.selection_sha256,
                        "normalization": configuration.normalization,
                        **release_provenance_fields(configuration),
                    },
                    quality_metrics={
                        **candidate.quality_metrics,
                        "release_quality": report.to_dict(),
                    },
                )
            finally:
                try:
                    await close_embedding_provider(provider)
                finally:
                    if cache is not None:
                        print(json.dumps({"embedding_cache": cache.stats()}, sort_keys=True))


async def _run(args: argparse.Namespace) -> None:
    from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
    from lawyer_agent.cli.corpus_publication import publication_summary
    from lawyer_agent.cli.corpus_publish import _publish_candidate
    from lawyer_agent.infrastructure.persistence.engine import create_engine, create_session_factory
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    require_review_arguments(args, check=args.check)
    require_cache_directory(args.embedding_cache_directory)
    require_build_arguments(
        index_name=args.index_name,
        alias=args.alias,
        batch_size=args.batch_size,
    )
    selected_path = args.release_set if args.release_set is not None else args.import_report
    if selected_path is None or not selected_path.is_absolute():
        raise ReleaseInputError("release_input_must_be_absolute")
    loaded = load_release_input(args)
    settings = Settings()
    configuration = release_configuration(
        loaded,
        alias=args.alias,
        model_ref=args.model_ref or settings.embedding_model_ref,
        dimension=args.dimension if args.dimension is not None else settings.embedding_dimension,
    )
    engine = create_engine(settings)
    try:
        candidate = await check_and_build(
            engine,
            settings,
            loaded.selections,
            configuration,
            check=args.check,
            review_ref=args.review_ref,
            expected_quality_sha256=args.expected_quality_sha256,
            index_name=args.index_name or f"lawyer_dataset_{uuid4().hex[:12]}",
            batch_size=args.batch_size,
            embedding_cache_directory=args.embedding_cache_directory,
        )
        if candidate is not None:
            publication = await _publish_candidate(
                create_session_factory(engine),
                LegalDatasetAliasService(OpenSearchRestClient(base_url=settings.opensearch_url)),
                candidate,
            )
            print(json.dumps(publication_summary(publication), sort_keys=True))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_review_arguments(args, check=args.check)
        asyncio.run(_run(args))
    except ReleaseInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - sanitized CLI boundary
        from lawyer_agent.domain.legal_dataset_publication import PublicationError
        from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError
        from lawyer_agent.infrastructure.documents.release_manifest import ReleaseManifestError
        from lawyer_agent.infrastructure.documents.release_set_manifest import (
            ReleaseSetManifestError,
        )

        code = (
            exc.code
            if isinstance(
                exc,
                (
                    PublicationError,
                    DatasetQualityError,
                    ReleaseManifestError,
                    ReleaseSetManifestError,
                ),
            )
            else "publication_failed"
        )
        print(json.dumps({"code": code}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
