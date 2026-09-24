"""Automatic mapping from parsed corpus structure to an import command.

The corpus import slice defined explicit `LegalImportCommand` metadata plus
ordered provision drafts; the structure parser produces
`ParsedInstrument/ParsedArticle` rows. Nothing bridged the two, so every real
import still meant hand-writing one `LegalProvisionDraft` per article. This
module is that bridge: given explicit metadata (never guessed) and parser
output viewed structurally (duck typed, so application code stays independent
of infrastructure), it derives one `ARTICLE` draft per article and returns a
command that already passes import validation. No DOCX or source file is read
here and no heuristic extracts metadata from text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportError,
    LegalImportCommand,
    LegalProvisionDraft,
    validate_import_command,
)
from lawyer_agent.domain.legal_corpus import (
    LegalCategory,
    LegalVersionStatus,
    ProvisionLevel,
)

_ARTICLE_FIELDS = ("provision_no", "structure_path", "text")


@dataclass(frozen=True, slots=True)
class LegalImportMetadata:
    """Explicit instrument/version metadata; never derived from document text."""

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
    category: LegalCategory = LegalCategory.UNKNOWN


class ParsedArticleView(Protocol):
    """Structural view of one parsed article (satisfied by ParsedArticle)."""

    provision_no: str
    structure_path: tuple[str, ...]
    text: str


def map_parsed_articles(
    metadata: LegalImportMetadata, articles: tuple[ParsedArticleView, ...]
) -> LegalImportCommand:
    """Map parsed articles plus explicit metadata to an import command.

    Raises `LegalCorpusImportError` with a stable message when the metadata is
    not strongly typed, there is no article to import, an article does not
    expose the required structural fields, or the resulting command would not
    pass import validation (blank text, blank number, duplicate numbers).
    """
    if not isinstance(metadata, LegalImportMetadata):
        raise LegalCorpusImportError("import metadata must be strongly typed")
    if not articles:
        raise LegalCorpusImportError("at least one article is required")

    drafts: list[LegalProvisionDraft] = []
    for article in articles:
        draft = _to_draft(article)
        drafts.append(draft)

    command = LegalImportCommand(
        title=metadata.title,
        issuing_authority=metadata.issuing_authority,
        jurisdiction=metadata.jurisdiction,
        region_code=metadata.region_code,
        version_label=metadata.version_label,
        status=metadata.status,
        published_on=metadata.published_on,
        effective_on=metadata.effective_on,
        repealed_on=metadata.repealed_on,
        law_number=metadata.law_number,
        source_ref=metadata.source_ref,
        dataset_version=metadata.dataset_version,
        parser_version=metadata.parser_version,
        provisions=tuple(drafts),
        category=metadata.category,
    )
    validate_import_command(command)
    return command


def _to_draft(article: ParsedArticleView) -> LegalProvisionDraft:
    missing = [field for field in _ARTICLE_FIELDS if not hasattr(article, field)]
    if missing:
        raise LegalCorpusImportError(
            "parsed article must expose provision_no, structure_path and text"
        )
    provision_no = article.provision_no
    structure_path = article.structure_path
    text = article.text
    if not isinstance(provision_no, str):
        raise LegalCorpusImportError("parsed article provision number must be text")
    if not isinstance(structure_path, tuple) or not all(
        isinstance(part, str) for part in structure_path
    ):
        raise LegalCorpusImportError("parsed article structure path must be a string tuple")
    if not isinstance(text, str):
        raise LegalCorpusImportError("parsed article text must be text")
    return LegalProvisionDraft(
        provision_no=provision_no,
        level=ProvisionLevel.ARTICLE,
        structure_path=structure_path,
        title=None,
        full_text=text,
    )
