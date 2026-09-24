import asyncio
import json
from datetime import date
from uuid import uuid4

import pytest

pytest.importorskip("llama_index.core", reason="requires contract-review extra")

from lawyer_agent.application.contract_review.contracts import (  # noqa: E402
    ReviewError,
    ReviewRequest,
    ReviewResult,
    ReviewScope,
    RunLimits,
)


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def chat(self, messages):
        self.calls += 1
        return next(self.responses)


class Tools:
    def __init__(self, scope):
        self.scope = scope
        self.tools = []
        self.calls = []
        self.limits = RunLimits()

    async def prepare(self):
        return '{"document_version_id":"doc-1","blocks":[{"block_id":"b1"}]}'

    async def call(self, name, arguments):
        self.calls.append(name)
        if name != "document_read_blocks":
            raise ReviewError("tool_not_allowed")
        return '{"block_id":"b1","text":"合成条款"}'

    def reset(self):
        pass  # This synthetic toolset has no private payload cache to clear.


class Gate:
    def __init__(self):
        self.calls = 0
        self.reject = False

    async def validate(self, scope, request, candidate):
        self.calls += 1
        if self.reject:
            raise ReviewError("citation_rejected")
        return ReviewResult(
            document_version_id=scope.document_version_id,
            run_id=scope.run_id,
            issues=candidate.issues,
        )


class Store:
    def __init__(self):
        self.saved = []
        self.fail = False

    async def save(self, scope, result):
        if self.fail:
            raise RuntimeError("PRIVATE_STORAGE_ERROR")
        self.saved.append(result)


def setup(responses, **limit_overrides):
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    scope = ReviewScope(
        tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(), document_version_id="doc-1"
    )
    model, gate, store, tools = Model(responses), Gate(), Store(), Tools(scope)
    tools.limits = RunLimits(**limit_overrides)
    workflow = ContractReviewWorkflow(
        scope=scope,
        model=model,
        tools=tools,
        gate=gate,
        store=store,
        limits=RunLimits(**limit_overrides),
    )
    return workflow, model, tools, gate, store


REQUEST = ReviewRequest(instruction="检查合同", as_of=date(2026, 9, 16))
FINAL = 'Thought: PRIVATE_REASONING\nAnswer: {"reviewed_block_ids":["b1"],"issues":[]}'
ACTION = (
    "Thought: PRIVATE_REASONING\nAction: document_read_blocks\n"
    'Action Input: {"request":{"document_version_id":"doc-1","block_ids":["b1"]}}'
)


@pytest.mark.asyncio
async def test_real_workflow_action_observation_gate_store_and_no_reasoning(caplog):
    workflow, model, tools, gate, store = setup([ACTION, FINAL])
    result = await workflow.review(REQUEST)
    assert model.calls == 2 and tools.calls == ["document_read_blocks"]
    assert gate.calls == 1 and store.saved == [result]
    assert "reasoning" not in result.model_dump_json()
    assert "PRIVATE_REASONING" not in caplog.text
    assert workflow.progress == ("preparing", "reviewing", "reviewing", "validating", "saved")
    assert workflow.has_active_state is False


@pytest.mark.asyncio
async def test_json_react_action_observation_and_final_keep_controlled_tool_loop():
    action = json.dumps({"action": "document_read_blocks", "action_input": {
        "request": {"document_version_id": "doc-1", "block_ids": ["b1"]},
    }})
    final = json.dumps({"reviewed_block_ids": ["b1"], "issues": []})
    workflow, model, tools, gate, store = setup([action, final])
    result = await workflow.review(REQUEST)
    assert model.calls == 2 and tools.calls == ["document_read_blocks"]
    assert gate.calls == 1 and store.saved == [result]


