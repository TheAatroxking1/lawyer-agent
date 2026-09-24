import asyncio
import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lawyer_agent.application.contract_review.contracts import (
    ReviewError,
    ReviewRequest,
    ReviewScope,
    RunLimits,
)
from lawyer_agent.application.contract_review.research import (
    BoundedContractResearch,
    ContractBrief,
    LegalCandidate,
    LegalDocument,
    LegalDocumentPage,
    ResearchBudget,
    ResearchResult,
)


class Model:
    def __init__(self, responses: list[str | BaseException]) -> None:
        self.responses = iter(responses)
        self.messages = []

    async def chat(self, messages):
        self.messages.append(messages)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class Tools:
    def __init__(self) -> None:
        self.search_results: dict[str, tuple[LegalCandidate, ...]] = {}
        self.pages: dict[tuple[str, str | None], LegalDocumentPage | BaseException] = {}
        self.search_calls: list[str] = []
        self.read_calls: list[tuple[str, str | None]] = []

    async def search(self, scope, query):
        self.search_calls.append(query)
        return self.search_results.get(query, ())

    async def read(self, scope, document_id, cursor):
        self.read_calls.append((document_id, cursor))
        page = self.pages[(document_id, cursor)]
        if isinstance(page, BaseException):
            raise page
        return page


SCOPE = ReviewScope(
    tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(), document_version_id="contract-1"
)
REQUEST = ReviewRequest(instruction="检查付款与解除条款", as_of=date(2026, 9, 16))
BRIEF = json.dumps(
    {
        "is_contract": True,
        "contract_type": "采购合同",
        "fact_summary": "采购方购买设备并分期付款。",
        "queries": ["采购合同 付款", "采购合同 解除"],
    },
    ensure_ascii=False,
)


@pytest.mark.asyncio
async def test_invalid_brief_reports_safe_field_diagnostics(caplog):
    raw = json.dumps({"is_contract": True, "contract_type": "采购合同",
                      "PRIVATE_FIELD": "PRIVATE_CONTRACT_BODY"})
    tools = Tools()
    with pytest.raises(ReviewError, match="research_model_output_invalid"):
        await BoundedContractResearch(model=Model([raw, raw]), tools=tools).run(
            scope=SCOPE, request=REQUEST, document="PRIVATE_SOURCE",
            budget=ResearchBudget(limits=RunLimits()),
        )
    assert "stage=brief" in caplog.text
    assert "fact_summary:missing" in caplog.text
    assert "response:extra_forbidden" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert not tools.search_calls


@pytest.mark.parametrize(("changes", "code"), [
    ({"fact_summary": " "}, "brief_summary_blank"),
    ({"queries": [" "]}, "brief_query_blank"),
    ({"contract_type": None}, "brief_contract_type_required"),
    ({"queries": []}, "brief_queries_required"),
    ({"is_contract": False}, "brief_non_contract_research"),
])
def test_brief_inconsistency_has_specific_safe_code(changes, code):
    with pytest.raises(ValidationError) as caught:
        ContractBrief.model_validate({**json.loads(BRIEF), **changes})
    assert caught.value.errors(include_input=False)[0]["type"] == code


@pytest.mark.asyncio
async def test_duplicate_focuses_are_normalized_without_regenerating_summary():
    raw = json.dumps({**json.loads(BRIEF), "queries": ["付款", "付款", "解除", "付款"]})
    tools = Tools()
    model = Model([raw, '{"document_ids":[]}'])
    result = await BoundedContractResearch(model=model, tools=tools).run(
        scope=SCOPE, request=REQUEST, document="完整原文",
        budget=ResearchBudget(limits=RunLimits()),
    )
    assert result.brief.queries == ("付款", "解除")
    assert result.brief.fact_summary == json.loads(BRIEF)["fact_summary"]
    assert tools.search_calls == [_query("付款"), _query("解除")]
    assert len(model.messages) == 2


