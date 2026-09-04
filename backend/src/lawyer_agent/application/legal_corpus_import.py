"""Controlled legal corpus import: persist instrument/version/provisions.

The corpus tables used to be filled only by test seeds; this service is the
production write path. An import command is explicit metadata (instrument
identity + version dates/status/source) plus ordered provision drafts; the
service validates, derives content hashes and character offsets, reuses an
instrument by exact (title, jurisdiction), replays an identical version, and
writes atomically through the repository. Similar statutes are never merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)


class LegalCorpusImportError(ValueError):
    """Base import failure carrying a stable message."""


class LegalCorpusImportConflict(LegalCorpusImportError):
    """Identity or version conflict; nothing was written."""


@dataclass(frozen=True, slots=True)
class LegalProvisionDraft:
    provision_no: str
    level: ProvisionLevel
    structure_path: tuple[str, ...]
    title: str | None
    full_text: str


@dataclass(frozen=True, slots=True)
class LegalImportCommand:
    title: str
    issuing_authority: str
    jurisdiction: str
    region_code: str | None
    version_label: str
    status: LegalVersionStatus
    published_on: date | None
    effective_on: date | None
    repealed_on: date | None
    law_number: str | None
    source_ref: str
    dataset_version: str
    parser_version: str
    provisions: tuple[LegalProvisionDraft, ...]


@dataclass(frozen=True, slots=True)
class LegalImportResult:
    instrument_id: UUID
    version_id: UUID
    replayed: bool


class LegalCorpusImportPort(Protocol):
    async def find_instrument_by_identity(
        self, title: str, jurisdiction: str
    ) -> LegalInstrument | None: ...

    async def create_instrument(self, instrument: LegalInstrument) -> None: ...

    async def find_version(
        self, instrument_id: UUID, version_label: str
    ) -> LegalVersion | None: ...

    async def create_version(self, version: LegalVersion) -> None: ...

    async def create_provisions(self, provisions: tuple[Provision, ...]) -> None: ...


class LegalCorpusImportService:
    """Validates and persists one instrument version with its provisions."""

    def __init__(self, repository: LegalCorpusImportPort) -> None:
        if not hasattr(repository, "find_instrument_by_identity"):
            raise ValueError("corpus import requires a write repository")
        self._repository = repository

    async def import_version(
        self, command: LegalImportCommand
    ) -> LegalImportResult:
        validate_import_command(command)

        instrument = await self._repository.find_instrument_by_identity(
            command.title, command.jurisdiction
        )
        if instrument is None:
            instrument = LegalInstrument(
                id=new_uuid7(),
                title=command.title,
                issuing_authority=command.issuing_authority,
                jurisdiction=command.jurisdiction,
                region_code=command.region_code,
            )
            await self._repository.create_instrument(instrument)
        elif instrument.issuing_authority != command.issuing_authority:
            raise LegalCorpusImportConflict(
                "instrument identity conflicts on issuing authority; "
                "similar statutes are never merged automatically"
            )

        existing = await self._repository.find_version(
            instrument.id, command.version_label
        )
        if existing is not None:
            if _version_matches(existing, command):
                return LegalImportResult(
                    instrument_id=instrument.id,
                    version_id=existing.id,
                    replayed=True,
                )
            raise LegalCorpusImportConflict(
                "version label already exists with different content or metadata"
            )

        version_id = new_uuid7()
        provisions = _derive_provisions(version_id, command.provisions)
        version = LegalVersion(
            id=version_id,
            instrument_id=instrument.id,
            version_label=command.version_label,
            status=command.status,
            published_on=command.published_on,
            effective_on=command.effective_on,
            repealed_on=command.repealed_on,
            law_number=command.law_number,
            content_hash=_content_hash(command.provisions),
            source_ref=command.source_ref,
            dataset_version=command.dataset_version,
            parser_version=command.parser_version,
        )
        await self._repository.create_version(version)
        await self._repository.create_provisions(provisions)
        return LegalImportResult(
            instrument_id=instrument.id,
            version_id=version_id,
            replayed=False,
        )


def validate_import_command(command: LegalImportCommand) -> None:
    """Validate an import command; shared by the service and the parser mapper."""
    if not isinstance(command, LegalImportCommand):
        raise LegalCorpusImportError("import command must be strongly typed")
    for field, name in (
        (command.title, "title"),
        (command.issuing_authority, "issuing_authority"),
        (command.jurisdiction, "jurisdiction"),
        (command.version_label, "version_label"),
        (command.source_ref, "source_ref"),
        (command.dataset_version, "dataset_version"),
        (command.parser_version, "parser_version"),
    ):
        if not isinstance(field, str) or not field.strip():
            raise LegalCorpusImportError(f"{name} must be non-empty text")
    if not isinstance(command.status, LegalVersionStatus):
        raise LegalCorpusImportError("version status must be strongly typed")
    if not command.provisions:
        raise LegalCorpusImportError("at least one provision is required")
    numbers: set[str] = set()
    for draft in command.provisions:
        if not isinstance(draft, LegalProvisionDraft):
            raise LegalCorpusImportError("provisions must be strongly typed")
        if not isinstance(draft.provision_no, str) or not draft.provision_no.strip():
            raise LegalCorpusImportError("provision number must be non-empty text")
        if not isinstance(draft.full_text, str) or not draft.full_text.strip():
            raise LegalCorpusImportError("provision full text must be non-empty")
        if not isinstance(draft.level, ProvisionLevel):
            raise LegalCorpusImportError("provision level must be strongly typed")
        if not isinstance(draft.structure_path, tuple):
            raise LegalCorpusImportError("provision structure path must be a tuple")
        if draft.provision_no in numbers:
            raise LegalCorpusImportError(
                f"duplicate provision number: {draft.provision_no}"
            )
        numbers.add(draft.provision_no)


def _derive_provisions(
    version_id: UUID, drafts: tuple[LegalProvisionDraft, ...]
) -> tuple[Provision, ...]:
    derived: list[Provision] = []
    cursor = 0
    for draft in drafts:
        text = draft.full_text.strip()
        char_start = cursor
        char_end = char_start + len(text)
        cursor = char_end
        derived.append(
            Provision(
                id=new_uuid7(),
                version_id=version_id,
                provision_no=draft.provision_no,
                level=draft.level,
                structure_path=draft.structure_path,
                title=draft.title,
                full_text=text,
                content_hash=content_sha256(text),
                char_start=char_start,
                char_end=char_end,
            )
        )
    return tuple(derived)


def _content_hash(drafts: tuple[LegalProvisionDraft, ...]) -> bytes:
    hasher = sha256()
    for draft in drafts:
        hasher.update(draft.full_text.strip().encode("utf-8"))
    return hasher.digest()


def _version_matches(existing: LegalVersion, command: LegalImportCommand) -> bool:
    """Replay only when metadata and provision content are all identical."""
    return (
        existing.version_label == command.version_label
        and existing.status is command.status
        and existing.published_on == command.published_on
        and existing.effective_on == command.effective_on
        and existing.repealed_on == command.repealed_on
        and existing.law_number == command.law_number
        and existing.source_ref == command.source_ref
        and existing.dataset_version == command.dataset_version
        and existing.parser_version == command.parser_version
        and existing.content_hash == _content_hash(command.provisions)
    )
