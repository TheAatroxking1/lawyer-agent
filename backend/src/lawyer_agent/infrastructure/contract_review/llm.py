"""Use the existing supplier-neutral model gateway; never default to OpenAI."""

from collections.abc import Sequence

from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.model_gateway import ChatMessage


class GatewayReviewModel:
    def __init__(self, gateway: ModelGateway, *, model_ref: str) -> None:
        if not model_ref.strip():
            raise ValueError("model_ref is required")
        self.gateway = gateway
        self.model_ref = model_ref

    async def chat(self, messages: Sequence[ChatMessage]) -> str:
        text, _usage = await self.gateway.chat(model_ref=self.model_ref, messages=messages)
        return text