@pytest.mark.asyncio
async def test_brief_regenerates_once_with_specific_feedback_and_same_source(caplog):
    malformed = json.dumps({**json.loads(BRIEF), "queries": []})
    tools = Tools()
    model = Model([malformed, BRIEF, '{"document_ids":[]}'])
    budget = ResearchBudget(limits=RunLimits())
    result = await BoundedContractResearch(model=model, tools=tools).run(
        scope=SCOPE, request=REQUEST, document="完整合成合同正文", budget=budget,
    )
    assert result.brief == ContractBrief.model_validate_json(BRIEF)
    assert len(model.messages) == budget.model_calls == 3
    assert model.messages[1][1].content == model.messages[0][1].content
    assert "brief_queries_required" in model.messages[1][0].content
    assert "brief_queries_required" in caplog.text
    assert len(tools.search_calls) == 2


@pytest.mark.asyncio
async def test_brief_repair_stops_at_second_invalid_output_and_obeys_budget():
    malformed = json.dumps({**json.loads(BRIEF), "queries": []})
    for steps, expected_calls, code in [
        (12, 2, "research_model_output_invalid"), (1, 1, "step_limit_exceeded"),
    ]:
        model = Model([malformed, malformed, BRIEF])
        tools = Tools()
        budget = ResearchBudget(limits=RunLimits(max_steps=steps))
        with pytest.raises(ReviewError, match=code):
            await BoundedContractResearch(model=model, tools=tools).run(
                scope=SCOPE, request=REQUEST, document="完整原文", budget=budget,
            )
        assert len(model.messages) == expected_calls
        assert not tools.search_calls


def _query(focus: str) -> str:
    return "合同类型：采购合同\n内容总结：采购方购买设备并分期付款。\n检索重点：" + focus


def candidate(document_id: str, title: str) -> LegalCandidate:
    return LegalCandidate(
        document_id=document_id,
        title=title,
        snippets=(f"{title}相关片段",),
        citations=(f"{title}第一条",),
    )


def page(
    document_id: str,
    title: str,
    *,
    cursor: str | None = None,
    next_cursor: str | None = None,
    text: str = "法规全文",
    complete: bool = True,
    version: str = "v1",
    content_hash: str = "a" * 64,
    quality: str = "verified",
    quality_flags: tuple[str, ...] = (),
) -> LegalDocumentPage:
    return LegalDocumentPage(
        document_id=document_id,
        title=title,
        version=version,
        content_hash=content_hash,
        cursor=cursor,
        next_cursor=next_cursor,
        text=text,
        complete=complete,
        quality=quality,
        quality_flags=quality_flags,
        source_ref=f"corpus:{document_id}",
    )


@pytest.mark.asyncio
async def test_summary_driven_research_calls_model_once_then_reads_ranked_candidates():
    tools = Tools()
    model = Model([BRIEF])
    query = "合同类型：采购合同\n内容总结：采购方购买设备并分期付款。"
    tools.search_results[query] = (candidate("law-1", "采购规则"),)
    tools.pages[("law-1", None)] = page("law-1", "采购规则", text="命中的相关段落")
    budget = ResearchBudget(limits=RunLimits())
    result = await BoundedContractResearch(
        model=model, tools=tools, summary_driven=True,
    ).run(scope=SCOPE, request=REQUEST, document="PRIVATE_COMPLETE_CONTRACT", budget=budget)
    assert len(model.messages) == 1 and budget.model_calls == 1
    assert tools.search_calls == [query]
    assert "PRIVATE_COMPLETE_CONTRACT" not in query
    assert result.evidence_ids == ("law-1",)
    assert result.documents[0].text == "命中的相关段落"


@pytest.mark.asyncio
async def test_summary_driven_empty_retrieval_does_not_add_selection_model_call():
    model, tools = Model([BRIEF]), Tools()
    result = await BoundedContractResearch(
        model=model, tools=tools, summary_driven=True,
    ).run(scope=SCOPE, request=REQUEST, document="合同", budget=ResearchBudget(limits=RunLimits()))
    assert result.status == "no_evidence"
    assert len(model.messages) == len(tools.search_calls) == 1
    assert not tools.read_calls


