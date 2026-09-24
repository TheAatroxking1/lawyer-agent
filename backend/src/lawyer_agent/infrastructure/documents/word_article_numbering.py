"""Conservative recovery of explicit Word automatic article numbering."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from hashlib import sha256

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ARTICLE_TEMPLATE = re.compile(r"第%1条[^\S\r\n\v\f\u2028\u2029]*\Z")


@dataclass(frozen=True, slots=True)
class NumberingProvenance:
    original_text: str
    xml_paragraph_ordinal: int
    numbering_sha256: str
    num_id: int
    abstract_num_id: int
    ilvl: int
    value: int
    start: int
    start_override: int | None


@dataclass(frozen=True, slots=True)
class _Level:
    num_id: int
    abstract_num_id: int
    ilvl: int
    start: int
    start_override: int | None


class ArticleNumbering:
    """Resolve only unambiguous, exact article-list definitions."""

    def __init__(self, payload: bytes) -> None:
        self.sha256 = sha256(payload).hexdigest()
        root = ET.fromstring(payload)  # noqa: S314 - caller rejects DTD/entities
        self._levels = _definitions(root)
        self._next: dict[tuple[int, int], int] = {}

    def restore(
        self, paragraph: ET.Element, text: str, xml_ordinal: int
    ) -> tuple[str, NumberingProvenance | None]:
        num_pr = paragraph.find(f"./{_W}pPr/{_W}numPr")
        if num_pr is None:
            return text, None
        num_id = _single_int(num_pr, f"{_W}numId")
        ilvl = _single_int(num_pr, f"{_W}ilvl")
        if num_id is None or num_id == 0 or ilvl is None:
            return text, None
        level = self._levels.get((num_id, ilvl))
        if level is None:
            return text, None
        key = (num_id, ilvl)
        value = self._next.get(key, level.start_override or level.start)
        self._next[key] = value + 1
        provenance = NumberingProvenance(
            original_text=text,
            xml_paragraph_ordinal=xml_ordinal,
            numbering_sha256=self.sha256,
            num_id=num_id,
            abstract_num_id=level.abstract_num_id,
            ilvl=ilvl,
            value=value,
            start=level.start,
            start_override=level.start_override,
        )
        marker = f"第{_chinese_counting(value)}条"
        return f"{marker} {text}" if text else marker, provenance


def _definitions(root: ET.Element) -> dict[tuple[int, int], _Level]:
    abstract_nodes: dict[int, list[ET.Element]] = {}
    for abstract in root.findall(f"{_W}abstractNum"):
        abstract_id = _attribute_int(abstract, "abstractNumId")
        if abstract_id is None:
            continue
        abstract_nodes.setdefault(abstract_id, []).append(abstract)

    abstracts: dict[int, tuple[int, str, str]] = {}
    for abstract_id, nodes in abstract_nodes.items():
        if len(nodes) != 1:
            continue
        levels = nodes[0].findall(f"{_W}lvl")
        level_ids = [_attribute_int(level, "ilvl") for level in levels]
        if None in level_ids or len(level_ids) != len(set(level_ids)):
            continue
        matching = [level for level, ilvl in zip(levels, level_ids, strict=True) if ilvl == 0]
        if len(matching) != 1:
            continue
        level = matching[0]
        start = _single_int(level, f"{_W}start")
        fmt = _single_value(level, f"{_W}numFmt")
        template = _single_value(level, f"{_W}lvlText")
        if start is not None:
            abstracts[abstract_id] = (start, fmt or "", template or "")

    num_nodes: dict[int, list[ET.Element]] = {}
    for num in root.findall(f"{_W}num"):
        num_id = _attribute_int(num, "numId")
        if num_id is None or num_id == 0:
            continue
        num_nodes.setdefault(num_id, []).append(num)

    resolved: dict[tuple[int, int], _Level] = {}
    for num_id, nodes in num_nodes.items():
        if len(nodes) != 1:
            continue
        num = nodes[0]
        abstract_id = _single_int(num, f"{_W}abstractNumId")
        if abstract_id is None:
            continue
        overrides: dict[int, int | None] = {}
        invalid_override = False
        for override in num.findall(f"{_W}lvlOverride"):
            ilvl = _attribute_int(override, "ilvl")
            value = _single_int(override, f"{_W}startOverride")
            if ilvl is None or value is None or ilvl in overrides:
                invalid_override = True
                break
            overrides[ilvl] = value
        definition = abstracts.get(abstract_id)
        if invalid_override or definition is None:
            continue
        start, fmt, template = definition
        if fmt != "chineseCounting" or _ARTICLE_TEMPLATE.fullmatch(template) is None:
            continue
        start_override = overrides.get(0)
        initial = start_override if start_override is not None else start
        if initial <= 0 or initial > 9999:
            continue
        resolved[(num_id, 0)] = _Level(num_id, abstract_id, 0, start, start_override)
    return resolved


def _single_int(parent: ET.Element, tag: str) -> int | None:
    values = parent.findall(tag)
    if len(values) != 1:
        return None
    return _attribute_int(values[0], "val")


def _single_value(parent: ET.Element, tag: str) -> str | None:
    values = parent.findall(tag)
    if len(values) != 1:
        return None
    return values[0].get(f"{_W}val")


def _attribute_int(node: ET.Element, name: str) -> int | None:
    raw = node.get(f"{_W}{name}")
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if value >= 0 else None


def _chinese_counting(value: int) -> str:
    if value <= 0 or value > 9999:
        raise ValueError("unsupported Chinese counting value")
    digits = "零一二三四五六七八九"
    units = ((1000, "千"), (100, "百"), (10, "十"))
    remaining = value
    pieces: list[str] = []
    pending_zero = False
    for unit, label in units:
        digit, remaining = divmod(remaining, unit)
        if digit:
            if pending_zero:
                pieces.append("零")
            if not (unit == 10 and digit == 1 and not pieces):
                pieces.append(digits[digit])
            pieces.append(label)
            pending_zero = False
        elif pieces and remaining:
            pending_zero = True
    if remaining:
        if pending_zero:
            pieces.append("零")
        pieces.append(digits[remaining])
    return "".join(pieces)