@pytest.mark.asyncio
async def test_json_react_cannot_call_an_unregistered_tool():
    action = json.dumps({"action": "delete_all", "action_input": {}})
    workflow, _, _, _, store = setup([action])
    with pytest.raises(ReviewError, match="tool_not_allowed"):
        await workflow.review(REQUEST)
    assert not store.saved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses,limits,code",
    [
        ([""], {}, "model_empty_response"),
        (["bad", "bad"], {}, "model_output_invalid"),
        ([ACTION, ACTION], {"max_steps": 1}, "step_limit_exceeded"),
        ([ACTION], {"max_tool_calls": 0}, "tool_limit_exceeded"),
        (["x" * 100], {"max_response_bytes": 64}, "model_response_too_large"),
        ([ACTION.replace("document_read_blocks", "delete_all")], {}, "tool_not_allowed"),
    ],
)
async def test_workflow_fails_closed(responses, limits, code):
    workflow, _, _, _, store = setup(responses, **limits)
    with pytest.raises(ReviewError, match=code):
        await workflow.review(REQUEST)
    assert store.saved == []
    assert workflow.has_active_state is False


@pytest.mark.asyncio
async def test_parser_repairs_once_then_succeeds():
    workflow, model, _, _, _ = setup(["bad", FINAL])
    await workflow.review(REQUEST)
    assert model.calls == 2


@pytest.mark.asyncio
async def test_truncated_draft_regenerates_once_without_saving_partial_output():
    from lawyer_agent.application.model_gateway import ModelProviderOutputTruncated

    workflow, _, _, _, store = setup([])
    calls = []

    class TruncatingModel:
        async def chat(self, messages):
            calls.append(messages)
            if len(calls) == 1:
                raise ModelProviderOutputTruncated("PRIVATE_PROVIDER_PAYLOAD")
            return FINAL

    workflow.model = TruncatingModel()
    result = await workflow.review(REQUEST)
    assert store.saved == [result] and len(calls) == 2
    prompt = "\n".join(message.content for message in calls[1])
    assert "PRIVATE_PROVIDER_PAYLOAD" not in prompt
    assert "截断" in prompt
    assert '"maxItems":2' in prompt


@pytest.mark.asyncio
async def test_repeated_truncation_is_bounded_and_identified():
    from lawyer_agent.application.model_gateway import ModelProviderOutputTruncated

    workflow, _, _, _, store = setup([])
    calls = 0

    class TruncatingModel:
        async def chat(self, messages):
            nonlocal calls
            calls += 1
            raise ModelProviderOutputTruncated("PRIVATE_PROVIDER_PAYLOAD")

    workflow.model = TruncatingModel()
    with pytest.raises(ReviewError, match="model_output_truncated"):
        await workflow.review(REQUEST)
    assert calls == 2 and not store.saved


@pytest.mark.asyncio
async def test_long_contract_batches_keep_full_context_and_save_once():
    workflow, _, tools, gate, store = setup([], max_steps=8)
    blocks = [{"block_id": f"b{i}", "text": f"context-{i}"} for i in range(201)]

    async def prepare():
        return json.dumps({"blocks": blocks})

    tools.prepare = prepare
    batches = []

    class BatchModel:
        async def chat(self, messages):
            assert "context-0" in messages[1].content and "context-200" in messages[1].content
            focus = json.loads(messages[-1].content)["review_batch"]
            batches.append(focus)
            return 'Thought: 检查完成\nAnswer: ' + json.dumps({
                "reviewed_batch_id": json.loads(messages[-1].content)["review_batch_id"],
                "batch_complete": True,
                "issues": [],
            })

    workflow.model = BatchModel()
    original_validate = gate.validate

    async def validate(scope, request, candidate):
        assert set(candidate.reviewed_block_ids) == {block["block_id"] for block in blocks}
        return await original_validate(scope, request, candidate)

    gate.validate = validate
    result = await workflow.review(REQUEST)
    assert len(batches) > 1 and max(map(len, batches)) <= 96
    assert sum(map(len, batches)) == 201
    assert gate.calls == 1 and store.saved == [result]


