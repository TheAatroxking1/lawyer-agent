import pytest

from lawyer_agent.application.legal_navigation_search import LegalNavigationSearchService
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_navigation import NavigationSearchHit, navigation_index_name


class Search:
    def __init__(self, hits=(), *, schema=1, fail=None):
        self.hits = hits
        self.schema = schema
        self.fail = fail
        self.calls = []

    async def navigation_schema(self, main_index_name):
        self.calls.append(("schema", main_index_name))
        if self.fail == "schema":
            raise RuntimeError("synthetic schema unavailable")
        return self.schema

    async def search_navigation(self, index_name, *, query, limit):
        self.calls.append(("search", index_name, query, limit))
        if self.fail == "search":
            raise RuntimeError("synthetic sidecar unavailable")
        return self.hits


async def candidates(search, query="合成民法典规则", **kwargs):
    return await LegalNavigationSearchService(search).candidate_versions(
        main_index_name="main-a",
        query=query,
        **kwargs,
    )


async def test_locator_normalization_and_ordered_deduplication() -> None:
    first, second = new_uuid7(), new_uuid7()
    search = Search(
        (
            NavigationSearchHit(second, "《Ａ法》", 0.1),
            NavigationSearchHit(first, "第一章 总则", 3.0),
            NavigationSearchHit(second, "Ａ法", 8.0),
        )
    )
    assert await candidates(search, "请解释《A 法》第一章总则") == (second, first)
    assert search.calls[1] == (
        "search",
        navigation_index_name("main-a"),
        "请解释《A 法》第一章总则",
        50,
    )


@pytest.mark.parametrize(
    "locator,query",
    [
        ("合成民法典", "民法典规则"),
        ("第一章 总则", "第一章有什么规定"),
        ("合成民法典", "合成解释民法典"),
        ("《 》", "一般问题"),
    ],
)
async def test_weak_or_noncontiguous_locator_uses_global_search(locator, query) -> None:
    assert (
        await candidates(Search((NavigationSearchHit(new_uuid7(), locator, 100.0),)), query) is None
    )


async def test_empty_hits_and_legacy_index_use_global_search() -> None:
    assert await candidates(Search()) is None
    legacy = Search(schema=None)
    assert await candidates(legacy) is None
    assert legacy.calls == [("schema", "main-a")]


async def test_too_many_strong_versions_or_full_scan_uses_global_search() -> None:
    hits = tuple(NavigationSearchHit(new_uuid7(), "合成民法典", 1.0) for _ in range(3))
    assert await candidates(Search(hits), candidate_limit=2) is None
    assert await candidates(Search(hits), scan_size=3) is None
    assert await candidates(Search(hits), scan_size=2) is None


@pytest.mark.parametrize("schema", [0, 2, True, "1", 1.0])
async def test_unsupported_marker_fails_closed(schema) -> None:
    search = Search(schema=schema)
    with pytest.raises(RuntimeError):
        await candidates(search)
    assert len(search.calls) == 1


@pytest.mark.parametrize("stage", ["schema", "search"])
async def test_service_failure_is_never_global_fallback(stage) -> None:
    with pytest.raises(RuntimeError, match="synthetic"):
        await candidates(Search(fail=stage))


@pytest.mark.parametrize(
    "params",
    [
        {"query": ""},
        {"query": "《 》"},
        {"candidate_limit": 0},
        {"candidate_limit": True},
        {"scan_size": 0},
        {"scan_size": 1.0},
    ],
)
async def test_invalid_arguments_rejected_without_calls(params) -> None:
    search = Search()
    with pytest.raises(ValueError):
        await candidates(search, **params)
    assert search.calls == []