@pytest.mark.asyncio
async def test_summary_driven_keeps_all_seven_reranked_sources():
    model, tools = Model([BRIEF]), Tools()
    query = "合同类型：采购合同\n内容总结：采购方购买设备并分期付款。"
    tools.search_results[query] = tuple(candidate(f"law-{i}", f"规则{i}") for i in range(7))
    tools.pages = {(f"law-{i}", None): page(f"law-{i}", f"规则{i}") for i in range(7)}
    result = await BoundedContractResearch(model=model, tools=tools, summary_driven=True).run(
        scope=SCOPE, request=REQUEST, document="合同", budget=ResearchBudget(limits=RunLimits()),
    )
    assert len(result.documents) == 7


def test_contract_brief_rejects_inconsistent_non_contract_shape():
    with pytest.raises(ValidationError):
        ContractBrief(
            is_contract=False,
            contract_type="采购合同",
            fact_summary="并非合同",
            queries=("不应检索",),
        )


def test_model_schema_requires_all_summary_fields_without_implicit_search_defaults():
    schema = ContractBrief.model_json_schema()
    assert set(schema["required"]) == {"is_contract", "contract_type", "fact_summary", "queries"}
    for field in ("contract_type", "queries"):
        assert "default" not in schema["properties"][field]


@pytest.mark.asyncio
async def test_rag_uses_summary_with_focus_without_contract_body():
    tools = Tools()
    model = Model([BRIEF, '{"document_ids":[]}'])
    document = "PRIVATE_FULL_DOCUMENT_MARKER " * 500
    result = await BoundedContractResearch(model=model, tools=tools).run(
        scope=SCOPE, request=REQUEST, document=document,
        budget=ResearchBudget(limits=RunLimits()),
    )
    assert result.brief.fact_summary == "采购方购买设备并分期付款。"
    assert tools.search_calls == [
        "合同类型：采购合同\n内容总结：采购方购买设备并分期付款。\n检索重点：采购合同 付款",
        "合同类型：采购合同\n内容总结：采购方购买设备并分期付款。\n检索重点：采购合同 解除",
    ]
    assert all("PRIVATE_FULL_DOCUMENT_MARKER" not in query for query in tools.search_calls)
    assert all(len(query) <= 2000 for query in tools.search_calls)
    assert document in json.loads(model.messages[0][1].content)["untrusted_document"]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [" ", "摘要" * 601], ids=["blank", "oversized"])
async def test_invalid_summary_stops_before_rag(summary):
    brief = json.loads(BRIEF)
    brief["fact_summary"] = summary
    tools = Tools()
    with pytest.raises(ReviewError, match="research_model_output_invalid"):
        await BoundedContractResearch(model=Model([json.dumps(brief)] * 2), tools=tools).run(
            scope=SCOPE, request=REQUEST, document="合同正文",
            budget=ResearchBudget(limits=RunLimits()),
        )
    assert not tools.search_calls


@pytest.mark.asyncio
async def test_maximum_summary_and_focus_fit_mcp_query_limit():
    brief = {
        "is_contract": True, "contract_type": "类" * 200,
        "fact_summary": "摘" * 1200, "queries": ["重" * 200],
    }
    tools = Tools()
    await BoundedContractResearch(
        model=Model([json.dumps(brief), '{"document_ids":[]}']), tools=tools
    ).run(scope=SCOPE, request=REQUEST, document="原文",
          budget=ResearchBudget(limits=RunLimits()))
    assert len(tools.search_calls) == 1
    assert brief["fact_summary"] in tools.search_calls[0]
    assert len(tools.search_calls[0]) <= 2000