@pytest.mark.asyncio
async def test_batch_omission_cannot_be_saved_as_complete():
    workflow, _, tools, _, store = setup([], max_steps=8)

    async def prepare():
        return json.dumps({"blocks": [{"block_id": f"b{i}"} for i in range(201)]})

    tools.prepare = prepare

    class OmittingModel:
        async def chat(self, messages):
            focus = json.loads(messages[-1].content)["review_batch"]
            return 'Thought: 检查完成\nAnswer: ' + json.dumps({
                "reviewed_block_ids": focus[:-1], "issues": [],
            })

    workflow.model = OmittingModel()
    with pytest.raises(ReviewError, match="model_output_invalid"):
        await workflow.review(REQUEST)
    assert not store.saved


@pytest.mark.asyncio
async def test_wrong_batch_receipt_cannot_claim_coverage():
    workflow, _, tools, _, store = setup([], max_steps=8)

    async def prepare():
        return json.dumps({"blocks": [{"block_id": f"b{i}"} for i in range(201)]})

    tools.prepare = prepare
    workflow.model = Model([json.dumps({"reviewed_batch_id": "unrelated-batch", "issues": []})] * 2)
    with pytest.raises(ReviewError, match="model_output_invalid"):
        await workflow.review(REQUEST)
    assert not store.saved


@pytest.mark.asyncio
async def test_matching_receipt_cannot_include_issues_from_another_batch():
    workflow, _, tools, gate, store = setup([], max_steps=8)

    async def prepare():
        return json.dumps({"blocks": [{"block_id": f"b{i}"} for i in range(201)]})

    tools.prepare = prepare

    class CrossBatchModel:
        async def chat(self, messages):
            batch = json.loads(messages[-1].content)
            return json.dumps({"reviewed_batch_id": batch["review_batch_id"],
                              "batch_complete": True, "issues": [{
                "block_id": "b200", "anchor_id": "anchor-200", "start": 0, "end": 2,
                "quote": "条款", "category": "commercial", "severity": "low",
                "problem": "合成测试", "suggestion": "合成建议", "evidence_ids": [],
            }]})

    workflow.model = CrossBatchModel()
    with pytest.raises(ReviewError, match="model_output_invalid"):
        await workflow.review(REQUEST)
    assert gate.calls == 0 and not store.saved


def _issue(number):
    return {"block_id": "b1", "anchor_id": "a1", "start": 0, "end": 2,
            "quote": "条款", "category": "commercial", "severity": "low",
            "problem": f"问题{number}", "suggestion": f"建议{number}", "evidence_ids": []}


@pytest.mark.asyncio
async def test_review_outputs_two_issues_at_a_time_and_reuses_private_cache():
    workflow, _, _, gate, store = setup([], max_steps=8)
    rounds = []

    class PagedModel:
        async def chat(self, messages):
            context = json.loads(messages[-1].content)
            index = len(rounds)
            assert len(context["previous_issues"]) == index * 2
            assert not store.saved and gate.calls == 0
            assert '"maxItems":2' in messages[0].content
            rounds.append(context["review_batch_id"])
            return json.dumps({
                "reviewed_batch_id": rounds[-1], "batch_complete": index == 2,
                "issues": [_issue(i) for i in range(index * 2, min(index * 2 + 2, 5))],
            })

    workflow.model = PagedModel()
    result = await workflow.review(REQUEST)
    assert len(rounds) == len(set(rounds)) == 3
    assert len(result.issues) == 5 and gate.calls == 1 and store.saved == [result]
    assert not workflow.has_active_state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["oversize", "missing_complete", "empty_continuation",
                                  "duplicate_continuation", "stale_receipt"])
