#!/usr/bin/env bash
# Launch the anonymizer-guardrail container in one of three modes,
# matching the flavours produced by scripts/build-image.sh.
#
# - slim:      regex detector only (no LLM creds needed)
# - pf:        privacy-filter detector; downloads the model on first
#              start. Mounts a named volume so subsequent runs reuse
#              the cache instead of re-downloading.
# - pf-baked:  privacy-filter detector; model is in the image. No
#              volume needed.
#
# If the corresponding image isn't on the host, the script offers to
# call scripts/build-image.sh with the matching options.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
say()  { printf '%s\n' "$*"; }
warn() { printf '%s%s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_rst" >&2; }
ok()   { printf '%s%s%s\n' "$c_grn" "$*" "$c_rst"; }

# ── Engine detection (matches build-image.sh) ────────────────────────────────
ENGINE="${ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
  if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  else
    err "Neither podman nor docker found in PATH. Install one or set ENGINE=…"
    exit 1
  fi
fi

# Default tags are kept in sync with build-image.sh — change one, change both.
TAG_SLIM="${TAG_SLIM:-anonymizer-guardrail:latest}"
TAG_PF="${TAG_PF:-anonymizer-guardrail:privacy-filter}"
TAG_PF_BAKED="${TAG_PF_BAKED:-anonymizer-guardrail:privacy-filter-baked}"

# Named volume for the runtime-download flavour. Reused across `pf` runs
# so the ~3 GB model download only happens once per host.
HF_VOLUME="${HF_VOLUME:-anonymizer-hf-cache}"

PORT="${PORT:-8000}"
CONTAINER_NAME_DEFAULT="anonymizer-guardrail"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-t TYPE] [-p PORT] [-n NAME] [-h] [-- EXTRA_RUN_ARGS...]

Launch the anonymizer-guardrail container. Without -t, prompts interactively.

  -t, --type TYPE   slim | pf | pf-baked
  -p, --port PORT   Host port to publish (default: ${PORT})
  -n, --name NAME   Container name (default: ${CONTAINER_NAME_DEFAULT})
  -h, --help        Show this help.

