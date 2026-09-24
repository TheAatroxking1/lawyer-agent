import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lawyer_agent.api.dependencies import services, tenant_actor
from lawyer_agent.api.errors import ApiProblem, api_problem_handler
from lawyer_agent.api.v1.contract_reviews import _problem, _stream, router
from lawyer_agent.application.contract_review.contracts import DocumentBlock, ReviewError
from lawyer_agent.application.contract_review.research import NO_EVIDENCE_MESSAGE
from lawyer_agent.application.contract_review.web import (
    DRAFT_MESSAGE,
    ContractReviewFinal,
    ContractRunView,
)


def client_for(service):
    tenant = uuid4()
    app = FastAPI()
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[services] = lambda: SimpleNamespace(contract_review_http=service)
    app.dependency_overrides[tenant_actor] = lambda: SimpleNamespace(
        context=SimpleNamespace(tenant_id=tenant))
    return TestClient(app), tenant


def test_truncated_model_output_has_a_specific_user_facing_error():
    problem = _problem(ReviewError("model_output_truncated"))
    assert problem.code == "model_output_truncated"
    assert "截断" in problem.title


class Service:
    def __init__(self):
        self.calls = 0

    async def get(self, actor, run_id):
        self.calls += 1
        return ContractRunView(id=run_id, status="uploaded", file_name="test.pdf")

    async def run(self, actor, run_id, on_progress):
        on_progress("reviewing")
        on_progress("raw private reasoning must never escape")
        raise ReviewError("legal_metadata_unverified")

    async def cancel(self, actor, run_id):
        pass


def test_cross_tenant_path_is_rejected_before_service_access():
    service = Service()
    client, _tenant = client_for(service)
    response = client.get(f"/api/v1/tenants/{uuid4()}/contract-reviews/{uuid4()}")
    assert response.status_code == 404
    assert service.calls == 0


def test_demo_stream_and_fixture_are_tenant_bound_and_do_not_require_review_service():
    client, tenant = client_for(None)
    stream = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/demo/events")
    assert stream.status_code == 200
    assert "contract_understanding" in stream.text
    assert '"is_demo": true' in stream.text
    document = client.get(f"/api/v1/tenants/{tenant}/contract-reviews/demo/document")
    assert document.status_code == 200
    assert document.content.startswith(b"%PDF-")


def test_stream_failure_is_not_success_and_no_untrusted_progress():
    client, tenant = client_for(Service())
    response = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/{uuid4()}/events")
    assert response.status_code == 200
    assert '"stage": "reviewing"' in response.text
    assert "raw private reasoning" not in response.text
    assert "legal_metadata_unverified" in response.text
    assert "event: result" not in response.text
    assert '"status": "failed"' in response.text


def test_coverage_error_stream_names_pages_and_reasons_without_raw_model_text():
    from lawyer_agent.application.contract_review.contracts import (
        DocumentVerificationError,
        DocumentVerificationReport,
    )

    class IncompleteService(Service):
        async def run(self, actor, run_id, on_progress):
            raise DocumentVerificationError(DocumentVerificationReport.model_validate({
                "pages": [{"page": 9, "reasons": ["text_mismatch"]},
                          {"page": 23, "reasons": ["missing_text"]}],
            }))

    client, tenant = client_for(IncompleteService())
    response = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/{uuid4()}/events")
    assert "第9页" in response.text and "第23页" in response.text
    assert "疑似缺字" in response.text and "文字比对不一致" in response.text
    assert "event: result" not in response.text
    assert '"status": "failed"' in response.text


def test_coverage_error_stream_fits_reserve_at_maximum_report_size():
    from lawyer_agent.application.contract_review.contracts import (
        DocumentVerificationError,
        DocumentVerificationReport,
    )

    class IncompleteService(Service):
        async def run(self, actor, run_id, on_progress):
            raise DocumentVerificationError(DocumentVerificationReport.model_validate({
                "pages": [{"page": page, "reasons": ["missing_native_coordinates", "unreadable",
                    "missing_text", "garbled_text", "text_mismatch", "unconfirmed_coverage"]}
                    for page in range(1, 31)],
            }))

    client, tenant = client_for(IncompleteService())
    response = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/{uuid4()}/events")
    assert "第30页" in response.text and '"status": "failed"' in response.text


