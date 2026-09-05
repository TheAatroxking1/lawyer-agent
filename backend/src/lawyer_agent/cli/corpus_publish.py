"""Publish a legal DOCX into the real retrieval dataset (CLI).

Runs the production pipeline end to end against the configured stack
(``LAWYER_*`` settings):

    DOCX -> LegalStructureParser -> map_parsed_articles (explicit metadata)
        -> controlled import (version + provisions)
        -> PROVISION chunk rows
        -> batch embedding (local BGE via ModelGateway)
        -> OpenSearch k-NN index
        -> atomically point the dataset alias (default ``dataset_v1``)
        -> record the dataset snapshot

Rerunning the same file + metadata replays idempotently to the same version;
publishing with a new index/alias builds a new snapshot without touching the
previous index. Real model weights load lazily on the first embed call.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from lawyer_agent.config import Settings
from lawyer_agent.domain.legal_corpus import LegalVersionStatus

_DEFAULT_PARSER_VERSION = "docx-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lawyer_agent.cli.corpus_publish",
        description="Parse, import, chunk, embed, index and publish a legal DOCX.",
    )
    parser.add_argument("--docx", type=Path, required=True, help="要发布的 .docx 文件路径")
    parser.add_argument("--instrument-title", required=True, help="法规名称（必填）")
    parser.add_argument("--issuing-authority", required=True, help="发布机关，如 全国人大")
    parser.add_argument("--jurisdiction", default="national", help="效力层级等，如 national")
    parser.add_argument("--region-code", default=None, help="行政区划代码（可为空）")
    parser.add_argument("--version-label", default=None, help="版本标签（缺省按公布日期生成）")
    parser.add_argument("--published-on", default=None, help="公布日期 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--effective-on", default=None, help="施行日期 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--repealed-on", default=None, help="废止日期 YYYY-MM-DD（可选）")
    parser.add_argument("--law-number", default=None, help="文号（可选）")
    parser.add_argument("--source-ref", default=None, help="来源引用（缺省 file://<文件名>）")
    parser.add_argument("--dataset-version", default="dataset_v1", help="数据集版本标识")
    parser.add_argument("--parser-version", default=_DEFAULT_PARSER_VERSION, help="解析器版本标识")
    parser.add_argument("--alias", default="dataset_v1", help="数据集索引别名（缺省 dataset_v1）")
    parser.add_argument("--index-name", default=None, help="OpenSearch 物理索引名（缺省自动生成）")
    parser.add_argument("--model-ref", default=None, help="embedding 模型名（缺省用 Settings）")
    parser.add_argument("--dimension", type=int, default=None, help="embedding 维度（缺省同上）")
    parser.add_argument("--batch-size", type=int, default=20, help="embedding 批大小")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except _InputError as exc:
        print(f"input invalid: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"corpus publish failed: {exc}", file=sys.stderr)
        return 1
    print("corpus publish completed")
    return 0


class _InputError(Exception):
    pass


def _parse_date(raw: str | None, *, field: str, default: date) -> date:
    if raw is None:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _InputError(f"{field} must be a YYYY-MM-DD date") from exc


async def _run(args: argparse.Namespace) -> None:
    from lawyer_agent.application.legal_corpus_chunks import derive_chunks
    from lawyer_agent.application.legal_corpus_import import LegalCorpusImportService
    from lawyer_agent.application.legal_corpus_import_mapping import (
        LegalImportMetadata,
        ParsedArticleView,
        map_parsed_articles,
    )
    from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
    from lawyer_agent.application.legal_index_publish import (
        LegalDatasetIndexPublishService,
    )
    from lawyer_agent.application.legal_vector_indexing import (
        LegalVectorIndexingService,
    )
    from lawyer_agent.application.model_gateway import ModelGateway
    from lawyer_agent.domain.model_gateway import CallLimits
    from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
    from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
    from lawyer_agent.infrastructure.persistence.engine import (
        create_engine,
        create_session_factory,
    )
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
        SqlAlchemyLegalCorpusChunkRepository,
        SqlAlchemyLegalCorpusImportRepository,
        SqlAlchemyLegalCorpusRepository,
    )
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
        SqlAlchemyLegalCorpusInventoryRepository,
    )
    from lawyer_agent.infrastructure.providers.embedding import (
        LocalSentenceTransformerEmbeddingProvider,
    )
    from lawyer_agent.infrastructure.providers.recorder import LoggingModelCallRecorder
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    if not args.docx.is_file():
        raise _InputError("--docx must point to a readable file")
    payload = args.docx.read_bytes()
    if not payload:
        raise _InputError("--docx file is empty")

    today = datetime.now(UTC).date()
    published_on = _parse_date(args.published_on, field="--published-on", default=today)
    effective_on = _parse_date(args.effective_on, field="--effective-on", default=today)
    repealed_on = _parse_date(args.repealed_on, field="--repealed-on", default=None)  # type: ignore[arg-type]
    version_label = args.version_label or f"{published_on.isoformat()} 导入版"
    source_ref = args.source_ref or f"file://{args.docx.name}"
    index_name = args.index_name or f"lawyer_dataset_{uuid4().hex[:12]}"

    settings = Settings()
    embed_model_ref = args.model_ref or settings.embedding_model_ref
    embed_dimension = (
        args.dimension if args.dimension is not None else settings.embedding_dimension
    )
    if not isinstance(embed_model_ref, str) or not embed_model_ref.strip():
        raise _InputError("embedding model ref must be non-empty text")
    if (
        isinstance(embed_dimension, bool)
        or not isinstance(embed_dimension, int)
        or embed_dimension <= 0
    ):
        raise _InputError("embedding dimension must be a positive integer")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        loader = ZipDocxLoader(source_ref=source_ref)
        document = loader.load(source_ref, payload)
        parsed = LegalStructureParser().parse(document)
        if not parsed.articles:
            raise _InputError("the DOCX contains no parseable articles")
        metadata = LegalImportMetadata(
            title=args.instrument_title.strip(),
            issuing_authority=args.issuing_authority.strip(),
            jurisdiction=args.jurisdiction.strip(),
            region_code=args.region_code.strip() if args.region_code else None,
            version_label=version_label,
            status=LegalVersionStatus.CURRENT,
            published_on=published_on,
            effective_on=effective_on,
            repealed_on=repealed_on,
            law_number=args.law_number.strip() if args.law_number else None,
            source_ref=source_ref,
            dataset_version=args.dataset_version,
            parser_version=args.parser_version,
        )
        from typing import cast

        view: tuple[ParsedArticleView, ...] = cast(
            tuple[ParsedArticleView, ...], tuple(parsed.articles)
        )
        command = map_parsed_articles(metadata, view)

        # 1) Import the version and derive its PROVISION chunks in one transaction.
        async with session_factory() as session, session.begin():
            import_repo = SqlAlchemyLegalCorpusImportRepository(session)
            imported = await LegalCorpusImportService(import_repo).import_version(command)
            corpus_repo = SqlAlchemyLegalCorpusRepository(session)
            provisions = await corpus_repo.provisions_for_version(imported.version_id)
            chunks = derive_chunks(
                version_id=imported.version_id,
                provisions=provisions,
                parser_version=metadata.parser_version,
            )
            await SqlAlchemyLegalCorpusChunkRepository(
                session
            ).replace_chunks_for_version(imported.version_id, chunks)
        print(
            f"imported instrument={imported.instrument_id} version={imported.version_id} "
            f"replayed={imported.replayed} articles={len(provisions)} chunks={len(chunks)}"
        )

        # 2) Embed, index into OpenSearch and atomically publish the alias.
        embed_gateway = ModelGateway(
            provider=LocalSentenceTransformerEmbeddingProvider(
                model_name_or_path=embed_model_ref
            ),
            recorder=LoggingModelCallRecorder(),
            limits=CallLimits(timeout_seconds=180.0, max_attempts=1),
        )
        search = OpenSearchRestClient(base_url=settings.opensearch_url)
        alias_service = LegalDatasetAliasService(search)
        # The chunk read port and the snapshot repository need a live session.
        async with session_factory() as session, session.begin():
            chunk_repo = SqlAlchemyLegalCorpusChunkRepository(session)
            indexer = LegalVectorIndexingService(
                chunks=chunk_repo,
                gateway=embed_gateway,
                search=search,
            )
            snapshot = SqlAlchemyLegalCorpusInventoryRepository(session)
            service = LegalDatasetIndexPublishService(
                indexer=indexer,
                alias=alias_service,
                snapshot=snapshot,
                dataset_parser_version=metadata.parser_version,
            )
            result = await service.publish_version(
                version_id=imported.version_id,
                index_name=index_name,
                alias=args.alias,
                model_ref=embed_model_ref,
                dimension=embed_dimension,
                batch_size=args.batch_size,
            )
        print(
            "published "
            f"index={result.index_name} alias={args.alias} "
            f"indexed_documents={result.indexed_documents} "
            f"previous_target={result.previous_target}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
