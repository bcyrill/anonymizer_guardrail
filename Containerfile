# syntax=docker/dockerfile:1.7
# Containerfile for anonymizer-guardrail
#
# Three image flavours, controlled by two build-args:
#
#   WITH_PRIVACY_FILTER         install torch + transformers (default false)
#   BAKE_PRIVACY_FILTER_MODEL   pre-download the model weights (default false)
#
# 1. Slim (~150 MB):
#      podman build -t anonymizer-guardrail:latest -f Containerfile .
#    No ML deps. Pick this if DETECTOR_MODE never includes privacy_filter.
#
# 2. Privacy-filter, runtime download (~700 MB image):
#      podman build -t anonymizer-guardrail:privacy-filter \
#          --build-arg WITH_PRIVACY_FILTER=true -f Containerfile .
#    Has torch/transformers, but downloads the ~3 GB model on first
#    container start. Mount a NAMED VOLUME at /app/.cache/huggingface so
#    the download survives container recreation:
#      podman volume create anonymizer-hf-cache
#      podman run --rm -p 8000:8000 \
#          -e DETECTOR_MODE=regex,privacy_filter,llm \
#          -v anonymizer-hf-cache:/app/.cache/huggingface \
#          anonymizer-guardrail:privacy-filter
#
# 3. Privacy-filter, model baked in (~3.5 GB):
#      podman build -t anonymizer-guardrail:privacy-filter-baked \
#          --build-arg WITH_PRIVACY_FILTER=true \
#          --build-arg BAKE_PRIVACY_FILTER_MODEL=true \
#          -f Containerfile .
#    Self-contained — no runtime download, works air-gapped. Volume is
#    optional; it only matters for sharing the cache across multiple
#    image versions.
#
# Default run (slim or privacy-filter image):
#   podman run --rm -p 8000:8000 \
#       -e LLM_API_BASE=http://litellm:4000/v1 \
#       -e LLM_API_KEY=sk-litellm-master \
#       -e LLM_MODEL=anonymize \
#       --name anonymizer anonymizer-guardrail:latest

FROM docker.io/library/python:3.12-slim AS base

LABEL org.opencontainers.image.title="anonymizer-guardrail" \
      org.opencontainers.image.description="LiteLLM Generic Guardrail API for round-trip anonymization" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl for HEALTHCHECK only.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root user (Podman is rootless by default; this also helps on K8s).
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid ${APP_GID} app \
 && useradd  --system --uid ${APP_UID} --gid ${APP_GID} \
             --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Install the project. We copy pyproject + README first so dependency installs
# get cached when only source changes.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Build-args separating two orthogonal decisions:
#   - WITH_PRIVACY_FILTER:        install torch + transformers (~500 MB)
#   - BAKE_PRIVACY_FILTER_MODEL:  pre-download the ~3 GB model into the image
# Defaulting both to false keeps the slim image as the no-flag-needed default.
# Operators who want runtime download must mount a persistent volume at
# /app/.cache/huggingface; otherwise every container start re-downloads.
ARG WITH_PRIVACY_FILTER=false
ARG BAKE_PRIVACY_FILTER_MODEL=false

RUN if [ "$WITH_PRIVACY_FILTER" = "true" ]; then \
        pip install --no-cache-dir ".[privacy-filter]"; \
    else \
        pip install --no-cache-dir .; \
    fi

# HF cache lives at a known path so operators can mount a named volume
# at the same location to persist downloads across container recreation.
ENV HF_HOME=/app/.cache/huggingface

# Optional: pre-download the model into the image. Skipped unless BOTH
# args are true — baking only makes sense when the deps are present.
RUN if [ "$WITH_PRIVACY_FILTER" = "true" ] && [ "$BAKE_PRIVACY_FILTER_MODEL" = "true" ]; then \
        python -c "from transformers import pipeline; pipeline('token-classification', model='openai/privacy-filter', aggregation_strategy='first')"; \
    fi

RUN chown -R app:app /app

# ── Defaults ─────────────────────────────────────────────────────────────────
ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=info \
    DETECTOR_MODE=regex,llm \
    LLM_API_BASE=http://litellm:4000/v1 \
    LLM_MODEL=anonymize \
    LLM_TIMEOUT_S=30 \
    LLM_MAX_CHARS=200000 \
    VAULT_TTL_S=600 \
    FAIL_CLOSED=true

EXPOSE 8000

USER app

# Generous start-period to accommodate the no-bake privacy-filter case,
# where the first container start downloads ~3 GB of model weights before
# /health responds. start-period only affects the "starting" → "unhealthy"
# transition; once /health succeeds the container moves to "healthy" and
# subsequent failures count normally. Operators with very slow networks
# can override at run time via `--health-start-period=…`.
HEALTHCHECK --interval=15s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Exec form so SIGTERM from `podman stop` reaches uvicorn cleanly. `sh -c` is
# needed so ${HOST}/${PORT} expand at container start, not at build time.
CMD ["sh", "-c", "exec uvicorn anonymizer_guardrail.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL} --proxy-headers"]
