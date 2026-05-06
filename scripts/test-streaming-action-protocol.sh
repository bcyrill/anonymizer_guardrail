#!/usr/bin/env bash
# End-to-end test for the LiteLLM streaming action-protocol PR.
#
# Brings up three podman containers on a shared network:
#   * fake-llm   — OpenAI-compatible upstream that echoes input back as
#                  a streamed response (rules in tests/streaming_e2e/).
#   * anonguard  — anonymizer guardrail (regex detector only — no LLM /
#                  privacy-filter / gliner deps in this test). Returns
#                  WAIT on partial-surrogate trailing edges. Implements
#                  apply_guardrail_action so the new LiteLLM iterator
#                  hook auto-enrolls into the action protocol.
#   * litellm    — proxy under test, built from
#                  feat/streaming-guardrail-action-protocol
#                  (image: litellm:streaming-action-protocol).
#
# Sends a streaming chat-completion containing a regex-detectable email
# address against TWO arms:
#
#   * `action`         — Default new-LiteLLM behavior. The unified
#                        iterator hook auto-detects the guardrail's
#                        apply_guardrail_action method and drives the
#                        wait/pass/block/modify state machine. Client
#                        should see the deanonymized email and no
#                        surrogate.
#
#   * `chunk_straddle` — Same as action arm + a fake-llm rule that
#                        splits the upstream into 7-char chunks so the
#                        surrogate token spans chunk boundaries. Tests
#                        the WAIT-on-partial-surrogate path: client
#                        must not see any partial-surrogate prefix and
#                        must see the deanonymized email at the end.
#
# Usage:
#   scripts/test-streaming-action-protocol.sh [--mode {action|chunk_straddle|all}] [--keep] [--trace]
#
# Options:
#   --mode   Which arm to run. `all` (default) runs both.
#   --keep   Don't tear down on exit. Useful for `podman logs litellm-act`.
#   --trace  After each arm, dump the per-call anonymizer guardrail trace
#            (input texts, is_final, action, output texts) — useful for
#            confirming WAIT-on-partial-surrogate fires and seeing the
#            chunk-straddling progression call by call.
#
# Dev iteration:
#   Set LITELLM_SRC=~/litellm to bind-mount the local litellm files over
#   the in-image installed copies. Skips a podman build when iterating
#   on the LiteLLM-side changes.
#
# Backward-compat check:
#   To verify the new anonymizer works against stock (pre-PR) LiteLLM,
#   run:
#     LITELLM_IMAGE=ghcr.io/berriai/litellm:main-stable \
#       scripts/test-streaming-action-protocol.sh --mode action
#   The arm will fail its assertion (stock LiteLLM doesn't auto-enrol
#   into action mode → surrogate leaks) but the run should complete
#   without errors — proving the wire format is compatible.
#
# Exit code: 0 if the requested arm(s) pass, non-zero otherwise.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ── Test parameters ───────────────────────────────────────────────────

NETWORK_NAME="streaming-action-test-net"
ANON_NAME="anonguard-act"
FAKE_NAME="fake-llm-act"
LITELLM_NAME="litellm-act"
LITELLM_HOST_PORT="4401"  # avoid colliding with normal :4000
LITELLM_IMAGE="${LITELLM_IMAGE:-litellm:streaming-action-protocol}"
ANON_IMAGE="anonymizer-guardrail:test"
FAKE_LLM_IMAGE="fake-llm:test"
MASTER_KEY="sk-streaming-test"
TEST_EMAIL="alice@corp.example"
# Long-ish prompt — fake-llm splits content on whitespace boundaries
# when streaming, producing ~20 upstream chunks for this prompt. With
# UnifiedLLMGuardrails' default streaming_sampling_rate=5, that gives
# ~4 mid-stream sample points + the EOS flush.
TEST_PROMPT="Please summarise everything you know about ${TEST_EMAIL} including their role, recent activity, organization, and any other publicly known background information."

MODE="all"
KEEP=false
TRACE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      shift
      MODE="${1:-}"
      case "${MODE}" in
        action|chunk_straddle|all) ;;
        *) echo "invalid --mode: ${MODE} (expected: action|chunk_straddle|all)" >&2; exit 2 ;;
      esac
      ;;
    --keep) KEEP=true ;;
    --trace) TRACE=true ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;34m[test]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

RESPONSE_FILE=""

