"""Reject redirected conversion paths before touching the source or derived tree."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


def unredirected_path(path: Path) -> Path:
    lexical = Path(os.path.abspath(path))
    for candidate in (*reversed(lexical.parents), lexical):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("unsafe_reparse_path")
    if lexical.resolve() != lexical:
        raise ValueError("unsafe_redirected_path")
    return lexical


@dataclass(frozen=True)
class ConversionPaths:
    source_root: Path
    output_root: Path

    def __post_init__(self) -> None:
        source = unredirected_path(self.source_root)
        output = unredirected_path(self.output_root)
        if source.is_relative_to(output) or output.is_relative_to(source):
            raise ValueError("source and output roots overlap")
        object.__setattr__(self, "source_root", source)
        object.__setattr__(self, "output_root", output)

    def output(self, path: Path) -> Path:
        # Recheck roots too: a caller may have replaced an ancestor since setup.
        unredirected_path(self.source_root)
        unredirected_path(self.output_root)
        candidate = unredirected_path(path)
        if not candidate.is_relative_to(self.output_root):
            raise ValueError("unsafe_output_path")
        if candidate.is_relative_to(self.source_root) or self.source_root.is_relative_to(candidate):
            raise ValueError("source and output roots overlap")
        return candidate

    def source(self, path: Path) -> Path:
        unredirected_path(self.source_root)
        candidate = unredirected_path(path)
        if not candidate.is_relative_to(self.source_root):
            raise ValueError("source outside root")
        return candidate