@pytest.mark.asyncio
async def test_research_deduplicates_top7_selects_three_and_reads_complete_pages():
    tools = Tools()
    shared = candidate("law-shared", "民法典")
    tools.search_results = {
        _query("采购合同 付款"): (shared,) + tuple(
            candidate(f"law-{i}", f"法规{i}") for i in range(1, 7)
        ),
        _query("采购合同 解除"): (shared, candidate("law-7", "法规7")),
    }
    tools.pages = {
        ("law-shared", None): page(
            "law-shared", "民法典", next_cursor="p2", complete=False, text="第一页"
        ),
        ("law-shared", "p2"): page(
            "law-shared", "民法典", cursor="p2", text="第二页"
        ),
        ("law-1", None): page("law-1", "法规1"),
        ("law-2", None): page("law-2", "法规2"),
    }
    model = Model([BRIEF, '{"document_ids":["law-shared","law-1","law-2"]}'])
    budget = ResearchBudget(limits=RunLimits(max_steps=8, max_tool_calls=8))

    result = await BoundedContractResearch(model=model, tools=tools).run(
        scope=SCOPE,
        request=REQUEST,
        document='{"blocks":[{"text":"不可信合同原文"}]}',
        budget=budget,
    )

    assert result.status == "ready"
    assert [item.document_id for item in result.candidates] == [
        "law-shared",
        "law-1",
        "law-7",
        "law-2",
        "law-3",
        "law-4",
        "law-5",
    ]
    assert result.evidence_ids == ("law-shared", "law-1", "law-2")
    assert {
        key: result.documents[0].model_dump()[key]
        for key in (
            "document_id",
            "title",
            "version",
            "content_hash",
            "text",
            "source_ref",
            "quality_flags",
        )
    } == {
        "document_id": "law-shared",
        "title": "民法典",
        "version": "v1",
        "content_hash": "a" * 64,
        "text": "第一页第二页",
        "source_ref": "corpus:law-shared",
        "quality_flags": (),
    }
    assert tools.read_calls == [
        ("law-shared", None),
        ("law-shared", "p2"),
        ("law-1", None),
        ("law-2", None),
    ]
    assert "第一页第二页" in result.context
    assert budget.model_calls == 2 and budget.tool_calls == 6
    assert all(messages[0].role == "system" for messages in model.messages)
    assert all("隐藏推理" in messages[0].content for messages in model.messages)
    assert all(messages[1].role == "user" for messages in model.messages)
    assert "不可信合同原文" not in model.messages[0][0].content


@pytest.mark.asyncio
async def test_zero_selection_is_normal_no_evidence_with_fixed_message():
    tools = Tools()
    tools.search_results[_query("采购合同 付款")] = (candidate("law-1", "民法典"),)
    model = Model([BRIEF, '{"document_ids":[]}'])
    result = await BoundedContractResearch(model=model, tools=tools).run(
        scope=SCOPE,
        request=REQUEST,
        document="合同",
        budget=ResearchBudget(limits=RunLimits()),
    )
    assert result.status == "no_evidence"
    assert result.context == "我没有找到相关的法律文档，暂时无法提供相关服务"
    assert not tools.read_calls
    assert result.documents == ()


def test_research_result_rejects_inconsistent_evidence_and_context():
    brief = ContractBrief(
        is_contract=True,
        contract_type="采购合同",
        fact_summary="设备采购",
        queries=("采购合同",),
    )
    document = page("law-1", "民法典")
    full = LegalDocument(
        document_id=document.document_id,
        title=document.title,
        version=document.version,
        content_hash=document.content_hash,
        text=document.text,
        source_ref=document.source_ref,
    )
    with pytest.raises(ValidationError):
        ResearchResult(
            status="ready",
            brief=brief,
            candidates=(candidate("law-1", "民法典"),),
            evidence_ids=("other",),
            evidence_titles=("民法典",),
            documents=(full,),
            context="untrusted",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_page,code",
    [
        (page("other", "民法典"), "legal_document_identity_mismatch"),
        (page("law-1", "民法典", quality="needs_review"), "legal_document_unverified"),
        (
            page("law-1", "民法典", next_cursor="p2", complete=False),
            "legal_document_cursor_invalid",
        ),
    ],
)
async def test_research_rejects_untrusted_or_incomplete_document_pages(bad_page, code):
    tools = Tools()
    tools.search_results[_query("采购合同 付款")] = (candidate("law-1", "民法典"),)
    tools.pages[("law-1", None)] = bad_page
    model = Model([BRIEF, '{"document_ids":["law-1"]}'])
    with pytest.raises(ReviewError, match=code):
        await BoundedContractResearch(model=model, tools=tools, max_pages_per_document=1).run(
            scope=SCOPE,
            request=REQUEST,
            document="合同",
            budget=ResearchBudget(limits=RunLimits()),
        )


