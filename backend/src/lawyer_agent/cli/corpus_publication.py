"""Inspect or conservatively resume a durable publication without rebuilding."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import TYPE_CHECKING
from uuid import UUID

from lawyer_agent.config import Settings
from lawyer_agent.domain.common import require_uuid7

if TYPE_CHECKING:
    from lawyer_agent.domain.legal_dataset_publication import DatasetPublication


def _publication_id(raw: str) -> UUID:
    try:
        return require_uuid7(UUID(raw), field="publication_id")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("publication_id must be UUIDv7") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lawyer_agent.cli.corpus_publication")
    actions = parser.add_subparsers(dest="action", required=True)
    status = actions.add_parser("status")
    selector = status.add_mutually_exclusive_group(required=True)
    selector.add_argument("--publication-id", type=_publication_id)
    selector.add_argument("--alias", type=_alias)
    resume = actions.add_parser("resume")
    resume.add_argument("--publication-id", required=True, type=_publication_id)
    return parser


def _alias(raw: str) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", raw) is None:
        raise argparse.ArgumentTypeError("alias must be 1-64 lowercase index-name characters")
    return raw


def publication_summary(publication: DatasetPublication) -> dict[str, object]:
    candidate = publication.candidate
    return {
        "publication_id": str(publication.id), "state": publication.state.value,
        "alias": candidate.dataset_name, "index_name": candidate.manifest["index_name"],
        "previous_target": publication.previous_target,
        "indexed_documents": candidate.manifest["indexed_documents"],
        "navigation_documents": candidate.manifest["navigation_documents"],
        "version_count": candidate.manifest.get(
            "version_count", len(candidate.manifest.get("version_ids", [None])),
        ),
    }


async def _run(args: argparse.Namespace) -> None:
    from lawyer_agent.application.legal_dataset_publication import (
        LegalDatasetPublicationService,
    )
    from lawyer_agent.application.legal_index_alias import LegalDatasetAliasService
    from lawyer_agent.domain.legal_dataset_publication import PublicationError
    from lawyer_agent.infrastructure.persistence.engine import (
        create_engine,
        create_session_factory,
    )
    from lawyer_agent.infrastructure.persistence.repositories.legal_dataset_publication import (
        SqlAlchemyDatasetPublicationStore,
    )
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    settings = Settings()
    engine = create_engine(settings)
    try:
        store = SqlAlchemyDatasetPublicationStore(create_session_factory(engine))
        if args.action == "status":
            publication = (
                await store.find_active(args.alias) if getattr(args, "alias", None)
                else await store.get(args.publication_id)
            )
            if publication is None:
                raise PublicationError("publication_not_found")
        else:
            alias = LegalDatasetAliasService(
                OpenSearchRestClient(base_url=settings.opensearch_url),
            )
            publication = await LegalDatasetPublicationService(store, alias).resume(
                args.publication_id, require_review=True,
            )
        print(json.dumps(publication_summary(publication), ensure_ascii=False, sort_keys=True))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - sanitized CLI boundary
        from lawyer_agent.domain.legal_dataset_publication import PublicationError

        code = exc.code if isinstance(exc, PublicationError) else "publication_failed"
        run_id = str(args.publication_id) if args.publication_id else None
        print(json.dumps({"code": code, "publication_id": run_id,
                          "alias": getattr(args, "alias", None)}),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
