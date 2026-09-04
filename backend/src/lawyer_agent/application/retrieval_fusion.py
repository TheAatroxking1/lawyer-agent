"""Reciprocal Rank Fusion for BM25 + dense retrieval results (pure)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from lawyer_agent.domain.legal_search import LegalSearchHit


def reciprocal_rank_fusion(
    bm25: tuple[LegalSearchHit, ...],
    dense: tuple[LegalSearchHit, ...],
    *,
    k: int = 60,
) -> tuple[LegalSearchHit, ...]:
    """Fuse two ranked lists by reciprocal rank.

    Each chunk id contributes 1/(k + rank) from each list it appears in; ids
    present in both lists accumulate. The result is ordered by descending fused
    score with the id tie-break. ``k`` must be a positive integer. Empty inputs
    are safe. Scores in the fused hits are RRF values, not raw model scores.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("fusion k must be a positive integer")
    scores: dict[str, float] = {}
    by_id: dict[str, LegalSearchHit] = {}
    for index, hit in enumerate(_iter(bm25)):
        key = str(hit.chunk_id)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + index + 1)
        by_id.setdefault(key, hit)
    for index, hit in enumerate(_iter(dense)):
        key = str(hit.chunk_id)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + index + 1)
        by_id.setdefault(key, hit)
    ordered = sorted(
        by_id.items(), key=lambda item: (-scores[item[0]], str(item[0]))
    )
    return tuple(
        replace(by_id[key], score=round(scores[key], 9)) for key, _ in ordered
    )


def _iter(hits: tuple[LegalSearchHit, ...]) -> Iterable[LegalSearchHit]:
    for hit in hits:
        if not isinstance(hit, LegalSearchHit):
            raise ValueError("fusion inputs must contain typed search hits")
        yield hit