@pytest.mark.asyncio
async def test_research_rejects_version_or_hash_drift_between_pages():
    tools = Tools()
    tools.search_results[_query("采购合同 付款")] = (candidate("law-1", "民法典"),)
    tools.pages[("law-1", None)] = page(
        "law-1", "民法典", next_cursor="p2", complete=False
    )
    tools.pages[("law-1", "p2")] = page(
        "law-1", "民法典", cursor="p2", version="v2"
    )
    model = Model([BRIEF, '{"document_ids":["law-1"]}'])
    with pytest.raises(ReviewError, match="legal_document_version_mismatch"):
        await BoundedContractResearch(model=model, tools=tools).run(
            scope=SCOPE,
            request=REQUEST,
            document="合同",
            budget=ResearchBudget(limits=RunLimits()),
        )


@pytest.mark.asyncio
async def test_research_budget_and_context_are_hard_limits():
    tools = Tools()
    tools.search_results[_query("采购合同 付款")] = (candidate("law-1", "民法典"),)
    tools.pages[("law-1", None)] = page("law-1", "民法典", text="法" * 1000)
    model = Model([BRIEF, '{"document_ids":["law-1"]}'])
    with pytest.raises(ReviewError, match="context_limit_exceeded"):
        await BoundedContractResearch(model=model, tools=tools).run(
            scope=SCOPE,
            request=REQUEST,
            document="合同",
            budget=ResearchBudget(limits=RunLimits(max_context_bytes=1024)),
        )

    budget = ResearchBudget(limits=RunLimits(max_steps=1))
    with pytest.raises(ReviewError, match="step_limit_exceeded"):
        await BoundedContractResearch(model=Model([BRIEF]), tools=Tools()).run(
            scope=SCOPE, request=REQUEST, document="合同", budget=budget
        )


@pytest.mark.asyncio
async def test_provider_and_tool_failures_have_distinct_codes_and_cancellation_propagates():
    with pytest.raises(ReviewError, match="research_model_unavailable"):
        await BoundedContractResearch(
            model=Model([RuntimeError("PRIVATE_PROVIDER")]), tools=Tools()
        ).run(
            scope=SCOPE,
            request=REQUEST,
            document="合同",
            budget=ResearchBudget(limits=RunLimits()),
        )

    tools = Tools()
    tools.search_results[_query("采购合同 付款")] = (candidate("law-1", "民法典"),)
    tools.pages[("law-1", None)] = RuntimeError("PRIVATE_MCP")
    with pytest.raises(ReviewError, match="legal_research_unavailable"):
        await BoundedContractResearch(
            model=Model([BRIEF, '{"document_ids":["law-1"]}']), tools=tools
        ).run(
            scope=SCOPE,
            request=REQUEST,
            document="合同",
            budget=ResearchBudget(limits=RunLimits()),
        )

    entered = asyncio.Event()

    class CancellingModel:
        async def chat(self, messages):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        BoundedContractResearch(model=CancellingModel(), tools=Tools()).run(
            scope=SCOPE,
            request=REQUEST,
            document="合同",
            budget=ResearchBudget(limits=RunLimits()),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class ToolResult:
    def __init__(self, structured_content=None, *, is_error=False):
        self.structured_content = structured_content
        self.is_error = is_error


class Client:
    def __init__(
        self,
        results,
        *,
        search_name="legal_search_documents",
        read_name="legal_read_document",
    ):
        self.results = iter(results)
        self.calls = []
        self.search_name = search_name
        self.read_name = read_name

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(
                name=self.search_name,
                input_schema={"type": "object", "required": ["request"]},
                output_schema={"type": "object"},
                meta={
                    "lawyer_agent/capability": "legal.search_documents",
                    "lawyer_agent/agent_visible": True,
                    "lawyer_agent/resource_constant": "public-law",
                },
            ),
            SimpleNamespace(
                name=self.read_name,
                input_schema={"type": "object", "required": ["request"]},
                output_schema={"type": "object"},
                meta={
                    "lawyer_agent/capability": "legal.read_document",
                    "lawyer_agent/agent_visible": True,
                    "lawyer_agent/resource_path": "/request/document_id",
                },
            ),
        ])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return next(self.results)


