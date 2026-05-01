# syntax=docker/dockerfile:1.7
# Containerfile for anonymizer-guardrail
#
# Three image flavours, controlled by two build-args:
#
#   WITH_PRIVACY_FILTER         install torch + transformers (default false)
#   BAKE_PRIVACY_FILTER_MODEL   pre-download the model weights (default false)
#
# See README for image sizes; they shift with each torch / model release.
# Default torch is CPU-only (TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu);
# override for a CUDA build.
#
# 1. Slim:
#      podman build -t anonymizer-guardrail:latest -f Containerfile .
#    No ML deps. Pick this if DETECTOR_MODE never includes privacy_filter.
#
# 2. Privacy-filter, runtime download:
#      podman build -t anonymizer-guardrail:privacy-filter \
#          --build-arg WITH_PRIVACY_FILTER=true -f Containerfile .
#    Has torch/transformers, but downloads the model on first container
#    start. Mount a NAMED VOLUME at /app/.cache/huggingface so the
#    download survives container recreation:
#      podman volume create anonymizer-hf-cache
#      podman run --rm -p 8000:8000 \
#          -e DETECTOR_MODE=regex,privacy_filter,llm \
#          -v anonymizer-hf-cache:/app/.cache/huggingface \
#          anonymizer-guardrail:privacy-filter
#
# 3. Privacy-filter, model baked in:
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

# Build-args separating three orthogonal decisions:
#   - WITH_PRIVACY_FILTER:        install torch + transformers
#   - BAKE_PRIVACY_FILTER_MODEL:  pre-download the model into the image
#   - TORCH_INDEX_URL:            which PyTorch wheel index to use
#
# Defaulting WITH_PRIVACY_FILTER and BAKE_PRIVACY_FILTER_MODEL to false
# keeps the slim image as the no-flag-needed default. TORCH_INDEX_URL
# defaults to the CPU-only index — `pip install torch` from PyPI on Linux
# x86 silently pulls in nvidia-cuda-runtime, nvidia-cudnn, nvidia-cublas,
# etc. that we don't use; the CPU wheel skips them, dropping the image
# substantially. Operators deploying behind GPUs can override:
#   --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121
ARG WITH_PRIVACY_FILTER=false
ARG BAKE_PRIVACY_FILTER_MODEL=false
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# When privacy-filter is enabled, install torch first from the chosen
# index so the version we want is already on disk. The subsequent
# `pip install ".[privacy-filter]"` then satisfies its torch dep without
# pulling a different (possibly CUDA-fattened) build from the default
# PyPI index.
#
# `--mount=type=cache,target=/root/.cache/pip` keeps the wheel cache
# across builds (rebuilds with the same pyproject.toml are nearly
# instant; even after a dep bump, only the changed wheels download).
# We deliberately drop --no-cache-dir on the pip calls — the mount
# IS the cache, and the wheels live in the cache mount, not in the
# image layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WITH_PRIVACY_FILTER" = "true" ]; then \
        pip install --index-url "$TORCH_INDEX_URL" torch \
     && pip install ".[privacy-filter]"; \
    else \
        pip install .; \
    fi

# HF cache lives at a known path so operators can mount a named volume
# at the same location to persist downloads across container recreation.
ENV HF_HOME=/app/.cache/huggingface

# Optional: pre-download the model into the image. Skipped unless BOTH
# args are true — baking only makes sense when the deps are present.
#
# When the model is baked in, also default HF_HUB_OFFLINE=1 so that
# transformers doesn't ping HuggingFace Hub on every container start to
# revalidate the cache (visible as 5+ seconds of HEAD requests and a
# misleading "set HF_TOKEN" warning). The setting is written to
# /app/_runtime-env, which the CMD sources before exec'ing uvicorn; the
# `${HF_HUB_OFFLINE:-1}` form lets operators force online mode at run
# time with `-e HF_HUB_OFFLINE=0` if they need to refresh weights from
# inside a baked image. Slim and runtime-download flavours get an empty
# file so the source statement in CMD is a no-op for them.
RUN if [ "$WITH_PRIVACY_FILTER" = "true" ] && [ "$BAKE_PRIVACY_FILTER_MODEL" = "true" ]; then \
        python -c "from transformers import pipeline; pipeline('token-classification', model='openai/privacy-filter', aggregation_strategy='first')" \
     && echo 'export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"' > /app/_runtime-env; \
    else \
        : > /app/_runtime-env; \
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
# where the first container start downloads the model weights before
# /health responds. start-period only affects the "starting" → "unhealthy"
# transition; once /health succeeds the container moves to "healthy" and
# subsequent failures count normally. Operators with very slow networks
# can override at run time via `--health-start-period=…`.
HEALTHCHECK --interval=15s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Exec form so SIGTERM from `podman stop` reaches uvicorn cleanly. `sh -c` is
# needed so ${HOST}/${PORT} expand at container start, not at build time.
# Sourcing /app/_runtime-env applies any flavour-specific defaults (e.g.
# HF_HUB_OFFLINE=1 for the baked image). The file is empty for flavours
# that don't need it, so the source is a cheap no-op.
CMD ["sh", "-c", ". /app/_runtime-env && exec uvicorn anonymizer_guardrail.main:app --host ${HOST} --port ${PORT} --log-level ${LOG_LEVEL} --proxy-headers"]