Anything after \`--\` is passed straight through to \`${ENGINE} run\`,
e.g. additional -e env vars (-e LLM_API_KEY=...) or -v volume mounts.

Per-flavour behaviour:
  slim      image "${TAG_SLIM}", DETECTOR_MODE=regex.
            No LLM creds, no volume.
  pf        image "${TAG_PF}", DETECTOR_MODE=privacy_filter,
            with a named volume "${HF_VOLUME}" mounted on
            /app/.cache/huggingface so the model survives recreation.
  pf-baked  image "${TAG_PF_BAKED}", DETECTOR_MODE=privacy_filter.
            Self-contained — no volume needed.

If the image isn't found locally, the script offers to invoke
scripts/build-image.sh with the matching -t flag.
EOF
}

TYPE=""
NAME="$CONTAINER_NAME_DEFAULT"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--type) TYPE="${2:-}"; shift 2 ;;
    -p|--port) PORT="${2:-}"; shift 2 ;;
    -n|--name) NAME="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)        shift; EXTRA_ARGS=("$@"); break ;;
    *) err "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ── Interactive menu ─────────────────────────────────────────────────────────
if [[ -z "$TYPE" ]]; then
  say ""
  say "Which container flavour?"
  say ""
  say "  ${c_grn}1)${c_rst} slim       regex detector only ${c_dim}(no LLM creds needed)${c_rst}"
  say "  ${c_grn}2)${c_rst} pf         privacy-filter, runtime download ${c_dim}(uses named volume)${c_rst}"
  say "  ${c_grn}3)${c_rst} pf-baked   privacy-filter, model baked in"
  say ""
  read -r -p "Choose [1-3, default 1]: " choice || true
  case "${choice:-1}" in
    1) TYPE="slim" ;;
    2) TYPE="pf" ;;
    3) TYPE="pf-baked" ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

# ── Resolve flavour to image / DETECTOR_MODE / volume ────────────────────────
case "$TYPE" in
  slim)
    IMAGE="$TAG_SLIM"
    DETECTOR_MODE="regex"
    USE_VOLUME=false
    ;;
  pf|privacy-filter)
    TYPE="pf"
    IMAGE="$TAG_PF"
    DETECTOR_MODE="privacy_filter"
    USE_VOLUME=true
    ;;
  pf-baked|privacy-filter-baked)
    TYPE="pf-baked"
    IMAGE="$TAG_PF_BAKED"
    DETECTOR_MODE="privacy_filter"
    USE_VOLUME=false
    ;;
  *)
    err "Unknown type '${TYPE}'. Valid: slim, pf, pf-baked."
    exit 1
    ;;
esac

# ── Image existence check + offer to build ──────────────────────────────────
if ! "$ENGINE" image inspect "$IMAGE" >/dev/null 2>&1; then
  warn "Image \"${IMAGE}\" not found locally."
  read -r -p "Run scripts/build-image.sh -t ${TYPE} now? [Y/n] " reply || true
  reply="${reply:-y}"
  if [[ ! "$reply" =~ ^[Yy]$ ]]; then
    err "Cannot run without an image. Aborting."
    exit 1
  fi
  "$SCRIPT_DIR/build-image.sh" -t "$TYPE"
  # Re-check after build — a declined build prompt inside the build
  # script would leave the image still missing, in which case bail.
  if ! "$ENGINE" image inspect "$IMAGE" >/dev/null 2>&1; then
    err "Build did not produce \"${IMAGE}\". Aborting."
    exit 1
  fi
fi

# ── Volume creation (pf only) ───────────────────────────────────────────────
RUN_VOLUME_ARGS=()
JUST_CREATED_VOLUME=false
if [[ "$USE_VOLUME" == "true" ]]; then
  if ! "$ENGINE" volume inspect "$HF_VOLUME" >/dev/null 2>&1; then
    say "Creating named volume \"${HF_VOLUME}\" for the HuggingFace cache..."
    "$ENGINE" volume create "$HF_VOLUME" >/dev/null
    JUST_CREATED_VOLUME=true
  fi
  RUN_VOLUME_ARGS=(-v "${HF_VOLUME}:/app/.cache/huggingface")
fi

# ── Pre-existing container with the same name? ──────────────────────────────
# We use --rm so a clean exit leaves nothing behind; this catches the
# crashed/Ctrl-Z/-d-but-stopped cases where a stub still hangs around.
if "$ENGINE" container inspect "$NAME" >/dev/null 2>&1; then
  warn "A container named \"${NAME}\" already exists."
  read -r -p "Remove it and proceed? [Y/n] " reply || true
  reply="${reply:-y}"
  if [[ ! "$reply" =~ ^[Yy]$ ]]; then
    err "Aborted."
    exit 1
  fi
  "$ENGINE" rm -f "$NAME" >/dev/null
fi

# ── Print plan + confirm ────────────────────────────────────────────────────
say ""
say "Engine:     ${c_grn}${ENGINE}${c_rst}"
say "Flavour:    ${c_grn}${TYPE}${c_rst}"
say "Image:      ${c_grn}${IMAGE}${c_rst}"
say "Container:  ${c_grn}${NAME}${c_rst}"
say "Port:       ${c_grn}${PORT}:8000${c_rst}"
say "Detectors:  ${c_grn}${DETECTOR_MODE}${c_rst}"
if [[ ${#RUN_VOLUME_ARGS[@]} -gt 0 ]]; then
  say "Volume:     ${c_grn}${HF_VOLUME}${c_rst} → /app/.cache/huggingface"
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  say "Passthrough: ${c_dim}${EXTRA_ARGS[*]}${c_rst}"
fi
say ""

if [[ "$JUST_CREATED_VOLUME" == "true" ]]; then
  warn "First run with this volume — the openai/privacy-filter model"
  warn "(~3 GB) downloads before /health responds. Subsequent runs"
  warn "with \"${HF_VOLUME}\" reuse the cache and start in seconds."
  say ""
fi

read -r -p "Proceed? [Y/n] " reply || true
reply="${reply:-y}"
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
  warn "Aborted."
  exit 0
fi

# ── Run (foreground; --rm cleans up on exit) ────────────────────────────────
say ""
say "${c_dim}Running. Ctrl-C to stop. Smoke test from another shell:${c_rst}"
say "${c_dim}  curl -s http://localhost:${PORT}/health | python -m json.tool${c_rst}"
say ""

exec "$ENGINE" run --rm --name "$NAME" -p "${PORT}:8000" \
  -e "DETECTOR_MODE=${DETECTOR_MODE}" \
  "${RUN_VOLUME_ARGS[@]}" "${EXTRA_ARGS[@]}" \
  "$IMAGE"