class Authorizer:
    def __init__(self):
        self.calls = []

    async def require(self, scope, action, resource_id):
        self.calls.append((scope, action, resource_id))


@pytest.mark.asyncio
async def test_mcp_research_adapter_authorizes_and_parses_only_structured_results():
    from lawyer_agent.infrastructure.contract_review.research import MCPResearchTools

    item = candidate("law-1", "民法典")
    legal_page = page("law-1", "民法典")
    client = Client(
        [
            ToolResult({"items": [item.model_dump(mode="json")]}),
            ToolResult(legal_page.model_dump(mode="json")),
        ]
    )
    authorizer = Authorizer()
    adapter = MCPResearchTools(client=client, authorizer=authorizer)
    await adapter.discover()

    assert await adapter.search(SCOPE, "采购合同") == (item,)
    assert await adapter.read(SCOPE, "law-1", None) == legal_page
    assert client.calls == [
        ("legal_search_documents", {"request": {"query": "采购合同"}}),
        (
            "legal_read_document",
            {"request": {"document_id": "law-1", "cursor": None}},
        ),
    ]
    assert [(action, resource) for _, action, resource in authorizer.calls] == [
        ("legal_search_documents", "public-law"),
        ("legal_read_document", "law-1"),
    ]


@pytest.mark.asyncio
async def test_mcp_research_adapter_fails_closed_without_structured_content():
    from lawyer_agent.infrastructure.contract_review.research import MCPResearchTools

    adapter = MCPResearchTools(
        client=Client([ToolResult(None), ToolResult({}, is_error=True)]),
        authorizer=Authorizer(),
    )
    await adapter.discover()
    with pytest.raises(ReviewError, match="mcp_call_failed"):
        await adapter.search(SCOPE, "采购合同")
    with pytest.raises(ReviewError, match="mcp_call_failed"):
        await adapter.read(SCOPE, "law-1", None)


@pytest.mark.asyncio
async def test_mcp_research_adapter_bounds_structured_bytes_before_type_validation():
    from lawyer_agent.infrastructure.contract_review.research import MCPResearchTools

    adapter = MCPResearchTools(
        client=Client([ToolResult({"items": [{"oversized": "法" * 1000}]})]),
        authorizer=Authorizer(),
        max_response_bytes=1024,
    )
    await adapter.discover()
    with pytest.raises(ReviewError, match="tool_result_too_large"):
        await adapter.search(SCOPE, "采购合同")


@pytest.mark.asyncio
async def test_mcp_research_discovers_capabilities_when_server_renames_tools():
    from lawyer_agent.infrastructure.contract_review.research import MCPResearchTools

    item = candidate("law-1", "民法典")
    legal_page = page("law-1", "民法典")
    client = Client(
        [
            ToolResult({"items": [item.model_dump(mode="json")]}),
            ToolResult(legal_page.model_dump(mode="json")),
        ],
        search_name="search_v2",
        read_name="read_v2",
    )
    adapter = MCPResearchTools(client=client, authorizer=Authorizer())
    await adapter.discover()

    assert await adapter.search(SCOPE, "采购合同") == (item,)
    assert await adapter.read(SCOPE, "law-1", None) == legal_page
    assert [name for name, _ in client.calls] == ["search_v2", "read_v2"]


