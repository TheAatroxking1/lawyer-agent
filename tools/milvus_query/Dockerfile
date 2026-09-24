FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 10001 query \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin query
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY legal_query/ ./legal_query/
COPY simple_search.py evaluate_ragas.py evaluate_ragas_answers.py ./
COPY tests/ ./tests/

USER 10001:10001
ENTRYPOINT ["python", "-m", "legal_query"]

# 测试镜像安装TestClient所需的httpx；正式镜像不安装开发依赖。
FROM runtime AS test
USER root
RUN uv sync --frozen --no-install-project --extra window
USER 10001:10001
