"""Identify legacy Word inputs without executing or unpacking their content."""

from pathlib import Path

_OLE_HEADER = bytes.fromhex("d0cf11e0a1b11ae1")


def is_legacy_word_source(path: Path) -> bool:
    """Classify an already scope-checked path; DOCM always stays static XML.

    Some legacy documents have a DOCX name and a trailing embedded ZIP. The
    leading OLE signature takes precedence over any ZIP directory at the end.
    """
    suffix = path.suffix.lower()
    if suffix == ".doc":
        return True
    if suffix != ".docx":
        return False
    with path.open("rb") as stream:
        return stream.read(len(_OLE_HEADER)) == _OLE_HEADER
