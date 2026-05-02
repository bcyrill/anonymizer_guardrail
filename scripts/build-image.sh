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
TAG_PF_SERVICE="${TAG_PF_SERVICE:-privacy-filter-service:cpu}"
TAG_PF_SERVICE_BAKED="${TAG_PF_SERVICE_BAKED:-privacy-filter-service:baked-cpu}"
TAG_PF_SERVICE_CU130="${TAG_PF_SERVICE_CU130:-privacy-filter-service:cu130}"
TAG_PF_SERVICE_BAKED_CU130="${TAG_PF_SERVICE_BAKED_CU130:-privacy-filter-service:baked-cu130}"
TAG_GLINER_SERVICE="${TAG_GLINER_SERVICE:-gliner-pii-service:cpu}"
TAG_GLINER_SERVICE_BAKED="${TAG_GLINER_SERVICE_BAKED:-gliner-pii-service:baked-cpu}"
TAG_GLINER_SERVICE_CU130="${TAG_GLINER_SERVICE_CU130:-gliner-pii-service:cu130}"
TAG_GLINER_SERVICE_BAKED_CU130="${TAG_GLINER_SERVICE_BAKED_CU130:-gliner-pii-service:baked-cu130}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-t TYPE] [-T TAG] [-h] [-- EXTRA_BUILD_ARGS...]

