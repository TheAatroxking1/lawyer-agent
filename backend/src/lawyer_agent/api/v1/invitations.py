from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from lawyer_agent.api.dependencies import AccountSession, Audit, Services
from lawyer_agent.application.invitations import AcceptInvitationCommand

router = APIRouter(prefix="/invitations", tags=["invitations"])


class AcceptInvitationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=43, max_length=43)


class InvitationMembershipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invitation_id: UUID
    membership_id: UUID
    tenant_id: UUID
    role_ids: tuple[UUID, ...]
    replayed: bool


@router.post("/accept", response_model=InvitationMembershipResponse)
async def accept_invitation(
    body: AcceptInvitationRequest,
    current: AccountSession,
    services: Services,
    audit: Audit,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> InvitationMembershipResponse:
    result = await services.invitations.accept(
        AcceptInvitationCommand(
            actor_user_id=current.user_id,
            token=body.token,
            idempotency_key=idempotency_key,
            audit_context=audit,
        )
    )
    return InvitationMembershipResponse(
        invitation_id=result.invitation_id,
        membership_id=result.membership.id,
        tenant_id=result.membership.tenant_id,
        role_ids=result.role_ids,
        replayed=result.replayed,
    )
