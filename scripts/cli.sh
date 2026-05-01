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
DENYLIST_PATH=""                 # empty | bundled:NAME | filesystem path
DENYLIST_BACKEND=""              # empty (server default) | regex | aho
PRIVACY_FILTER_URL=""            # empty (in-process) | http(s) URL
PRIVACY_FILTER_BACKEND=""        # empty | service | external
LLM_USE_FORWARDED_KEY=false
LLM_FAIL_CLOSED=true
PRIVACY_FILTER_FAIL_CLOSED=true
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
  -d, --detector-mode M    Comma-separated list. Tokens: regex, denylist,
                           privacy_filter, llm. Order = type-resolution
                           priority. Example: regex,denylist,llm.

LLM detector (only when DETECTOR_MODE includes \`llm\`):
  --llm-backend B          fake-llm | custom
  --llm-api-base URL       (custom only) e.g. http://litellm:4000/v1
  --llm-api-key KEY        (custom only) Bearer token; empty allowed
  --llm-model MODEL        (custom only) model alias

Detector / runtime tuning:
  --regex-overlap-strategy S   longest (default) | priority
  --regex-patterns P       REGEX_PATTERNS_PATH override.
                           Examples: bundled:regex_pentest.yaml | /etc/anonymizer/my.yaml
  --denylist-path P        DENYLIST_PATH — only meaningful when
                           DETECTOR_MODE includes \`denylist\`. Empty
                           leaves the detector loaded with no entries.
                           Examples: /etc/anonymizer/deny.yaml | bundled:NAME
  --denylist-backend B     DENYLIST_BACKEND — regex (default; stdlib re
                           alternation) or aho (Aho-Corasick via
                           pyahocorasick, sub-linear in pattern count).
  --privacy-filter-url URL PRIVACY_FILTER_URL — when set, the guardrail's
                           RemotePrivacyFilterDetector talks to a
                           standalone privacy-filter service and the
                           in-process model load is skipped. Lets the
                           slim image cover the privacy_filter detector.
                           Example: http://privacy-filter-service:8001
  --privacy-filter-backend B  service | external. \`service\` auto-starts
                           a privacy-filter-service container in the
                           background (mirrors --llm-backend fake-llm)
                           and points the guardrail at it. \`external\`
                           uses the URL provided via --privacy-filter-url
                           and starts nothing locally.
  --llm-prompt P           LLM_SYSTEM_PROMPT_PATH override.
                           Examples: bundled:llm_pentest.md | /etc/anonymizer/prompt.md
  --forward-llm-key        Set LLM_USE_FORWARDED_KEY=true (custom backend
                           only — sends caller's Authorization header to
                           the detection LLM).
  --llm-fail-open          Set LLM_FAIL_CLOSED=false (LLM errors fall
                           through to coverage from the other detectors;
                           default is fail-closed). \`--fail-open\` is a
                           deprecated alias.
  --privacy-filter-fail-open
                           Set PRIVACY_FILTER_FAIL_CLOSED=false
                           (privacy_filter errors degrade to no PF
                           matches; default is fail-closed). The two
                           fail-mode flags are independent.
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
    --denylist-path)         need_val "$@"; DENYLIST_PATH="$2"; shift 2 ;;
    --denylist-backend)      need_val "$@"; DENYLIST_BACKEND="$2"; shift 2 ;;
    --privacy-filter-url)    need_val "$@"; PRIVACY_FILTER_URL="$2"; PRIVACY_FILTER_BACKEND="${PRIVACY_FILTER_BACKEND:-external}"; shift 2 ;;
    --privacy-filter-backend) need_val "$@"; PRIVACY_FILTER_BACKEND="$2"; shift 2 ;;
    --llm-prompt)            need_val "$@"; LLM_SYSTEM_PROMPT_PATH="$2"; shift 2 ;;
    --forward-llm-key)       LLM_USE_FORWARDED_KEY=true; shift ;;
    --no-forward-llm-key)    LLM_USE_FORWARDED_KEY=false; shift ;;
    --llm-fail-open)         LLM_FAIL_CLOSED=false; shift ;;
    --llm-fail-closed)       LLM_FAIL_CLOSED=true; shift ;;
    --privacy-filter-fail-open)   PRIVACY_FILTER_FAIL_CLOSED=false; shift ;;
    --privacy-filter-fail-closed) PRIVACY_FILTER_FAIL_CLOSED=true;  shift ;;
    # Deprecated aliases — `--fail-open` / `--fail-closed` predated the
    # privacy_filter fail-mode flag, when LLM was the only detector
    # with one. Keep them working with a warning.
    --fail-open)             warn "--fail-open is deprecated; use --llm-fail-open."; LLM_FAIL_CLOSED=false; shift ;;
    --fail-closed)           warn "--fail-closed is deprecated; use --llm-fail-closed."; LLM_FAIL_CLOSED=true; shift ;;
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
case "${DENYLIST_BACKEND:-}" in
  ""|regex|aho) ;;
  *) err "Invalid --denylist-backend '${DENYLIST_BACKEND}'. Use 'regex' or 'aho'."; exit 1 ;;
esac
case "${PRIVACY_FILTER_BACKEND:-}" in
  ""|service|external) ;;
  *) err "Invalid --privacy-filter-backend '${PRIVACY_FILTER_BACKEND}'. Use 'service' or 'external'."; exit 1 ;;
esac

# `service` backend → auto-start the local container, point URL at it.
# An explicit --privacy-filter-url override still wins (operator may
# want a different host on the shared network).
if [[ "$PRIVACY_FILTER_BACKEND" == "service" && -z "$PRIVACY_FILTER_URL" ]]; then
  PRIVACY_FILTER_URL="$PF_SERVICE_URL_LOCAL"
fi
if [[ "$PRIVACY_FILTER_BACKEND" == "external" && -z "$PRIVACY_FILTER_URL" ]]; then
  err "--privacy-filter-backend external requires --privacy-filter-url."
  exit 1
fi

resolve_flavour
resolve_predicates

# privacy_filter on slim requires PRIVACY_FILTER_URL. The slim image
# doesn't ship torch / the model, so the in-process detector can't
# load — the remote detector needs a URL. Block with a clear error
# rather than letting the container crash with a torch ImportError.
resolve_predicates
if [[ "$DETECTOR_HAS_PRIVACY_FILTER" == "true" \
      && "$TYPE" == "slim" \
      && -z "$PRIVACY_FILTER_URL" ]]; then
  err "DETECTOR_MODE includes 'privacy_filter' on --type slim, but"
  err "--privacy-filter-url is unset. The slim image doesn't ship"
  err "torch + the model, so the in-process detector can't load."
  err "Either pass --privacy-filter-url URL (remote service) or"
  err "switch to --type pf / pf-baked."
  exit 1
fi

# Friendly nudge: denylist with no path → silent no-op detector.
# Don't fail — the operator may want to ship the env var via a
# separate file mount — but flag it so a forgotten path doesn't
# look like a bug at runtime.
if [[ "$DETECTOR_HAS_DENYLIST" == "true" && -z "$DENYLIST_PATH" ]]; then
  warn "DETECTOR_MODE includes 'denylist' but --denylist-path is unset."
  warn "The detector will load with no entries (matches nothing)."
fi

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

# ── Auto-start privacy-filter-service if needed ─────────────────────────────
if [[ "$DETECTOR_HAS_PRIVACY_FILTER" == "true" \
      && "$PRIVACY_FILTER_BACKEND" == "service" ]]; then
  start_privacy_filter
fi

print_plan
run_guardrail