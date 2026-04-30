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
TAG_FAKE_LLM="${TAG_FAKE_LLM:-fake-llm:latest}"

# Named volume for the runtime-download flavour. Reused across `pf` runs
# so the model download only happens once per host.
HF_VOLUME="${HF_VOLUME:-anonymizer-hf-cache}"

# Shared user-defined network so the guardrail container can reach
# fake-llm by name (`http://fake-llm:4000`). Created on demand. fake-llm
# always joins; guardrail only joins when its LLM backend is fake-llm.
SHARED_NETWORK="${SHARED_NETWORK:-anonymizer-net}"

PORT="${PORT:-}"
CONTAINER_NAME_DEFAULT="anonymizer-guardrail"
CONTAINER_NAME_FAKE_LLM="fake-llm"

# Idempotently create the shared network. podman/docker `network create`
# both fail noisily if the network already exists; we check with
# `network inspect` first so re-runs stay quiet.
ensure_shared_network() {
  if ! "$ENGINE" network inspect "$SHARED_NETWORK" >/dev/null 2>&1; then
    say "Creating shared network \"${SHARED_NETWORK}\"..."
    "$ENGINE" network create "$SHARED_NETWORK" >/dev/null
  fi
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [-t TYPE] [-p PORT] [-n NAME]
                       [-d|--detector-mode MODE]
                       [--llm-backend {fake-llm|custom}]
                       [--llm-api-base URL] [--llm-api-key KEY] [--llm-model MODEL]
                       [-l|--log-level LEVEL]
                       [--faker|--no-faker] [--locale LOCALE]
                       [--rules PATH]
                       [-h] [-- EXTRA_RUN_ARGS...]

Launch the anonymizer-guardrail container. When the chosen
DETECTOR_MODE includes \`llm\` and the LLM backend is set to
\`fake-llm\`, the script also auto-starts the fake-llm companion
container in the background. Without -t, prompts interactively.

  -t, --type TYPE   slim | pf | pf-baked
  -p, --port PORT   Host port to publish for the guardrail (default: 8000).
                    fake-llm, when auto-started, always publishes 4000.
  -n, --name NAME   Container name (default: ${CONTAINER_NAME_DEFAULT}).
  -d, --detector-mode MODE
                    Override DETECTOR_MODE on the guardrail (e.g.
                    \`regex,llm\` or \`regex,privacy_filter,llm\`).
                    Skips the interactive prompt.
  --llm-backend B   When DETECTOR_MODE includes \`llm\`, which backend to
                    talk to: \`fake-llm\` (joins ${SHARED_NETWORK} and
                    points at http://fake-llm:4000/v1) or \`custom\`
                    (uses the --llm-api-base/key/model values, or
                    prompts for them).
  --llm-api-base URL
                    Custom LLM API base URL. Implies --llm-backend custom.
  --llm-api-key KEY
                    Custom LLM API key (sent as Bearer token). Implies
                    --llm-backend custom.
  --llm-model MODEL
                    Model alias the detector requests. Implies
                    --llm-backend custom.
  -l, --log-level LEVEL
                    Container LOG_LEVEL (debug|info|warning|error).
                    Skips the interactive prompt.
  --faker           Use Faker for realistic surrogates (skips the prompt).
  --no-faker        Set USE_FAKER=false (surrogates are opaque tokens).
  --locale LOCALE   Faker locale (e.g. pt_BR). Implies --faker.
  --rules PATH      When the auto-started fake-llm is used, mount this
                    YAML file at /app/rules.yaml instead of the bundled
                    rules.example.yaml.
  -h, --help        Show this help.

Without explicit flags, the script prompts interactively for the
detector mode, the LLM backend (when DETECTOR_MODE includes \`llm\`),
the log level, and Faker on/off + locale.

Anything after \`--\` is passed straight through to \`${ENGINE} run\`,
e.g. extra -e env vars or -v volume mounts. Don't pass --network or
LLM_API_BASE through that channel — the script manages those when
--llm-backend fake-llm is selected.

Per-flavour behaviour:
  slim      image "${TAG_SLIM}". Without privacy-filter; no volume.
  pf        image "${TAG_PF}", with a named volume "${HF_VOLUME}"
            mounted on /app/.cache/huggingface so the model survives
            recreation.
  pf-baked  image "${TAG_PF_BAKED}". Self-contained — no volume needed.

When the chosen DETECTOR_MODE includes \`llm\`, the script asks which
LLM backend to use:
  fake-llm  image "${TAG_FAKE_LLM}". Companion test backend, started
            in the background on shared network "${SHARED_NETWORK}".
            The guardrail reaches it as http://fake-llm:4000/v1.
            See fake-llm/README.md for the rules-file schema.
  custom    Your own OpenAI-compatible endpoint (LiteLLM, Ollama, …).
            The script prompts for LLM_API_BASE / LLM_API_KEY /
            LLM_MODEL, or pass them via the matching CLI flags.

If a needed image isn't found locally, the script offers to invoke
scripts/build-image.sh with the matching -t flag.
EOF
}

TYPE=""
NAME=""
EXTRA_ARGS=()
# Each "_CHOICE" / "_OVERRIDE" defaults to empty = "ask interactively".
# CLI flags fill them in to skip the matching prompt.
USE_FAKER_CHOICE=""        # "true" | "false" | ""
LOCALE_CHOICE=""           # locale string or ""
DETECTOR_MODE_OVERRIDE=""  # comma-separated detectors, or ""
RULES_FILE=""              # fake-llm only: host path to a custom rules.yaml
LLM_BACKEND=""             # "fake-llm" | "custom" | ""
LLM_API_BASE_OVERRIDE=""
LLM_API_KEY_OVERRIDE=""
LLM_MODEL_OVERRIDE=""
LOG_LEVEL_CHOICE=""        # "debug" | "info" | "warning" | "error" | ""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--type)             TYPE="${2:-}"; shift 2 ;;
    -p|--port)             PORT="${2:-}"; shift 2 ;;
    -n|--name)             NAME="${2:-}"; shift 2 ;;
    -d|--detector-mode)    DETECTOR_MODE_OVERRIDE="${2:-}"; shift 2 ;;
    --llm-backend)         LLM_BACKEND="${2:-}"; shift 2 ;;
    --llm-api-base)        LLM_API_BASE_OVERRIDE="${2:-}"; LLM_BACKEND="custom"; shift 2 ;;
    --llm-api-key)         LLM_API_KEY_OVERRIDE="${2:-}"; LLM_BACKEND="custom"; shift 2 ;;
    --llm-model)           LLM_MODEL_OVERRIDE="${2:-}"; LLM_BACKEND="custom"; shift 2 ;;
    -l|--log-level)        LOG_LEVEL_CHOICE="${2:-}"; shift 2 ;;
    --faker)               USE_FAKER_CHOICE="true";  shift ;;
    --no-faker)            USE_FAKER_CHOICE="false"; shift ;;
    --locale)              LOCALE_CHOICE="${2:-}";   USE_FAKER_CHOICE="true"; shift 2 ;;
    --rules)               RULES_FILE="${2:-}"; shift 2 ;;
    -h|--help)             usage; exit 0 ;;
    --)                    shift; EXTRA_ARGS=("$@"); break ;;
    *) err "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ── Interactive menu ─────────────────────────────────────────────────────────
if [[ -z "$TYPE" ]]; then
  say ""
  say "Which container flavour?"
  say ""
  say "  ${c_grn}1)${c_rst} slim       ${c_dim}without privacy-filter${c_rst}"
  say "  ${c_grn}2)${c_rst} pf         privacy-filter, runtime download ${c_dim}(uses named volume)${c_rst}"
  say "  ${c_grn}3)${c_rst} pf-baked   privacy-filter, model baked in"
  say ""
  say "${c_dim}(The fake-llm test backend is auto-started when you pick an"
  say " LLM-using detector mode and choose fake-llm as the backend.)${c_rst}"
  say ""
  read -r -p "Choose [1-3, default 1]: " choice || true
  case "${choice:-1}" in
    1) TYPE="slim" ;;
    2) TYPE="pf" ;;
    3) TYPE="pf-baked" ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

# ── Resolve flavour to image / volume ───────────────────────────────────────
case "$TYPE" in
  slim)
    IMAGE="$TAG_SLIM"
    USE_VOLUME=false
    ;;
  pf|privacy-filter)
    TYPE="pf"
    IMAGE="$TAG_PF"
    USE_VOLUME=true
    ;;
  pf-baked|privacy-filter-baked)
    TYPE="pf-baked"
    IMAGE="$TAG_PF_BAKED"
    USE_VOLUME=false
    ;;
  *)
    err "Unknown type '${TYPE}'. Valid: slim, pf, pf-baked."
    exit 1
    ;;
esac

NAME="${NAME:-$CONTAINER_NAME_DEFAULT}"
PORT="${PORT:-8000}"

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

# ── Detector mode (interactive unless -d/--detector-mode given) ─────────────
# DETECTOR_MODE is the comma-separated list of detection layers. Each
# flavour defaults to a sensible mode (slim → regex, pf/pf-baked →
# privacy_filter), but we let the operator pick anything since the
# image only constrains *availability*: an image without torch can't
# actually run privacy_filter, but DETECTOR_MODE itself is a runtime
# string. Custom is always available for unusual combinations.
DETECTOR_MODE=""
if [[ -n "$DETECTOR_MODE_OVERRIDE" ]]; then
  DETECTOR_MODE="$DETECTOR_MODE_OVERRIDE"
else
  say ""
  say "DETECTOR_MODE? Comma-separated, in priority order (first wins on overlaps):"
  say ""
  say "  ${c_grn}1)${c_rst} regex"
  say "  ${c_grn}2)${c_rst} regex,llm"
  if [[ "$TYPE" == "pf" || "$TYPE" == "pf-baked" ]]; then
    say "  ${c_grn}3)${c_rst} privacy_filter,llm"
    say "  ${c_grn}4)${c_rst} regex,privacy_filter,llm"
  fi
  say "  ${c_grn}c)${c_rst} custom ${c_dim}— enter your own list${c_rst}"
  read -r -p "Choose [default 1]: " dm_choice || true
  case "${dm_choice:-1}" in
    1) DETECTOR_MODE="regex" ;;
    2) DETECTOR_MODE="regex,llm" ;;
    3)
      if [[ "$TYPE" == "pf" || "$TYPE" == "pf-baked" ]]; then
        DETECTOR_MODE="privacy_filter,llm"
      else
        err "Option 3 requires a pf/pf-baked image (privacy_filter not in slim)."
        exit 1
      fi
      ;;
    4)
      if [[ "$TYPE" == "pf" || "$TYPE" == "pf-baked" ]]; then
        DETECTOR_MODE="regex,privacy_filter,llm"
      else
        err "Option 4 requires a pf/pf-baked image (privacy_filter not in slim)."
        exit 1
      fi
      ;;
    c|C)
      read -r -p "Enter DETECTOR_MODE (e.g. regex,llm): " DETECTOR_MODE || true
      DETECTOR_MODE="${DETECTOR_MODE//[[:space:]]/}"
      if [[ -z "$DETECTOR_MODE" ]]; then
        err "Empty DETECTOR_MODE."
        exit 1
      fi
      ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

# Quick predicate used by later blocks: does DETECTOR_MODE include
# the LLM detector? Comma-wrapping keeps `llm` from matching `xllm`,
# `llm2`, etc.
DETECTOR_HAS_LLM=false
if [[ ",${DETECTOR_MODE}," == *",llm,"* ]]; then
  DETECTOR_HAS_LLM=true
fi

# ── LLM backend selection (only when DETECTOR_MODE includes llm) ────────────
# Two backends:
#   fake-llm — auto-started here on the shared network so the guardrail
#              can reach it as http://fake-llm:4000/v1. Image must
#              already be built (fake-llm:latest); we offer to invoke
#              build-image.sh -t fake-llm if missing.
#   custom   — the operator's own OpenAI-compatible endpoint. We prompt
#              for API base, key, and model alias unless they were
#              passed on the command line.
RUN_LLM_ARGS=()
RUN_NETWORK_ARGS=()
if [[ "$DETECTOR_HAS_LLM" == "true" ]]; then
  if [[ -z "$LLM_BACKEND" ]]; then
    say ""
    say "LLM detector backend?"
    say ""
    say "  ${c_grn}1)${c_rst} fake-llm   ${c_dim}auto-started, deterministic responses driven by rules.yaml${c_rst}"
    say "  ${c_grn}2)${c_rst} custom     ${c_dim}your own OpenAI-compatible endpoint (LiteLLM, Ollama, …)${c_rst}"
    read -r -p "Choose [1-2, default 1]: " backend_choice || true
    case "${backend_choice:-1}" in
      1) LLM_BACKEND="fake-llm" ;;
      2) LLM_BACKEND="custom" ;;
      *) err "Invalid choice."; exit 1 ;;
    esac
  fi

  case "$LLM_BACKEND" in
    fake-llm)
      RUN_NETWORK_ARGS=(--network "$SHARED_NETWORK")
      RUN_LLM_ARGS=(
        -e "LLM_API_BASE=http://${CONTAINER_NAME_FAKE_LLM}:4000/v1"
        -e "LLM_API_KEY=any-non-empty"
        -e "LLM_MODEL=fake"
      )
      ;;
    custom)
      if [[ -z "$LLM_API_BASE_OVERRIDE" ]]; then
        say ""
        read -r -p "LLM_API_BASE [http://litellm:4000/v1]: " LLM_API_BASE_OVERRIDE || true
        LLM_API_BASE_OVERRIDE="${LLM_API_BASE_OVERRIDE:-http://litellm:4000/v1}"
      fi
      if [[ -z "$LLM_API_KEY_OVERRIDE" ]]; then
        read -r -p "LLM_API_KEY (leave empty for unauthenticated dev backends): " LLM_API_KEY_OVERRIDE || true
      fi
      if [[ -z "$LLM_MODEL_OVERRIDE" ]]; then
        read -r -p "LLM_MODEL [anonymize]: " LLM_MODEL_OVERRIDE || true
        LLM_MODEL_OVERRIDE="${LLM_MODEL_OVERRIDE:-anonymize}"
      fi
      RUN_LLM_ARGS=(-e "LLM_API_BASE=${LLM_API_BASE_OVERRIDE}")
      [[ -n "$LLM_API_KEY_OVERRIDE" ]] && RUN_LLM_ARGS+=(-e "LLM_API_KEY=${LLM_API_KEY_OVERRIDE}")
      RUN_LLM_ARGS+=(-e "LLM_MODEL=${LLM_MODEL_OVERRIDE}")
      ;;
    *)
      err "Invalid --llm-backend '${LLM_BACKEND}'. Use 'fake-llm' or 'custom'."
      exit 1
      ;;
  esac
fi

# ── Log level (interactive unless -l/--log-level given) ─────────────────────
# The container honours LOG_LEVEL for its own logger. Also propagated
# to the auto-started fake-llm so its rule-match traces line up.
if [[ -z "$LOG_LEVEL_CHOICE" ]]; then
  say ""
  say "Container LOG_LEVEL?"
  say ""
  say "  ${c_grn}1)${c_rst} info     ${c_dim}(default — request line per call)${c_rst}"
  say "  ${c_grn}2)${c_rst} debug    ${c_dim}(per-detector matches, parsed entities, dedup output)${c_rst}"
  say "  ${c_grn}3)${c_rst} warning  ${c_dim}(only warnings and errors)${c_rst}"
  say "  ${c_grn}4)${c_rst} error    ${c_dim}(errors only)${c_rst}"
  read -r -p "Choose [1-4, default 1]: " ll_choice || true
  case "${ll_choice:-1}" in
    1) LOG_LEVEL_CHOICE="info" ;;
    2) LOG_LEVEL_CHOICE="debug" ;;
    3) LOG_LEVEL_CHOICE="warning" ;;
    4) LOG_LEVEL_CHOICE="error" ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi
RUN_LOG_LEVEL_ARGS=(-e "LOG_LEVEL=${LOG_LEVEL_CHOICE}")

# ── Faker config (interactive unless flags provided) ────────────────────────
# USE_FAKER controls whether surrogates are realistic (Faker-generated
# names/companies/etc.) or opaque tokens like [PERSON_HEX]. FAKER_LOCALE
# picks the locale Faker uses for those substitutes — only meaningful
# when Faker is enabled.
RUN_FAKER_ARGS=()
if [[ -z "$USE_FAKER_CHOICE" ]]; then
  say ""
  read -r -p "Use Faker for realistic surrogates? [y/N] " reply || true
  reply="${reply:-n}"
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
if [[ "$USE_FAKER_CHOICE" == "false" ]]; then
  RUN_FAKER_ARGS+=(-e "USE_FAKER=false")
fi
if [[ -n "$LOCALE_CHOICE" ]]; then
  RUN_FAKER_ARGS+=(-e "FAKER_LOCALE=${LOCALE_CHOICE}")
fi

# ── Auto-start fake-llm (LLM backend = fake-llm only) ───────────────────────
# Run as a detached sibling on the shared network. We try not to be
# noisy: if a fake-llm container is already running, leave it alone
# (the operator may have started it themselves with custom rules) and
# don't tear it down on exit either. When WE start fake-llm fresh, we
# stop it via the EXIT trap so Ctrl-C of the guardrail also cleans up
# the test backend — pairing is one-shot and operators rarely want a
# stale fake-llm around after the foreground guardrail goes away.
GUARDRAIL_STARTED_FAKE_LLM=false

start_fake_llm() {
  ensure_shared_network

  if "$ENGINE" container inspect "$CONTAINER_NAME_FAKE_LLM" >/dev/null 2>&1; then
    state="$("$ENGINE" container inspect -f '{{.State.Status}}' "$CONTAINER_NAME_FAKE_LLM" 2>/dev/null || echo unknown)"
    if [[ "$state" == "running" ]]; then
      ok "fake-llm container already running — reusing it (will NOT stop on exit)."
      return 0
    fi
    warn "fake-llm container exists but is in state \"${state}\" — removing."
    "$ENGINE" rm -f "$CONTAINER_NAME_FAKE_LLM" >/dev/null
  fi

  if ! "$ENGINE" image inspect "$TAG_FAKE_LLM" >/dev/null 2>&1; then
    warn "fake-llm image \"${TAG_FAKE_LLM}\" not found locally."
    read -r -p "Run scripts/build-image.sh -t fake-llm now? [Y/n] " reply || true
    reply="${reply:-y}"
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
      err "Cannot start fake-llm without an image. Aborting."
      exit 1
    fi
    "$SCRIPT_DIR/build-image.sh" -t fake-llm
    if ! "$ENGINE" image inspect "$TAG_FAKE_LLM" >/dev/null 2>&1; then
      err "Build did not produce \"${TAG_FAKE_LLM}\". Aborting."
      exit 1
    fi
  fi

  # Optional rules-file mount, resolved against the operator's invocation
  # cwd (not the script's repo-root cwd) so relative paths work.
  local fake_rules_args=()
  if [[ -n "$RULES_FILE" ]]; then
    if [[ ! -f "$RULES_FILE" ]]; then
      err "Rules file not found: ${RULES_FILE}"
      exit 1
    fi
    local abs="$(cd "$(dirname "$RULES_FILE")" && pwd)/$(basename "$RULES_FILE")"
    fake_rules_args=(-v "${abs}:/app/rules.yaml:ro")
  fi

  say "Starting fake-llm on network \"${SHARED_NETWORK}\" (port 4000)..."
  "$ENGINE" run -d --rm \
    --name "$CONTAINER_NAME_FAKE_LLM" \
    --network "$SHARED_NETWORK" \
    -p 4000:4000 \
    -e "LOG_LEVEL=${LOG_LEVEL_CHOICE}" \
    "${fake_rules_args[@]}" \
    "$TAG_FAKE_LLM" >/dev/null

  GUARDRAIL_STARTED_FAKE_LLM=true

  # Wait for /health before starting the guardrail. Without this the
  # guardrail's first LLM call may race past fake-llm's bind() and hit
  # connection-refused, which routes through FAIL_CLOSED and looks like
  # a real backend outage.
  for _ in $(seq 1 30); do
    if "$ENGINE" exec "$CONTAINER_NAME_FAKE_LLM" \
         python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:4000/health',timeout=1)" \
         >/dev/null 2>&1; then
      ok "fake-llm is ready."
      return 0
    fi
    sleep 0.5
  done
  err "fake-llm did not become ready within 15s. Check logs: ${ENGINE} logs ${CONTAINER_NAME_FAKE_LLM}"
  exit 1
}

# EXIT trap fires on every termination path (clean exit, set -e abort,
# Ctrl-C, SIGTERM). bash's default INT/TERM behaviour during foreground
# `wait` is to deliver to the child first, then run traps once the child
# returns — so we don't need separate INT/TERM handlers. The trap is a
# no-op when GUARDRAIL_STARTED_FAKE_LLM stayed false (operator's own
# fake-llm, or LLM backend wasn't fake-llm).
cleanup() {
  if [[ "$GUARDRAIL_STARTED_FAKE_LLM" == "true" ]]; then
    say ""
    say "Stopping auto-started fake-llm..."
    # `--rm` on the run means the container vanishes once stopped, so
    # there's nothing left over for next time. Swallow errors — if the
    # container died on its own we don't want to obscure the real exit.
    "$ENGINE" stop "$CONTAINER_NAME_FAKE_LLM" >/dev/null 2>&1 || true
  fi
}

if [[ "$LLM_BACKEND" == "fake-llm" ]]; then
  start_fake_llm
  trap cleanup EXIT
fi

# ── Print plan + confirm ────────────────────────────────────────────────────
say ""
say "Engine:     ${c_grn}${ENGINE}${c_rst}"
say "Flavour:    ${c_grn}${TYPE}${c_rst}"
say "Image:      ${c_grn}${IMAGE}${c_rst}"
say "Container:  ${c_grn}${NAME}${c_rst}"
say "Port:       ${c_grn}${PORT}:8000${c_rst}"
say "Detectors:  ${c_grn}${DETECTOR_MODE}${c_rst}"
say "Log level:  ${c_grn}${LOG_LEVEL_CHOICE}${c_rst}"
if [[ "$DETECTOR_HAS_LLM" == "true" ]]; then
  case "$LLM_BACKEND" in
    fake-llm)
      say "LLM:        ${c_grn}fake-llm${c_rst} ${c_dim}(auto-started on ${SHARED_NETWORK})${c_rst}"
      ;;
    custom)
      say "LLM:        ${c_grn}custom${c_rst} ${c_dim}base=${LLM_API_BASE_OVERRIDE} model=${LLM_MODEL_OVERRIDE}${c_rst}"
      ;;
  esac
fi
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
if [[ "$LLM_BACKEND" == "fake-llm" && "$GUARDRAIL_STARTED_FAKE_LLM" == "true" ]]; then
  say ""
  say "${c_dim}  # fake-llm will be stopped automatically when you exit (Ctrl-C).${c_rst}"
fi
say ""

# Run the guardrail as a child process (NOT exec) so the EXIT trap
# fires after it terminates and cleans up the auto-started fake-llm.
# Bash forwards Ctrl-C to the child first; once podman exits we hit
# the trap. `|| status=$?` opts out of `set -e`, then we re-exit with
# the same status so callers (and `$?`) see what podman actually
# returned (130 on Ctrl-C, etc.).
status=0
"$ENGINE" run --rm --name "$NAME" -p "${PORT}:8000" \
  "${RUN_NETWORK_ARGS[@]}" \
  -e "DETECTOR_MODE=${DETECTOR_MODE}" \
  "${RUN_LOG_LEVEL_ARGS[@]}" \
  "${RUN_LLM_ARGS[@]}" \
  "${RUN_FAKER_ARGS[@]}" \
  "${RUN_OFFLINE_ARGS[@]}" \
  "${RUN_VOLUME_ARGS[@]}" "${EXTRA_ARGS[@]}" \
  "$IMAGE" || status=$?
exit "$status"