Build the anonymizer-guardrail (or companion fake-llm) container image.
Without -t, prompts interactively.

  -t, --type TYPE     One value, or a comma-separated list (e.g.
                      \`-t slim,pf-service,gliner-service\`). One of:
                        slim              without privacy-filter
                        pf                privacy-filter, runtime download
                                          — needs a persistent volume to avoid
                                          re-downloading the model every run
                        pf-baked          privacy-filter + model baked in
                                          — self-contained; works air-gapped
                        pf-service        privacy-filter inference service —
                                          standalone HTTP wrapper around the
                                          model, paired with the guardrail's
                                          RemotePrivacyFilterDetector. Runtime
                                          download, CPU-only torch.
                        pf-service-baked  pf-service + model baked in (CPU)
                        pf-service-cu130  pf-service with CUDA 13.0 torch
                                          wheels (~+2 GB image; needs an
                                          nvidia GPU at run time)
                        pf-service-baked-cu130  pf-service + model baked in,
                                          CUDA 13.0 torch wheels
                        gliner-service          nvidia/gliner-pii inference
                                          service. Runtime download,
                                          CPU-only torch.
                        gliner-service-baked    gliner-service + model baked in
                        gliner-service-cu130    gliner-service with CUDA 13.0
                        gliner-service-baked-cu130  gliner-service + model
                                          baked in, CUDA 13.0
                        fake-llm          companion test backend — an OpenAI-
                                          compatible Chat Completions server
                                          with a YAML rules file, for driving
                                          the guardrail's LLM detector
                                          deterministically (see services/fake_llm/)
                        all               build every CPU flavour in sequence
                                          (uses the default tag for each — pass
                                          -T separately if you want overrides)
                        all-runtime       like 'all' but skips baked variants
                                          (slim, pf, pf-service,
                                          gliner-service, fake-llm)
  -T, --tag TAG       Override the default image tag (single-flavour only).
  -h, --help          Show this help.

Anything after \`--\` is passed straight through to the build engine,
e.g. \`-- --no-cache --pull\`.

Environment overrides:
  ENGINE                podman | docker (auto-detected; current: ${ENGINE})
  TAG_SLIM              Default tag for slim (current: ${TAG_SLIM})
  TAG_PF                Default tag for pf (current: ${TAG_PF})
  TAG_PF_BAKED          Default tag for pf-baked (current: ${TAG_PF_BAKED})
  TAG_PF_SERVICE              Default tag for pf-service (current: ${TAG_PF_SERVICE})
  TAG_PF_SERVICE_BAKED        Default tag for pf-service-baked (current: ${TAG_PF_SERVICE_BAKED})
  TAG_PF_SERVICE_CU130        Default tag for pf-service-cu130 (current: ${TAG_PF_SERVICE_CU130})
  TAG_PF_SERVICE_BAKED_CU130  Default tag for pf-service-baked-cu130 (current: ${TAG_PF_SERVICE_BAKED_CU130})
  TAG_GLINER_SERVICE          Default tag for gliner-service (current: ${TAG_GLINER_SERVICE})
  TAG_GLINER_SERVICE_BAKED    Default tag for gliner-service-baked (current: ${TAG_GLINER_SERVICE_BAKED})
  TAG_GLINER_SERVICE_CU130    Default tag for gliner-service-cu130 (current: ${TAG_GLINER_SERVICE_CU130})
  TAG_GLINER_SERVICE_BAKED_CU130  Default tag for gliner-service-baked-cu130 (current: ${TAG_GLINER_SERVICE_BAKED_CU130})
  TAG_FAKE_LLM                Default tag for fake-llm (current: ${TAG_FAKE_LLM})
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
# Indexed 1..N so the prompt text and the choice-lookup share one
# source of truth — keeps the two from drifting when a flavour is
# added or reordered. The 'a' alias for "all" lives below as a
# special case (no number).
declare -a MENU_FLAVOURS=(
  [1]="slim"
  [2]="pf"
  [3]="pf-baked"
  [4]="pf-service"
  [5]="pf-service-baked"
  [6]="pf-service-cu130"
  [7]="pf-service-baked-cu130"
  [8]="gliner-service"
  [9]="gliner-service-baked"
  [10]="gliner-service-cu130"
  [11]="gliner-service-baked-cu130"
  [12]="fake-llm"
)

if [[ -z "$TYPE" ]]; then
  say ""
  say "Which image flavour do you want to build?"
  say ""
  say "  ${c_grn} 1)${c_rst} slim                       without privacy-filter"
  say "  ${c_grn} 2)${c_rst} pf                         privacy-filter, runtime download"
  say "  ${c_grn} 3)${c_rst} pf-baked                   privacy-filter + model baked in"
  say "  ${c_grn} 4)${c_rst} pf-service                 privacy-filter inference service ${c_dim}(CPU)${c_rst}"
  say "  ${c_grn} 5)${c_rst} pf-service-baked           pf-service + model baked in ${c_dim}(CPU)${c_rst}"
  say "  ${c_grn} 6)${c_rst} pf-service-cu130           pf-service with CUDA 13.0 torch ${c_dim}(needs GPU at runtime)${c_rst}"
  say "  ${c_grn} 7)${c_rst} pf-service-baked-cu130     pf-service + model baked in, CUDA 13.0"
  say "  ${c_grn} 8)${c_rst} gliner-service             nvidia/gliner-pii service ${c_dim}(CPU)${c_rst}"
  say "  ${c_grn} 9)${c_rst} gliner-service-baked       gliner-service + model baked in ${c_dim}(CPU)${c_rst}"
  say "  ${c_grn}10)${c_rst} gliner-service-cu130       gliner-service with CUDA 13.0"
  say "  ${c_grn}11)${c_rst} gliner-service-baked-cu130 gliner-service + model baked in, CUDA 13.0"
  say "  ${c_grn}12)${c_rst} fake-llm                   companion test backend ${c_dim}(deterministic LLM)${c_rst}"
  say "  ${c_grn} a)${c_rst} all                        build every CPU flavour ${c_dim}(skips CUDA)${c_rst}"
  say "  ${c_grn} r)${c_rst} all-runtime                like 'all' but skips baked variants ${c_dim}(no model baked into image)${c_rst}"
  say ""
  read -r -p "Choose [1-12/a/r, comma-separated for multiple e.g. 1,4,8, default 1]: " choice || true
  choice="${choice:-1}"

  # Translate the comma-separated menu input (e.g. "1,4,8") into the
  # comma-joined flavour-name form (e.g. "slim,pf-service,gliner-service")
  # the existing -t parsing below already handles. A single token works
  # exactly as before.
  declare -a _menu_flavours=()
  IFS=',' read -ra _menu_tokens <<< "$choice"
  for raw in "${_menu_tokens[@]}"; do
    tok="${raw#"${raw%%[![:space:]]*}"}"   # ltrim
    tok="${tok%"${tok##*[![:space:]]}"}"   # rtrim
    [[ -z "$tok" ]] && continue
    if [[ "$tok" == "a" || "$tok" == "A" ]]; then
      _menu_flavours+=("all")
    elif [[ "$tok" == "r" || "$tok" == "R" ]]; then
      _menu_flavours+=("all-runtime")
    elif [[ "$tok" =~ ^[0-9]+$ ]] && [[ -n "${MENU_FLAVOURS[$tok]:-}" ]]; then
      _menu_flavours+=("${MENU_FLAVOURS[$tok]}")
    else
      err "Invalid choice: '$tok'."
      exit 1
    fi
  done
  if [[ ${#_menu_flavours[@]} -eq 0 ]]; then
    err "No valid choices."
    exit 1
  fi
  TYPE="$(IFS=,; echo "${_menu_flavours[*]}")"
  unset _menu_tokens _menu_flavours
fi

# Comma-separated TYPE → list of flavours. One value works exactly as
# before; multiple values let an operator do `-t slim,pf-service` to
# build a curated subset without invoking the script repeatedly.
IFS=',' read -ra TYPE_PARTS <<< "$TYPE"

# Pre-flight: trim whitespace, drop empties, expand "all" / "all-runtime".
BUILD_LIST=()
for raw in "${TYPE_PARTS[@]}"; do
  t="${raw#"${raw%%[![:space:]]*}"}"   # ltrim
  t="${t%"${t##*[![:space:]]}"}"        # rtrim
  [[ -z "$t" ]] && continue
  if [[ "$t" == "all" ]]; then
    BUILD_LIST+=(slim pf pf-baked pf-service pf-service-baked gliner-service gliner-service-baked fake-llm)
  elif [[ "$t" == "all-runtime" ]]; then
    BUILD_LIST+=(slim pf pf-service gliner-service fake-llm)
  else
    BUILD_LIST+=("$t")
  fi
done

if [[ ${#BUILD_LIST[@]} -eq 0 ]]; then
  err "No build types resolved from -t '$TYPE'."
  exit 1
fi

# Dedupe so `-t slim,all` doesn't build slim twice. Order-preserving:
# walks the list once and records the first occurrence.
declare -A _seen=()
DEDUPED_LIST=()
for t in "${BUILD_LIST[@]}"; do
  if [[ -z "${_seen[$t]:-}" ]]; then
    DEDUPED_LIST+=("$t")
    _seen[$t]=1
  fi
done
BUILD_LIST=("${DEDUPED_LIST[@]}")
unset _seen DEDUPED_LIST

# -T / --tag only makes sense for a single flavour — otherwise N
# different images would all get the same tag, with the last build
# silently overwriting the earlier ones.
if [[ ${#BUILD_LIST[@]} -gt 1 && -n "$TAG_OVERRIDE" ]]; then
  err "-T/--tag isn't supported when building multiple flavours (each uses its own default tag)."
  exit 1
fi

# ── Per-flavour resolver: maps a flavour name to build-args, Containerfile,
# build context, and default tag. Sets globals that build_one consumes.
# Most flavours build the main guardrail image from the repo root
# Containerfile; the auxiliary services (fake-llm, privacy-filter
# inference) are separate companion images with their own Containerfile
# and a self-contained build context under services/.
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
      CONTAINERFILE="services/fake_llm/Containerfile"
      BUILD_CONTEXT="services/fake_llm"
      RESOLVED_TYPE="fake-llm"
      ;;
    pf-service|privacy-filter-service)
      BUILD_ARGS=()
      DEFAULT_TAG="$TAG_PF_SERVICE"
      CONTAINERFILE="services/privacy_filter/Containerfile"
      BUILD_CONTEXT="services/privacy_filter"
      RESOLVED_TYPE="pf-service"
      ;;
    pf-service-baked|privacy-filter-service-baked)
      BUILD_ARGS=(--build-arg BAKE_MODEL=true)
      DEFAULT_TAG="$TAG_PF_SERVICE_BAKED"
      CONTAINERFILE="services/privacy_filter/Containerfile"
      BUILD_CONTEXT="services/privacy_filter"
      RESOLVED_TYPE="pf-service-baked"
      ;;
    # CUDA variants — only the standalone pf-service has them; the
    # guardrail's built-in pf flavour stays CPU-only because anything
    # that needs GPU acceleration is better off with the standalone
    # service so the model can scale independently of the API tier.
    # cu130 matches the convention pytorch uses for its CUDA wheel
    # index (cu130 = CUDA 13.0); change the index URL or add another
    # case to support a different CUDA version.
    pf-service-cu130|privacy-filter-service-cu130)
      BUILD_ARGS=(--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130)
      DEFAULT_TAG="$TAG_PF_SERVICE_CU130"
      CONTAINERFILE="services/privacy_filter/Containerfile"
      BUILD_CONTEXT="services/privacy_filter"
      RESOLVED_TYPE="pf-service-cu130"
      ;;
    pf-service-baked-cu130|privacy-filter-service-baked-cu130)
      BUILD_ARGS=(
        --build-arg BAKE_MODEL=true
        --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
      )
      DEFAULT_TAG="$TAG_PF_SERVICE_BAKED_CU130"
      CONTAINERFILE="services/privacy_filter/Containerfile"
      BUILD_CONTEXT="services/privacy_filter"
      RESOLVED_TYPE="pf-service-baked-cu130"
      ;;
    # Companion service for nvidia/gliner-pii. Same four-variant grid
    # as pf-service (CPU/CUDA × runtime/baked).
    gliner-service|gliner-pii-service)
      BUILD_ARGS=()
      DEFAULT_TAG="$TAG_GLINER_SERVICE"
      CONTAINERFILE="services/gliner_pii/Containerfile"
      BUILD_CONTEXT="services/gliner_pii"
      RESOLVED_TYPE="gliner-service"
      ;;
    gliner-service-baked|gliner-pii-service-baked)
      BUILD_ARGS=(--build-arg BAKE_MODEL=true)
      DEFAULT_TAG="$TAG_GLINER_SERVICE_BAKED"
      CONTAINERFILE="services/gliner_pii/Containerfile"
      BUILD_CONTEXT="services/gliner_pii"
      RESOLVED_TYPE="gliner-service-baked"
      ;;
    gliner-service-cu130|gliner-pii-service-cu130)
      BUILD_ARGS=(--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130)
      DEFAULT_TAG="$TAG_GLINER_SERVICE_CU130"
      CONTAINERFILE="services/gliner_pii/Containerfile"
      BUILD_CONTEXT="services/gliner_pii"
      RESOLVED_TYPE="gliner-service-cu130"
      ;;
    gliner-service-baked-cu130|gliner-pii-service-baked-cu130)
      BUILD_ARGS=(
        --build-arg BAKE_MODEL=true
        --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
      )
      DEFAULT_TAG="$TAG_GLINER_SERVICE_BAKED_CU130"
      CONTAINERFILE="services/gliner_pii/Containerfile"
      BUILD_CONTEXT="services/gliner_pii"
      RESOLVED_TYPE="gliner-service-baked-cu130"
      ;;
    *)
      err "Unknown type '$1'. Valid: slim, pf, pf-baked,"
      err "  pf-service, pf-service-baked, pf-service-cu130, pf-service-baked-cu130,"
      err "  gliner-service, gliner-service-baked, gliner-service-cu130, gliner-service-baked-cu130,"
      err "  fake-llm, all."
      exit 1
      ;;
  esac
  TAG="${TAG_OVERRIDE:-$DEFAULT_TAG}"
}

# Run a single build. Honors EXTRA_ARGS (passthrough after `--`).
build_one() {
  resolve_flavour "$1"
  # `--format=docker` is needed under podman because podman defaults to
  # the OCI image format, which doesn't include a HEALTHCHECK field —
  # podman drops our HEALTHCHECK silently and warns. Docker format
  # preserves it. The flag isn't valid on `docker build`, so we only
  # add it when the engine is podman.
  local fmt_args=()
  [[ "$ENGINE" == "podman" ]] && fmt_args=(--format=docker)
  say ""
  say "${c_bld}── Building ${RESOLVED_TYPE} → ${TAG} ──${c_rst}"
  say "${c_dim}${ENGINE} build ${fmt_args[*]} -t ${TAG} ${BUILD_ARGS[*]} ${EXTRA_ARGS[*]:-} -f ${CONTAINERFILE} ${BUILD_CONTEXT}${c_rst}"
  "$ENGINE" build "${fmt_args[@]}" -t "$TAG" "${BUILD_ARGS[@]}" "${EXTRA_ARGS[@]}" -f "$CONTAINERFILE" "$BUILD_CONTEXT"
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

# Any flavour that bakes the model downloads weights at build time —
# call it out whether selected directly or pulled in via "all".
for f in "${BUILD_LIST[@]}"; do
  case "$f" in
    pf-baked|privacy-filter-baked|pf-service-baked|privacy-filter-service-baked|pf-service-baked-cu130|privacy-filter-service-baked-cu130|gliner-service-baked|gliner-pii-service-baked|gliner-service-baked-cu130|gliner-pii-service-baked-cu130)
      warn "This build downloads the model at build time (openai/privacy-filter"
      warn "or nvidia/gliner-pii depending on flavour). Network access is required"
      warn "and the build will take several minutes the first time (subsequent"
      warn "builds use the layer cache)."
      say ""
      break
      ;;
  esac
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
  say "  scripts/launcher.sh --ui                       ${c_dim}# interactive picker${c_rst}"
  say "  scripts/launcher.sh --preset uuid-debug    ${c_dim}# slim + fake-llm${c_rst}"
  say "  scripts/launcher.sh --preset pentest       ${c_dim}# pf + fake-llm + pentest config${c_rst}"
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
  pf-service)
    say "  # Create a named volume so the model download survives recreates."
    say "  ${ENGINE} volume create privacy-filter-hf-cache"
    say "  ${ENGINE} run --rm -p 8001:8001 \\"
    say "      -v privacy-filter-hf-cache:/app/.cache/huggingface \\"
    say "      ${TAG}"
    say ""
    say "  ${c_dim}# Then point the guardrail at it (RemotePrivacyFilterDetector,${c_rst}"
    say "  ${c_dim}# planned): -e PRIVACY_FILTER_URL=http://privacy-filter-service:8001${c_rst}"
    ;;
  pf-service-baked)
    say "  ${ENGINE} run --rm -p 8001:8001 ${TAG}"
    say ""
    say "  ${c_dim}# Self-contained — no runtime download. Point the guardrail at${c_rst}"
    say "  ${c_dim}# it via PRIVACY_FILTER_URL once RemotePrivacyFilterDetector lands.${c_rst}"
    ;;
  pf-service-cu130)
    say "  # Create a named volume so the model download survives recreates."
    say "  ${ENGINE} volume create privacy-filter-hf-cache"
    say "  ${ENGINE} run --rm -p 8001:8001 \\"
    say "      --device nvidia.com/gpu=all \\"
    say "      -v privacy-filter-hf-cache:/app/.cache/huggingface \\"
    say "      ${TAG}"
    say ""
    say "  ${c_dim}# CUDA wheels in image; needs an nvidia GPU + nvidia-container-toolkit${c_rst}"
    say "  ${c_dim}# at run time. With docker, use --gpus all instead of --device.${c_rst}"
    ;;
  pf-service-baked-cu130)
    say "  ${ENGINE} run --rm -p 8001:8001 \\"
    say "      --device nvidia.com/gpu=all \\"
    say "      ${TAG}"
    say ""
    say "  ${c_dim}# Self-contained CUDA build. Needs an nvidia GPU at run time.${c_rst}"
    say "  ${c_dim}# With docker, use --gpus all instead of --device.${c_rst}"
    ;;
  gliner-service)
    say "  # Create a named volume so the model download survives recreates."
    say "  ${ENGINE} volume create gliner-hf-cache"
    say "  ${ENGINE} run --rm -p 8002:8002 \\"
    say "      -v gliner-hf-cache:/app/.cache/huggingface \\"
    say "      ${TAG}"
    say ""
    say "  ${c_dim}# Probe it:${c_rst}"
    say "  ${c_dim}# curl -s http://localhost:8002/detect -H 'Content-Type: application/json' \\\\${c_rst}"
    say "  ${c_dim}#   -d '{\"text\": \"alice@example.com\", \"labels\": [\"email\"]}'${c_rst}"
    ;;
  gliner-service-baked)
    say "  ${ENGINE} run --rm -p 8002:8002 ${TAG}"
    say ""
    say "  ${c_dim}# Self-contained — no runtime download.${c_rst}"
    ;;
  gliner-service-cu130)
    say "  # Create a named volume so the model download survives recreates."
    say "  ${ENGINE} volume create gliner-hf-cache"
    say "  ${ENGINE} run --rm -p 8002:8002 \\"
    say "      --device nvidia.com/gpu=all \\"
    say "      -v gliner-hf-cache:/app/.cache/huggingface \\"
    say "      ${TAG}"
    say ""
    say "  ${c_dim}# CUDA wheels in image; needs an nvidia GPU + nvidia-container-toolkit${c_rst}"
    say "  ${c_dim}# at run time. With docker, use --gpus all instead of --device.${c_rst}"
    ;;
  gliner-service-baked-cu130)
    say "  ${ENGINE} run --rm -p 8002:8002 \\"
    say "      --device nvidia.com/gpu=all \\"
    say "      ${TAG}"
    say ""
    say "  ${c_dim}# Self-contained CUDA build. Needs an nvidia GPU at run time.${c_rst}"
    ;;
  fake-llm)
    say "  ${c_dim}# fake-llm is auto-started by scripts/launcher.sh when you${c_rst}"
    say "  ${c_dim}# pick a DETECTOR_MODE that includes llm and choose the${c_rst}"
    say "  ${c_dim}# fake-llm backend. Just run a guardrail flavour:${c_rst}"
    say "  scripts/launcher.sh -t slim -d regex,llm --llm-backend fake-llm"
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