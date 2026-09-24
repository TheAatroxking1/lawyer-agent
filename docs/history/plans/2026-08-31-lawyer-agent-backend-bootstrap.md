# Lawyer Agent Backend Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first independently testable backend slice: a reproducible Python package, a UTF-8 FastAPI service with stable health/error contracts, and a Docker Compose stack containing all required local infrastructure.

**Architecture:** Keep one Python package under `backend/src/lawyer_agent` and build one non-root backend image that later API, worker, and migration processes can share. This plan intentionally stops before database models and authentication; those receive a separate plan after the bootstrap paths and commands have been executed successfully.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, pytest, Ruff, mypy, Docker Compose, MySQL, Redis, RabbitMQ, OpenSearch, MinIO.

## Global Constraints

- Use UTF-8 for all source, configuration, tests, and generated text.
- Only support mainland China jurisdiction.
- LangChain is the first-phase AI core; LangGraph and concrete MCP servers are not dependencies of this bootstrap.
- OpenSearch is mandatory infrastructure even though legal corpus indexing starts in a later plan.
- Local development uses Docker Compose; backend containers run as non-root with a read-only root filesystem.
- Do not commit `.env`, credentials, tokens, OTPs, or sensitive legal documents.
- Use TDD, run the named verification command after each implementation step, and make the listed commit before starting the next task.

## File Map

```text
backend/
  pyproject.toml                         # Dependencies and quality-tool configuration
  uv.lock                                # Reproducible resolved dependencies
  README.md                              # Backend developer commands
  src/lawyer_agent/__init__.py           # Package version
  src/lawyer_agent/config.py             # Validated environment settings
  src/lawyer_agent/main.py               # FastAPI application factory
  src/lawyer_agent/api/__init__.py        # API package marker
  src/lawyer_agent/api/errors.py          # Stable HTTP error response
  src/lawyer_agent/api/router.py          # Health routes
  tests/unit/test_package.py              # Package contract
  tests/unit/test_compose_contract.py     # Compose service contract
  tests/api/test_health.py                # HTTP contract
deploy/
  compose.yaml                            # Local service topology
  compose.env.example                     # Non-secret environment template
  docker/backend.Dockerfile               # Shared non-root backend image
scripts/dev.ps1                           # Windows one-command stack startup
```

---

### Task 1: Reproducible Python Package

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/README.md`
- Create: `backend/src/lawyer_agent/__init__.py`
- Create: `backend/tests/unit/test_package.py`
- Create: `backend/uv.lock` with `uv lock`

**Interfaces:**
- Consumes: Python 3.12 and uv.
- Produces: importable package `lawyer_agent`, constant `lawyer_agent.__version__: str`, and stable commands `uv run pytest`, `uv run ruff check .`, and `uv run mypy src`.

- [ ] **Step 1: Create dependency metadata and the failing test**

Create `backend/pyproject.toml`:

```toml
[project]
name = "lawyer-agent-backend"
version = "0.1.0"
description = "Multi-tenant mainland China legal agent backend"
readme = "README.md"
requires-python = ">=3.12,<3.14"
dependencies = [
  "fastapi>=0.115,<1",
  "orjson>=3.10,<4",
  "pydantic-settings>=2.6,<3",
  "uvicorn[standard]>=0.32,<1",
]

[dependency-groups]
dev = [
  "httpx>=0.27,<1",
  "mypy>=1.13,<2",
  "pytest>=8.3,<9",
  "pytest-asyncio>=0.24,<2",
  "pyyaml>=6,<7",
  "ruff>=0.8,<1",
]

