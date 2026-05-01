#!/usr/bin/env bash
# Flag-driven launcher for the anonymizer-guardrail container.
# Zero prompts — every setting comes from CLI flags, environment, or
# a named preset. For an interactive whiptail UI, see scripts/menu.sh.
#
# Examples:
#   scripts/cli.sh --type slim -d regex,llm --llm-backend fake-llm -l debug
#   scripts/cli.sh --preset uuid-debug
#   scripts/cli.sh --type pf -d regex,privacy_filter,llm \
#                  --llm-api-base http://litellm:4000/v1 --llm-model gemma

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

detect_engine

# ── Defaults (sane starting points; flags or presets override) ──────────────
TYPE=""                          # required (or via preset)
DETECTOR_MODE=""                 # required (or via preset)
NAME=""
PORT=""
LOG_LEVEL_CHOICE=""              # required (or default below)
USE_FAKER_CHOICE=""
LOCALE_CHOICE=""
REGEX_OVERLAP_CHOICE=""
LLM_BACKEND=""
LLM_API_BASE_OVERRIDE=""
LLM_API_KEY_OVERRIDE=""
LLM_MODEL_OVERRIDE=""
HF_OFFLINE=false                 # only meaningful for `pf` flavour
RULES_FILE=""                    # fake-llm rules.yaml, optional
REGEX_PATTERNS_PATH=""           # empty | bundled:NAME | filesystem path
LLM_SYSTEM_PROMPT_PATH=""        # empty | bundled:NAME | filesystem path
LLM_USE_FORWARDED_KEY=false
FAIL_CLOSED=true
SURROGATE_SALT=""
EXTRA_ARGS=()
PRESET=""
REPLACE_EXISTING=false           # how to handle a name-conflicting container

