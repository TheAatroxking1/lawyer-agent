"""Build a verified navigation sidecar solely from authoritative corpus data."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.legal_corpus import LegalInstrument, LegalVersion, Provision
from lawyer_agent.domain.legal_navigation import (
    NavigationDocument,
    NavigationDocumentKind,
    navigation_index_name,
)

_STRUCTURE = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+[编章]")


@dataclass(frozen=True, slots=True)
class NavigationBuildResult:
    index_name: str
    indexed_documents: int


class NavigationSourcePort(Protocol):
    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument] | None: ...

    async def provisions_for_version(self, version_id: UUID) -> tuple[Provision, ...]: ...


class NavigationIndexPort(Protocol):
    async def ensure_navigation_index(self, index_name: str) -> None: ...

    async def append_navigation_documents(
        self,
        index_name: str,
        documents: tuple[NavigationDocument, ...],
        *,
        parser_version: str,
    ) -> None: ...

    async def count_documents(self, index_name: str) -> int: ...

    async def mark_navigation_ready(self, main_index_name: str) -> None: ...


class LegalNavigationIndexService:
    def __init__(self, source: NavigationSourcePort, search: NavigationIndexPort) -> None:
        self._source = source
        self._search = search

    async def build(
        self,
        *,
        version_ids: tuple[UUID, ...],
        main_index_name: str,
        parser_version: str,
    ) -> NavigationBuildResult:
        index_name = navigation_index_name(main_index_name)
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("navigation parser_version must be non-empty text")
        if not isinstance(version_ids, tuple) or not version_ids:
            raise ValueError("navigation version_ids must be a non-empty tuple")
        for version_id in version_ids:
            require_uuid7(version_id, field="navigation version_id")
        if len(set(version_ids)) != len(version_ids):
            raise ValueError("navigation version_ids must be unique")

        summaries: list[tuple[str, int]] = []
        for version_id in version_ids:
            documents, digest = await self._version_documents(version_id, parser_version)
            summaries.append((digest, len(documents)))
            del documents

        # No external writes until every requested version has passed preflight.
        await self._search.ensure_navigation_index(index_name)
        expected_count = sum(count for _, count in summaries)
        written = 0
        for version_id, (expected_digest, expected_size) in zip(
            version_ids, summaries, strict=True,
        ):
            documents, digest = await self._version_documents(version_id, parser_version)
            if digest != expected_digest or len(documents) != expected_size:
                raise ValueError("navigation source changed after preflight")
            for start in range(0, len(documents), 256):
                batch = documents[start : start + 256]
                await self._search.append_navigation_documents(
                    index_name, batch, parser_version=parser_version,
                )
                written += len(batch)
                del batch
            del documents
        count = await self._search.count_documents(index_name)
        if type(count) is not int or count <= 0 or count != expected_count or count != written:
            raise RuntimeError("navigation document count verification failed")
        await self._search.mark_navigation_ready(main_index_name)
        return NavigationBuildResult(index_name, count)

    async def _version_documents(
        self, version_id: UUID, parser_version: str,
    ) -> tuple[tuple[NavigationDocument, ...], str]:
        documents: list[NavigationDocument] = []
        pair = await self._source.version_with_instrument(version_id)
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], LegalVersion)
            or not isinstance(pair[1], LegalInstrument)
        ):
            raise ValueError("navigation source version is missing or invalid")
        version, instrument = pair
        if version.id != version_id or version.instrument_id != instrument.id:
            raise ValueError("navigation source identity mismatch")
        provisions = await self._source.provisions_for_version(version_id)
        if not isinstance(provisions, tuple) or not provisions:
            raise ValueError("navigation source provisions must be non-empty")
        documents.append(
            _document(
                version_id,
                NavigationDocumentKind.INSTRUMENT,
                (instrument.title,),
                instrument.title,
                instrument.title,
                parser_version,
            )
        )
        seen: set[tuple[str, ...]] = set()
        for provision in provisions:
            if not isinstance(provision, Provision) or provision.version_id != version_id:
                raise ValueError("navigation provision identity mismatch")
            path = provision.structure_path
            if not isinstance(path, tuple) or any(
                not isinstance(node, str) or not node.strip() for node in path
            ):
                raise ValueError("navigation structure path is invalid")
            for position, node in enumerate(path):
                prefix = path[: position + 1]
                if not _STRUCTURE.match(node) or prefix in seen:
                    continue
                seen.add(prefix)
                documents.append(
                    _document(
                        version_id,
                        NavigationDocumentKind.STRUCTURE,
                        prefix,
                        node,
                        "\n".join((instrument.title, *prefix)),
                        parser_version,
                    )
                )
        fingerprint = sha256()
        for value in (asdict(version), asdict(instrument), parser_version):
            _fingerprint_value(fingerprint, value)
        for provision in provisions:
            _fingerprint_value(fingerprint, asdict(provision))
        return tuple(documents), fingerprint.hexdigest()


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...


def _fingerprint_value(digest: _Digest, value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest.update(encoded)
    digest.update(b"\n")


def _document(
    version_id: UUID,
    kind: NavigationDocumentKind,
    path: tuple[str, ...],
    locator: str,
    content: str,
    parser_version: str,
) -> NavigationDocument:
    identity = json.dumps(
        [str(version_id), kind.value, path],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return NavigationDocument(
        sha256(identity.encode("utf-8")).hexdigest(),
        version_id,
        kind,
        locator,
        content,
        parser_version,
    )
