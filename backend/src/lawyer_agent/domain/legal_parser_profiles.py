"""Explicit parser identities that preserve provision text byte-for-byte in UTF-8."""

from types import MappingProxyType

EXACT_TEXT_PARSER_VERSIONS = frozenset({"corpus-docx-v4", "corpus-docx-v5"})
EXACT_TEXT_HASH_DOMAINS = MappingProxyType(
    {
        "corpus-docx-v4": b"lawyer-agent:legal-provisions:v4\x00",
        "corpus-docx-v5": b"lawyer-agent:legal-provisions:v5\x00",
    }
)
EXACT_TEXT_CHUNK_PARSER_VERSIONS = frozenset(
    f"{profile}/hierarchical-v2" for profile in EXACT_TEXT_PARSER_VERSIONS
)