class WorkflowTools:
    def __init__(self, limits, tools=()):
        self.scope = SCOPE
        self.limits = limits
        self.tools = list(tools)
        self.prepared = 0

    async def prepare(self):
        self.prepared += 1
        return '{"blocks":[{"block_id":"b1","text":"合同"}]}'

    async def call(self, name, arguments):
        raise AssertionError("no ReAct tool call expected")

    def reset(self):
        pass


class BudgetAwareWorkflowTools(WorkflowTools):
    accounts_for_tool_budget = True

    def bind_budget(self, budget):
        self.budget = budget

    async def prepare(self):
        self.budget.consume_tool()
        return await super().prepare()


class WorkflowGate:
    def __init__(self):
        self.calls = 0
        self.workflow = None
        self.research_seen = None

    async def validate(self, scope, request, candidate):
        from lawyer_agent.application.contract_review.contracts import ReviewResult

        self.calls += 1
        if self.workflow is not None:
            self.research_seen = self.workflow.research_result
        return ReviewResult(
            document_version_id=scope.document_version_id,
            run_id=scope.run_id,
            issues=candidate.issues,
        )


class WorkflowStore:
    def __init__(self):
        self.saved = []

    async def save(self, scope, result):
        self.saved.append(result)


class Research:
    def __init__(self, result, *, consume_model=0, consume_tool=0):
        self.result = result
        self.consume_model = consume_model
        self.consume_tool = consume_tool
        self.documents = []

    async def run(self, *, scope, request, document, budget, on_progress=None):
        self.documents.append(document)
        if on_progress is not None:
            for stage in (
                "contract_understanding",
                "legal_search",
                "legal_selection",
                "legal_read",
            ):
                on_progress(stage)
        for _ in range(self.consume_model):
            budget.consume_model()
        for _ in range(self.consume_tool):
            budget.consume_tool()
        return self.result


class ToolMetadata:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class VisibleTool:
    def __init__(self, name):
        self.metadata = ToolMetadata(name)


def research_result(status="ready"):
    brief = ContractBrief(
        is_contract=True,
        contract_type="采购合同",
        fact_summary="设备采购",
        queries=("采购合同",),
    )
    if status == "no_evidence":
        return ResearchResult(
            status=status,
            brief=brief,
            context="我没有找到相关的法律文档，暂时无法提供相关服务",
        )
    document = LegalDocument(
        document_id="law-1",
        title="民法典",
        version="v1",
        content_hash="a" * 64,
        text="完整法律文本",
        source_ref="corpus:law-1",
    )
    from lawyer_agent.application.contract_review.research import serialize_research_documents

    return ResearchResult(
        status=status,
        brief=brief,
        candidates=(candidate("law-1", "民法典"),),
        evidence_ids=("law-1",),
        evidence_titles=("民法典",),
        documents=(document,),
        context=serialize_research_documents((document,)),
    )


@pytest.mark.asyncio
async def test_workflow_injects_ready_research_context_before_react_and_saves():
    pytest.importorskip("llama_index.core")
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    limits = RunLimits()
    tools = WorkflowTools(limits)
    gate, store = WorkflowGate(), WorkflowStore()
    model = Model(['Thought: 无隐藏内容\nAnswer: {"reviewed_block_ids":["b1"],"issues":[]}'])
    workflow = ContractReviewWorkflow(
        scope=SCOPE,
        model=model,
        tools=tools,
        gate=gate,
        store=store,
        research=Research(research_result()),
        limits=limits,
    )
    gate.workflow = workflow
    result = await workflow.review(REQUEST)
    assert result.status == "draft" and gate.calls == 1 and store.saved == [result]
    assert "完整法律文本" in "\n".join(m.content for m in model.messages[0])
    assert "law-1" in "\n".join(m.content for m in model.messages[0])
    assert gate.research_seen is not None
    assert gate.research_seen.evidence_ids == ("law-1",)
    assert workflow.research_result is None
    assert workflow.progress == (
        "preparing",
        "researching",
        "contract_understanding",
        "legal_search",
        "legal_selection",
        "legal_read",
        "reviewing",
        "validating",
        "saved",
    )


