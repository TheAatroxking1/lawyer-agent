"""Import explicitly reviewed public corpus rows with one transaction per source."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from uuid import uuid4

from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalCorpusImportError,
)
from lawyer_agent.cli import corpus_publish
from lawyer_agent.cli.corpus_publish import _import_prepared
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path
from lawyer_agent.infrastructure.documents.corpus_source import (
    CorpusSourceError,
    load_conversion_catalog,
)
from lawyer_agent.infrastructure.documents.import_manifest import (
    CorpusImportRow,
    ImportManifestError,
    read_import_manifest,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按已核对清单逐文件导入，不发布索引")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="新建 UTF-8 JSONL 结果文件")
    parser.add_argument("--conversion-manifest", type=Path)
    parser.add_argument("--converted-root", type=Path)
    parser.add_argument("--preflight", action="store_true", help="只预检，不连接数据库")
    parser.add_argument("--dataset-version", default="dataset_v1")
    parser.add_argument("--parser-version", default=corpus_publish._DEFAULT_PARSER_VERSION)
    return parser


def _report_path(args: argparse.Namespace, manifest: Path) -> Path:
    raw = args.report
    if raw.drive and not raw.is_absolute():
        raise ImportManifestError("unsafe_report_path")
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
    for part in raw.parts:
        if part == raw.anchor:
            continue
        if (
            ":" in part or "\x00" in part or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in devices
        ):
            raise ImportManifestError("unsafe_report_path")
    output = unredirected_path(args.report)
    roots = [unredirected_path(args.source_root)]
    if args.converted_root is not None:
        roots.append(unredirected_path(args.converted_root))
    if any(output.is_relative_to(root) or root.is_relative_to(output) for root in roots):
        raise ImportManifestError("report_source_or_derived_overlap")
    inputs = [manifest]
    if args.conversion_manifest is not None:
        inputs.append(unredirected_path(args.conversion_manifest))
    if output in inputs or output.exists():
        raise ImportManifestError("report_must_be_new_file")
    return output


def _write_record(stream: TextIO, record: dict[str, object]) -> None:
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _row_args(row: CorpusImportRow, args: argparse.Namespace) -> argparse.Namespace:
    fields: dict[str, object] = {
        "source": row.source_path,
        "source-root": args.source_root,
        "source-sha256": row.source_sha256,
        "instrument-title": row.title,
        "issuing-authority": row.issuing_authority,
        "jurisdiction": row.jurisdiction,
        "category": row.category.value,
        "status": row.status.value,
        "region-code": row.region_code,
        "version-label": row.version_label,
        "published-on": row.published_on,
        "effective-on": row.effective_on,
        "repealed-on": row.repealed_on,
        "law-number": row.law_number,
        "source-ref": row.source_ref,
        "conversion-manifest": args.conversion_manifest,
        "converted-root": args.converted_root,
        "dataset-version": args.dataset_version,
        "parser-version": args.parser_version,
        "content-mode": row.content_mode,
        "date-review-ref": row.date_review_ref,
        "static-review-path": row.static_review_path,
        "static-review-sha256": row.static_review_sha256,
    }
    tokens = [
        f"--{key}={value.isoformat() if isinstance(value, date) else value}"
        for key, value in fields.items()
        if value is not None
    ]
    tokens.append("--preflight" if args.preflight else "--import-only")
    return corpus_publish.build_parser().parse_args(tokens)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, CorpusSourceError):
        return exc.code
    if isinstance(exc.__cause__, CorpusSourceError):
        return exc.__cause__.code
    if str(exc) in {
        "static_recovery_requires_quality_review",
        "source_proof_conflict",
        "source_proof_missing_requires_review",
    }:
        return str(exc)
    if isinstance(exc, LegalCorpusImportConflict):
        return "legal_import_conflict"
    if isinstance(exc, (LegalCorpusImportError, corpus_publish._InputError)):
        return "invalid_source_or_metadata"
    return "import_failed"


async def _open_database() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    from sqlalchemy import text

    from lawyer_agent.config import Settings
    from lawyer_agent.infrastructure.persistence.engine import create_engine, create_session_factory

    engine = create_engine(Settings())
    try:
        # A global unavailable database should fail the run before iterating all sources.
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except BaseException:
        await engine.dispose()
        raise
    return engine, create_session_factory(engine)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if (
        not args.dataset_version.strip()
        or len(args.dataset_version) > 64
        or not args.parser_version.strip()
        or len(f"{args.parser_version}/hierarchical-v1") > 64
    ):
        raise ImportManifestError("invalid_batch_versions")
    manifest = read_import_manifest(args.manifest, args.source_root)
    report = _report_path(args, manifest.path)
    if (args.conversion_manifest is None) != (args.converted_root is None):
        raise ImportManifestError("incomplete_conversion_configuration")
    catalog = (
        load_conversion_catalog(args.conversion_manifest, args.source_root, args.converted_root)
        if args.conversion_manifest is not None
        else None
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    _report_path(args, manifest.path)
    counts = {"imported": 0, "replayed": 0, "preflighted": 0, "failed": 0}
    with report.open("x", encoding="utf-8", newline="\n") as stream:
        _write_record(
            stream,
            {
                "event": "run",
                "schema_version": "corpus-import-batch-v1",
                "run_id": uuid4().hex,
                "manifest_sha256": manifest.sha256,
                "conversion_manifest_sha256": catalog.manifest_sha256 if catalog else None,
                "total": len(manifest.rows),
                "mode": "preflight" if args.preflight else "import",
                "dataset_version": args.dataset_version,
                "parser_version": args.parser_version,
                "chunk_parser_version": f"{args.parser_version}/hierarchical-v2",
            },
        )
        engine, session_factory = (None, None) if args.preflight else await _open_database()
        try:
            for index, row in enumerate(manifest.rows, 1):
                record: dict[str, object] = {
                    "event": "file",
                    "row": index,
                    "source_path": row.source_path,
                    "source_sha256": row.source_sha256,
                    "metadata_review_ref": row.metadata_review_ref,
                    "date_review_ref": row.date_review_ref,
                    "content_mode": row.content_mode,
                    "static_review_sha256": row.static_review_sha256,
                    "source_hash_verified": False,
                }
                try:
                    prepared = corpus_publish._prepare(
                        _row_args(row, args), conversion_catalog=catalog
                    )
                    record.update(
                        source_path=str(prepared.source.source_path),
                        source_hash_verified=True,
                        input_sha256=prepared.source.input_sha256,
                        structure_sha256=prepared.structure_sha256,
                        quality_flags=list(prepared.source.quality_flags),
                        article_count=(len(prepared.articles)
                                       if prepared.content_mode == "articles" else 0),
                        provision_count=len(prepared.command.provisions),
                    )
                    corpus_publish._require_importable(prepared)
                    if args.preflight:
                        status = "preflighted"
                        record.update(status=status, code="source_preflight_passed")
                    else:
                        assert session_factory is not None
                        result = await _import_prepared(session_factory, prepared)
                        status = "replayed" if result.imported.replayed else "imported"
                        record.update(
                            status=status,
                            code="import_completed",
                            version_id=str(result.imported.version_id),
                            instrument_id=str(result.imported.instrument_id),
                            article_count=result.article_count,
                            provision_count=(result.provision_count
                                             if result.provision_count is not None
                                             else result.article_count),
                            chunk_count=result.chunk_count,
                        )
                except Exception as exc:  # noqa: BLE001 - per-file isolation, no raw payloads
                    status = "failed"
                    record.update(status=status, code=_safe_error(exc))
                # Report failures must escape the per-file handler and stop subsequent imports.
                _write_record(stream, record)
                counts[status] += 1
            summary: dict[str, object] = {
                "event": "summary",
                "complete": counts["failed"] == 0,
                "total": len(manifest.rows),
                "processed": sum(counts.values()),
                **counts,
                "report": str(report),
            }
            _write_record(stream, summary)
        finally:
            if engine is not None:
                await engine.dispose()
    return summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = asyncio.run(_run(args))
    except (ImportManifestError, CorpusSourceError, ValueError):
        print("batch input invalid; no complete result was produced", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - do not expose SQL parameters or source content
        print("batch interrupted; inspect the partial report before retrying", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
