"""Shared article markers for structural parsing and child-prefix removal."""

from __future__ import annotations

import re

_NUMBER = r"[一二三四五六七八九十百千零〇0-9０-９]+"
_HORIZONTAL_SPACE = r"[^\S\r\n\v\f\u2028\u2029]*"

# Unresolved supplements must never fall back to a base number or be appended
# to the previous article. Candidate detection is not a boundary decision.
ARTICLE_SUPPLEMENT_CANDIDATE = re.compile(rf"^\s*第{_NUMBER}{_HORIZONTAL_SPACE}条之")

# Preserve the existing ordinary article contract, including compact headings.
# Supplements require their complete number and a visible separator/end; the
# base branch cannot consume the prefix of a “条之…” candidate.
ARTICLE_HEADING = re.compile(
    rf"^\s*(?P<marker>第{_NUMBER}{_HORIZONTAL_SPACE}条"
    rf"(?:(?P<supplement>之{_NUMBER})(?=\s|【|$)|(?!之)))\s*"
)
