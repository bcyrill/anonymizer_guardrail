# syntax=docker/dockerfile:1.7
# Containerfile for anonymizer-guardrail
#
# Single image — no ML deps. Privacy-filter and gliner-pii ship as
# standalone services (see services/{privacy_filter,gliner_pii}/);
# the guardrail talks to them over HTTP via PRIVACY_FILTER_URL /
# GLINER_PII_URL.
#
# Two optional extras are bundled by default because they're tiny
# and let operators flip a switch at runtime without rebuilding:
#   * `denylist-aho`  → pyahocorasick C-extension (~600 KB) for the
#                        DENYLIST_BACKEND=aho path.
#   * `vault-redis`   → redis-py (~3 MB pure Python) for the
#                        VAULT_BACKEND=redis path used in
#                        multi-replica deployments.
# Direct-pip-install users can still opt out by installing the
# package without these extras.
#
# Build:
#   podman build -t anonymizer-guardrail:latest -f Containerfile .
#
# Default run:
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
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Note: PIP_NO_CACHE_DIR is intentionally unset. The pip RUN steps
# below mount a buildkit cache at /root/.cache/pip — that cache lives
# OUTSIDE the image (it's a buildkit volume detached after the RUN
# completes), so the image stays small AND incremental rebuilds reuse
# previously-downloaded wheels. Setting PIP_NO_CACHE_DIR=1 would defeat
# the mount entirely.

# curl for HEALTHCHECK only. Cache mounts let buildkit reuse the apt
# package + lists across builds; rm -rf of /var/lib/apt/lists isn't
# needed because the cache mounts are unmounted at RUN exit, leaving
# nothing in the image layer.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl

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

# `denylist-aho` (pyahocorasick) and `vault-redis` (redis-py) are
# both included unconditionally because they're tiny:
#   * pyahocorasick is a ~600 KB C extension with prebuilt wheels
#     for every platform that matters; lets DENYLIST_BACKEND=aho
#     work without a rebuild.
#   * redis-py is ~3 MB of pure Python with zero transitive deps;
#     lets VAULT_BACKEND=redis work for multi-replica deployments
#     without a rebuild. Lazy-imported in `vault.build_vault()` so
#     memory-backend operators don't pay the import cost at
#     runtime.
# Direct `pip install` users can opt out by skipping the extras.
#
# `--mount=type=cache,target=/root/.cache/pip` keeps the wheel cache
# across builds (rebuilds with the same pyproject.toml are nearly
# instant; even after a dep bump, only the changed wheels download).
# The mount IS the cache, and the wheels live in the cache mount,
# not in the image layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ".[denylist-aho,vault-redis]"

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
    LLM_FAIL_CLOSED=true

EXPOSE 8000

USER app

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Exec form so SIGTERM from `podman stop` reaches uvicorn cleanly. `sh -c`
# is needed so ${HOST}/${PORT} expand at container start, not build time.
CMD ["sh", "-c", "exec uvicorn anonymizer_guardrail.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL} --proxy-headers"]
