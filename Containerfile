# syntax=docker/dockerfile:1.7
# Containerfile for anonymizer-guardrail
#
# Build:  podman build -t anonymizer-guardrail:latest -f Containerfile .
# Run:    podman run --rm -p 8000:8000 \
#           -e LLM_API_BASE=http://litellm:4000/v1 \
#           -e LLM_API_KEY=sk-litellm-master \
#           -e LLM_MODEL=anonymize \
#           --name anonymizer anonymizer-guardrail:latest

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

RUN pip install --no-cache-dir .

RUN chown -R app:app /app

# ── Defaults ─────────────────────────────────────────────────────────────────
ENV HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=INFO \
    DETECTOR_MODE=both \
    LLM_API_BASE=http://litellm:4000/v1 \
    LLM_MODEL=anonymize \
    LLM_TIMEOUT_S=30 \
    LLM_MAX_CHARS=200000 \
    VAULT_TTL_S=600 \
    FAIL_CLOSED=true

EXPOSE 8000

USER app

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Exec form so SIGTERM from `podman stop` reaches uvicorn cleanly. `sh -c` is
# needed so ${HOST}/${PORT} expand at container start, not at build time.
CMD ["sh", "-c", "exec uvicorn anonymizer_guardrail.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL,,} --proxy-headers"]
