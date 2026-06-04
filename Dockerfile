# ============================================================================
# GRAFOMEM — Production Dockerfile for Railway / cloud deployment
# ============================================================================
# Multi-stage build:
#   Stage 1: Install dependencies + download BGE model weights
#   Stage 2: Slim runtime image with cached model
#
# Supports two modes via GRAFOMEM_BACKEND env var:
#   - sqlite (default): SQLite + sqlite-vec, file at GRAFOMEM_DB_PATH
#   - postgres: PostgreSQL + pgvector, connection via GRAFOMEM_DB_URL
# ============================================================================

FROM python:3.12-slim AS builder

WORKDIR /app

# System deps for building native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ src/
COPY adapter_template/ adapter_template/
COPY corpus/ corpus/
COPY tests/ tests/

# Install the package with all production extras (includes cloud: stripe, bcrypt, PyJWT)
ARG CACHEBUST=2
RUN pip install --no-cache-dir ".[all]"

# Pre-download the BGE embedding model so it's baked into the image
# (avoids ~400MB download on every cold start)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

# ============================================================================
# Runtime stage
# ============================================================================
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy app source
COPY --from=builder /app /app
COPY --from=builder /app/tests /app/tests

# Copy cached model from builder
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

# Railway injects PORT as an environment variable
ENV PORT=8642

# Backend selection: "sqlite" or "postgres"
# When GRAFOMEM_BACKEND=postgres, the server uses PostgresGMPBackend and
# connects to GRAFOMEM_DB_URL (provided by Railway's PostgreSQL plugin).
ENV GRAFOMEM_BACKEND=postgres
ENV GRAFOMEM_DB_URL=""
ENV GRAFOMEM_AUTH_MODE=token

# Persistent storage mount point (Railway volumes — for SQLite fallback)
RUN mkdir -p /data

EXPOSE ${PORT}

# Health check for Railway
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

# Start the server — Railway sets $PORT automatically.
# GRAFOMEM_BACKEND selects sqlite or postgres; GRAFOMEM_DB_URL is the
# PostgreSQL connection URL (injected by Railway's Postgres plugin).
CMD grafomem serve \
    --host 0.0.0.0 \
    --port ${PORT} \
    -b ${GRAFOMEM_BACKEND} \
    --db ${DATABASE_URL:-${GRAFOMEM_DB_URL:-/data/grafomem.db}} \
    --auth ${GRAFOMEM_AUTH_MODE} \
    -e bge
