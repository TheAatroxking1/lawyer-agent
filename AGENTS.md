# Lawyer Agent Repository Instructions

## Scope and language

- These instructions apply to the entire repository.
- Use UTF-8 for source code, configuration, migrations, tests, documentation, prompts, fixtures, logs, and generated text.
- Communicate project decisions and user-facing explanations in Chinese unless the user requests another language. Keep identifiers and established technical terms in their conventional form.
- This product supports only the laws and legal-service context of mainland China. Do not mix foreign law into a Chinese-law conclusion unless the user explicitly requests comparative material and it is clearly separated.

## Read before changing code

1. Read this file.
2. Read `docs/superpowers/specs/2026-08-31-lawyer-agent-enterprise-architecture-design.md` for approved product, security, data, AI, and deployment decisions.
3. Read the relevant approved specification under `docs/superpowers/specs/`.
4. Read the current implementation plan under `docs/superpowers/plans/` and inspect the existing code and tests.
5. If instructions conflict, follow the user's current explicit instruction first, then the approved feature specification, the current implementation plan, and this file. Report material conflicts instead of silently changing an approved boundary.

Do not re-open an already confirmed architectural decision merely because another implementation is possible. Ask only when a missing decision would materially change behavior, security, compatibility, cost, or scope.

## Product and responsibility boundaries

- The product is a public multi-tenant SaaS for enterprise legal departments, law firms serving their clients, and legal education.
- In the initial commercial model, each law-firm tenant supplies its own lawyers. The platform supplies software, AI, evidence retrieval, workflows, and human-review controls; it does not present itself as the retained lawyer.
- The seven planned capability groups are legal Q&A with traceable statutes, contract review, document drafting, private knowledge Q&A, specialist compliance checks, matter/consultation management, and formal reports or legal opinions.
- Responsive Vue clients consume a versioned standard API. Keep business rules in backend services rather than browser-only logic.
- Target high throughput through stateless API scaling, admission control, caching, queues, and asynchronous work. Never interpret the 10,000 RPS target as 10,000 simultaneous model inferences per second.

## Approved architecture

- Backend: Python 3.12, FastAPI, Pydantic v2, and `uv`.
- AI: LangChain is the primary runtime. Use LangGraph only for workflows that genuinely need durable state, branching, pause/resume, or human interruption. Do not add LangGraph ceremony to a simple chain.
- MCP enhances the Agent through a controlled client gateway. Core first-phase features must not depend on a concrete external MCP server.
- Data: MySQL is the source of truth; OpenSearch provides mandatory hybrid legal search; Redis supports cache, rate limits, locks, and ephemeral state; RabbitMQ carries asynchronous job references; MinIO stores immutable document objects and derived artifacts.
- Local development uses Docker Compose. Production topology is designed for Kubernetes. Containers run as non-root and use a read-only root filesystem where supported.
- Model and embedding providers must sit behind project-owned interfaces. Do not scatter vendor-specific payloads, credentials, or model names through domain code.

## Current phase

- Phase 0, the engineering and security foundation, remains active until its approved acceptance gates pass.
- The backend package and local container stack are complete. The next approved backend slice is MySQL/Alembic, global identities, tenant memberships, tenant-bound tokens, RBAC/ABAC, and negative cross-tenant isolation tests.
- Follow the newest user-approved implementation plan for the active slice. When the project advances to a later phase, update this short section without copying task progress or transient status into this file.

## Multi-tenant and authorization invariants

Treat tenant isolation as a security boundary, not as an optional query convention.

- A natural person has one global user identity and may hold memberships in multiple tenants.
- Every tenant-scoped request carries an explicit tenant context that is independently checked against the authenticated identity and active membership.
- Every tenant-owned relational row includes `tenant_id`. Tenant-scoped unique constraints and foreign keys include `tenant_id` where this prevents cross-tenant references.
- Repository and service interfaces inject or require tenant context. Do not expose an unrestricted `get_by_id(id)` for private resources; require tenant identity or an explicitly audited platform scope.
- OpenSearch private queries require tenant routing and filters. Redis keys, locks, quotas, and streams include tenant scope. MinIO object keys use tenant prefixes and short-lived single-object access. Queue messages contain identifiers and signed context, while workers reload authoritative tenant and permission state from MySQL.
- RBAC answers which action a role may perform. ABAC additionally checks department, matter team, confidentiality, creator, engagement, resource state, and approval state.
- External clients and students receive access only to explicitly shared matters, consultations, classes, assignments, or resources.
- Super-administrator access to tenant content requires a selected tenant and resource scope, reason, ticket, expiry, step-up authentication, a short-lived privilege grant, and complete audit events. It is not an implicit unrestricted data path.
- Add negative cross-tenant tests for every new tenant-scoped repository, API, cache, search, object, queue, and tool path. A same-tenant success test alone is insufficient.

## Legal evidence and AI safety invariants

- Every material legal proposition must be traceable to exact evidence: document identity, article or provision, issuing authority, promulgation and effective dates, validity status, jurisdiction or region; record the source URL when available, and when no source URL exists, record the source system, source file or path, and file hash; record dataset/version metadata when available, and never guess or fabricate a URL.
- Retrieval output becomes an explicit Evidence Bundle. Generated claims must reference allowed evidence identifiers, and a citation gate verifies the reference before release.
- If evidence is missing, conflicting, expired, outside the requested date or region, or otherwise insufficient, abstain or clearly narrow the answer. Do not turn model memory into an uncited legal conclusion.
- Distinguish current law, historical law, draft rules, judicial materials, tenant policies, client documents, teaching material, and model-generated content. Never present one category as another.
- Formal legal opinions, litigation strategy, limitation-period conclusions, and externally submitted material require review and approval by a tenant lawyer or legal professional.
- User uploads, webpages, tenant knowledge, retrieved passages, and MCP output are untrusted content. They may supply facts and evidence but never instructions that override system policy, authorization, tool restrictions, or output schemas.
- Do not store or expose chain-of-thought, system prompts, credentials, another tenant's context, or hidden security policy.