teardown() {
  if [[ -n "${RESPONSE_FILE}" ]]; then
    rm -f "${RESPONSE_FILE}"
  fi
  if [[ "${KEEP}" == "true" ]]; then
    log "leaving containers running (--keep). To stop manually:"
    log "  podman stop ${LITELLM_NAME} ${ANON_NAME} ${FAKE_NAME}"
    log "  podman rm   ${LITELLM_NAME} ${ANON_NAME} ${FAKE_NAME}"
    log "  podman network rm ${NETWORK_NAME}"
    return
  fi
  log "tearing down…"
  for c in "${LITELLM_NAME}" "${ANON_NAME}" "${FAKE_NAME}"; do
    podman rm -f "${c}" >/dev/null 2>&1 || true
  done
  podman network rm "${NETWORK_NAME}" >/dev/null 2>&1 || true
}
trap teardown EXIT

# ── Image preflight ───────────────────────────────────────────────────

require_image() {
  local image="$1"
  if ! podman image exists "${image}"; then
    fail "image '${image}' not found in podman storage. Build it first."
  fi
  ok "image present: ${image}"
}

build_image() {
  local image="$1"; local containerfile="$2"; local context="$3"
  if podman image exists "${image}"; then
    log "image already built: ${image} (skipping rebuild)"
    return
  fi
  log "building ${image} from ${containerfile}…"
  podman build -t "${image}" -f "${containerfile}" "${context}"
  ok "built: ${image}"
}

require_image "${LITELLM_IMAGE}"
build_image "${ANON_IMAGE}"     "Containerfile"                    "."
build_image "${FAKE_LLM_IMAGE}" "services/fake_llm/Containerfile"  "services/fake_llm"

# ── Network + persistent backing services ────────────────────────────

if ! podman network exists "${NETWORK_NAME}"; then
  log "creating podman network ${NETWORK_NAME}"
  podman network create "${NETWORK_NAME}" >/dev/null
fi

for c in "${LITELLM_NAME}" "${ANON_NAME}" "${FAKE_NAME}"; do
  podman rm -f "${c}" >/dev/null 2>&1 || true
done

log "starting fake-llm…"
podman run -d --rm \
  --name "${FAKE_NAME}" \
  --network "${NETWORK_NAME}" \
  --network-alias "fake-llm" \
  -v "${REPO_ROOT}/tests/streaming_e2e/fake_llm_rules.yaml:/app/rules.yaml:ro,Z" \
  "${FAKE_LLM_IMAGE}" >/dev/null

log "starting anonymizer guardrail (regex detector only, USE_FAKER=false)…"
# USE_FAKER=false forces opaque-token surrogates ([EMAIL_ADDRESS_…])
# instead of realistic-name Faker output. The chunk_straddle assertion
# checks the surrogate-shape regex; opaque tokens are deterministic.
podman run -d --rm \
  --name "${ANON_NAME}" \
  --network "${NETWORK_NAME}" \
  --network-alias "anonguard" \
  -e DETECTOR_MODE=regex \
  -e USE_FAKER=false \
  -e LOG_LEVEL=info \
  "${ANON_IMAGE}" >/dev/null

# ── Test driver ──────────────────────────────────────────────────────

wait_for_url() {
  local url="$1"; local name="$2"; local timeout="${3:-60}"
  local elapsed=0
  while ! curl -fsS -m 1 "${url}" >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if (( elapsed >= timeout )); then
      log "$(podman logs --tail 30 ${name} 2>&1 || true)"
      fail "${name} did not become ready at ${url} within ${timeout}s"
    fi
  done
  ok "${name} ready (${url})"
}

assemble_streamed_content() {
  awk '/^data: /{ sub(/^data: /,""); print }' "$1" \
    | grep -v '^\[DONE\]$' \
    | python3 -c '
import json, sys
out = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    for ch in ev.get("choices", []):
        delta = ch.get("delta") or {}
        c = delta.get("content")
        if isinstance(c, str):
            out.append(c)
print("".join(out))
'
}

