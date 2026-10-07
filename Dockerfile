# syntax=docker/dockerfile:1

# ---- build: resolve dependencies with uv into a self-contained virtualenv ----
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- runtime: slim image, non-root user ----
FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app configs ./configs
COPY --chown=app:app data ./data
COPY --chown=app:app results ./results

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TALLY_LLM_CACHE_DIR=/home/app/.cache/llm \
    TALLY_LLM_LEDGER=/home/app/.cache/calls.jsonl \
    LOG_FORMAT=json

USER app
RUN mkdir -p /home/app/.cache/llm

EXPOSE 8000
# The first start generates the demo database (~240k rows, about 15 seconds) when TALLY_SEED_DEMO=true.
HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["uvicorn", "tally.api.app:create_default_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
