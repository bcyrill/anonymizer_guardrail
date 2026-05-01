# shellcheck shell=bash
# Shared helpers for the launcher scripts.
#
# Sourced by `scripts/cli.sh` (flag-driven) and `scripts/menu.sh`
# (whiptail UI). Pure functions and side-effect helpers — no top-level
# work, no prompts, no `set -e`. Callers decide error handling.
#
# Convention: globals that the caller is expected to populate before
# calling `run_guardrail`:
#   ENGINE                       — auto-detected; override via env
#   TYPE                         — slim | pf | pf-baked
#   IMAGE                        — set by resolve_flavour from TYPE
#   USE_VOLUME                   — set by resolve_flavour
#   NAME                         — guardrail container name
#   PORT                         — host port to publish
#   DETECTOR_MODE                — comma-separated list
#   REGEX_OVERLAP_CHOICE         — longest | priority
#   REGEX_PATTERNS_PATH          — empty | bundled:NAME | filesystem path
#   LLM_SYSTEM_PROMPT_PATH       — empty | bundled:NAME | filesystem path
#   LLM_BACKEND                  — fake-llm | custom (only if has llm)
#   LLM_API_BASE_OVERRIDE        — only for custom backend
#   LLM_API_KEY_OVERRIDE
#   LLM_MODEL_OVERRIDE
#   LLM_USE_FORWARDED_KEY        — true | false (custom backend only)
#   LOG_LEVEL_CHOICE             — debug | info | warning | error
#   USE_FAKER_CHOICE             — true | false
#   LOCALE_CHOICE                — empty or locale string
#   HF_OFFLINE                   — true | false (pf only)
#   RULES_FILE                   — host path or empty (fake-llm only)
#   FAIL_CLOSED                  — true | false (default true)
#   SURROGATE_SALT               — empty (random per restart) or string
#   EXTRA_ARGS                   — array of extra args for `podman run`

