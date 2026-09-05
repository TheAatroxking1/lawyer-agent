"""Retrieval-grounded Q&A HTTP endpoint (public, account-authenticated).

spec 6.5/6.6: a logged-in account asks the published legal dataset a question;
the endpoint runs the retrieval QA orchestration and returns the structured
answer (allowed claims + authoritative citations) or a stable refusal. The
orchestration is composed behind ``ApplicationServices.legal_retrieval_qa_http``;
when the model/search prerequisites are not configured the endpoint answers
503 ``retrieval_qa_unavailable`` — it never fabricates a supported answer.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lawyer_agent.api.dependencies import AccountSession, Services
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.v1.legal_chat import ChatUsageBody
from lawyer_agent.application.legal_dataset_search import LegalDatasetNotPublished
from lawyer_agent.application.legal_retrieval_qa import (
    DEFAULT_ALIAS,
    LegalRetrievalAnswer,
    LegalRetrievalQaService,
)
from lawyer_agent.application.model_gateway import (
    ModelGatewayError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)

router = APIRouter(prefix="/legal", tags=["legal-retrieval-qa"])

_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
_MAX_QUESTION_CHARS = 4000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalQuestionBody(StrictModel):
    question: str = Field(min_length=1, max_length=_MAX_QUESTION_CHARS)
    alias: str = Field(default=DEFAULT_ALIAS, min_length=1, max_length=64)
    target_date: date | None = None
    version_id: UUID | None = None

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value

    @field_validator("alias")
    @classmethod
    def _alias_whitelist(cls, value: str) -> str:
        if not value or not _ALIAS_PATTERN.fullmatch(value):
            raise ValueError("alias must match [a-zA-Z0-9_.-]{1,64}")
        return value


class RetrievalCitationBody(StrictModel):
    evidence_id: UUID
    instrument_title: str
    version_label: str
    provision_no: str
    provision_text: str
    source_ref: str
    dataset_version: str


class LegalRetrievalReply(StrictModel):
    refused: bool
    reason: str
    text: str
    usage: ChatUsageBody | None = None
    citations: list[RetrievalCitationBody] = Field(default_factory=list)


def _citation(item: Any) -> RetrievalCitationBody:
    return RetrievalCitationBody(
        evidence_id=item.evidence_id,
        instrument_title=item.instrument_title,
        version_label=item.version_label,
        provision_no=item.provision_no,
        provision_text=item.provision_text,
        source_ref=item.source_ref,
        dataset_version=item.dataset_version,
    )


def _reply(answer: LegalRetrievalAnswer) -> LegalRetrievalReply:
    usage = None
    if answer.usage is not None:
        usage = ChatUsageBody(
            prompt_tokens=answer.usage.prompt_tokens,
            completion_tokens=answer.usage.completion_tokens,
            total_tokens=answer.usage.total_tokens,
        )
    return LegalRetrievalReply(
        refused=answer.refused,
        reason=answer.reason,
        text=answer.text,
        usage=usage,
        citations=[_citation(item) for item in answer.citations],
    )


def _map_error(exc: Exception) -> ApiProblem:
    if isinstance(exc, LegalDatasetNotPublished):
        return ApiProblem(503, "legal_dataset_not_published", "Legal dataset is not published")
    if isinstance(exc, ModelProviderTimeout):
        return ApiProblem(504, "model_provider_timeout", "Model provider timed out")
    if isinstance(exc, ModelProviderUnavailable):
        return ApiProblem(503, "model_provider_unavailable", "Model provider is unavailable")
    if isinstance(exc, ModelGatewayError):
        return ApiProblem(502, "model_provider_failure", "Model provider failed")
    if isinstance(exc, (ValueError, TypeError)):
        return ApiProblem(
            422,
            "legal_retrieval_invalid_request",
            "Retrieval question request is invalid",
        )
    return ApiProblem(502, "retrieval_failed", "Retrieval Q&A failed")


@router.post("/questions", response_model=LegalRetrievalReply)
async def retrieval_question(
    body: Annotated[RetrievalQuestionBody, Body()],
    current: AccountSession,
    services: Services,
) -> LegalRetrievalReply:
    del current
    value = getattr(services, "legal_retrieval_qa_http", None)
    if value is None:
        raise ApiProblem(
            503,
            "retrieval_qa_unavailable",
            "Retrieval Q&A is unavailable",
        )
    service = cast(LegalRetrievalQaService, value)
    try:
        answer = await service.answer(
            alias=body.alias,
            question=body.question,
            target_date=body.target_date or datetime.now(UTC).date(),
            version_id=body.version_id,
        )
    except Exception as exc:
        raise _map_error(exc) from None
    return _reply(answer)