run_arm() {
  # Run one arm of the test against the litellm proxy started with the
  # given config file. Cycles the litellm container so the new config
  # is loaded; backing services (fake-llm, anonguard) are persistent.
  #   $1: arm name (action | chunk_straddle)
  #   $2: litellm config file (relative to REPO_ROOT)
  #   $3: prompt to send (the user message)
  local arm="$1"; local config="$2"; local prompt="$3"
  log "── arm: ${arm} ── config: ${config}"
  log "── prompt: ${prompt}"

  podman rm -f "${LITELLM_NAME}" >/dev/null 2>&1 || true
  # Optional dev override: bind-mount local litellm sources over the
  # in-image installed copies so a podman rebuild isn't needed for
  # iteration. Set LITELLM_SRC=~/litellm to enable.
  local extra_mounts=()
  if [[ -n "${LITELLM_SRC:-}" ]]; then
    extra_mounts+=(
      -v "${LITELLM_SRC}/litellm/proxy/guardrails/guardrail_hooks/unified_guardrail/unified_guardrail.py:/app/.venv/lib/python3.13/site-packages/litellm/proxy/guardrails/guardrail_hooks/unified_guardrail/unified_guardrail.py:ro,Z"
      -v "${LITELLM_SRC}/litellm/proxy/guardrails/guardrail_hooks/generic_guardrail_api/generic_guardrail_api.py:/app/.venv/lib/python3.13/site-packages/litellm/proxy/guardrails/guardrail_hooks/generic_guardrail_api/generic_guardrail_api.py:ro,Z"
      -v "${LITELLM_SRC}/litellm/proxy/guardrails/guardrail_hooks/generic_guardrail_api/__init__.py:/app/.venv/lib/python3.13/site-packages/litellm/proxy/guardrails/guardrail_hooks/generic_guardrail_api/__init__.py:ro,Z"
      -v "${LITELLM_SRC}/litellm/types/proxy/guardrails/guardrail_hooks/generic_guardrail_api.py:/app/.venv/lib/python3.13/site-packages/litellm/types/proxy/guardrails/guardrail_hooks/generic_guardrail_api.py:ro,Z"
      -v "${LITELLM_SRC}/litellm/types/utils.py:/app/.venv/lib/python3.13/site-packages/litellm/types/utils.py:ro,Z"
    )
  fi
  podman run -d --rm \
    --name "${LITELLM_NAME}" \
    --network "${NETWORK_NAME}" \
    --network-alias "litellm" \
    -p "${LITELLM_HOST_PORT}:4000" \
    -v "${REPO_ROOT}/${config}:/app/config.yaml:ro,Z" \
    "${extra_mounts[@]}" \
    -e LITELLM_MASTER_KEY="${MASTER_KEY}" \
    "${LITELLM_IMAGE}" \
    --config /app/config.yaml --port 4000 >/dev/null

  wait_for_url "http://localhost:${LITELLM_HOST_PORT}/health/liveliness" "${LITELLM_NAME}" 60

  log "sending streaming chat completion with PII (${TEST_EMAIL})…"
  RESPONSE_FILE="$(mktemp)"

  local body
  body="$(python3 -c 'import json, sys; print(json.dumps({"model":"test_filter","stream":True,"messages":[{"role":"user","content":sys.argv[1]}]}))' "${prompt}")"

  curl -fsS -N \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -X POST "http://localhost:${LITELLM_HOST_PORT}/chat/completions" \
    -d "${body}" > "${RESPONSE_FILE}"

  local assembled
  assembled="$(assemble_streamed_content "${RESPONSE_FILE}")"
  log "assembled streamed content: ${assembled}"

  log "per-chunk content (in order):"
  awk '/^data: /{ sub(/^data: /,""); print }' "${RESPONSE_FILE}" \
    | grep -v '^\[DONE\]$' \
    | python3 -c '
import json, sys
idx = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    for ch in ev.get("choices", []):
        delta = ch.get("delta") or {}
        c = delta.get("content")
        if isinstance(c, str) and c:
            print(f"  [{idx:2d}] {json.dumps(c)}")
            idx += 1
' >&2

  local content_events
  content_events="$(
    awk '/^data: /{ sub(/^data: /,""); print }' "${RESPONSE_FILE}" \
      | grep -v '^\[DONE\]$' \
      | python3 -c '
import json, sys
n = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    for ch in ev.get("choices", []):
        delta = ch.get("delta") or {}
        c = delta.get("content")
        if isinstance(c, str) and c:
            n += 1
print(n)
'
  )"
  log "content-bearing SSE events: ${content_events}"
  if (( content_events < 2 )); then
    fail "expected multiple streamed content events (≥2), got ${content_events}. The response may not actually be streaming."
  else
    ok "received ${content_events} content-bearing SSE events (multi-chunk streaming confirmed)"
  fi

  case "${arm}" in
    action)
      # Action mode: deanonymized email reaches the client; no surrogate
      # leaks; no partial-surrogate fragment leaks.
      if [[ "${assembled}" == *"${TEST_EMAIL}"* ]]; then
        ok "action: client received the original email"
      else
        fail "action: client did NOT receive the original email. Got: ${assembled}"
      fi
      if [[ "${assembled}" =~ \[EMAIL_ADDRESS_[A-F0-9]+\] ]]; then
        fail "action: full surrogate leaked into client output. Got: ${assembled}"
      else
        ok "action: no surrogate in client output"
      fi
      local partial_re='\[EMAIL[A-Z_0-9]*'
      if [[ "${assembled}" =~ ${partial_re} ]]; then
        fail "action: partial-surrogate fragment leaked (matched: '${BASH_REMATCH[0]}'). Got: ${assembled}"
      else
        ok "action: no partial-surrogate fragment leaked"
      fi
      ;;
    chunk_straddle)
      # Same expected outcome as action arm: deanonymized email reaches
      # the client, no surrogate leaks. The straddling case exercises
      # the anonymizer's WAIT-on-partial-surrogate path because the
      # fake-llm splits content into 7-char chunks (rule
      # `chunk_chars: 7`) — surrogate tokens span multiple chunks.
      if [[ "${assembled}" == *"${TEST_EMAIL}"* ]]; then
        ok "chunk_straddle: client received the original email despite surrogate spanning chunks"
      else
        fail "chunk_straddle: client did NOT receive the original email. Got: ${assembled}"
      fi
      if [[ "${assembled}" =~ \[EMAIL_ADDRESS_[A-F0-9]+\] ]]; then
        fail "chunk_straddle: full surrogate leaked into client output. Got: ${assembled}"
      else
        ok "chunk_straddle: no full surrogate token leaked through"
      fi
      local partial_re='\[EMAIL[A-Z_0-9]*'
      if [[ "${assembled}" =~ ${partial_re} ]]; then
        fail "chunk_straddle: partial-surrogate fragment leaked (matched: '${BASH_REMATCH[0]}'). Got: ${assembled}"
      else
        ok "chunk_straddle: no partial-surrogate fragment leaked (WAIT-on-partial-surrogate worked)"
      fi
      ;;
  esac

  if [[ "${TRACE}" == "true" ]]; then
    log "guardrail-trace (one line per anonymizer call):"
    # Filter for the structured log line emitted by main.py's _trace_call.
    # `--since 30s` keeps the dump scoped to this arm — backing services
    # are persistent across arms so without it we'd repeat earlier arms.
    # The Python decoder is wrapped in a quoted heredoc so we can use
    # single-quoted dict keys without bash chewing them up.
    podman logs --since 30s "${ANON_NAME}" 2>&1 \
      | grep -F "guardrail-trace" \
      | python3 -c "$(cat <<'PY'
