"""Explicit whole-body mapping; never synthesizes legal article numbers."""

from dataclasses import asdict, dataclass

from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportError,
    LegalImportCommand,
    LegalProvisionDraft,
    validate_import_command,
)
from lawyer_agent.application.legal_corpus_import_mapping import LegalImportMetadata
from lawyer_agent.domain.legal_corpus import ProvisionLevel


@dataclass(frozen=True, slots=True)
class ParsedBody:
    provision_no: str
    structure_path: tuple[str, ...]
    text: str
    paragraphs: tuple[str, ...]


def map_non_article_body(
    metadata: LegalImportMetadata, paragraphs: tuple[str, ...],
) -> tuple[LegalImportCommand, ParsedBody]:
    pieces = tuple(text.strip() for text in paragraphs if text.strip())
    if not pieces:
        raise LegalCorpusImportError("non_article_body_empty")
    body = ParsedBody("正文", ("正文",), "".join(pieces), pieces)
    command = LegalImportCommand(
        **asdict(metadata), provisions=(LegalProvisionDraft(
            provision_no=body.provision_no, level=ProvisionLevel.PARAGRAPH,
            structure_path=body.structure_path, title=None, full_text=body.text,
        ),),
    )
    validate_import_command(command)
    return command, body
