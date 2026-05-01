#!/usr/bin/env bash
# Build the anonymizer-guardrail container image.
#
# The Containerfile takes two orthogonal build-args (WITH_PRIVACY_FILTER
# and BAKE_PRIVACY_FILTER_MODEL); this script wraps the three sensible
# combinations as named flavours so you don't have to remember which
# flag goes where.
#
# Usage: scripts/build-image.sh [-t TYPE] [-T TAG] [--] [extra build args]

set -euo pipefail

# cd to repo root regardless of where the script was invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_bld=$'\033[1m'; c_rst=$'\033[0m'
say()  { printf '%s\n' "$*"; }
warn() { printf '%s%s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_rst" >&2; }
ok()   { printf '%s%s%s\n' "$c_grn" "$*" "$c_rst"; }

# ── Container engine detection ───────────────────────────────────────────────
# Prefer podman (rootless by default) but fall back to docker. Operators
# with both can force one via ENGINE=docker scripts/build-image.sh.
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

# ── Default image tags per flavour ───────────────────────────────────────────
TAG_SLIM="${TAG_SLIM:-anonymizer-guardrail:latest}"
TAG_PF="${TAG_PF:-anonymizer-guardrail:privacy-filter}"
TAG_PF_BAKED="${TAG_PF_BAKED:-anonymizer-guardrail:privacy-filter-baked}"
TAG_FAKE_LLM="${TAG_FAKE_LLM:-fake-llm:latest}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-t TYPE] [-T TAG] [-h] [-- EXTRA_BUILD_ARGS...]

Build the anonymizer-guardrail (or companion fake-llm) container image.
Without -t, prompts interactively.

  -t, --type TYPE     One of:
                        slim       without privacy-filter
                        pf         privacy-filter, runtime download
                                   — needs a persistent volume to avoid
                                   re-downloading the model every run
                        pf-baked   privacy-filter + model baked in
                                   — self-contained; works air-gapped
                        fake-llm   companion test backend — an OpenAI-
                                   compatible Chat Completions server
                                   with a YAML rules file, for driving
                                   the guardrail's LLM detector
                                   deterministically (see fake-llm/)
                        all        build all four flavours in sequence
                                   (uses the default tag for each — pass
                                   -T separately if you want overrides)
  -T, --tag TAG       Override the default image tag (single-flavour only).
  -h, --help          Show this help.

Anything after \`--\` is passed straight through to the build engine,
e.g. \`-- --no-cache --pull\`.

Environment overrides:
  ENGINE         podman | docker (auto-detected; current: ${ENGINE})
  TAG_SLIM       Default tag for slim (current: ${TAG_SLIM})
  TAG_PF         Default tag for pf (current: ${TAG_PF})
  TAG_PF_BAKED   Default tag for pf-baked (current: ${TAG_PF_BAKED})
  TAG_FAKE_LLM   Default tag for fake-llm (current: ${TAG_FAKE_LLM})
EOF
}

TYPE=""
TAG_OVERRIDE=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--type) TYPE="${2:-}"; shift 2 ;;
    -T|--tag)  TAG_OVERRIDE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)        shift; EXTRA_ARGS=("$@"); break ;;
    *) err "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ── Interactive menu when no -t given ────────────────────────────────────────
if [[ -z "$TYPE" ]]; then
  say ""
  say "Which image flavour do you want to build?"
  say ""
  say "  ${c_grn}1)${c_rst} slim       without privacy-filter"
  say "  ${c_grn}2)${c_rst} pf         privacy-filter, runtime download"
  say "  ${c_grn}3)${c_rst} pf-baked   privacy-filter + model baked in"
  say "  ${c_grn}4)${c_rst} fake-llm   companion test backend ${c_dim}(deterministic LLM)${c_rst}"
  say "  ${c_grn}a)${c_rst} all        build all four flavours in sequence"
  say ""
  read -r -p "Choose [1-4/a, default 1]: " choice || true
  case "${choice:-1}" in
    1)   TYPE="slim" ;;
    2)   TYPE="pf" ;;
    3)   TYPE="pf-baked" ;;
    4)   TYPE="fake-llm" ;;
    a|A) TYPE="all" ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

# Expand "all" into a build list; otherwise use the single requested flavour.
if [[ "$TYPE" == "all" ]]; then
  if [[ -n "$TAG_OVERRIDE" ]]; then
    err "-T/--tag isn't supported with --type all (each flavour uses its own default tag)."
    exit 1
  fi
  BUILD_LIST=(slim pf pf-baked fake-llm)
else
  BUILD_LIST=("$TYPE")
fi

# ── Per-flavour resolver: maps a flavour name to build-args, Containerfile,
# build context, and default tag. Sets globals that build_one consumes.
# Most flavours build the main guardrail image from the repo root
# Containerfile; fake-llm is a separate companion image with its own
# Containerfile and a self-contained build context under fake-llm/.
resolve_flavour() {
  CONTAINERFILE="Containerfile"
  BUILD_CONTEXT="."
  case "$1" in
    slim)
      BUILD_ARGS=()
      DEFAULT_TAG="$TAG_SLIM"
      RESOLVED_TYPE="slim"
      ;;
    pf|privacy-filter)
      BUILD_ARGS=(--build-arg WITH_PRIVACY_FILTER=true)
      DEFAULT_TAG="$TAG_PF"
      RESOLVED_TYPE="pf"
      ;;
    pf-baked|privacy-filter-baked)
      BUILD_ARGS=(
        --build-arg WITH_PRIVACY_FILTER=true
        --build-arg BAKE_PRIVACY_FILTER_MODEL=true
      )
      DEFAULT_TAG="$TAG_PF_BAKED"
      RESOLVED_TYPE="pf-baked"
      ;;
    fake-llm)
      BUILD_ARGS=()
      DEFAULT_TAG="$TAG_FAKE_LLM"
      CONTAINERFILE="fake-llm/Containerfile"
      BUILD_CONTEXT="fake-llm"
      RESOLVED_TYPE="fake-llm"
      ;;
    *)
      err "Unknown type '$1'. Valid: slim, pf, pf-baked, fake-llm, all."
      exit 1
      ;;
  esac
  TAG="${TAG_OVERRIDE:-$DEFAULT_TAG}"
}

# Run a single build. Honors EXTRA_ARGS (passthrough after `--`).
build_one() {
  resolve_flavour "$1"
  say ""
  say "${c_bld}── Building ${RESOLVED_TYPE} → ${TAG} ──${c_rst}"
  say "${c_dim}${ENGINE} build -t ${TAG} ${BUILD_ARGS[*]} ${EXTRA_ARGS[*]:-} -f ${CONTAINERFILE} ${BUILD_CONTEXT}${c_rst}"
  "$ENGINE" build -t "$TAG" "${BUILD_ARGS[@]}" "${EXTRA_ARGS[@]}" -f "$CONTAINERFILE" "$BUILD_CONTEXT"
  ok "Built ${TAG}."
}

# ── Plan output ──────────────────────────────────────────────────────────────
# Pre-resolve each requested flavour so the plan can list final tags.
declare -a PLAN_TAGS=()
for f in "${BUILD_LIST[@]}"; do
  resolve_flavour "$f"
  PLAN_TAGS+=("${RESOLVED_TYPE} → ${TAG}")
done

say ""
say "Engine:    ${c_grn}${ENGINE}${c_rst}"
if [[ ${#BUILD_LIST[@]} -eq 1 ]]; then
  say "Flavour:   ${c_grn}${PLAN_TAGS[0]}${c_rst}"
else
  say "Flavours:"
  for entry in "${PLAN_TAGS[@]}"; do
    say "  ${c_grn}${entry}${c_rst}"
  done
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  say "Passthrough: ${c_dim}${EXTRA_ARGS[*]}${c_rst}"
fi
say ""

# pf-baked downloads the model at build time — call it out whether
# selected directly or pulled in via "all".
for f in "${BUILD_LIST[@]}"; do
  if [[ "$f" == "pf-baked" || "$f" == "privacy-filter-baked" ]]; then
    warn "The pf-baked build downloads the openai/privacy-filter model at"
    warn "build time. Network access is required and the build will take"
    warn "several minutes the first time (subsequent builds use the layer cache)."
    say ""
    break
  fi
done

read -r -p "Proceed? [Y/n] " reply || true
reply="${reply:-y}"
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
  warn "Aborted."
  exit 0
fi

# ── Build each flavour. Bail on the first failure. ──────────────────────────
for f in "${BUILD_LIST[@]}"; do
  build_one "$f"
done

# Restore the final flavour's resolved state for the post-build hint
# below (single-flavour case). For "all" we'll skip the per-flavour
# hint and just summarize.
if [[ ${#BUILD_LIST[@]} -gt 1 ]]; then
  say ""
  ok "Built ${#BUILD_LIST[@]} images."
  say ""
  say "${c_dim}Try them via:${c_rst}"
  say "  scripts/menu.sh                       ${c_dim}# interactive picker${c_rst}"
  say "  scripts/cli.sh --preset uuid-debug    ${c_dim}# slim + fake-llm${c_rst}"
  say "  scripts/cli.sh --preset pentest       ${c_dim}# pf + fake-llm + pentest config${c_rst}"
  exit 0
fi
TYPE="$RESOLVED_TYPE"

# ── Post-build run hint, tailored to the flavour ─────────────────────────────
say ""
say "${c_dim}Next — try it locally:${c_rst}"
case "$TYPE" in
  slim)
    say "  ${ENGINE} run --rm -p 8000:8000 \\"
    say "      -e DETECTOR_MODE=regex,llm \\"
    say "      -e LLM_API_BASE=http://litellm:4000/v1 \\"
    say "      -e LLM_API_KEY=sk-litellm-master \\"
    say "      ${TAG}"
    ;;
  pf)
    say "  # Create a named volume so the model download survives recreates."
    say "  ${ENGINE} volume create anonymizer-hf-cache"
    say "  ${ENGINE} run --rm -p 8000:8000 \\"
    say "      -e DETECTOR_MODE=regex,privacy_filter,llm \\"
    say "      -v anonymizer-hf-cache:/app/.cache/huggingface \\"
    say "      ${TAG}"
    ;;
  pf-baked)
    say "  ${ENGINE} run --rm -p 8000:8000 \\"
    say "      -e DETECTOR_MODE=regex,privacy_filter,llm \\"
    say "      ${TAG}"
    ;;
  fake-llm)
    say "  ${c_dim}# fake-llm is auto-started by run_container.sh when you${c_rst}"
    say "  ${c_dim}# pick a DETECTOR_MODE that includes llm and choose the${c_rst}"
    say "  ${c_dim}# fake-llm backend. Just run a guardrail flavour:${c_rst}"
    say "  scripts/run_container.sh -t slim -d regex,llm"
    say ""
    say "  ${c_dim}# To run it standalone (e.g. to probe rules directly):${c_rst}"
    say "  ${ENGINE} run --rm -p 4000:4000 ${TAG}"
    ;;
esac
say ""
if [[ "$TYPE" != "fake-llm" ]]; then
  say "  ${c_dim}# Then in another terminal:${c_rst}"
  say "  curl -s http://localhost:8000/health | python -m json.tool"
fi