def test_unconfigured_service_has_stable_unavailable_status():
    client, tenant = client_for(None)
    response = client.get(f"/api/v1/tenants/{tenant}/contract-reviews/{uuid4()}")
    assert response.status_code == 503
    assert response.json()["code"] == "contract_review_unavailable"


@pytest.mark.parametrize("status,message", [("draft", DRAFT_MESSAGE),
                                               ("no_evidence", NO_EVIDENCE_MESSAGE)])
def test_final_dto_derives_only_fixed_message(status, message):
    run = ContractRunView(id=uuid4(), status=status, file_name="test.pdf",
                          message="raw model reasoning")
    final = ContractReviewFinal.from_run(run)
    assert final.message == message
    assert "raw model reasoning" not in final.model_dump_json()


def test_final_dto_bounds_pages_blocks_and_issues():
    base = {"id": uuid4(), "status": "draft", "file_name": "test.pdf",
            "message": DRAFT_MESSAGE}
    with pytest.raises(ValidationError):
        ContractReviewFinal.model_validate({**base, "page_dimensions": [(1, 1)] * 31})
    with pytest.raises(ValidationError):
        ContractReviewFinal.model_validate({**base, "file_name": "x" * 256})


class SuccessfulService(Service):
    def __init__(self, result):
        super().__init__()
        self.result = result

    async def run(self, actor, run_id, on_progress):
        on_progress("saved")
        return self.result


def test_stream_final_is_bounded_and_does_not_export_arbitrary_message():
    run_id = uuid4()
    result = ContractRunView(id=run_id, status="draft", file_name="test.pdf",
                             message="private provider payload")
    client, tenant = client_for(SuccessfulService(result))
    response = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/{run_id}/events")
    assert response.status_code == 200
    assert DRAFT_MESSAGE in response.text
    assert "private provider payload" not in response.text


def test_content_warning_stream_continues_to_saved_result_and_done():
    class WarningService(SuccessfulService):
        async def run(self, actor, run_id, on_progress):
            on_progress("content_warning")
            return await super().run(actor, run_id, on_progress)

    run_id = uuid4()
    result = ContractRunView.model_validate({
        "id": run_id, "status": "draft", "file_name": "test.pdf",
        "verification_report": {"pages": [{"page": 7, "reasons": ["missing_text"]}]},
    })
    client, tenant = client_for(WarningService(result))
    response = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/{run_id}/events")
    assert response.status_code == 200
    assert response.text.index("content_warning") < response.text.index("saved")
    assert response.text.index("event: result") < response.text.index("event: done")
    assert '"page":7' in response.text.replace(" ", "")
    assert "missing_text" in response.text
    assert '"status":"draft"' in response.text.replace(" ", "")
    assert "event: error" not in response.text


def test_stream_rejects_result_over_two_mibibytes():
    run_id = uuid4()
    blocks = tuple(DocumentBlock(block_id=f"b-{index}", kind="text", text="x" * 32000,
                                 anchors=(), quality="verified") for index in range(66))
    result = ContractRunView(id=run_id, status="draft", file_name="test.pdf", blocks=blocks)
    client, tenant = client_for(SuccessfulService(result))
    response = client.post(f"/api/v1/tenants/{tenant}/contract-reviews/{run_id}/events")
    assert "event: result" not in response.text
    assert "contract_stream_too_large" in response.text
    assert '"status": "failed"' in response.text
    assert '"status": "draft"' not in response.text


@pytest.mark.asyncio
async def test_disconnected_stream_delegates_owned_run_cancellation_to_service():
    started = asyncio.Event()

    class RunningService(Service):
        def __init__(self):
            super().__init__()
            self.cancelled = 0

        async def run(self, actor, run_id, on_progress):
            on_progress("reviewing")
            started.set()
            await asyncio.Event().wait()

        async def cancel(self, actor, run_id):
            self.cancelled += 1

    service = RunningService()
    stream = _stream(service, SimpleNamespace(), uuid4())
    assert "progress" in await anext(stream)
    assert started.is_set()
    await stream.aclose()
    assert service.cancelled == 1
