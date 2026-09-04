from fastapi import APIRouter

from lawyer_agent.api.v1 import (
    accounts,
    ai_jobs,
    audit,
    auth,
    invitations,
    legal_corpus,
    matter_documents,
    platform,
    reviews,
    rule_checks,
    rule_packs,
    tenants,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth.router)
api_v1_router.include_router(accounts.router)
api_v1_router.include_router(tenants.router)
api_v1_router.include_router(invitations.router)
api_v1_router.include_router(platform.router)
api_v1_router.include_router(ai_jobs.router)
api_v1_router.include_router(rule_checks.router)
api_v1_router.include_router(matter_documents.router)
api_v1_router.include_router(reviews.router)
api_v1_router.include_router(rule_packs.router)
api_v1_router.include_router(audit.router)
api_v1_router.include_router(legal_corpus.router)
