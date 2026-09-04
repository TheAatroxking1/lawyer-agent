from __future__ import annotations

from datetime import date
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from lawyer_agent.api.dependencies import (
    AccountSession,
    Services,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.legal_corpus_read import (
    LegalCorpusQueryError,
    LegalCorpusQueryService,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LegalVersionSummary(StrictModel):
    id: UUID
    instrument_id: UUID
    version_label: str
    status: str
    published_on: date | None = None
    effective_on: date | None = None
    repealed_on: date | None = None
    law_number: str | None = None
    source_ref: str | None = None
    dataset_version: str | None = None
    parser_version: str | None = None


class ProvisionSummary(StrictModel):
    id: UUID
    version_id: UUID
    provision_no: str
    level: str
    structure_path: tuple[str, ...]
    title: str | None = None
    full_text: str


router = APIRouter(
    prefix="/legal",
    tags=["legal-corpus"],
)


def _require_service(services: Services) -> LegalCorpusQueryService:
    value = getattr(services, "legal_corpus_http", None)
    if value is None:
        raise ApiProblem(503, LegalCorpusQueryError.code, LegalCorpusQueryError.title)
    return cast(LegalCorpusQueryService, value)


def _map_error(exc: LegalCorpusQueryError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.get(
    "/instruments/{instrument_id}/version",
    response_model=LegalVersionSummary,
)
async def get_version_at(
    instrument_id: UUID,
    current: AccountSession,
    services: Services,
    as_of: Annotated[date, Query()],
) -> LegalVersionSummary:
    del current
    service = _require_service(services)
    try:
        version = await service.version_at(instrument_id=instrument_id, as_of=as_of)
    except LegalCorpusQueryError as exc:
        raise _map_error(exc) from None
    return LegalVersionSummary(
        id=version.id,
        instrument_id=version.instrument_id,
        version_label=version.version_label,
        status=version.status.value,
        published_on=version.published_on,
        effective_on=version.effective_on,
        repealed_on=version.repealed_on,
        law_number=version.law_number,
        source_ref=version.source_ref,
        dataset_version=version.dataset_version,
        parser_version=version.parser_version,
    )


@router.get(
    "/versions/{version_id}/provisions",
    response_model=list[ProvisionSummary],
)
async def get_provisions(
    version_id: UUID,
    current: AccountSession,
    services: Services,
) -> list[ProvisionSummary]:
    del current
    service = _require_service(services)
    try:
        provisions = await service.provisions_for_version(version_id=version_id)
    except LegalCorpusQueryError as exc:
        raise _map_error(exc) from None
    return [
        ProvisionSummary(
            id=provision.id,
            version_id=provision.version_id,
            provision_no=provision.provision_no,
            level=provision.level.value,
            structure_path=provision.structure_path,
            title=provision.title,
            full_text=provision.full_text,
        )
        for provision in provisions
    ]
