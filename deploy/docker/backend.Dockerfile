FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

RUN pip install --no-cache-dir uv==0.8.15 \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
COPY backend/README.md ./
RUN uv sync --frozen --no-dev

USER 10001
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "lawyer_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
