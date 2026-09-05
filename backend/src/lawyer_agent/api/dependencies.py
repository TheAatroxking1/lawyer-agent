from __future__ import annotations

import hmac
from base64 import b64decode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Any, cast
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, Header, Request
from sqlalchemy import text

from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.api.router import ConcurrentReadinessProbe
from lawyer_agent.application.accounts import (
    AccountQueryPort,
    AccountQueryService,
    AccountQueryUnitOfWork,
)
from lawyer_agent.application.idempotency import IdempotencyService
from lawyer_agent.application.identity import (
    AuditContext,
    IdentityService,
    IdentityUnitOfWork,
)
from lawyer_agent.application.invitations import (
    InvitationDeliveryCapability,
    InvitationDeliveryPort,
    InvitationService,
    InvitationTargetFingerprintHasher,
    InvitationTokenHasher,
)
from lawyer_agent.application.platform import PlatformActor, PlatformReviewService
from lawyer_agent.application.sessions import (
    SessionService,
    SessionUnitOfWork,
)
from lawyer_agent.application.tenancy import (
    TenantActor,
    TenantService,
    TenantWorkflowUnitOfWork,
)
from lawyer_agent.config import Settings
from lawyer_agent.domain.authorization import AuthorizationScope, Principal, PrincipalAudience
from lawyer_agent.domain.sessions import Audience, InvalidToken
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.persistence.account_queries import (
    SqlAlchemyAccountQueryUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.engine import create_engine, create_session_factory
from lawyer_agent.infrastructure.persistence.invitations_uow import (
    SqlAlchemyInvitationWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.platform_uow import (
    SqlAlchemyPlatformWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.repositories.identity import (
    SqlAlchemyIdentityUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.repositories.sessions import (
    SqlAlchemySessionUnitOfWork,
)
from lawyer_agent.infrastructure.persistence.tenancy_uow import (
    SqlAlchemyTenantWorkflowUnitOfWork,
)
from lawyer_agent.infrastructure.providers.development import TestInvitationDeliveryAdapter
from lawyer_agent.infrastructure.redis.authz_cache import AuthorizationCache
from lawyer_agent.infrastructure.redis.client import RedisAsyncioAdapter
from lawyer_agent.infrastructure.redis.rate_limit import RateLimiter
from lawyer_agent.infrastructure.redis.step_up import StepUpStore
from lawyer_agent.infrastructure.security.blind_index import BlindIndexService
from lawyer_agent.infrastructure.security.cipher import SensitiveValueCipher
from lawyer_agent.infrastructure.security.csrf import CsrfService
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService
from lawyer_agent.infrastructure.security.passwords import Argon2PasswordHasher


@dataclass(slots=True)
class ApplicationServices:
    identity: Any
    sessions: Any
    tenancy: Any
    invitations: Any
    platform: Any
    rate_limiter: Any
    step_up: Any
    csrf: Any
    token_service: Any
    accounts: AccountQueryPort
    ai_jobs: Any = None
    rule_check_http: Any = None
    matter_document_http: Any = None
    document_review_http: Any = None
    rule_pack_admin_http: Any = None
    audit_query_http: Any = None
    legal_corpus_http: Any = None
    legal_version_diff_http: Any = None
    legal_chat_http: Any = None
    legal_retrieval_qa_http: Any = None
    mcp_gateway_http: Any = None
    invitation_delivery: InvitationDeliveryCapability = field(
        default_factory=lambda: InvitationDeliveryCapability(None)
    )
    readiness: ConcurrentReadinessProbe | None = None


def _derived_key(domain: bytes, key: bytes) -> bytes:
    return hmac.digest(key, b"lawyer-api-composition:v1:" + domain, sha256)


@asynccontextmanager
async def application_services(
    settings: Settings,
    *,
    invitation_delivery: InvitationDeliveryPort | None = None,
) -> AsyncIterator[ApplicationServices]:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    redis = RedisAsyncioAdapter.from_url(
        settings.redis_url,
        key_prefix=settings.redis_key_prefix,
        topology=settings.redis_security_topology,
    )
    data_keys = settings.decoded_data_encryption_key_ring()
    blind_keys = settings.decoded_blind_index_key_ring()
    refresh_key = b64decode(settings.refresh_token_key_b64, validate=True)
    csrf_key = b64decode(settings.csrf_key_b64, validate=True)
    cipher = SensitiveValueCipher(
        data_keys,
        active_key_version=settings.effective_data_encryption_active_key_version,
    )
    blind_index = BlindIndexService(
        blind_keys,
        active_key_version=settings.effective_blind_index_active_key_version,
    )
    private_keys = {
        kid: Ed25519PrivateKey.from_private_bytes(b64decode(material, validate=True))
        for kid, material in settings.jwt_ed25519_key_ring.items()
    }
    token_service = TokenService(
        issuer=settings.jwt_issuer,
        active_kid=settings.jwt_active_kid,
        signing_keys=private_keys,
        verification_keys={kid: key.public_key() for kid, key in private_keys.items()},
    )
    idempotency = IdempotencyService(
        key_hash_secret=_derived_key(b"idempotency", refresh_key)
    )
    authz_cache = AuthorizationCache(redis=redis)
    step_up = StepUpStore(redis=redis, hmac_key=_derived_key(b"step-up", csrf_key))
    delivery = invitation_delivery
    if delivery is None and settings.environment == "test":
        delivery = TestInvitationDeliveryAdapter(environment="test")
    delivery_capability = InvitationDeliveryCapability(delivery)

    async def mysql_readiness() -> None:
        async with engine.connect() as connection:
            if await connection.scalar(text("SELECT 1")) != 1:
                raise RuntimeError("database readiness check failed")

    async def redis_readiness() -> None:
        await redis.ping()

    identity = IdentityService(
        uow_factory=lambda: cast(
            IdentityUnitOfWork,
            SqlAlchemyIdentityUnitOfWork(session_factory),
        ),
        password_hasher=Argon2PasswordHasher(),
        cipher=cipher,
        blind_index=blind_index,
        rollout_policy=settings.identity_rollout_policy(),
    )
    sessions = SessionService(
        uow_factory=lambda: cast(
            SessionUnitOfWork,
            SqlAlchemySessionUnitOfWork(session_factory),
        ),
        token_service=token_service,
        refresh_hash_key=refresh_key,
    )
    tenancy = TenantService(
        uow_factory=lambda: cast(
            TenantWorkflowUnitOfWork,
            SqlAlchemyTenantWorkflowUnitOfWork(session_factory),
        ),
        idempotency=idempotency,
        cursor_secret=_derived_key(b"member-cursor", csrf_key),
        authorization_cache=authz_cache,
    )
    invitations = InvitationService(
        uow_factory=lambda: SqlAlchemyInvitationWorkflowUnitOfWork(session_factory),
        idempotency=idempotency,
        token_hasher=InvitationTokenHasher(
            hmac_key=_derived_key(b"invitation-token", refresh_key)
        ),
        blind_index=blind_index,
        target_fingerprint_hasher=InvitationTargetFingerprintHasher(
            hmac_key=_derived_key(b"invitation-target", refresh_key)
        ),
        cipher=cipher,
        delivery=delivery,
    )
    platform = PlatformReviewService(
        uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(session_factory),
        idempotency=idempotency,
        step_up=step_up,
    )
    active = ApplicationServices(
        identity=identity,
        sessions=sessions,
        tenancy=tenancy,
        invitations=invitations,
        platform=platform,
        rate_limiter=RateLimiter(
            redis=redis,
            hmac_key=_derived_key(b"rate-limit", csrf_key),
        ),
        step_up=step_up,
        csrf=CsrfService(key=csrf_key, trusted_origins=settings.trusted_origins),
        token_service=token_service,
        accounts=AccountQueryService(
            uow_factory=lambda: cast(
                AccountQueryUnitOfWork,
                SqlAlchemyAccountQueryUnitOfWork(session_factory),
            )
        ),
        ai_jobs=_build_ai_job_service(session_factory, idempotency),
        rule_check_http=_build_rule_check_http_service(session_factory, idempotency),
        matter_document_http=_build_matter_document_http_service(
            session_factory, idempotency
        ),
        document_review_http=_build_document_review_http_service(
            session_factory, idempotency
        ),
        rule_pack_admin_http=_build_rule_pack_admin_http_service(
            session_factory, idempotency
        ),
        audit_query_http=_build_audit_query_http_service(session_factory),
        legal_corpus_http=_build_legal_corpus_http_service(session_factory),
        legal_version_diff_http=_build_legal_version_diff_http_service(session_factory),
        legal_chat_http=_build_legal_chat_http_service(settings),
        legal_retrieval_qa_http=_build_legal_retrieval_qa_http_service(
            settings, session_factory
        ),
        mcp_gateway_http=_build_mcp_gateway_http_service(session_factory),
        invitation_delivery=delivery_capability,
        readiness=ConcurrentReadinessProbe(
            checks=(mysql_readiness, redis_readiness),
            timeout_seconds=2.0,
        ),
    )
    try:
        yield active
    finally:
        try:
            await redis.aclose()
        finally:
            await engine.dispose()


def _build_ai_job_service(
    session_factory: Any,
    idempotency: IdempotencyService,
) -> Any:
    """Composition root for the synthetic AI Job HTTP service."""
    from lawyer_agent.application.ai_job_service import AIJobService
    from lawyer_agent.infrastructure.persistence.ai_jobs_uow import (
        SqlAlchemyAIJobUnitOfWork,
    )

    return AIJobService(
        lambda: SqlAlchemyAIJobUnitOfWork(session_factory),
        idempotency,
    )


def _build_rule_check_http_service(
    session_factory: Any, idempotency: IdempotencyService | None = None
) -> Any:
    """Composition root for the deterministic Rule Check HTTP service."""
    from lawyer_agent.application.rule_check_api import RuleCheckHttpService
    from lawyer_agent.infrastructure.persistence.rule_check_uow import (
        SqlAlchemyRuleCheckUnitOfWork,
    )

    return RuleCheckHttpService(
        lambda: SqlAlchemyRuleCheckUnitOfWork(session_factory),
        idempotency=idempotency,
    )


def _build_matter_document_http_service(
    session_factory: Any, idempotency: IdempotencyService | None = None
) -> Any:
    """Composition root for the tenant Matter/Document HTTP service."""
    from lawyer_agent.application.matter_document_api import (
        MatterDocumentHttpService,
    )
    from lawyer_agent.infrastructure.persistence.matter_document_uow import (
        SqlAlchemyMatterDocumentUnitOfWork,
    )

    return MatterDocumentHttpService(
        lambda: SqlAlchemyMatterDocumentUnitOfWork(session_factory),
        idempotency=idempotency,
    )


def _build_document_review_http_service(
    session_factory: Any, idempotency: IdempotencyService | None = None
) -> Any:
    """Composition root for the Document Review HTTP service."""
    from lawyer_agent.application.document_review_api import (
        DocumentReviewHttpService,
    )
    from lawyer_agent.infrastructure.persistence.matter_document_uow import (
        SqlAlchemyMatterDocumentUnitOfWork,
    )

    return DocumentReviewHttpService(
        lambda: SqlAlchemyMatterDocumentUnitOfWork(session_factory),
        idempotency=idempotency,
    )


def _build_rule_pack_admin_http_service(
    session_factory: Any, idempotency: IdempotencyService | None = None
) -> Any:
    """Composition root for the Rule Pack admin HTTP service."""
    from lawyer_agent.application.rule_pack_admin_api import (
        RulePackAdminHttpService,
    )
    from lawyer_agent.infrastructure.persistence.rule_pack_admin_uow import (
        SqlAlchemyRulePackAdminUnitOfWork,
    )

    return RulePackAdminHttpService(
        lambda: SqlAlchemyRulePackAdminUnitOfWork(session_factory),
        idempotency=idempotency,
    )


def _build_audit_query_http_service(session_factory: Any) -> Any:
    """Composition root for the tenant audit query HTTP service."""
    from lawyer_agent.application.audit_query_api import AuditQueryHttpService
    from lawyer_agent.infrastructure.persistence.matter_document_uow import (
        SqlAlchemyMatterDocumentUnitOfWork,
    )

    return AuditQueryHttpService(
        lambda: SqlAlchemyMatterDocumentUnitOfWork(session_factory)
    )


def _build_legal_corpus_http_service(session_factory: Any) -> Any:
    """Composition root for the public legal corpus read HTTP service."""
    from lawyer_agent.application.legal_corpus_read import LegalCorpusQueryService
    from lawyer_agent.infrastructure.persistence.legal_corpus_read_uow import (
        SqlAlchemyLegalCorpusReadUnitOfWork,
    )

    return LegalCorpusQueryService(
        lambda: SqlAlchemyLegalCorpusReadUnitOfWork(session_factory)
    )


def _build_legal_chat_http_service(settings: Any) -> Any:
    """Composition root for the public legal chat HTTP service.

    Without a configured DeepSeek API key the gateway is None and every call
    fails with a stable 503 ``model_provider_unavailable`` (never a fake reply).
    """
    from lawyer_agent.application.legal_chat import LegalChatHttpService
    from lawyer_agent.application.model_gateway import ModelGateway
    from lawyer_agent.domain.model_gateway import CallLimits
    from lawyer_agent.infrastructure.providers.deepseek import DeepSeekChatProvider
    from lawyer_agent.infrastructure.providers.recorder import (
        LoggingModelCallRecorder,
    )

    api_key = getattr(settings, "deepseek_api_key", None)
    if not api_key:
        return LegalChatHttpService(gateway=None)
    gateway = ModelGateway(
        provider=DeepSeekChatProvider(api_key=api_key),
        recorder=LoggingModelCallRecorder(),
        limits=CallLimits(timeout_seconds=30.0, max_attempts=1),
    )
    return LegalChatHttpService(gateway=gateway)


def _build_legal_version_diff_http_service(session_factory: Any) -> Any:
    """Composition root for the public legal version diff HTTP service."""
    from lawyer_agent.application.legal_corpus_diff import LegalVersionDiffReadService
    from lawyer_agent.infrastructure.persistence.legal_corpus_read_uow import (
        SqlAlchemyLegalCorpusReadUnitOfWork,
    )

    return LegalVersionDiffReadService(
        lambda: SqlAlchemyLegalCorpusReadUnitOfWork(session_factory)
    )


def _build_legal_retrieval_qa_http_service(
    settings: Any,
    session_factory: Any,
) -> Any:
    """Composition root for retrieval-grounded Q&A over the legal dataset.

    Requires a configured DeepSeek API key (the claims chat) and an OpenSearch
    endpoint (hybrid search target). The embedding provider loads its model
    lazily on the first query, so startup performs no model or network work.
    Without the prerequisites the service stays None and the endpoint answers
    a stable 503 ``retrieval_qa_unavailable`` — nothing is fabricated.
    """
    api_key = getattr(settings, "deepseek_api_key", None)
    opensearch_url = getattr(settings, "opensearch_url", None)
    if not api_key or not opensearch_url:
        return None
    from lawyer_agent.application.legal_dataset_evidence import (
        LegalDatasetEvidenceService,
    )
    from lawyer_agent.application.legal_dataset_search import (
        LegalDatasetSearchService,
    )
    from lawyer_agent.application.legal_evidence_assembly import (
        LegalEvidenceAssemblyService,
    )
    from lawyer_agent.application.legal_hybrid_search import (
        LegalHybridSearchService,
    )
    from lawyer_agent.application.legal_index_alias import (
        LegalDatasetAliasService,
    )
    from lawyer_agent.application.legal_retrieval_qa import (
        DEFAULT_EMBED_DIMENSION,
        DEFAULT_EMBED_MODEL_REF,
        LegalRetrievalQaService,
    )
    from lawyer_agent.application.model_gateway import ModelGateway
    from lawyer_agent.domain.model_gateway import CallLimits
    from lawyer_agent.infrastructure.providers.deepseek import DeepSeekChatProvider
    from lawyer_agent.infrastructure.providers.embedding import (
        LocalSentenceTransformerEmbeddingProvider,
    )
    from lawyer_agent.infrastructure.providers.recorder import (
        LoggingModelCallRecorder,
    )
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    embed_model_ref = getattr(
        settings, "embedding_model_ref", DEFAULT_EMBED_MODEL_REF
    )
    embed_dimension = getattr(
        settings, "embedding_dimension", DEFAULT_EMBED_DIMENSION
    )
    embed_gateway = ModelGateway(
        provider=LocalSentenceTransformerEmbeddingProvider(
            model_name_or_path=embed_model_ref
        ),
        recorder=LoggingModelCallRecorder(),
        limits=CallLimits(timeout_seconds=60.0, max_attempts=1),
    )
    os_client = OpenSearchRestClient(base_url=opensearch_url)
    alias_service = LegalDatasetAliasService(os_client)
    hybrid = LegalHybridSearchService(gateway=embed_gateway, search=os_client)
    dataset_search = LegalDatasetSearchService(
        hybrid=hybrid,
        alias=alias_service,
    )
    assembly = LegalEvidenceAssemblyService(
        query=_LegalEvidenceAssemblyQueryAdapter(session_factory)
    )
    dataset_evidence = LegalDatasetEvidenceService(
        dataset_search=dataset_search,
        evidence_assembly=assembly,
    )
    chat_gateway = ModelGateway(
        provider=DeepSeekChatProvider(api_key=api_key),
        recorder=LoggingModelCallRecorder(),
        limits=CallLimits(timeout_seconds=30.0, max_attempts=1),
    )
    return LegalRetrievalQaService(
        dataset_evidence=dataset_evidence,
        chat=chat_gateway,
        embed_model_ref=embed_model_ref,
        embed_dimension=embed_dimension,
    )


class _LegalEvidenceAssemblyQueryAdapter:
    """Fresh-session corpus reads for evidence assembly (public corpus)."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def version_with_instrument(self, version_id: Any) -> Any:
        from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
            SqlAlchemyLegalCorpusRepository,
        )

        async with self._session_factory() as session:
            return await SqlAlchemyLegalCorpusRepository(session).version_with_instrument(
                version_id
            )

    async def provisions_for_version(self, version_id: Any) -> Any:
        from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
            SqlAlchemyLegalCorpusRepository,
        )

        async with self._session_factory() as session:
            return await SqlAlchemyLegalCorpusRepository(
                session
            ).provisions_for_version(version_id)


def _corpus_instrument_payload(instrument: Any) -> dict[str, Any]:
    """Whitelisted public instrument identity (never tenant-private data)."""
    return {
        "id": str(instrument.id),
        "title": instrument.title,
        "issuing_authority": instrument.issuing_authority,
        "jurisdiction": instrument.jurisdiction,
        "region_code": instrument.region_code,
    }


def _corpus_version_payload(version: Any) -> dict[str, Any]:
    """Whitelisted version metadata (mirrors LegalVersionSummary fields)."""
    return {
        "id": str(version.id),
        "version_label": version.version_label,
        "status": version.status.value,
        "published_on": (
            version.published_on.isoformat() if version.published_on is not None else None
        ),
        "effective_on": (
            version.effective_on.isoformat() if version.effective_on is not None else None
        ),
        "repealed_on": (
            version.repealed_on.isoformat() if version.repealed_on is not None else None
        ),
        "law_number": version.law_number,
        "dataset_version": version.dataset_version,
        "parser_version": version.parser_version,
    }


def _corpus_provision_payload(provision: Any) -> dict[str, Any]:
    """Whitelisted provision text (mirrors ProvisionSummary fields)."""
    return {
        "id": str(provision.id),
        "provision_no": provision.provision_no,
        "level": provision.level.value,
        "structure_path": list(provision.structure_path),
        "title": provision.title,
        "full_text": provision.full_text,
    }


def _build_mcp_gateway_http_service(session_factory: Any) -> Any:
    """Composition root for the controlled agent tool gateway (no external
    MCP server; only tools registered here may ever run). Registers the public
    corpus read/search tools backed by the same read service the corpus HTTP
    endpoints use, so an agent can search the catalogue and then read an
    instrument, its versions and their provisions end to end."""

    from uuid import UUID as UuidType

    from lawyer_agent.application.legal_corpus_read import (
        LegalCorpusInstrumentNotFound,
        LegalCorpusQueryService,
        LegalCorpusVersionNotFound,
    )
    from lawyer_agent.application.mcp_gateway import (
        AllowedToolRegistry,
        MCPClientGateway,
        MCPGatewayError,
    )
    from lawyer_agent.domain.common import require_uuid7
    from lawyer_agent.infrastructure.persistence.legal_corpus_read_uow import (
        SqlAlchemyLegalCorpusReadUnitOfWork,
    )

    def _service() -> LegalCorpusQueryService:
        return LegalCorpusQueryService(
            lambda: SqlAlchemyLegalCorpusReadUnitOfWork(session_factory)
        )

    def _uuid_arg(args: dict[str, Any], key: str) -> UUID:
        raw = args[key]
        try:
            return require_uuid7(UuidType(str(raw).strip()), field=key)
        except (ValueError, TypeError) as exc:
            raise MCPGatewayError(
                "invalid_arguments", f"{key} 必须是 UUID7 字符串"
            ) from exc

    async def _search_instruments(args: dict[str, Any]) -> object:
        limit = int(args.get("limit", 20))
        title = args.get("title")
        instruments = await _service().instruments(
            limit=limit,
            before_id=None,
            title=title if isinstance(title, str) and title.strip() else None,
            issuing_authority=None,
            jurisdiction=None,
            region_code=None,
        )
        return [_corpus_instrument_payload(item) for item in instruments]

    async def _get_instrument(args: dict[str, Any]) -> object:
        instrument_id = _uuid_arg(args, "instrument_id")
        try:
            instrument = await _service().instrument(instrument_id=instrument_id)
        except LegalCorpusInstrumentNotFound:
            return {"found": False, "instrument": None}
        return {"found": True, "instrument": _corpus_instrument_payload(instrument)}

    async def _list_versions(args: dict[str, Any]) -> object:
        instrument_id = _uuid_arg(args, "instrument_id")
        service = _service()
        try:
            instrument = await service.instrument(instrument_id=instrument_id)
            versions = await service.versions_for_instrument(
                instrument_id=instrument_id
            )
        except LegalCorpusInstrumentNotFound:
            return {
                "found": False,
                "instrument": None,
                "version_count": 0,
                "versions": [],
            }
        return {
            "found": True,
            "instrument": _corpus_instrument_payload(instrument),
            "version_count": len(versions),
            "versions": [_corpus_version_payload(version) for version in versions],
        }

    async def _list_provisions(args: dict[str, Any]) -> object:
        version_id = _uuid_arg(args, "version_id")
        service = _service()
        try:
            version = await service.version(version_id=version_id)
        except LegalCorpusVersionNotFound:
            return {
                "found": False,
                "version": None,
                "provision_count": 0,
                "provisions": [],
            }
        provisions = await service.provisions_for_version(version_id=version_id)
        return {
            "found": True,
            "version": _corpus_version_payload(version),
            "provision_count": len(provisions),
            "provisions": [_corpus_provision_payload(item) for item in provisions],
        }

    registry = AllowedToolRegistry()
    registry.register(
        name="corpus.instruments_search",
        description="按名称搜索已入库的公共法规目录（title 子串，可选 limit 1-100）",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "法规名称子串"},
                "limit": {"type": "integer", "enum": [10, 20, 50, 100]},
            },
            "additionalProperties": False,
        },
        handler=_search_instruments,
    )
    registry.register(
        name="corpus.instrument_get",
        description="按 instrument_id 读取单部公共法规的身份元数据",
        input_schema={
            "type": "object",
            "properties": {
                "instrument_id": {
                    "type": "string",
                    "description": "法规 UUID7（来自 instruments_search 输出的 id）",
                },
            },
            "required": ["instrument_id"],
            "additionalProperties": False,
        },
        handler=_get_instrument,
    )
    registry.register(
        name="corpus.versions_list",
        description="按 instrument_id 读取一部公共法规的全部版本清单",
        input_schema={
            "type": "object",
            "properties": {
                "instrument_id": {
                    "type": "string",
                    "description": "法规 UUID7（来自 instruments_search 输出的 id）",
                },
            },
            "required": ["instrument_id"],
            "additionalProperties": False,
        },
        handler=_list_versions,
    )
    registry.register(
        name="corpus.provisions_list",
        description="按 version_id 读取单个公共法规版本的全部条文全文（含条号与结构路径）",
        input_schema={
            "type": "object",
            "properties": {
                "version_id": {
                    "type": "string",
                    "description": "版本 UUID7（来自 versions_list 输出的 id）",
                },
            },
            "required": ["version_id"],
            "additionalProperties": False,
        },
        handler=_list_provisions,
    )
    # Explicit allowlist: exactly these five tools may ever run. The registry
    # stays the closed set of known tools; widening what an agent may call is a
    # deliberate act of extending this tuple, never an automatic consequence of
    # registering another tool.
    allowlist = (
        "meta.list_tools",
        "corpus.instruments_search",
        "corpus.instrument_get",
        "corpus.versions_list",
        "corpus.provisions_list",
    )
    return MCPClientGateway(registry, allowlist=allowlist)


def services(request: Request) -> ApplicationServices:
    value = getattr(request.app.state, "services", None)
    if not isinstance(value, ApplicationServices):
        raise RuntimeError("application services are unavailable")
    return value


Services = Annotated[ApplicationServices, Depends(services)]


def audit_context(request: Request) -> AuditContext:
    trace_id = getattr(request.state, "trace_id", "missing-trace-id")
    key = request.app.state.settings.secret_key.encode("utf-8")
    client = request.client.host if request.client is not None else "unknown"
    user_agent = request.headers.get("user-agent", "")[:512]
    return AuditContext(
        trace_id=trace_id,
        client_ip_hash=hmac.digest(key, f"client-ip:v1:{client}".encode(), sha256),
        user_agent_hash=hmac.digest(key, f"user-agent:v1:{user_agent}".encode(), sha256),
    )


Audit = Annotated[AuditContext, Depends(audit_context)]


def require_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in request.app.state.settings.trusted_origins:
        raise ApiProblem(403, "csrf_origin_rejected", "Request origin is not trusted")


TrustedOrigin = Annotated[None, Depends(require_trusted_origin)]


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if authorization is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not token or " " in token:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    return token


Bearer = Annotated[str, Depends(bearer_token)]


async def account_session(token: Bearer, app_services: Services) -> Any:
    return await app_services.sessions.validate_access(token, audience=Audience.ACCOUNT)


AccountSession = Annotated[Any, Depends(account_session)]


def _claims(app_services: ApplicationServices, token: str, audience: Audience) -> Any:
    try:
        return app_services.token_service.verify(
            token,
            audience=audience,
            now=datetime.now(UTC),
        )
    except InvalidToken:
        raise ApiProblem(401, "authentication_failed", "Authentication failed") from None


async def tenant_actor(token: Bearer, app_services: Services) -> TenantActor:
    claims = _claims(app_services, token, Audience.TENANT)
    validated = await app_services.sessions.validate_access(token, audience=Audience.TENANT)
    if validated.tenant_id is None or validated.membership_id is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    principal = Principal(
        user_id=validated.user_id,
        session_id=validated.session_id,
        audience=PrincipalAudience.TENANT,
        tenant_id=validated.tenant_id,
        membership_id=validated.membership_id,
        user_status="active",
        session_valid=True,
        auth_version=claims.auth_version,
        session_auth_version=claims.auth_version,
        permissions=frozenset(),
        role_codes=frozenset(),
        authenticated_at=claims.issued_at,
    )
    context = TenantContext(
        tenant_id=validated.tenant_id,
        membership_id=validated.membership_id,
        membership_user_id=validated.user_id,
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=claims.issued_at,
        valid_until=None,
        authz_version=claims.authz_version,
        session_authz_version=claims.authz_version,
        scope=AuthorizationScope(allow_tenant_wide=True),
    )
    return TenantActor(principal, context)


TenantActorDependency = Annotated[TenantActor, Depends(tenant_actor)]


async def platform_actor(token: Bearer, app_services: Services) -> PlatformActor:
    claims = _claims(app_services, token, Audience.PLATFORM)
    validated = await app_services.sessions.validate_access(token, audience=Audience.PLATFORM)
    principal = Principal(
        user_id=validated.user_id,
        session_id=validated.session_id,
        audience=PrincipalAudience.PLATFORM,
        tenant_id=None,
        membership_id=None,
        user_status="active",
        session_valid=True,
        auth_version=claims.auth_version,
        session_auth_version=claims.auth_version,
        permissions=frozenset(),
        role_codes=frozenset(),
        authenticated_at=claims.issued_at,
    )
    return PlatformActor(principal)


PlatformActorDependency = Annotated[PlatformActor, Depends(platform_actor)]


def require_path_tenant(path_tenant_id: UUID, actor: TenantActor) -> None:
    if path_tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "tenant_resource_not_found", "Resource not found")