import re, sys
# Decompose the flat key=value… repr=… line into a per-call block so the
# trailing-edge text and the action are easy to scan.
pat = re.compile(
    r"guardrail-trace call_id=(?P<id>\S+) type=(?P<type>\S+) "
    r"is_final=(?P<final>\S+) action=(?P<action>\S+) "
    r"in=(?P<in>.*?) out=(?P<out>.*)$"
)
for n, line in enumerate(sys.stdin, 1):
    m = pat.search(line)
    if not m:
        continue
    g = m.groupdict()
    print(f"  [{n:2d}] type={g['type']:8s} is_final={g['final']:5s} action={g['action']}")
    print(f"        in:  {g['in']}")
    if g['out'] != "''":
        print(f"        out: {g['out']}")
PY
)" >&2
  fi

  rm -f "${RESPONSE_FILE}"
  RESPONSE_FILE=""
  ok "arm ${arm} PASSED"
}

# ── Dispatch ──────────────────────────────────────────────────────────

# `split:` prefix triggers the chunk_chars=7 rule in fake-llm, which
# fragments surrogate tokens across upstream chunks (see
# tests/streaming_e2e/fake_llm_rules.yaml). The plain prompt uses
# whitespace-boundary chunks (each word a single upstream chunk).
PROMPT_PLAIN="${TEST_PROMPT}"
PROMPT_STRADDLE="split:${TEST_PROMPT}"

case "${MODE}" in
  action)
    run_arm "action"         "tests/streaming_e2e/litellm.config.yaml" "${PROMPT_PLAIN}"
    ;;
  chunk_straddle)
    run_arm "chunk_straddle" "tests/streaming_e2e/litellm.config.yaml" "${PROMPT_STRADDLE}"
    ;;
  all)
    run_arm "action"         "tests/streaming_e2e/litellm.config.yaml" "${PROMPT_PLAIN}"
    run_arm "chunk_straddle" "tests/streaming_e2e/litellm.config.yaml" "${PROMPT_STRADDLE}"
    ;;
esac

ok "streaming action-protocol end-to-end test PASSED (mode=${MODE})"
