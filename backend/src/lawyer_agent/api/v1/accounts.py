from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from lawyer_agent.api.dependencies import AccountSession, Services

router = APIRouter(tags=["accounts"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountResponse(StrictModel):
    id: UUID
    display_name: str
    status: str
    created_at: datetime


class AccountTenantResponse(StrictModel):
    tenant_id: UUID
    membership_id: UUID
    name: str
    tenant_type: str
    tenant_status: str
    membership_status: str


class AccountTenantListResponse(StrictModel):
    items: tuple[AccountTenantResponse, ...]


@router.get("/me", response_model=AccountResponse)
async def me(current: AccountSession, services: Services) -> AccountResponse:
    projection = await services.accounts.get(current.user_id)
    return AccountResponse.model_validate(projection, from_attributes=True)


@router.get("/me/tenants", response_model=AccountTenantListResponse)
async def my_tenants(
    current: AccountSession,
    services: Services,
) -> AccountTenantListResponse:
    projections = await services.accounts.list_tenants(current.user_id)
    return AccountTenantListResponse(
        items=tuple(
            AccountTenantResponse.model_validate(item, from_attributes=True)
            for item in projections
        )
    )