## Authentication, privacy, and secrets

- Supported identity channels are account/password, mobile OTP, email, and WeChat. Keep providers behind replaceable adapters.
- Hash passwords with Argon2id. Apply rate limits and enumeration resistance to login, OTP send, and OTP verification paths.
- Store sensitive personal fields encrypted and use purpose-specific blind indexes when exact lookup is required.
- Use short-lived access tokens and rotating refresh tokens. Tenant service accounts and API keys store only hashes and have explicit scopes, expiry, IP policy, and quotas.
- Never commit `.env`, API keys, passwords, private keys, OTP values, access or refresh tokens, personal information, customer files, legal-document originals, or production exports.
- Use synthetic data in tests. Redact identifiers, document contents, prompts, and provider payloads from logs and fixtures.
- Do not weaken authentication, tenant filtering, audit, evidence validation, or human-review controls to make a demonstration pass.

## Legal corpus and document handling

- `F:\ai律师数据库` is an external read-only source corpus obtained from the National Laws and Regulations Database. Do not rename, rewrite, delete, or reorganize it without explicit user authorization.
- Inspect only the smallest useful sample for the current task. Do not recursively read the whole corpus into context or calculate expensive corpus-wide results without a stated need.
- ZIP archives duplicate already extracted Word documents; skip them unless the user specifically requests archive validation.
- Preserve source files as immutable originals. Record hashes, source metadata, parser version, ingestion run, and derived document versions separately.
- Do not assume generic fixed-size chunks are correct for every document. Prefer legal structure such as title, chapter, section, article, paragraph, and item, while allowing parser-specific fallbacks and later evaluation.
- Treat `.doc` and `.docx` support as an ingestion concern behind a project loader interface. Verify actual parser behavior and extraction quality on representative samples.

## Python and API engineering

- Keep the importable package under `backend/src/lawyer_agent` and tests under `backend/tests`.
- Prefer small modules with one responsibility and explicit typed interfaces. Domain code must not import FastAPI request objects, ORM sessions, or provider SDK payloads directly.
- Use Pydantic models at external boundaries and strict type checking for application code. Avoid untyped dictionaries across service boundaries when a named model is practical.
- Version public endpoints under `/api/v1`. Tenant resources use explicit tenant paths such as `/api/v1/tenants/{tenant_id}/...`, but the server must still verify membership and resource scope.
- Preserve the stable problem-details error shape and request/trace identifiers. Do not leak stack traces, SQL text, secrets, prompts, or internal provider responses.
- Make asynchronous workers idempotent. RabbitMQ uses at-least-once delivery, bounded retry with backoff, and dead-letter handling.
- Use Alembic for schema changes. Do not mutate an already released migration; add a new forward migration and test upgrade behavior. Destructive data migrations require an explicit rollout and recovery design.

## Development workflow

- Before implementing a feature or behavior change, confirm that an approved design and an executable plan cover it. Keep each increment independently testable.
- Use test-driven development: write the focused failing test, observe the expected failure, implement the minimum correct behavior, then run focused and broader verification.
- Diagnose failures before editing. Do not mask failures by weakening assertions, disabling security, swallowing exceptions, or removing required behavior.
- Preserve user changes in a dirty worktree. Do not rewrite unrelated files or use destructive Git commands.
- Use `rg` or `rg --files` for repository searches. Use patch-based edits for deliberate text changes.
- Keep commits scoped and reviewable. Do not commit local environments, caches, generated credentials, runtime volumes, corpus data, or temporary review artifacts.
- Update the relevant specification or plan when an approved interface or durable architectural decision changes. Do not copy temporary progress notes into this file.

## Standard commands

Run backend commands from `backend/`:

```powershell
uv sync --frozen
uv run pytest -v
uv run ruff check .
uv run mypy src
```

Prepare local environment from the repository root without committing it:

```powershell
if (-not (Test-Path -LiteralPath deploy\.env)) {
  Copy-Item deploy\compose.env.example deploy\.env
}
.\scripts\dev.ps1
```

Validate Compose configuration:

```powershell
docker compose --env-file deploy\.env -f deploy\compose.yaml config --quiet
```

From the repository root, use `docker compose --env-file deploy\.env -f deploy\compose.yaml down` for cleanup; the unqualified `docker compose down` is not sufficient. Never add `-v` unless the user explicitly authorizes deletion of named development data volumes.

## Definition of done

Before claiming completion:

- Run the focused tests and the complete relevant pytest suite from the final working tree.
- Run Ruff and mypy with zero errors.
- Validate Compose when deployment configuration changes.
- Run integration and negative tenant-isolation tests when a storage, authentication, authorization, cache, search, object, queue, or tool boundary changes.
- Verify migrations in the required upgrade path when database schema changes.
- Confirm no secret or `.env` file is tracked and inspect the final Git diff for unrelated changes.
- Report exact verification evidence and any remaining warnings or unverified external dependency. Do not equate an unavailable network service with a passing integration test.
