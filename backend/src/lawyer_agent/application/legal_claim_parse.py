"""Parse model output text into structured LegalClaim (non-model).

Model output is untrusted content. This parser accepts the agreed JSON shape
``{"claims": [{"text": ..., "evidence_ids": [uuid...]}, ...]}``, rejects
unknown keys and empty/malformed fields, and performs at most one controlled
repair (strip a markdown fence or locate the JSON object inside prose) before
failing safe with a stable error. Errors never echo the full input text.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from lawyer_agent.application.legal_claim_gate import LegalClaim
from lawyer_agent.domain.common import is_uuid7

_CLAIM_KEYS = frozenset({"text", "evidence_ids"})


class LegalClaimsParseError(ValueError):
    """Model output could not be parsed into structured claims."""


def parse_claims_text(
    text: str,
    *,
    max_claims: int = 20,
    max_text_chars: int = 2000,
    max_evidence_per_claim: int = 20,
) -> tuple[LegalClaim, ...]:
    if not isinstance(text, str) or not text.strip():
        raise LegalClaimsParseError("claims output must be non-empty text")
    if isinstance(max_claims, bool) or not isinstance(max_claims, int) or max_claims < 1:
        raise ValueError("max_claims must be a positive integer")
    if (
        isinstance(max_text_chars, bool)
        or not isinstance(max_text_chars, int)
        or max_text_chars < 1
    ):
        raise ValueError("max_text_chars must be a positive integer")
    if (
        isinstance(max_evidence_per_claim, bool)
        or not isinstance(max_evidence_per_claim, int)
        or max_evidence_per_claim < 1
    ):
        raise ValueError("max_evidence_per_claim must be a positive integer")

    payload = _parse_json_once(text)
    if payload is None:
        raise LegalClaimsParseError(
            "claims output is not valid JSON and could not be repaired"
        )
    if not isinstance(payload, dict):
        raise LegalClaimsParseError("claims output must be a JSON object")
    raw_claims = payload.get("claims")
    if raw_claims is None:
        raise LegalClaimsParseError("claims output is missing the claims array")
    if not isinstance(raw_claims, list):
        raise LegalClaimsParseError("claims output must contain a claims array")
    if len(raw_claims) > max_claims:
        raise LegalClaimsParseError(
            f"claims output exceeds the limit of {max_claims} claims"
        )

    claims: list[LegalClaim] = []
    for index, raw in enumerate(raw_claims):
        claim = _parse_claim(
            raw,
            claim_index=index,
            max_text_chars=max_text_chars,
            max_evidence_per_claim=max_evidence_per_claim,
        )
        claims.append(claim)
    return tuple(claims)


def _parse_json_once(text: str) -> Any | None:
    """Strict parse with one controlled repair attempt (spec 7.6)."""
    for candidate in (_candidates(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _candidates(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        # Strip one markdown fence (```json ... ```).
        lines = stripped.splitlines()
        if len(lines) >= 2:
            inner = "\n".join(lines[1:])
            if inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3]
            candidates.append(inner.strip())
    # Last resort: locate the first '{' to the last '}' in the text.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    return tuple(candidates)


def _parse_claim(
    raw: Any,
    *,
    claim_index: int,
    max_text_chars: int,
    max_evidence_per_claim: int,
) -> LegalClaim:
    if not isinstance(raw, dict):
        raise LegalClaimsParseError(f"claim #{claim_index} must be a JSON object")
    unknown = set(raw) - _CLAIM_KEYS
    if unknown:
        raise LegalClaimsParseError(
            f"claim #{claim_index} contains unknown keys"
        )
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LegalClaimsParseError(f"claim #{claim_index} text must be non-empty")
    if len(text) > max_text_chars:
        raise LegalClaimsParseError(
            f"claim #{claim_index} text exceeds the size limit"
        )
    raw_ids = raw.get("evidence_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise LegalClaimsParseError(
            f"claim #{claim_index} must bind at least one evidence id"
        )
    if len(raw_ids) > max_evidence_per_claim:
        raise LegalClaimsParseError(
            f"claim #{claim_index} exceeds the evidence id limit"
        )
    evidence_ids: list[UUID] = []
    for item in raw_ids:
        try:
            evidence_id = UUID(str(item))
        except (ValueError, TypeError) as exc:
            raise LegalClaimsParseError(
                f"claim #{claim_index} evidence id is not a UUID"
            ) from exc
        if not is_uuid7(evidence_id):
            raise LegalClaimsParseError(
                f"claim #{claim_index} evidence id is not a UUIDv7"
            )
        evidence_ids.append(evidence_id)
    if len(set(evidence_ids)) != len(evidence_ids):
        raise LegalClaimsParseError(
            f"claim #{claim_index} evidence ids must be unique"
        )
    return LegalClaim(
        text=text.strip(),
        evidence_ids=tuple(evidence_ids),
    )