async def test_invalid_issue_pages_cannot_be_saved(kind):
    workflow, _, _, gate, store = setup([], max_steps=8)
    first_id = None

    class InvalidPageModel:
        async def chat(self, messages):
            nonlocal first_id
            context = json.loads(messages[-1].content)
            payload = {"reviewed_batch_id": context["review_batch_id"],
                       "batch_complete": False, "issues": []}
            if kind == "oversize":
                payload["issues"] = [_issue(i) for i in range(3)]
            elif kind == "missing_complete":
                del payload["batch_complete"]
            elif kind in {"duplicate_continuation", "stale_receipt"}:
                payload["issues"] = [_issue(0)]
                if first_id is None:
                    first_id = context["review_batch_id"]
                elif kind == "stale_receipt":
                    payload["reviewed_batch_id"] = first_id
                    payload["batch_complete"] = True
            if kind == "duplicate_continuation" and context["previous_issues"]:
                assert '"batch_complete": true, "issues": []' in messages[-1].content
            return json.dumps(payload)

    workflow.model = InvalidPageModel()
    with pytest.raises(ReviewError, match="model_output_invalid"):
        await workflow.review(REQUEST)
    assert gate.calls == 0 and not store.saved and not workflow.has_active_state


@pytest.mark.asyncio
async def test_two_issue_cache_is_cleared_between_runs():
    workflow, _, _, _, store = setup([], max_steps=8)

    class ModelWithCacheCheck:
        async def chat(self, messages):
            context = json.loads(messages[-1].content)
            assert context["previous_issues"] == []
            return json.dumps({"reviewed_batch_id": context["review_batch_id"],
                               "batch_complete": True, "issues": [_issue(1)]})

    workflow.model = ModelWithCacheCheck()
    await workflow.review(REQUEST)
    await workflow.review(REQUEST)
    assert len(store.saved) == 2


@pytest.mark.asyncio
async def test_structural_repairs_also_keep_two_issues_per_output():
    workflow, _, _, gate, store = setup([], max_steps=10)
    repair_sizes = []

    class RepairPagesModel:
        async def chat(self, messages):
            context = json.loads(messages[-1].content)
            if "repair_issues" in context:
                issues = [{**issue, "anchor_id": "correct-anchor"}
                          for issue in context["repair_issues"]]
                repair_sizes.append(len(issues))
                complete = True
            else:
                start = len(context["previous_issues"])
                issues = [_issue(i) for i in range(start, min(start + 2, 5))]
                complete = start == 4
            return json.dumps({"reviewed_batch_id": context["review_batch_id"],
                               "batch_complete": complete, "issues": issues})

    checks = []
    original_validate = gate.validate

    async def validate(scope, request, candidate):
        checks.append(candidate)
        if len(checks) == 1:
            raise ReviewError("anchor_mismatch")
        assert len(candidate.issues) == 5
        assert all(issue.anchor_id == "correct-anchor" for issue in candidate.issues)
        return await original_validate(scope, request, candidate)

    gate.validate = validate
    workflow.model = RepairPagesModel()
    result = await workflow.review(REQUEST)
    assert repair_sizes == [2, 2, 1] and store.saved == [result]


@pytest.mark.asyncio
async def test_incomplete_cached_pages_are_not_saved_when_budget_ends():
    workflow, _, _, gate, store = setup([], max_steps=2)

    class NeverCompleteModel:
        async def chat(self, messages):
            context = json.loads(messages[-1].content)
            start = len(context["previous_issues"])
            return json.dumps({"reviewed_batch_id": context["review_batch_id"],
                               "batch_complete": False, "issues": [_issue(start)]})

    workflow.model = NeverCompleteModel()
    with pytest.raises(ReviewError, match="step_limit_exceeded"):
        await workflow.review(REQUEST)
    assert gate.calls == 0 and not store.saved and not workflow.has_active_state


@pytest.mark.asyncio
async def test_schema_repair_reports_safe_fields_without_model_payload(caplog):
    invalid = ('Thought: PRIVATE_REASONING\nAnswer: '
               '{"reviewed_block_ids":["b1"],"issues":[],"PRIVATE_FIELD":"PRIVATE_VALUE"}')
    workflow, _, _, _, _ = setup([])
    calls = []

    class RepairModel:
        async def chat(self, messages):
            calls.append(messages)
            return invalid if len(calls) == 1 else FINAL

    workflow.model = RepairModel()
    await workflow.review(REQUEST)
    feedback = "\n".join(message.content for message in calls[1])
    assert "extra_forbidden" in feedback and "extra_forbidden" in caplog.text
    assert all(value not in feedback + caplog.text for value in (
        "PRIVATE_REASONING", "PRIVATE_FIELD", "PRIVATE_VALUE",
    ))


