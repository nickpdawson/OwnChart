# OwnChart API container
# Multi-stage with uv for fast, deterministic builds.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# System deps for native wheels we actually use:
# - lxml (libxml2/libxslt)
# - PyMuPDF ships its own libs but needs build essentials on slim
# - psycopg/asyncpg builds cleanly without extras
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev

COPY ownchart ./ownchart
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --no-dev

# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Tesseract + poppler are intentionally NOT installed here.
# The worker image (later) will install them; the API stays lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 1000 ownchart

WORKDIR /app
COPY --from=builder --chown=ownchart:ownchart /opt/venv /opt/venv
COPY --from=builder --chown=ownchart:ownchart /app /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

USER ownchart

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "ownchart.main:app", "--host", "0.0.0.0", "--port", "8000"]
