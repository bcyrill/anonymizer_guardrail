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
# so the model download only happens once per host.
HF_VOLUME="${HF_VOLUME:-anonymizer-hf-cache}"

PORT="${PORT:-8000}"
CONTAINER_NAME_DEFAULT="anonymizer-guardrail"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-t TYPE] [-p PORT] [-n NAME] [--faker|--no-faker]
                       [--locale LOCALE] [-h] [-- EXTRA_RUN_ARGS...]

Launch the anonymizer-guardrail container. Without -t, prompts interactively.

  -t, --type TYPE   slim | pf | pf-baked
  -p, --port PORT   Host port to publish (default: ${PORT})
  -n, --name NAME   Container name (default: ${CONTAINER_NAME_DEFAULT})
  --faker           Use Faker for realistic surrogates (skips the prompt).
  --no-faker        Set USE_FAKER=false (skips the prompt; surrogates
                    are opaque [TYPE_…] tokens).
  --locale LOCALE   Faker locale (e.g. pt_BR). Skips the locale prompt.
                    Implies --faker.
  -h, --help        Show this help.

Without --faker / --no-faker / --locale flags, the script prompts
interactively to let you toggle Faker on/off and pick a locale.

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
# Faker config: empty string = "ask interactively". CLI flags override.
USE_FAKER_CHOICE=""    # "true" | "false" | ""
LOCALE_CHOICE=""       # locale string or ""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--type)    TYPE="${2:-}"; shift 2 ;;
    -p|--port)    PORT="${2:-}"; shift 2 ;;
    -n|--name)    NAME="${2:-}"; shift 2 ;;
    --faker)      USE_FAKER_CHOICE="true";  shift ;;
    --no-faker)   USE_FAKER_CHOICE="false"; shift ;;
    --locale)     LOCALE_CHOICE="${2:-}";   USE_FAKER_CHOICE="true"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    --)           shift; EXTRA_ARGS=("$@"); break ;;
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

# ── HuggingFace Hub offline toggle (pf only, volume reused) ─────────────────
# When the cache volume already had the model in it, transformers will
# still hit HuggingFace Hub on every start to revalidate the cache —
# that's 5+ seconds of HEAD requests plus a "set HF_TOKEN" warning even
# when the local copy is fine. Offer to short-circuit that for users
# who don't want to refresh the model right now. We only ask in the
# already-existed case; first-run obviously needs network access.
RUN_OFFLINE_ARGS=()
if [[ "$USE_VOLUME" == "true" && "$JUST_CREATED_VOLUME" == "false" ]]; then
  say ""
  say "Volume \"${HF_VOLUME}\" already has the model cached."
  read -r -p "Check HuggingFace Hub for an updated model? [y/N] " reply || true
  reply="${reply:-n}"
  if [[ ! "$reply" =~ ^[Yy]$ ]]; then
    RUN_OFFLINE_ARGS=(-e "HF_HUB_OFFLINE=1")
  fi
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

# ── Faker config (interactive unless flags provided) ────────────────────────
# USE_FAKER controls whether surrogates are realistic (Faker-generated
# names/companies/etc.) or opaque tokens like [PERSON_HEX]. FAKER_LOCALE
# picks the locale Faker uses for those substitutes — only meaningful
# when Faker is enabled.
if [[ -z "$USE_FAKER_CHOICE" ]]; then
  say ""
  read -r -p "Use Faker for realistic surrogates? [Y/n] " reply || true
  reply="${reply:-y}"
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    USE_FAKER_CHOICE="true"
  else
    USE_FAKER_CHOICE="false"
  fi
fi

if [[ "$USE_FAKER_CHOICE" == "true" && -z "$LOCALE_CHOICE" ]]; then
  say ""
  say "Override the Faker locale? Picks a region for generated names,"
  say "addresses, phone formats, etc. Empty = container default (en_US)."
  say ""
  say "  ${c_grn}1)${c_rst} skip — use the container default ${c_dim}(en_US)${c_rst}"
  say "  ${c_grn}2)${c_rst} pt_BR ${c_dim}(Brazilian Portuguese)${c_rst}"
  say "  ${c_grn}3)${c_rst} de_CH ${c_dim}(Switzerland)${c_rst}"
  say "  ${c_grn}4)${c_rst} fr_FR ${c_dim}(French)${c_rst}"
  say "  ${c_grn}5)${c_rst} es_ES ${c_dim}(Spanish)${c_rst}"
  say "  ${c_grn}6)${c_rst} ja_JP ${c_dim}(Japanese)${c_rst}"
  say "  ${c_grn}7)${c_rst} custom ${c_dim}— enter your own (e.g. zh_CN, en_GB, ko_KR, or a comma-separated list)${c_rst}"
  read -r -p "Choose [1-7, default 1]: " loc_choice || true
  case "${loc_choice:-1}" in
    1) LOCALE_CHOICE="" ;;
    2) LOCALE_CHOICE="pt_BR" ;;
    3) LOCALE_CHOICE="de_CH" ;;
    4) LOCALE_CHOICE="fr_FR" ;;
    5) LOCALE_CHOICE="es_ES" ;;
    6) LOCALE_CHOICE="ja_JP" ;;
    7)
      read -r -p "Enter locale (e.g. zh_CN or pt_BR,en_US): " LOCALE_CHOICE || true
      LOCALE_CHOICE="${LOCALE_CHOICE//[[:space:]]/}"
      ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