@pytest.mark.asyncio
async def test_gate_failure_prevents_save():
    workflow, _, _, gate, store = setup([FINAL])
    gate.reject = True
    with pytest.raises(ReviewError, match="citation_rejected"):
        await workflow.review(REQUEST)
    assert store.saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["review_coverage_incomplete", "quote_mismatch", "anchor_mismatch"],
)
async def test_structural_gate_repair_is_rechecked_before_single_save(code):
    workflow, model, _, gate, store = setup([FINAL, FINAL])
    validate = gate.validate

    async def reject_once(scope, request, candidate):
        if model.calls == 1:
            raise ReviewError(code)
        return await validate(scope, request, candidate)

    gate.validate = reject_once
    result = await workflow.review(REQUEST)
    assert model.calls == 2 and gate.calls == 1
    assert store.saved == [result]
    assert workflow.progress.count("validating") == 2
    assert workflow.progress.count("saved") == 1


@pytest.mark.asyncio
async def test_structural_gate_repair_is_bounded_and_does_not_save_rejections():
    workflow, model, _, gate, store = setup([FINAL, FINAL, FINAL])

    async def always_reject(scope, request, candidate):
        raise ReviewError("anchor_mismatch")

    gate.validate = always_reject
    with pytest.raises(ReviewError, match="anchor_mismatch"):
        await workflow.review(REQUEST)
    assert model.calls == 2 and not store.saved
    assert not workflow.has_active_state and "saved" not in workflow.progress


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["legal_metadata_unverified", "content_unsupported"])
async def test_content_rejection_does_not_enter_structural_repair(code):
    workflow, model, _, gate, store = setup([FINAL, FINAL])

    async def reject_content(scope, request, candidate):
        raise ReviewError(code)

    gate.validate = reject_content
    with pytest.raises(ReviewError, match=code):
        await workflow.review(REQUEST)
    assert model.calls == 1 and not store.saved


@pytest.mark.asyncio
async def test_store_failure_does_not_emit_success(caplog):
    workflow, _, _, _, store = setup([FINAL])
    store.fail = True
    with pytest.raises(ReviewError, match="review_failed"):
        await workflow.review(REQUEST)
    assert "saved" not in workflow.progress
    assert "PRIVATE_STORAGE_ERROR" not in caplog.text


@pytest.mark.asyncio
async def test_cancel_cleans_runtime_and_does_not_save():
    workflow, model, _, _, store = setup([])
    entered = asyncio.Event()

    async def wait_forever(messages):
        entered.set()
        await asyncio.Event().wait()

    model.chat = wait_forever
    task = asyncio.create_task(workflow.review(REQUEST))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workflow.has_active_state and not store.saved


@pytest.mark.asyncio
async def test_invalid_gate_result_is_rejected_before_save():
    workflow, _, _, gate, store = setup([FINAL])

    async def invalid(scope, request, candidate):
        return ReviewResult(
            document_version_id=scope.document_version_id, run_id=scope.run_id, issues=()
        ).model_copy(update={"issues": ("PRIVATE_INVALID_RESULT",)})

    gate.validate = invalid
    with pytest.raises(ReviewError, match="review_gate_failed"):
        await workflow.review(REQUEST)
    assert not store.saved and "saved" not in workflow.progress


def test_workflow_rejects_separate_tool_budget():
    from lawyer_agent.infrastructure.contract_review.workflow import ContractReviewWorkflow

    workflow, model, tools, gate, store = setup([FINAL])
    with pytest.raises(ReviewError, match="budget_mismatch"):
        ContractReviewWorkflow(
            scope=workflow.scope,
            model=model,
            tools=tools,
            gate=gate,
            store=store,
            limits=RunLimits(max_tool_calls=0),
        )
