import pytest

from lawyer_agent.application.model_gateway import ModelGateway, ModelProviderUnavailable
from lawyer_agent.domain.model_gateway import ChatMessage
from lawyer_agent.infrastructure.contract_review.llm import GatewayReviewModel
from tests.unit.test_model_gateway import DeterministicProvider, MemoryRecorder


@pytest.mark.asyncio
async def test_adapter_keeps_gateway_usage_recording_and_explicit_provider():
    recorder = MemoryRecorder()
    model = GatewayReviewModel(
        ModelGateway(DeterministicProvider(), recorder), model_ref="synthetic-contract-model"
    )
    assert await model.chat([ChatMessage(role="user", content="合成合同")])
    assert len(recorder.records) == 1
    assert recorder.records[0].model_ref == "synthetic-contract-model"


@pytest.mark.asyncio
async def test_adapter_does_not_fabricate_provider_fallback():
    model = GatewayReviewModel(
        ModelGateway(
            DeterministicProvider(fail_chat=ModelProviderUnavailable("unavailable")),
            MemoryRecorder(),
        ),
        model_ref="synthetic-contract-model",
    )
    with pytest.raises(ModelProviderUnavailable):
        await model.chat([ChatMessage(role="user", content="合成合同")])