# Translate the Faker decisions into -e env-var arguments. Empty values
# are deliberately omitted so the container falls back to its compiled-in
# defaults (USE_FAKER=true, FAKER_LOCALE="").
RUN_FAKER_ARGS=()
if [[ "$USE_FAKER_CHOICE" == "false" ]]; then
  RUN_FAKER_ARGS+=(-e "USE_FAKER=false")
fi
if [[ -n "$LOCALE_CHOICE" ]]; then
  RUN_FAKER_ARGS+=(-e "FAKER_LOCALE=${LOCALE_CHOICE}")
fi

# ── Print plan + confirm ────────────────────────────────────────────────────
say ""
say "Engine:     ${c_grn}${ENGINE}${c_rst}"
say "Flavour:    ${c_grn}${TYPE}${c_rst}"
say "Image:      ${c_grn}${IMAGE}${c_rst}"
say "Container:  ${c_grn}${NAME}${c_rst}"
say "Port:       ${c_grn}${PORT}:8000${c_rst}"
say "Detectors:  ${c_grn}${DETECTOR_MODE}${c_rst}"
if [[ "$USE_FAKER_CHOICE" == "false" ]]; then
  say "Faker:      ${c_grn}disabled${c_rst} ${c_dim}(opaque [TYPE_HEX] surrogates)${c_rst}"
else
  if [[ -n "$LOCALE_CHOICE" ]]; then
    say "Faker:      ${c_grn}enabled${c_rst}, locale=${c_grn}${LOCALE_CHOICE}${c_rst}"
  else
    say "Faker:      ${c_grn}enabled${c_rst} ${c_dim}(default locale)${c_rst}"
  fi
fi
if [[ ${#RUN_VOLUME_ARGS[@]} -gt 0 ]]; then
  say "Volume:     ${c_grn}${HF_VOLUME}${c_rst} → /app/.cache/huggingface"
fi
if [[ ${#RUN_OFFLINE_ARGS[@]} -gt 0 ]]; then
  say "HF Hub:     ${c_grn}offline${c_rst} ${c_dim}(reusing cached model, no revalidation)${c_rst}"
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  say "Passthrough: ${c_dim}${EXTRA_ARGS[*]}${c_rst}"
fi
say ""

if [[ "$JUST_CREATED_VOLUME" == "true" ]]; then
  warn "First run with this volume — the openai/privacy-filter model"
  warn "downloads before /health responds. Subsequent runs with"
  warn "\"${HF_VOLUME}\" reuse the cache and start in seconds."
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
say "${c_dim}Running. Ctrl-C to stop. Smoke tests from another shell:${c_rst}"
say "${c_dim}  # Health probe:${c_rst}"
say "${c_dim}  curl -s http://localhost:${PORT}/health | python -m json.tool${c_rst}"
say ""
say "${c_dim}  # Anonymize a request with PII the detectors should catch:${c_rst}"
say "${c_dim}  curl -s -X POST http://localhost:${PORT}/beta/litellm_basic_guardrail_api \\${c_rst}"
say "${c_dim}      -H 'Content-Type: application/json' \\${c_rst}"
say "${c_dim}      -d '{${c_rst}"
say "${c_dim}        \"texts\": [${c_rst}"
say "${c_dim}          \"Hi, my name is Alice Smith and you can reach me at alice.smith@example.com or +1-415-555-0100. I live at 123 Main Street, Springfield. My DOB is 1985-04-15.\"${c_rst}"
say "${c_dim}        ],${c_rst}"
say "${c_dim}        \"input_type\": \"request\",${c_rst}"
say "${c_dim}        \"litellm_call_id\": \"smoke-test\"${c_rst}"
say "${c_dim}      }' | python -m json.tool${c_rst}"
say ""

exec "$ENGINE" run --rm --name "$NAME" -p "${PORT}:8000" \
  -e "DETECTOR_MODE=${DETECTOR_MODE}" \
  "${RUN_FAKER_ARGS[@]}" \
  "${RUN_OFFLINE_ARGS[@]}" \
  "${RUN_VOLUME_ARGS[@]}" "${EXTRA_ARGS[@]}" \
  "$IMAGE"