usage() {
  cat <<EOF
Usage: $(basename "$0") [flags] [-- EXTRA_RUN_ARGS...]

Flag-driven launcher. For interactive use, see scripts/menu.sh.

Required (unless --preset supplies them):
  -t, --type TYPE          slim | pf | pf-baked
  -d, --detector-mode M    e.g. regex,llm

LLM detector (only when DETECTOR_MODE includes \`llm\`):
  --llm-backend B          fake-llm | custom
  --llm-api-base URL       (custom only) e.g. http://litellm:4000/v1
  --llm-api-key KEY        (custom only) Bearer token; empty allowed
  --llm-model MODEL        (custom only) model alias

Detector / runtime tuning:
  --regex-overlap-strategy S   longest (default) | priority
  --regex-patterns P       REGEX_PATTERNS_PATH override.
                           Examples: bundled:regex_pentest.yaml | /etc/anonymizer/my.yaml
  --llm-prompt P           LLM_SYSTEM_PROMPT_PATH override.
                           Examples: bundled:llm_pentest.md | /etc/anonymizer/prompt.md
  --forward-llm-key        Set LLM_USE_FORWARDED_KEY=true (custom backend
                           only — sends caller's Authorization header to
                           the detection LLM).
  --fail-open              Set FAIL_CLOSED=false (LLM errors fall through
                           to regex-only redaction; default is fail-closed).
  --surrogate-salt SALT    Stable SURROGATE_SALT across restarts
                           (default: random per process).
  -l, --log-level LEVEL    debug | info | warning | error  (default: info)
  --faker | --no-faker     enable/disable Faker realistic surrogates
                           (default: --no-faker)
  --locale LOC             Faker locale (e.g. pt_BR). Implies --faker.
  --hf-offline             Pass HF_HUB_OFFLINE=1 (pf flavour only)
  --rules PATH             Mount this YAML at /app/rules.yaml in
                           the auto-started fake-llm container.

Container:
  -p, --port PORT          Host port (default: 8000)
  -n, --name NAME          Container name (default: ${CONTAINER_NAME_DEFAULT})
  --replace                Remove a stale container of the same name
                           before starting (default: fail loudly).

Bundled presets (--preset NAME):
  uuid-debug     slim + regex,llm + fake-llm + LOG_LEVEL=debug,
                 USE_FAKER=false. Reproduces the UUID overlap case.
  pentest        pf + regex,privacy_filter,llm + fake-llm + debug,
                 USE_FAKER=false. Loads bundled pentest regex patterns
                 + pentest LLM prompt for the security-engagement
                 entity set.
  regex-only     slim + regex + LOG_LEVEL=info, USE_FAKER=true.
                 No LLM creds needed.

  -h, --help     Show this help.

Anything after \`--\` is forwarded to \`${ENGINE} run\`.
EOF
}

apply_preset() {
  case "$1" in
    uuid-debug)
      : "${TYPE:=slim}"
      : "${DETECTOR_MODE:=regex,llm}"
      : "${LLM_BACKEND:=fake-llm}"
      : "${LOG_LEVEL_CHOICE:=debug}"
      : "${USE_FAKER_CHOICE:=false}"
      : "${REGEX_OVERLAP_CHOICE:=longest}"
      ;;
    pentest)
      : "${TYPE:=pf}"
      : "${DETECTOR_MODE:=regex,privacy_filter,llm}"
      : "${LLM_BACKEND:=fake-llm}"
      : "${LOG_LEVEL_CHOICE:=debug}"
      : "${USE_FAKER_CHOICE:=false}"
      : "${REGEX_OVERLAP_CHOICE:=longest}"
      : "${REGEX_PATTERNS_PATH:=bundled:regex_pentest.yaml}"
      : "${LLM_SYSTEM_PROMPT_PATH:=bundled:llm_pentest.md}"
      ;;
    regex-only)
      : "${TYPE:=slim}"
      : "${DETECTOR_MODE:=regex}"
      : "${LOG_LEVEL_CHOICE:=info}"
      : "${USE_FAKER_CHOICE:=true}"
      : "${REGEX_OVERLAP_CHOICE:=longest}"
      ;;
    *)
      err "Unknown preset '${1}'. See --help."
      exit 1
      ;;
  esac
}

# `shift 2` in bash silently does nothing when fewer than two
# positional args remain — without this guard, `cli.sh --preset`
# (no value) leaves $1 still pointing at "--preset" and the while
# loop spins forever. need_val errors loud instead.
need_val() {
  if [[ $# -lt 2 ]]; then
    err "$1 requires a value."
    exit 1
  fi
}

# ── Flag parse ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--type)               need_val "$@"; TYPE="$2"; shift 2 ;;
    -p|--port)               need_val "$@"; PORT="$2"; shift 2 ;;
    -n|--name)               need_val "$@"; NAME="$2"; shift 2 ;;
    -d|--detector-mode)      need_val "$@"; DETECTOR_MODE="$2"; shift 2 ;;
    --regex-overlap-strategy) need_val "$@"; REGEX_OVERLAP_CHOICE="$2"; shift 2 ;;
    --llm-backend)           need_val "$@"; LLM_BACKEND="$2"; shift 2 ;;
    --llm-api-base)          need_val "$@"; LLM_API_BASE_OVERRIDE="$2"; LLM_BACKEND="${LLM_BACKEND:-custom}"; shift 2 ;;
    --llm-api-key)           need_val "$@"; LLM_API_KEY_OVERRIDE="$2"; LLM_BACKEND="${LLM_BACKEND:-custom}"; shift 2 ;;
    --llm-model)             need_val "$@"; LLM_MODEL_OVERRIDE="$2"; LLM_BACKEND="${LLM_BACKEND:-custom}"; shift 2 ;;
    -l|--log-level)          need_val "$@"; LOG_LEVEL_CHOICE="$2"; shift 2 ;;
    --faker)                 USE_FAKER_CHOICE="true"; shift ;;
    --no-faker)              USE_FAKER_CHOICE="false"; shift ;;
    --locale)                need_val "$@"; LOCALE_CHOICE="$2"; USE_FAKER_CHOICE="true"; shift 2 ;;
    --hf-offline)            HF_OFFLINE=true; shift ;;
    --rules)                 need_val "$@"; RULES_FILE="$2"; shift 2 ;;
    --regex-patterns)        need_val "$@"; REGEX_PATTERNS_PATH="$2"; shift 2 ;;
    --llm-prompt)            need_val "$@"; LLM_SYSTEM_PROMPT_PATH="$2"; shift 2 ;;
    --forward-llm-key)       LLM_USE_FORWARDED_KEY=true; shift ;;
    --no-forward-llm-key)    LLM_USE_FORWARDED_KEY=false; shift ;;
    --fail-open)             FAIL_CLOSED=false; shift ;;
    --fail-closed)           FAIL_CLOSED=true; shift ;;
    --surrogate-salt)        need_val "$@"; SURROGATE_SALT="$2"; shift 2 ;;
    --replace)               REPLACE_EXISTING=true; shift ;;
    --preset)                need_val "$@"; PRESET="$2"; shift 2 ;;
    -h|--help)               usage; exit 0 ;;
    --)                      shift; EXTRA_ARGS=("$@"); break ;;
    *) err "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# Apply preset only for the values not already set on the command line.
[[ -n "$PRESET" ]] && apply_preset "$PRESET"

# ── Defaults for anything still empty ────────────────────────────────────────
LOG_LEVEL_CHOICE="${LOG_LEVEL_CHOICE:-info}"
USE_FAKER_CHOICE="${USE_FAKER_CHOICE:-false}"
REGEX_OVERLAP_CHOICE="${REGEX_OVERLAP_CHOICE:-longest}"
NAME="${NAME:-$CONTAINER_NAME_DEFAULT}"
PORT="${PORT:-8000}"

# ── Validate required fields ─────────────────────────────────────────────────
if [[ -z "$TYPE" ]]; then
  err "Missing --type (slim | pf | pf-baked) or --preset."
  exit 1
fi
if [[ -z "$DETECTOR_MODE" ]]; then
  err "Missing --detector-mode (or --preset)."
  exit 1
fi
case "$LOG_LEVEL_CHOICE" in
  debug|info|warning|error) ;;
  *) err "Invalid --log-level '${LOG_LEVEL_CHOICE}'."; exit 1 ;;
esac
case "$REGEX_OVERLAP_CHOICE" in
  longest|priority) ;;
  *) err "Invalid --regex-overlap-strategy '${REGEX_OVERLAP_CHOICE}'."; exit 1 ;;
esac

resolve_flavour
resolve_predicates

# Validate LLM backend consistency before any side effects.
if [[ "$DETECTOR_HAS_LLM" == "true" ]]; then
  case "$LLM_BACKEND" in
    fake-llm) ;;
    custom)
      if [[ -z "$LLM_API_BASE_OVERRIDE" ]]; then
        err "--llm-backend custom requires --llm-api-base."
        exit 1
      fi
      ;;
    "")
      err "DETECTOR_MODE includes 'llm' but --llm-backend is not set (need fake-llm or custom)."
      exit 1
      ;;
    *)
      err "Invalid --llm-backend '${LLM_BACKEND}'. Use 'fake-llm' or 'custom'."
      exit 1
      ;;
  esac
fi

# ── Image must exist (don't auto-build silently) ─────────────────────────────
if ! image_exists "$IMAGE"; then
  err "Image \"${IMAGE}\" not found locally."
  err "Build it with:  scripts/build-image.sh -t ${TYPE}"
  exit 1
fi

# ── Volume + HF offline (pf only) ────────────────────────────────────────────
if [[ "$USE_VOLUME" == "true" ]]; then
  ensure_hf_volume
  if [[ "$JUST_CREATED_VOLUME" == "false" && "$HF_OFFLINE" != "true" ]]; then
    warn "Volume \"${HF_VOLUME}\" already exists. transformers will revalidate the"
    warn "cache against HuggingFace Hub on every start (~5s of HEAD requests +"
    warn "a 'set HF_TOKEN' warning). Pass --hf-offline to skip."
  fi
fi

# ── Container conflict ───────────────────────────────────────────────────────
if container_exists "$NAME"; then
  if [[ "$REPLACE_EXISTING" == "true" ]]; then
    "$ENGINE" rm -f "$NAME" >/dev/null
  else
    err "A container named \"${NAME}\" already exists. Pass --replace to remove it."
    exit 1
  fi
fi

# ── Auto-start fake-llm if needed ────────────────────────────────────────────
if [[ "$DETECTOR_HAS_LLM" == "true" && "$LLM_BACKEND" == "fake-llm" ]]; then
  start_fake_llm
fi

print_plan
run_guardrail