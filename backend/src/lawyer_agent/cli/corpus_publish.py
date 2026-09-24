"""Publish a legal DOCX into the real retrieval dataset (CLI).

Runs the production pipeline end to end against the configured stack
(``LAWYER_*`` settings):

    DOCX -> LegalStructureParser -> map_parsed_articles (explicit metadata)
        -> controlled import (version + provisions)
        -> hierarchical chunk rows (whole articles and trusted child units)
        -> leaf-only embedding (configured model via ModelGateway)
        -> OpenSearch k-NN index
        -> verified title/structure navigation sidecar and readiness marker
        -> atomically point the dataset alias (default ``dataset_v1``)
        -> record the dataset snapshot

Rerunning the same file + metadata replays idempotently to the same version;
publishing with a new index/alias builds a new snapshot without touching the
previous index. Real model weights load lazily on the first embed call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from lawyer_agent.application.legal_corpus_import import LegalImportCommand
from lawyer_agent.application.legal_non_article_import import ParsedBody, map_non_article_body
from lawyer_agent.config import Settings
from lawyer_agent.domain.legal_corpus import LegalCategory, LegalVersionStatus
from lawyer_agent.infrastructure.documents.parsers import ParsedArticle

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from lawyer_agent.application.legal_corpus_import import LegalImportResult
    from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
    from lawyer_agent.domain.legal_corpus import DatasetSnapshot
    from lawyer_agent.domain.legal_dataset_publication import DatasetPublication
    from lawyer_agent.domain.legal_source_proof import LegalSourceProof
    from lawyer_agent.infrastructure.documents.corpus_source import (
        CorpusConversionCatalog,
        PreparedCorpusSource,
    )

_DEFAULT_PARSER_VERSION = "corpus-docx-v3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lawyer_agent.cli.corpus_publish",
        description="Parse, import, chunk, embed, index and publish a legal DOCX.",
    )
    parser.add_argument(
        "--source", "--docx", dest="docx", type=Path, required=True,
        help="原始法规文件路径；旧 DOC 必须提供受控转换清单",
    )
    parser.add_argument("--source-root", type=Path, help="只读来源根目录（缺省为原件父目录）")
    parser.add_argument("--source-sha256", help="预期原件 SHA-256")
    parser.add_argument("--conversion-manifest", type=Path, help="受控转换 JSONL 清单")
    parser.add_argument("--converted-root", type=Path, help="转换产物根目录")
    parser.add_argument("--instrument-title", required=True, help="法规名称（必填）")
    parser.add_argument("--issuing-authority", required=True, help="发布机关，如 全国人大")
    parser.add_argument("--jurisdiction", default="national", help="效力层级等，如 national")
    parser.add_argument("--region-code", default=None, help="行政区划代码（可为空）")
    parser.add_argument(
        "--category",
        choices=[category.value for category in LegalCategory],
        default=LegalCategory.UNKNOWN.value,
        help="法规类别（缺省 unknown）",
    )
    parser.add_argument("--version-label", default=None, help="版本标签（缺省按公布日期生成）")
    parser.add_argument("--published-on", default=None, help="公布日期 YYYY-MM-DD（缺省未知）")
    parser.add_argument("--effective-on", default=None, help="施行日期 YYYY-MM-DD（缺省未知）")
    parser.add_argument("--repealed-on", default=None, help="废止日期 YYYY-MM-DD（可选）")
    parser.add_argument(
        "--status", choices=[status.value for status in LegalVersionStatus],
        default=LegalVersionStatus.STATUS_UNKNOWN.value, help="效力状态（缺省 status_unknown）",
    )
    parser.add_argument("--law-number", default=None, help="文号（可选）")
    parser.add_argument("--source-ref", default=None, help="来源引用（缺省为绝对文件 URI）")
    parser.add_argument("--dataset-version", default="dataset_v1", help="数据集版本标识")
    parser.add_argument("--parser-version", default=_DEFAULT_PARSER_VERSION, help="解析器版本标识")
    parser.add_argument("--content-mode", choices=("articles", "non_article_document"),
                        default="articles")
    parser.add_argument("--date-review-ref", default=None)
    parser.add_argument("--static-review-path", type=Path)
    parser.add_argument("--static-review-sha256")
    parser.add_argument("--alias", default="dataset_v1", help="数据集索引别名（缺省 dataset_v1）")
    parser.add_argument("--index-name", default=None, help="OpenSearch 物理索引名（缺省自动生成）")
    parser.add_argument("--model-ref", default=None, help="embedding 模型名（缺省用 Settings）")
    parser.add_argument("--dimension", type=int, default=None, help="embedding 维度（缺省同上）")
    parser.add_argument("--batch-size", type=int, default=20, help="embedding 批大小")
    parser.add_argument("--metadata-review-ref")
    parser.add_argument("--review-ref")
    parser.add_argument("--expected-quality-sha256")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quality-check", action="store_true", help="导入后检查质量，不构建发布")
    mode.add_argument("--preflight", action="store_true", help="输出预检 JSON，不连接业务服务")
    mode.add_argument(
        "--import-only",
        action="store_true",
        help="只导入+分块入库（不 embed/不发布），配合集合发布批量试点使用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _require_review_arguments(args)
        asyncio.run(_run(args))
    except _InputError as exc:
        print(f"input invalid: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - sanitized CLI boundary
        from lawyer_agent.domain.legal_dataset_publication import PublicationError
        from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError

        code = (exc.code if isinstance(exc, (PublicationError, DatasetQualityError))
                else "publication_failed")
        print(f"corpus publish failed: {code}", file=sys.stderr)
        return 1
    if not args.preflight and not args.quality_check:
        print("corpus publish completed")
    return 0


class _InputError(Exception):
    pass


def _require_review_arguments(args: argparse.Namespace) -> None:
    if args.preflight or args.import_only:
        return
    from lawyer_agent.cli.corpus_publish_set import (
        ReleaseInputError,
        require_build_arguments,
        require_review_arguments,
    )

    if (not isinstance(args.metadata_review_ref, str) or not args.metadata_review_ref.strip()
        or len(args.metadata_review_ref) > 512):
        raise _InputError("metadata_review_ref_required")
    try:
        require_review_arguments(args, check=args.quality_check)
        require_build_arguments(index_name=args.index_name, alias=args.alias,
                                batch_size=args.batch_size)
    except ReleaseInputError as exc:
        raise _InputError(str(exc)) from None


def _parse_date(raw: str | None, *, field: str) -> date | None:
    if raw is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) is None:
        raise _InputError(f"{field} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _InputError(f"{field} must be a YYYY-MM-DD date") from exc


@dataclass(frozen=True, slots=True)
class _PreparedImport:
    command: LegalImportCommand
    articles: tuple[ParsedArticle | ParsedBody, ...]
    source: PreparedCorpusSource
    structure_sha256: str
    content_mode: str = "articles"
    static_review_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CorpusFileImportResult:
    imported: LegalImportResult
    article_count: int
    chunk_count: int
    provision_count: int | None = None


def _structure_sha256(articles: tuple[ParsedArticle, ...]) -> str:
    content = [
        {
            "provision_no": article.provision_no,
            "structure_path": article.structure_path,
            "text": article.text,
            "paragraphs": article.paragraphs,
        }
        for article in articles
    ]
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _prepare(
    args: argparse.Namespace, *, conversion_catalog: CorpusConversionCatalog | None = None,
) -> _PreparedImport:
    """Resolve explicit metadata and parse the source before opening services."""
    from lawyer_agent.application.legal_corpus_import import LegalCorpusImportError
    from lawyer_agent.application.legal_corpus_import_mapping import (
        LegalImportMetadata,
        ParsedArticleView,
        map_parsed_articles,
    )
    from lawyer_agent.domain.legal_parser_profiles import EXACT_TEXT_PARSER_VERSIONS
    from lawyer_agent.infrastructure.documents.corpus_source import (
        CorpusSourceError,
        CorpusSourceRequest,
        prepare_corpus_source,
    )
    from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser

    if len(f"{args.parser_version}/hierarchical-v1") > 64:
        raise _InputError("--parser-version is too long for the hierarchical chunk version")
    published_on = _parse_date(args.published_on, field="--published-on")
    effective_on = _parse_date(args.effective_on, field="--effective-on")
    repealed_on = _parse_date(args.repealed_on, field="--repealed-on")
    try:
        source = prepare_corpus_source(CorpusSourceRequest(
            source=args.docx,
            source_root=args.source_root or args.docx.absolute().parent,
            expected_source_sha256=args.source_sha256,
            conversion_manifest=args.conversion_manifest,
            converted_root=args.converted_root,
            conversion_catalog=conversion_catalog,
        ))
    except CorpusSourceError as exc:
        raise _InputError(f"source preparation failed: {exc}") from exc
    version_label = args.version_label or (
        f"{published_on.isoformat()} 导入版" if published_on else
        f"source-sha256:{source.source_sha256}"
    )
    source_ref = args.source_ref or source.source_path.as_uri()
    try:
        parser_profile = (
            args.parser_version if args.parser_version in EXACT_TEXT_PARSER_VERSIONS else None
        )
        parsed = LegalStructureParser(profile=parser_profile).parse(source.document)
    except ValueError as exc:
        raise _InputError(f"structure parsing failed: {exc}") from exc
    content_mode = getattr(args, "content_mode", "articles")
    if content_mode == "non_article_document" and parsed.articles:
        raise _InputError("non_article_mode_contains_articles")
    if not parsed.articles and content_mode != "non_article_document":
        raise _InputError("the DOCX contains no parseable articles")
    if len({article.provision_no for article in parsed.articles}) != len(parsed.articles):
        raise _InputError("duplicate article numbers")
    metadata = LegalImportMetadata(
        title=args.instrument_title.strip(),
        issuing_authority=args.issuing_authority.strip(),
        jurisdiction=args.jurisdiction.strip(),
        region_code=args.region_code.strip() if args.region_code else None,
        version_label=version_label,
        status=LegalVersionStatus(args.status),
        published_on=published_on,
        effective_on=effective_on,
        repealed_on=repealed_on,
        law_number=args.law_number.strip() if args.law_number else None,
        source_ref=source_ref,
        dataset_version=args.dataset_version,
        parser_version=args.parser_version,
        category=LegalCategory(args.category),
    )
    if content_mode == "non_article_document":
        try:
            command, body = map_non_article_body(
                metadata, tuple(item.text for item in source.document.paragraphs),
            )
        except LegalCorpusImportError as exc:
            raise _InputError(str(exc)) from exc
        source = replace(source, quality_flags=tuple(sorted({
            *source.quality_flags, "non_article_document",
        })))
        encoded = json.dumps({"content_mode": content_mode, "body": asdict(body),
                              "level": "paragraph"}, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        return _apply_static_review(_PreparedImport(command, (body,), source,
            sha256(encoded.encode("utf-8")).hexdigest(), content_mode), args)
    view = cast(tuple[ParsedArticleView, ...], parsed.articles)
    try:
        command = map_parsed_articles(metadata, view)
    except LegalCorpusImportError as exc:
        raise _InputError(str(exc)) from exc
    return _apply_static_review(
        _PreparedImport(command, parsed.articles, source, _structure_sha256(parsed.articles)), args,
    )


def _apply_static_review(prepared: _PreparedImport, args: argparse.Namespace) -> _PreparedImport:
    from lawyer_agent.infrastructure.documents.static_recovery_review import (
        verify_static_recovery_review,
    )

    path = getattr(args, "static_review_path", None)
    digest = getattr(args, "static_review_sha256", None)
    if path is None and digest is None:
        return prepared
    if path is None or digest is None:
        raise _InputError("static_recovery_review_invalid")
    proof = verify_static_recovery_review(
        path, digest, _source_proof(prepared),
        body_paragraph_count=len(prepared.source.document.paragraphs),
        auxiliary_paragraph_count=len(prepared.source.auxiliary_paragraphs),
        article_count=len(prepared.articles) if prepared.content_mode == "articles" else 0,
    )
    return replace(prepared, static_review_sha256=digest,
                   source=replace(prepared.source, quality_flags=proof.quality_flags))


def _import_blockers(prepared: _PreparedImport) -> list[str]:
    from lawyer_agent.domain.legal_source_proof import static_review_matches

    if ("legacy_binary_static_text_recovery" in prepared.source.quality_flags
            and not static_review_matches(_source_proof(prepared), prepared.static_review_sha256)):
        return ["static_recovery_requires_quality_review"]
    return []


def _require_importable(prepared: _PreparedImport) -> None:
    if blockers := _import_blockers(prepared):
        raise _InputError(", ".join(blockers))


def _prepare_import(
    args: argparse.Namespace,
) -> tuple[LegalImportCommand, tuple[ParsedArticle | ParsedBody, ...]]:
    prepared = _prepare(args)
    _require_importable(prepared)
    return prepared.command, prepared.articles


def _preflight_report(prepared: _PreparedImport) -> dict[str, object]:
    source, command = prepared.source, prepared.command
    return {
        "schema_version": "corpus-preflight-v1",
        "source_path": str(source.source_path),
        "input_path": str(source.input_path),
        "source_ref": command.source_ref,
        "source_sha256": source.source_sha256,
        "input_sha256": source.input_sha256,
        "loader_version": source.document.loader_version,
        "parser_version": command.parser_version,
        "chunk_parser_version": f"{command.parser_version}/hierarchical-v2",
        "conversion_provenance": (
            asdict(source.conversion_provenance) if source.conversion_provenance else None
        ),
        "quality_flags": list(source.quality_flags),
        "article_count": len(prepared.articles) if prepared.content_mode == "articles" else 0,
        "provision_count": len(command.provisions),
        "content_mode": prepared.content_mode,
        "static_review_sha256": prepared.static_review_sha256,
        "auxiliary_paragraph_count": len(source.auxiliary_paragraphs),
        "structure_sha256": prepared.structure_sha256,
        "version_label": command.version_label,
        "status": command.status.value,
        "published_on": command.published_on.isoformat() if command.published_on else None,
        "effective_on": command.effective_on.isoformat() if command.effective_on else None,
        "repealed_on": command.repealed_on.isoformat() if command.repealed_on else None,
        "metadata_review_status": "not_verified",
        "database_provenance_persisted": False,
        "import_blockers": _import_blockers(prepared),
    }


async def _run(args: argparse.Namespace) -> None:
    _require_review_arguments(args)
    prepared = _prepare(args)
    if args.preflight:
        print(json.dumps(_preflight_report(prepared), ensure_ascii=False, sort_keys=True))
        return
    _require_importable(prepared)
    await _write(args, prepared)


def _source_proof(prepared: _PreparedImport) -> LegalSourceProof:
    from lawyer_agent.domain.legal_source_proof import LegalSourceProof

    source = prepared.source
    conversion = source.conversion_provenance
    return LegalSourceProof(
        source_ref=source.source_path.as_uri(), input_ref=source.input_path.as_uri(),
        source_sha256=bytes.fromhex(source.source_sha256),
        input_sha256=bytes.fromhex(source.input_sha256),
        structure_sha256=bytes.fromhex(prepared.structure_sha256),
        loader_version=source.document.loader_version,
        parser_version=prepared.command.parser_version,
        converter_version=conversion.converter_version if conversion else None,
        converter_fingerprint=(
            conversion.converter_fingerprint.lower()
            if conversion and conversion.converter_fingerprint else None
        ),
        recovery_reason=conversion.recovery_reason if conversion else None,
        quality_flags=source.quality_flags,
    )


async def _import_prepared(
    session_factory: async_sessionmaker[AsyncSession], prepared: _PreparedImport,
) -> CorpusFileImportResult:
    """Commit one version, its proof and chunks together; return only after commit."""
    from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks
    from lawyer_agent.application.legal_corpus_import import LegalCorpusImportService
    from lawyer_agent.application.legal_corpus_replay import (
        assert_provisions_match,
        persist_or_replay_chunk_graph,
    )
    from lawyer_agent.application.legal_source_proof import LegalSourceProofService
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
        SqlAlchemyLegalCorpusChunkRepository,
        SqlAlchemyLegalCorpusImportRepository,
        SqlAlchemyLegalCorpusRepository,
    )
    from lawyer_agent.infrastructure.persistence.repositories.legal_source_proof import (
        SqlAlchemyLegalSourceProofRepository,
    )

    _require_importable(prepared)
    proof = _source_proof(prepared)
    async with session_factory() as session, session.begin():
        imported = await LegalCorpusImportService(
            SqlAlchemyLegalCorpusImportRepository(session),
        ).import_version(prepared.command)
        await LegalSourceProofService(SqlAlchemyLegalSourceProofRepository(session)).ensure(
            imported, proof, static_review_sha256=prepared.static_review_sha256,
        )
        provisions = await SqlAlchemyLegalCorpusRepository(session).provisions_for_version(
            imported.version_id,
        )
        if imported.replayed:
            assert_provisions_match(
                prepared.command.provisions,
                provisions,
                parser_version=prepared.command.parser_version,
            )
        chunks = derive_hierarchical_chunks(
            version_id=imported.version_id, provisions=provisions, articles=prepared.articles,
            parser_version=f"{prepared.command.parser_version}/hierarchical-v2",
        )
        chunk_count = await persist_or_replay_chunk_graph(
            SqlAlchemyLegalCorpusChunkRepository(session),
            imported.version_id,
            chunks,
            replayed=imported.replayed,
        )
    return CorpusFileImportResult(
        imported, len(provisions) if prepared.content_mode == "articles" else 0,
        chunk_count, len(provisions),
    )


async def _write(args: argparse.Namespace, prepared: _PreparedImport) -> None:
    from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
    from lawyer_agent.cli.corpus_publish_set import check_and_build
    from lawyer_agent.domain.legal_dataset_quality import ReleaseConfiguration, ReleaseSelection
    from lawyer_agent.infrastructure.persistence.engine import (
        create_engine,
        create_session_factory,
    )
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    command = prepared.command
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
        result = await _import_prepared(session_factory, prepared)
        imported = result.imported
        print(
            f"imported instrument={imported.instrument_id} version={imported.version_id} "
            f"replayed={imported.replayed} articles={result.article_count} "
            f"chunks={result.chunk_count}"
        )
        if args.import_only:
            print("import_only completed")
            return

        selection = ReleaseSelection(
            version_id=imported.version_id, source_ref=prepared.source.source_path.as_uri(),
            source_sha256=prepared.source.source_sha256,
            input_sha256=prepared.source.input_sha256,
            structure_sha256=prepared.structure_sha256,
            metadata_review_ref=args.metadata_review_ref,
            expected_article_count=result.article_count, expected_chunk_count=result.chunk_count,
            expected_instrument_id=imported.instrument_id,
            date_review_ref=getattr(args, "date_review_ref", None),
            content_mode=prepared.content_mode,
            expected_provision_count=result.provision_count,
            static_review_sha256=prepared.static_review_sha256,
        )
        configuration = ReleaseConfiguration(
            alias=args.alias, model_ref=embed_model_ref, dimension=embed_dimension,
            parser_version=f"{command.parser_version}/hierarchical-v2",
            selection_sha256=prepared.source.source_sha256,
        )
        candidate = await check_and_build(
            engine, settings, (selection,), configuration, check=args.quality_check,
            review_ref=args.review_ref, expected_quality_sha256=args.expected_quality_sha256,
            index_name=index_name, batch_size=args.batch_size,
        )
        if candidate is None:
            return
        alias_service = LegalDatasetAliasService(
            OpenSearchRestClient(base_url=settings.opensearch_url),
        )
        published = await _publish_candidate(session_factory, alias_service, candidate)
        print(
            "published "
            f"publication_id={published.id} "
            f"index={index_name} alias={args.alias} "
            f"indexed_documents={candidate.manifest['indexed_documents']} "
            f"previous_target={published.previous_target}"
        )
    finally:
        await engine.dispose()


async def _publish_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    alias_service: LegalDatasetAliasService,
    candidate: DatasetSnapshot,
) -> DatasetPublication:
    from lawyer_agent.application.legal_dataset_publication import LegalDatasetPublicationService
    from lawyer_agent.domain.legal_dataset_publication import validate_reviewed_candidate
    from lawyer_agent.infrastructure.persistence.repositories.legal_dataset_publication import (
        SqlAlchemyDatasetPublicationStore,
    )

    validate_reviewed_candidate(candidate)
    store = SqlAlchemyDatasetPublicationStore(session_factory)
    previous = await alias_service.active_dataset_index(candidate.dataset_name)
    record = await store.create(candidate, previous)
    print(f"publication_id={record.id}", flush=True)
    return await LegalDatasetPublicationService(store, alias_service).resume(record.id)


if __name__ == "__main__":
    raise SystemExit(main())
