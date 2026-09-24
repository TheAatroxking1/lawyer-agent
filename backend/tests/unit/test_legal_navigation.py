from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from uuid import uuid4

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_navigation import (
    NavigationDocument,
    NavigationDocumentKind,
    NavigationSearchHit,
    navigation_index_name,
)


def test_navigation_document_is_frozen_and_strongly_validated() -> None:
    document = NavigationDocument(
        "a" * 64,
        new_uuid7(),
        NavigationDocumentKind.INSTRUMENT,
        "民法典",
        "民法典",
        "navigation-v1",
    )
    with pytest.raises(FrozenInstanceError):
        document.locator = "changed"  # type: ignore[misc]
    for changes in (
        {"navigation_id": "A" * 64},
        {"navigation_id": "a" * 63},
        {"version_id": uuid4()},
        {"kind": "instrument"},
        {"locator": ""},
        {"content": " "},
        {"parser_version": None},
    ):
        with pytest.raises(ValueError):
            replace(document, **changes)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, "1", 1])
def test_navigation_hit_rejects_invalid_score(score: object) -> None:
    with pytest.raises(ValueError):
        NavigationSearchHit(new_uuid7(), "民法典", score)  # type: ignore[arg-type]


def test_navigation_hit_checks_identity_and_locator() -> None:
    hit = NavigationSearchHit(new_uuid7(), "民法典", 1.0)
    assert hit.score == 1.0
    with pytest.raises(ValueError):
        replace(hit, version_id=uuid4())
    with pytest.raises(ValueError):
        replace(hit, locator=" ")


def test_navigation_name_is_bounded_and_deterministic() -> None:
    name = "lawyer_dataset_" + "a" * 230
    assert navigation_index_name(name) == "lawyer-nav-" + sha256(name.encode()).hexdigest()[:32]
    assert navigation_index_name("index-a") != navigation_index_name("index-b")


@pytest.mark.parametrize("name", ["", "A", "_all", "a,b", "a/b", "a*", "a" * 256, None])
def test_navigation_name_rejects_unsafe_names(name: object) -> None:
    with pytest.raises(ValueError):
        navigation_index_name(name)  # type: ignore[arg-type]