[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/lawyer_agent"]

[tool.pytest.ini_options]
addopts = "-ra --strict-markers --strict-config"
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC", "S"]
ignore = ["S101"]

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["lawyer_agent"]
```

Create `backend/tests/unit/test_package.py`:

```python
from lawyer_agent import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"
```

- [ ] **Step 2: Resolve dependencies and prove the test fails**

Run from the repository root:

```powershell
Set-Location backend
uv lock
uv run pytest tests/unit/test_package.py -v
```

Expected: `uv lock` succeeds; pytest fails during collection because `lawyer_agent` or `__version__` does not exist.

- [ ] **Step 3: Add the minimal package and README**

Create `backend/src/lawyer_agent/__init__.py`:

```python
"""Backend package for the mainland China Lawyer Agent SaaS."""

__version__ = "0.1.0"
```

Create `backend/README.md`:

````markdown
# Lawyer Agent Backend

Python 3.12 backend for the multi-tenant Lawyer Agent SaaS.

```powershell
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src
```
````

- [ ] **Step 4: Run the package quality gate**

Run from `backend`:

```powershell
uv run pytest tests/unit/test_package.py -v
uv run ruff check .
uv run mypy src
```

Expected: one test passes; Ruff and mypy exit with status 0.

- [ ] **Step 5: Commit**

Run from the repository root:

```powershell
git add backend/pyproject.toml backend/uv.lock backend/README.md backend/src backend/tests
git commit -m "chore: scaffold backend package"
```

### Task 2: FastAPI App, Settings, Health, and Error Contract

**Files:**
- Create: `backend/src/lawyer_agent/config.py`
- Create: `backend/src/lawyer_agent/main.py`
- Create: `backend/src/lawyer_agent/api/__init__.py`
- Create: `backend/src/lawyer_agent/api/errors.py`
- Create: `backend/src/lawyer_agent/api/router.py`
- Create: `backend/tests/api/test_health.py`

**Interfaces:**
- Consumes: `lawyer_agent.__version__` from Task 1.
- Produces: `Settings`, `get_settings()`, `create_app(settings: Settings | None = None) -> FastAPI`, `GET /health/live`, and errors containing `type`, `title`, `status`, `code`, and `trace_id`.

- [ ] **Step 1: Write failing health and error-contract tests**

Create `backend/tests/api/test_health.py`:

```python
from fastapi.testclient import TestClient

from lawyer_agent.config import Settings
from lawyer_agent.main import create_app


def build_client() -> TestClient:
    settings = Settings(environment="test", secret_key="x" * 32)
    return TestClient(create_app(settings))


def test_liveness_contract() -> None:
    response = build_client().get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_unknown_route_uses_problem_details() -> None:
    response = build_client().get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["code"] == "route_not_found"
    assert len(body["trace_id"]) == 32


def test_request_id_is_reused_as_trace_id() -> None:
    response = build_client().get("/missing", headers={"x-request-id": "request-123"})
    assert response.json()["trace_id"] == "request-123"
```

- [ ] **Step 2: Prove the API tests fail**

Run from `backend`:

```powershell
uv run pytest tests/api/test_health.py -v
```

Expected: pytest fails during collection because `lawyer_agent.config` and `lawyer_agent.main` do not exist.

- [ ] **Step 3: Implement complete settings, error handler, router, and app factory**

Create `backend/src/lawyer_agent/api/__init__.py`:

```python
"""HTTP API contracts."""
```

Create `backend/src/lawyer_agent/config.py`:

```python
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_SECRET = "development-only-change-before-exposure"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAWYER_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    secret_key: str = Field(default=DEVELOPMENT_SECRET, min_length=32)
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def reject_development_secret_outside_local_environments(self) -> "Settings":
        if self.environment in {"staging", "production"} and self.secret_key == DEVELOPMENT_SECRET:
            raise ValueError("LAWYER_SECRET_KEY must be replaced outside development and test")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Create `backend/src/lawyer_agent/api/errors.py`:

```python
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def error_code(status_code: int) -> str:
    if status_code == 404:
        return "route_not_found"
    if status_code == 401:
        return "authentication_required"
    if status_code == 403:
        return "permission_denied"
    return "http_error"


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> ORJSONResponse:
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    title = str(exc.detail) if isinstance(exc.detail, str) else "HTTP Error"
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": exc.status_code,
        "code": error_code(exc.status_code),
        "trace_id": trace_id,
    }
    return ORJSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)
```

Create `backend/src/lawyer_agent/api/router.py`:

```python
from fastapi import APIRouter

from lawyer_agent import __version__

health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
```

Create `backend/src/lawyer_agent/main.py`:

```python
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lawyer_agent.api.errors import http_exception_handler
from lawyer_agent.api.router import health_router
from lawyer_agent.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    app = FastAPI(
        title="Lawyer Agent API",
        version="0.1.0",
        default_response_class=ORJSONResponse,
    )
    app.state.settings = active_settings
    app.include_router(health_router)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    return app


app = create_app()
```

- [ ] **Step 4: Run API tests and all static checks**

Run from `backend`:

```powershell
uv run pytest tests/api/test_health.py -v
uv run ruff check src tests
uv run mypy src
```

Expected: three API tests pass; Ruff and mypy exit with status 0.

- [ ] **Step 5: Commit**

Run from the repository root:

```powershell
git add backend/src/lawyer_agent backend/tests/api
git commit -m "feat: add backend health and error contracts"
```

### Task 3: Non-Root Backend Image and One-Command Local Stack

**Files:**
- Create: `deploy/compose.yaml`
- Create: `deploy/compose.env.example`
- Create: `deploy/docker/backend.Dockerfile`
- Create: `scripts/dev.ps1`
- Create: `backend/tests/unit/test_compose_contract.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `lawyer_agent.main:app` and `backend/uv.lock`.
- Produces: services `api`, `mysql`, `redis`, `rabbitmq`, `opensearch`, and `minio`; startup command `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1`.

- [ ] **Step 1: Write the failing Compose contract test**

Create `backend/tests/unit/test_compose_contract.py`:

```python
from pathlib import Path
from typing import Any

import yaml


def load_compose() -> dict[str, Any]:
    path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_compose_contains_required_services() -> None:
    services = load_compose()["services"]
    assert {"api", "mysql", "redis", "rabbitmq", "opensearch", "minio"} <= services.keys()


def test_stateful_services_have_healthchecks_and_volumes() -> None:
    services = load_compose()["services"]
    for name in {"mysql", "redis", "rabbitmq", "opensearch", "minio"}:
        assert "healthcheck" in services[name]
        assert "volumes" in services[name]


def test_api_runs_non_privileged_and_read_only() -> None:
    api = load_compose()["services"]["api"]
    assert api["read_only"] is True
    assert api["security_opt"] == ["no-new-privileges:true"]
```

- [ ] **Step 2: Prove the Compose contract fails**

Run from `backend`:

```powershell
uv run pytest tests/unit/test_compose_contract.py -v
```

Expected: all three tests fail with `FileNotFoundError` for `deploy/compose.yaml`.

- [ ] **Step 3: Add the complete image, environment template, Compose stack, and startup script**

Create `deploy/docker/backend.Dockerfile`:

```dockerfile
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

RUN pip install --no-cache-dir uv==0.8.15 \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
RUN uv sync --frozen --no-dev

USER 10001
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "lawyer_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Create `deploy/compose.env.example`:

```dotenv
LAWYER_ENVIRONMENT=development
LAWYER_SECRET_KEY=replace-with-at-least-32-random-characters
MYSQL_PASSWORD=lawyer-local-password
RABBITMQ_PASSWORD=lawyer-local-password
MINIO_ROOT_PASSWORD=lawyer-local-password
```

Create `deploy/compose.yaml`:

```yaml
name: lawyer-agent

services:
  api:
    build:
      context: ..
      dockerfile: deploy/docker/backend.Dockerfile
    env_file: [.env]
    ports: ["8000:8000"]
    depends_on:
      mysql: {condition: service_healthy}
      redis: {condition: service_healthy}
      rabbitmq: {condition: service_healthy}
      opensearch: {condition: service_healthy}
      minio: {condition: service_healthy}
    read_only: true
    tmpfs: [/tmp]
    security_opt: ["no-new-privileges:true"]

  mysql:
    image: mysql:8.4
    environment:
      MYSQL_DATABASE: lawyer_agent
      MYSQL_USER: lawyer
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
      MYSQL_ROOT_PASSWORD: ${MYSQL_PASSWORD}-root
    ports: ["3306:3306"]
    volumes: ["mysql-data:/var/lib/mysql"]
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u lawyer -p$${MYSQL_PASSWORD}"]
      interval: 5s
      timeout: 3s
      retries: 30

  redis:
    image: redis:7.4-alpine
    command: ["redis-server", "--appendonly", "yes"]
    ports: ["6379:6379"]
    volumes: ["redis-data:/data"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20

  rabbitmq:
    image: rabbitmq:4-management-alpine
    environment:
      RABBITMQ_DEFAULT_USER: lawyer
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASSWORD}
    ports: ["5672:5672", "15672:15672"]
    volumes: ["rabbitmq-data:/var/lib/rabbitmq"]
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 5s
      timeout: 5s
      retries: 30

  opensearch:
    image: opensearchproject/opensearch:3
    environment:
      discovery.type: single-node
      plugins.security.disabled: "true"
      OPENSEARCH_JAVA_OPTS: -Xms512m -Xmx512m
    ports: ["9200:9200"]
    volumes: ["opensearch-data:/usr/share/opensearch/data"]
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:9200/_cluster/health >/dev/null"]
      interval: 10s
      timeout: 5s
      retries: 30

  minio:
    image: minio/minio:latest
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: lawyer
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    ports: ["9000:9000", "9001:9001"]
    volumes: ["minio-data:/data"]
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:9000/minio/health/live >/dev/null"]
      interval: 5s
      timeout: 5s
      retries: 30

volumes:
  mysql-data: {}
  redis-data: {}
  rabbitmq-data: {}
  opensearch-data: {}
  minio-data: {}
```

Create `scripts/dev.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$examplePath = Join-Path $projectRoot 'deploy/compose.env.example'
$envPath = Join-Path $projectRoot 'deploy/.env'
$composePath = Join-Path $projectRoot 'deploy/compose.yaml'

if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Host 'Created deploy/.env. Replace development secrets before exposing services.'
}

docker compose --env-file $envPath -f $composePath up --build
```

Append to `.gitignore`:

```gitignore
deploy/.env
.env
.venv/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

- [ ] **Step 4: Validate Compose, build the image, and run the HTTP smoke test**

Run from the repository root:

```powershell
Copy-Item deploy/compose.env.example deploy/.env -Force
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
Set-Location backend
uv run pytest tests/unit/test_compose_contract.py -v
Set-Location ..
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
$response = Invoke-RestMethod http://localhost:8000/health/live
if ($response.status -ne 'ok' -or $response.version -ne '0.1.0') { throw 'health smoke failed' }
docker compose --env-file deploy/.env -f deploy/compose.yaml down
```

Expected: Compose validation exits 0; three contract tests pass; the image builds; health returns `status=ok` and `version=0.1.0`; `down` stops containers without deleting named volumes.

- [ ] **Step 5: Run the complete bootstrap gate and commit**

Run from `backend`:

```powershell
uv run pytest -v
uv run ruff check .
uv run mypy src
Set-Location ..
git add .gitignore backend deploy/compose.yaml deploy/compose.env.example deploy/docker/backend.Dockerfile scripts/dev.ps1
git commit -m "chore: add containerized local backend stack"
```

Expected: all tests and static checks pass; the commit contains no `deploy/.env`.

## Bootstrap Exit Criteria

This plan is complete only when all of the following are demonstrated from a clean checkout:

1. `uv sync --frozen` succeeds under Python 3.12.
2. `uv run pytest`, `uv run ruff check .`, and `uv run mypy src` exit 0.
3. `docker compose ... config --quiet` validates the stack.
4. API, MySQL, Redis, RabbitMQ, OpenSearch, and MinIO become healthy.
5. `GET /health/live` returns the exact versioned contract.
6. The API container runs as UID 10001 with a read-only root filesystem.
7. `.env` is ignored and no secret appears in Git.

After these criteria pass, write and execute the next plan for MySQL/Alembic, global identities, tenant memberships, tenant-bound tokens, RBAC/ABAC, and cross-tenant isolation tests.
