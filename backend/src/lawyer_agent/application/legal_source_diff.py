"""File-level diff between two parsed legal source documents.

spec 6.6 semi-automatic updates need both file-level and provision-level
differences before publishing a new dataset. The DB-backed version diff
(`legal_corpus_diff`) only compares already-imported versions; a brand-new
source file on disk has nothing to compare against in MySQL. This module is a
pure comparison over two parsed article sets (the same structural view the
DOCX parser emits), so an operator can see added / removed / modified /
unchanged articles and whether the file changed at all *before* any import.
Nothing here reads a file, writes to a database or invents ordering: source
order is the only order, matching is by ``provision_no`` plus full text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LegalSourceDiffError(ValueError):
    """Parsed source articles cannot be diffed."""


class ParsedSourceArticle(Protocol):
    provision_no: str
    structure_path: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class SourceDiffEntry:
    provision_no: str
    structure_path: tuple[str, ...]
    full_text: str


@dataclass(frozen=True, slots=True)
class ModifiedSourceEntry:
    provision_no: str
    previous: SourceDiffEntry
    current: SourceDiffEntry

    def __post_init__(self) -> None:
        if self.previous.full_text == self.current.full_text:
            raise LegalSourceDiffError(
                "modified source entries must differ in full text"
            )


@dataclass(frozen=True, slots=True)
class LegalSourceDiff:
    changed: bool
    added: tuple[SourceDiffEntry, ...]
    removed: tuple[SourceDiffEntry, ...]
    modified: tuple[ModifiedSourceEntry, ...]
    unchanged: tuple[SourceDiffEntry, ...]


@dataclass(frozen=True, slots=True)
class _IndexedArticles:
    by_number: dict[str, SourceDiffEntry]
    order: list[str]


def diff_source_articles(
    old_articles: tuple[ParsedSourceArticle, ...],
    new_articles: tuple[ParsedSourceArticle, ...],
) -> LegalSourceDiff:
    """Group two parsed source article sets by number + full text.

    Raises `LegalSourceDiffError` when either side repeats a provision number
    or exposes a blank number / blank text; a duplicated number would make the
    grouping ambiguous and is never silently merged.
    """
    old = _index(old_articles, "old")
    new = _index(new_articles, "new")

    removed: list[SourceDiffEntry] = []
    modified: list[ModifiedSourceEntry] = []
    unchanged: list[SourceDiffEntry] = []

    for number in old.order:
        old_entry = old.by_number[number]
        new_entry = new.by_number.get(number)
        if new_entry is None:
            removed.append(old_entry)
        elif old_entry.full_text != new_entry.full_text:
            modified.append(
                ModifiedSourceEntry(
                    provision_no=number,
                    previous=old_entry,
                    current=new_entry,
                )
            )
        else:
            unchanged.append(new_entry)

    added = [new.by_number[number] for number in new.order if number not in old.by_number]

    changed = bool(added or removed or modified)
    return LegalSourceDiff(
        changed=changed,
        added=tuple(added),
        removed=tuple(removed),
        modified=tuple(modified),
        unchanged=tuple(unchanged),
    )


def _index(
    articles: tuple[ParsedSourceArticle, ...], side: str
) -> _IndexedArticles:
    if not isinstance(articles, tuple):
        raise LegalSourceDiffError(f"{side} articles must be a tuple")
    by_number: dict[str, SourceDiffEntry] = {}
    order: list[str] = []
    for article in articles:
        if not hasattr(article, "provision_no") or not hasattr(article, "text"):
            raise LegalSourceDiffError(
                f"{side} parsed article must expose provision_no and text"
            )
        number = article.provision_no
        if not isinstance(number, str) or not number.strip():
            raise LegalSourceDiffError("provision number must be non-empty text")
        stripped_number = number.strip()
        text = article.text
        if not isinstance(text, str) or not text.strip():
            raise LegalSourceDiffError("article full text must be non-empty")
        structure_path = getattr(article, "structure_path", ())
        if structure_path is None or not isinstance(structure_path, tuple):
            raise LegalSourceDiffError("article structure path must be a string tuple")
        entry = SourceDiffEntry(
            provision_no=stripped_number,
            structure_path=tuple(
                part for part in structure_path if isinstance(part, str)
            ),
            full_text=text,
        )
        if stripped_number in by_number:
            raise LegalSourceDiffError(f"duplicate provision number: {stripped_number}")
        by_number[stripped_number] = entry
        order.append(stripped_number)
    return _IndexedArticles(by_number=by_number, order=order)
