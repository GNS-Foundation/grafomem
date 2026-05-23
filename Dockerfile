# ============================================================================
# GRAFOMEM — Production Dockerfile for Railway / cloud deployment
# ============================================================================
# Multi-stage build:
#   Stage 1: Install dependencies + download BGE model weights
#   Stage 2: Slim runtime image with cached model
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

# Install the package with all production extras
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

# Copy cached model from builder
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

# Railway injects PORT as an environment variable
ENV PORT=8642
ENV GRAFOMEM_DB_PATH=/data/grafomem.db
ENV GRAFOMEM_AUTH_MODE=none

# Persistent storage mount point (Railway volumes)
RUN mkdir -p /data

EXPOSE ${PORT}

# Health check for Railway
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

# Start the server — Railway sets $PORT automatically
CMD grafomem serve \
    --host 0.0.0.0 \
    --port ${PORT} \
    --db ${GRAFOMEM_DB_PATH} \
    --auth ${GRAFOMEM_AUTH_MODE} \
    -e bge