# ── Color helpers ────────────────────────────────────────────────────────────
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
say()  { printf '%s\n' "$*"; }
warn() { printf '%s%s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_rst" >&2; }
ok()   { printf '%s%s%s\n' "$c_grn" "$*" "$c_rst"; }

# ── Defaults the callers can override via env ────────────────────────────────
TAG_SLIM="${TAG_SLIM:-anonymizer-guardrail:latest}"
TAG_PF="${TAG_PF:-anonymizer-guardrail:privacy-filter}"
TAG_PF_BAKED="${TAG_PF_BAKED:-anonymizer-guardrail:privacy-filter-baked}"
TAG_FAKE_LLM="${TAG_FAKE_LLM:-fake-llm:latest}"

HF_VOLUME="${HF_VOLUME:-anonymizer-hf-cache}"
SHARED_NETWORK="${SHARED_NETWORK:-anonymizer-net}"

CONTAINER_NAME_DEFAULT="anonymizer-guardrail"
CONTAINER_NAME_FAKE_LLM="fake-llm"

# ── Engine detection ─────────────────────────────────────────────────────────
detect_engine() {
  if [[ -n "${ENGINE:-}" ]]; then return 0; fi
  if command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  else
    err "Neither podman nor docker found in PATH. Install one or set ENGINE=…"
    exit 1
  fi
}

# ── State predicates (no side effects) ───────────────────────────────────────
image_exists()       { "$ENGINE" image inspect "$1" >/dev/null 2>&1; }
volume_exists()      { "$ENGINE" volume inspect "$1" >/dev/null 2>&1; }
container_exists()   { "$ENGINE" container inspect "$1" >/dev/null 2>&1; }
network_exists()     { "$ENGINE" network inspect "$1" >/dev/null 2>&1; }
container_running() {
  local s
  s=$("$ENGINE" container inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo unknown)
  [[ "$s" == "running" ]]
}

# ── Side-effect helpers ──────────────────────────────────────────────────────
ensure_shared_network() {
  if ! network_exists "$SHARED_NETWORK"; then
    say "Creating shared network \"${SHARED_NETWORK}\"..."
    "$ENGINE" network create "$SHARED_NETWORK" >/dev/null
  fi
}

# Create the HF cache volume if missing. Sets JUST_CREATED_VOLUME so the
# caller can decide whether to offer the operator the offline mode.
ensure_hf_volume() {
  if volume_exists "$HF_VOLUME"; then
    JUST_CREATED_VOLUME=false
  else
    say "Creating named volume \"${HF_VOLUME}\" for the HuggingFace cache..."
    "$ENGINE" volume create "$HF_VOLUME" >/dev/null
    JUST_CREATED_VOLUME=true
  fi
}

# Map TYPE → IMAGE / USE_VOLUME. Single source of truth so the two
# launchers never disagree on flavour semantics.
resolve_flavour() {
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
      err "Unknown flavour '${TYPE}'. Valid: slim, pf, pf-baked."
      exit 1
      ;;
  esac
}

# DETECTOR_MODE-derived predicates. Comma-wrapping keeps `llm` from
# matching `xllm`, `llm2`, etc.
resolve_predicates() {
  DETECTOR_HAS_LLM=false
  DETECTOR_HAS_REGEX=false
  [[ ",${DETECTOR_MODE}," == *",llm,"* ]]   && DETECTOR_HAS_LLM=true
  [[ ",${DETECTOR_MODE}," == *",regex,"* ]] && DETECTOR_HAS_REGEX=true
  return 0
}

# Resolve a possibly-relative path to an absolute one against the
# operator's invocation cwd (NOT the script's cwd).
abs_path() {
  printf '%s/%s\n' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")"
}

# ── fake-llm lifecycle ───────────────────────────────────────────────────────
# Set to true by start_fake_llm when WE created the container; cleanup
# trap reads it so we don't tear down a fake-llm the operator started
# themselves.
GUARDRAIL_STARTED_FAKE_LLM=false

start_fake_llm() {
  ensure_shared_network

  if container_exists "$CONTAINER_NAME_FAKE_LLM"; then
    if container_running "$CONTAINER_NAME_FAKE_LLM"; then
      ok "fake-llm container already running — reusing it (will NOT stop on exit)."
      return 0
    fi
    warn "fake-llm container exists but isn't running — removing."
    "$ENGINE" rm -f "$CONTAINER_NAME_FAKE_LLM" >/dev/null
  fi

  if ! image_exists "$TAG_FAKE_LLM"; then
    err "fake-llm image \"${TAG_FAKE_LLM}\" not found."
    err "Build it with:  scripts/build-image.sh -t fake-llm"
    exit 1
  fi

  local fake_rules_args=()
  if [[ -n "${RULES_FILE:-}" ]]; then
    if [[ ! -f "$RULES_FILE" ]]; then
      err "Rules file not found: ${RULES_FILE}"
      exit 1
    fi
    fake_rules_args=(-v "$(abs_path "$RULES_FILE"):/app/rules.yaml:ro")
  fi

  say "Starting fake-llm on network \"${SHARED_NETWORK}\" (port 4000)..."
  "$ENGINE" run -d --rm \
    --name "$CONTAINER_NAME_FAKE_LLM" \
    --network "$SHARED_NETWORK" \
    -p 4000:4000 \
    -e "LOG_LEVEL=${LOG_LEVEL_CHOICE:-info}" \
    "${fake_rules_args[@]}" \
    "$TAG_FAKE_LLM" >/dev/null

  GUARDRAIL_STARTED_FAKE_LLM=true

  # Probe /health from inside the container to avoid host-vs-network
  # routing edge cases. ~15s upper bound.
  local i
  for i in $(seq 1 30); do
    if "$ENGINE" exec "$CONTAINER_NAME_FAKE_LLM" \
         python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health', timeout=1)" \
         >/dev/null 2>&1; then
      ok "fake-llm is ready."
      return 0
    fi
    sleep 0.5
  done
  err "fake-llm did not become ready within 15s. Check: ${ENGINE} logs ${CONTAINER_NAME_FAKE_LLM}"
  exit 1
}

# EXIT-trap target. No-op when we didn't start fake-llm ourselves.
cleanup_fake_llm() {
  if [[ "${GUARDRAIL_STARTED_FAKE_LLM:-false}" == "true" ]]; then
    say ""
    say "Stopping auto-started fake-llm..."
    "$ENGINE" stop "$CONTAINER_NAME_FAKE_LLM" >/dev/null 2>&1 || true
  fi
}

# ── Run-arg composition ──────────────────────────────────────────────────────
# Builds the global `*_ARGS` arrays consumed by `run_guardrail`. Reads
# every config global; idempotent — safe to call multiple times.
build_run_args() {
  RUN_VOLUME_ARGS=()
  if [[ "$USE_VOLUME" == "true" ]]; then
    RUN_VOLUME_ARGS=(-v "${HF_VOLUME}:/app/.cache/huggingface")
  fi

  RUN_OFFLINE_ARGS=()
  if [[ "${HF_OFFLINE:-false}" == "true" ]]; then
    RUN_OFFLINE_ARGS=(-e "HF_HUB_OFFLINE=1")
  fi

  RUN_LLM_ARGS=()
  RUN_NETWORK_ARGS=()
  if [[ "${DETECTOR_HAS_LLM:-false}" == "true" ]]; then
    case "${LLM_BACKEND:-}" in
      fake-llm)
        RUN_NETWORK_ARGS=(--network "$SHARED_NETWORK")
        RUN_LLM_ARGS=(
          -e "LLM_API_BASE=http://${CONTAINER_NAME_FAKE_LLM}:4000/v1"
          -e "LLM_API_KEY=any-non-empty"
          -e "LLM_MODEL=fake"
        )
        ;;
      custom)
        if [[ -z "${LLM_API_BASE_OVERRIDE:-}" ]]; then
          err "LLM_BACKEND=custom requires LLM_API_BASE."
          exit 1
        fi
        RUN_LLM_ARGS=(-e "LLM_API_BASE=${LLM_API_BASE_OVERRIDE}")
        [[ -n "${LLM_API_KEY_OVERRIDE:-}" ]] && \
          RUN_LLM_ARGS+=(-e "LLM_API_KEY=${LLM_API_KEY_OVERRIDE}")
        RUN_LLM_ARGS+=(-e "LLM_MODEL=${LLM_MODEL_OVERRIDE:-anonymize}")
        ;;
      *)
        err "Invalid LLM_BACKEND='${LLM_BACKEND:-}' (need fake-llm or custom)."
        exit 1
        ;;
    esac
  fi

  RUN_FAKER_ARGS=()
  if [[ "${USE_FAKER_CHOICE:-false}" == "false" ]]; then
    RUN_FAKER_ARGS+=(-e "USE_FAKER=false")
  fi
  if [[ -n "${LOCALE_CHOICE:-}" ]]; then
    RUN_FAKER_ARGS+=(-e "FAKER_LOCALE=${LOCALE_CHOICE}")
  fi

  REGEX_OVERLAP_ARGS=()
  if [[ "${DETECTOR_HAS_REGEX:-false}" == "true" ]]; then
    REGEX_OVERLAP_ARGS=(-e "REGEX_OVERLAP_STRATEGY=${REGEX_OVERLAP_CHOICE:-longest}")
  fi

  # Pattern / prompt overrides — only emit -e when set, so the
  # container falls through to its bundled defaults otherwise.
  RUN_REGEX_PATTERNS_ARGS=()
  if [[ -n "${REGEX_PATTERNS_PATH:-}" ]]; then
    RUN_REGEX_PATTERNS_ARGS=(-e "REGEX_PATTERNS_PATH=${REGEX_PATTERNS_PATH}")
  fi
  RUN_LLM_PROMPT_ARGS=()
  if [[ -n "${LLM_SYSTEM_PROMPT_PATH:-}" ]]; then
    RUN_LLM_PROMPT_ARGS=(-e "LLM_SYSTEM_PROMPT_PATH=${LLM_SYSTEM_PROMPT_PATH}")
  fi

  # Forwarded-key only flips the container default when explicitly on.
  RUN_FORWARDED_KEY_ARGS=()
  if [[ "${LLM_USE_FORWARDED_KEY:-false}" == "true" ]]; then
    RUN_FORWARDED_KEY_ARGS=(-e "LLM_USE_FORWARDED_KEY=true")
  fi

  # FAIL_CLOSED defaults to true in the container; only set when the
  # operator chose fail-open. Keeps the env clean at default settings.
  RUN_FAIL_CLOSED_ARGS=()
  if [[ "${FAIL_CLOSED:-true}" == "false" ]]; then
    RUN_FAIL_CLOSED_ARGS=(-e "FAIL_CLOSED=false")
  fi

  RUN_SALT_ARGS=()
  if [[ -n "${SURROGATE_SALT:-}" ]]; then
    RUN_SALT_ARGS=(-e "SURROGATE_SALT=${SURROGATE_SALT}")
  fi

  RUN_LOG_LEVEL_ARGS=(-e "LOG_LEVEL=${LOG_LEVEL_CHOICE:-info}")
}