@pytest.mark.asyncio
async def test_workflow_returns_structured_no_evidence_without_react_gate_or_save():
    pytest.importorskip("llama_index.core")
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    limits = RunLimits()
    tools = WorkflowTools(limits)
    gate, store = WorkflowGate(), WorkflowStore()
    model = Model([])
    stages = []
    workflow = ContractReviewWorkflow(
        scope=SCOPE,
        model=model,
        tools=tools,
        gate=gate,
        store=store,
        research=Research(research_result("no_evidence")),
        on_progress=stages.append,
        limits=limits,
    )
    result = await workflow.review(REQUEST)
    assert result.status == "no_evidence"
    assert not model.messages and gate.calls == 0 and store.saved == []
    assert workflow.progress == (
        "preparing",
        "researching",
        "contract_understanding",
        "legal_search",
        "legal_selection",
        "legal_read",
        "no_evidence",
    )
    assert stages == list(workflow.progress)
    assert workflow.has_active_state is False


@pytest.mark.asyncio
async def test_workflow_research_and_react_share_model_budget():
    pytest.importorskip("llama_index.core")
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    limits = RunLimits(max_steps=1)
    workflow = ContractReviewWorkflow(
        scope=SCOPE,
        model=Model(['Thought: x\nAnswer: {"reviewed_block_ids":["b1"],"issues":[]}']),
        tools=WorkflowTools(limits),
        gate=WorkflowGate(),
        store=WorkflowStore(),
        research=Research(research_result(), consume_model=1),
        limits=limits,
    )
    with pytest.raises(ReviewError, match="step_limit_exceeded"):
        await workflow.review(REQUEST)


@pytest.mark.asyncio
async def test_workflow_prepare_and_research_share_tool_budget():
    pytest.importorskip("llama_index.core")
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    limits = RunLimits(max_tool_calls=1)
    workflow = ContractReviewWorkflow(
        scope=SCOPE,
        model=Model([]),
        tools=BudgetAwareWorkflowTools(limits),
        gate=WorkflowGate(),
        store=WorkflowStore(),
        research=Research(research_result(), consume_tool=1),
        limits=limits,
    )
    with pytest.raises(ReviewError, match="tool_limit_exceeded"):
        await workflow.review(REQUEST)


def test_research_workflow_hides_legal_retrieval_tools_from_react():
    pytest.importorskip("llama_index.core")
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    limits = RunLimits()
    tools = WorkflowTools(
        limits,
        tools=(VisibleTool("legal_search"), VisibleTool("document_read_blocks")),
    )
    workflow = ContractReviewWorkflow(
        scope=SCOPE,
        model=Model([]),
        tools=tools,
        gate=WorkflowGate(),
        store=WorkflowStore(),
        research=Research(research_result()),
        limits=limits,
    )
    assert [tool.metadata.get_name() for tool in workflow._visible_tools()] == [
        "document_read_blocks"
    ]


@pytest.mark.asyncio
async def test_workflow_rejects_research_external_evidence_before_gate():
    pytest.importorskip("llama_index.core")
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    limits = RunLimits()
    tools = WorkflowTools(limits)
    gate, store = WorkflowGate(), WorkflowStore()
    response = (
        'Thought: x\nAnswer: {"reviewed_block_ids":["b1"],"issues":'
        '[{"block_id":"b1","anchor_id":"a1","start":0,"end":1,"quote":"合",'
        '"category":"legal","severity":"high","problem":"风险","suggestion":"修改",'
        '"evidence_ids":["outside"]}]}'
    )
    workflow = ContractReviewWorkflow(
        scope=SCOPE,
        model=Model([response]),
        tools=tools,
        gate=gate,
        store=store,
        research=Research(research_result()),
        limits=limits,
    )
    with pytest.raises(ReviewError, match="research_evidence_not_allowed"):
        await workflow.review(REQUEST)
    assert gate.calls == 0 and not store.saved