# ── Plan + run ───────────────────────────────────────────────────────────────
print_plan() {
  # Compute run-arg arrays first so the conditional `Volume:` /
  # `HF Hub:` / etc. lines below can read array sizes without
  # tripping `set -u` on unbound variables. build_run_args is
  # idempotent — the same call from run_guardrail just re-sets the
  # same values.
  build_run_args
  say ""
  say "Engine:     ${c_grn}${ENGINE}${c_rst}"
  say "Flavour:    ${c_grn}${TYPE}${c_rst}"
  say "Image:      ${c_grn}${IMAGE}${c_rst}"
  say "Container:  ${c_grn}${NAME}${c_rst}"
  say "Port:       ${c_grn}${PORT}:8000${c_rst}"
  say "Detectors:  ${c_grn}${DETECTOR_MODE}${c_rst}"
  if [[ "${DETECTOR_HAS_REGEX:-false}" == "true" ]]; then
    say "Regex strat: ${c_grn}${REGEX_OVERLAP_CHOICE:-longest}${c_rst}"
  fi
  say "Log level:  ${c_grn}${LOG_LEVEL_CHOICE:-info}${c_rst}"
  if [[ "${DETECTOR_HAS_LLM:-false}" == "true" ]]; then
    case "${LLM_BACKEND:-}" in
      fake-llm)
        say "LLM:        ${c_grn}fake-llm${c_rst} ${c_dim}(auto-started on ${SHARED_NETWORK})${c_rst}"
        ;;
      custom)
        say "LLM:        ${c_grn}custom${c_rst} ${c_dim}base=${LLM_API_BASE_OVERRIDE} model=${LLM_MODEL_OVERRIDE:-anonymize}${c_rst}"
        ;;
    esac
  fi
  if [[ -n "${REGEX_PATTERNS_PATH:-}" ]]; then
    say "Regex YAML:  ${c_grn}${REGEX_PATTERNS_PATH}${c_rst}"
  fi
  if [[ -n "${LLM_SYSTEM_PROMPT_PATH:-}" ]]; then
    say "LLM prompt:  ${c_grn}${LLM_SYSTEM_PROMPT_PATH}${c_rst}"
  fi
  if [[ "${LLM_USE_FORWARDED_KEY:-false}" == "true" ]]; then
    say "Forward key: ${c_grn}yes${c_rst} ${c_dim}(send caller's Authorization header to detection LLM)${c_rst}"
  fi
  if [[ "${FAIL_CLOSED:-true}" == "false" ]]; then
    say "Fail mode:   ${c_ylw}open${c_rst} ${c_dim}(LLM errors fall through to regex-only)${c_rst}"
  fi
  if [[ -n "${SURROGATE_SALT:-}" ]]; then
    say "Salt:        ${c_grn}set${c_rst} ${c_dim}(stable surrogates across restarts)${c_rst}"
  fi
  if [[ "${USE_FAKER_CHOICE:-false}" == "false" ]]; then
    say "Faker:      ${c_grn}disabled${c_rst} ${c_dim}(opaque [TYPE_HEX] surrogates)${c_rst}"
  elif [[ -n "${LOCALE_CHOICE:-}" ]]; then
    say "Faker:      ${c_grn}enabled${c_rst}, locale=${c_grn}${LOCALE_CHOICE}${c_rst}"
  else
    say "Faker:      ${c_grn}enabled${c_rst} ${c_dim}(default locale)${c_rst}"
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
}

# Foreground run. Replaces the script via `exec` only if there's
# nothing to clean up; otherwise wraps so the EXIT trap fires.
run_guardrail() {
  build_run_args
  trap cleanup_fake_llm EXIT

  local status=0
  "$ENGINE" run --rm --name "$NAME" -p "${PORT}:8000" \
    "${RUN_NETWORK_ARGS[@]}" \
    -e "DETECTOR_MODE=${DETECTOR_MODE}" \
    "${RUN_LOG_LEVEL_ARGS[@]}" \
    "${REGEX_OVERLAP_ARGS[@]}" \
    "${RUN_REGEX_PATTERNS_ARGS[@]}" \
    "${RUN_LLM_ARGS[@]}" \
    "${RUN_LLM_PROMPT_ARGS[@]}" \
    "${RUN_FORWARDED_KEY_ARGS[@]}" \
    "${RUN_FAIL_CLOSED_ARGS[@]}" \
    "${RUN_SALT_ARGS[@]}" \
    "${RUN_FAKER_ARGS[@]}" \
    "${RUN_OFFLINE_ARGS[@]}" \
    "${RUN_VOLUME_ARGS[@]}" "${EXTRA_ARGS[@]}" \
    "$IMAGE" || status=$?
  exit "$status"
}