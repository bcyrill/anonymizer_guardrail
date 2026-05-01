#!/usr/bin/env bash
# Run the curl examples from docs/examples.md against a running guardrail
# container and assert the basic invariants per test:
#
#   - "should anonymize" examples → action=GUARDRAIL_INTERVENED, the
#     original PII substring is gone from the response text.
#   - "no match" example → action=NONE.
#   - Round-trip → anonymize, then deanonymize the resulting text
#     under the same litellm_call_id, expect the original back.
#
# Tests whose required detection layer isn't in the running guardrail's
# DETECTOR_MODE are skipped (not failed) so this script can run against
# slim/regex-only setups too.
#
# Two modes:
#   default            connect to $BASE_URL (caller manages the guardrail).
#   --preset NAME      spawn a fresh test guardrail via scripts/cli.sh
#                      (in the background), run the tests, tear it down
#                      on exit. Uses non-default ports/names so it
#                      doesn't fight your regular containers.
#
# Exits 0 only when every executed test passed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

BASE_URL="${BASE_URL:-http://localhost:8000}"
PRESET=""
KEEP=false
TEST_PORT=8001
TEST_NAME="anonymizer-test"
TEST_LOG="/tmp/anonymizer-test-guardrail.log"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--preset NAME [--port N] [--keep]] [-h]

Run the curl examples from docs/examples.md.

Without --preset:
  Connects to \$BASE_URL (default http://localhost:8000) — assumes the
  guardrail is already running.

With --preset NAME:
  Boots a self-contained test environment by invoking
  scripts/cli.sh --preset NAME on port ${TEST_PORT} (--port to override)
  with container name "${TEST_NAME}", waits for /health, runs the
  tests, then stops it. Skips teardown when --keep is set.

Tests whose required detection layer isn't enabled are skipped.

Options:
  --preset NAME   Bundled cli.sh preset (uuid-debug | pentest | regex-only).
  --port N        Host port for the test guardrail (default: ${TEST_PORT}).
  --keep          Don't tear down the test guardrail on exit.
  -h, --help      Show this help.

Env:
  BASE_URL        URL of the guardrail (only used when --preset is NOT set).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --preset) shift; PRESET="${1:-}"; [[ -z "$PRESET" ]] && { usage; exit 1; }; shift ;;
    --port)   shift; TEST_PORT="${1:-}"; shift ;;
    --keep)   KEEP=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage; exit 1 ;;
  esac
done

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'
c_dim=$'\033[2m'; c_bld=$'\033[1m'; c_rst=$'\033[0m'

PASS=0; FAIL=0; SKIP=0
say()  { printf '%s\n' "$*"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_rst" >&2; }
pass() { printf '  %s✓%s %s\n' "$c_grn" "$c_rst" "$1"; PASS=$((PASS+1)); }
ffail() { printf '  %s✗%s %s%s\n' "$c_red" "$c_rst" "$1" "${2:+: $2}"; FAIL=$((FAIL+1)); }
skip() { printf '  %s—%s %s%s\n' "$c_ylw" "$c_rst" "$1" "${2:+ ($2)}"; SKIP=$((SKIP+1)); }

# ── HTTP + JSON helpers ─────────────────────────────────────────────────────
post() {
  curl -sS --fail-with-body -X POST \
    "$BASE_URL/beta/litellm_basic_guardrail_api" \
    -H 'Content-Type: application/json' \
    --data-raw "$1"
}

# Build a single-text guardrail request body. Args: text input_type call_id.
mkbody() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys
text, input_type, call_id = sys.argv[1:4]
body = {"texts": [text], "input_type": input_type}
if call_id:
    body["litellm_call_id"] = call_id
print(json.dumps(body))
PY
}

# Multi-text variant. Args: input_type call_id text [text...].
mkbody_multi() {
  python3 - "$@" <<'PY'
import json, sys
input_type, call_id, *texts = sys.argv[1:]
body = {"texts": texts, "input_type": input_type}
if call_id:
    body["litellm_call_id"] = call_id
print(json.dumps(body))
PY
}

# Read $1 path from JSON on stdin. Path is Python subscript form, e.g.
#   jget '["action"]'    →  "GUARDRAIL_INTERVENED"
#   jget '["texts"][0]'  →  the first response text
jget() {
  python3 -c "import json,sys; print(json.load(sys.stdin)$1)"
}

# Same, with .get(key, default) — for fields that may be absent (e.g.
# blocked_reason on non-BLOCKED responses).
jgetdef() {
  python3 -c "import json,sys; print(json.load(sys.stdin).get($1))"
}

# ── Generic test runners ────────────────────────────────────────────────────
# Expect anonymization: action=GUARDRAIL_INTERVENED and texts[0] != original.
# Variadic 3rd+ args: substrings that MUST each be absent from the response
# (the specific PII pieces we expect to be redacted). Each is checked
# independently; the test fails listing the first one still present, so
# a four-secret payload with one missed credential reports exactly which.
expect_intervened() {
  local name="$1" text="$2"
  shift 2
  local body resp action mod
  body=$(mkbody "$text" request "$name")
  resp=$(post "$body" 2>&1) || { ffail "$name" "HTTP error: $resp"; return; }
  action=$(printf '%s' "$resp" | jget '["action"]')
  case "$action" in
    GUARDRAIL_INTERVENED)
      mod=$(printf '%s' "$resp" | jget '["texts"][0]')
      if [[ "$mod" == "$text" ]]; then
        ffail "$name" "intervened but text unchanged"
        return
      fi
      local f
      for f in "$@"; do
        [[ -z "$f" ]] && continue
        if [[ "$mod" == *"$f"* ]]; then
          ffail "$name" "PII '$f' still in response"
          return
        fi
      done
      pass "$name"
      ;;
    NONE)
      ffail "$name" "no detection — expected intervention" ;;
    BLOCKED)
      ffail "$name" "BLOCKED: $(printf '%s' "$resp" | jgetdef '"blocked_reason", ""')" ;;
    *)
      ffail "$name" "unexpected action '$action'" ;;
  esac
}

expect_none() {
  local name="$1" text="$2"
  local body resp action
  body=$(mkbody "$text" request "$name")
  resp=$(post "$body" 2>&1) || { ffail "$name" "HTTP error: $resp"; return; }
  action=$(printf '%s' "$resp" | jget '["action"]')
  if [[ "$action" == "NONE" ]]; then
    pass "$name"
  else
    ffail "$name" "expected NONE, got $action"
  fi
}

# ── Engine detection (only used by the cleanup hook below) ──────────────────
detect_engine() {
  if [[ -n "${ENGINE:-}" ]]; then return; fi
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  fi
}

# ── Optional: spin up a test guardrail via cli.sh ──────────────────────────
# We background cli.sh and watch /health on the chosen port. cli.sh
# inherits a clean SIGTERM handler from _lib.sh that stops the
# auto-started fake-llm — so a plain `kill` of the cli.sh PID tears
# the whole stack down.
GUARDRAIL_PID=""
TEARDOWN_REGISTERED=false

start_test_guardrail() {
  detect_engine
  BASE_URL="http://localhost:${TEST_PORT}"
  say ""
  say "${c_bld}Setting up test environment via cli.sh --preset ${PRESET}${c_rst}"
  say "  port=${TEST_PORT}  name=${TEST_NAME}  log=${TEST_LOG}"

  # `--replace` so a leftover container from a previous run doesn't
  # error us out. Output goes to a log file because dialog/run output
  # is noisy and we want clean test output here.
  "$SCRIPT_DIR/cli.sh" --preset "$PRESET" \
    --port "$TEST_PORT" --name "$TEST_NAME" --replace \
    > "$TEST_LOG" 2>&1 &
  GUARDRAIL_PID=$!
  TEARDOWN_REGISTERED=true
  trap stop_test_guardrail EXIT

  local i
  for i in $(seq 1 90); do
    if curl -sf "$BASE_URL/health" >/dev/null 2>&1; then
      say "  ${c_grn}ready${c_rst} after ~${i}s"
      return 0
    fi
    if ! kill -0 "$GUARDRAIL_PID" 2>/dev/null; then
      err "cli.sh exited before guardrail came up. Tail of $TEST_LOG:"
      tail -30 "$TEST_LOG" >&2
      exit 1
    fi
    sleep 1
  done
  err "Guardrail did not become ready within 90s. Tail of $TEST_LOG:"
  tail -30 "$TEST_LOG" >&2
  exit 1
}

stop_test_guardrail() {
  [[ "$TEARDOWN_REGISTERED" != "true" ]] && return
  if [[ "$KEEP" == "true" ]]; then
    say ""
    say "${c_dim}--keep set; leaving guardrail running at ${BASE_URL} (PID ${GUARDRAIL_PID}). Stop with:${c_rst}"
    say "${c_dim}  kill ${GUARDRAIL_PID}  ||  ${ENGINE:-podman} stop ${TEST_NAME}${c_rst}"
    return
  fi
  say ""
  say "${c_dim}Stopping test guardrail...${c_rst}"
  if [[ -n "$GUARDRAIL_PID" ]] && kill -0 "$GUARDRAIL_PID" 2>/dev/null; then
    kill "$GUARDRAIL_PID" 2>/dev/null || true
    # Give cli.sh a moment to run its EXIT trap (which stops fake-llm).
    wait "$GUARDRAIL_PID" 2>/dev/null || true
  fi
  # Belt-and-braces if the signal got lost.
  if [[ -n "${ENGINE:-}" ]]; then
    "$ENGINE" rm -f "$TEST_NAME" >/dev/null 2>&1 || true
    "$ENGINE" rm -f fake-llm >/dev/null 2>&1 || true
  fi
}

if [[ -n "$PRESET" ]]; then
  start_test_guardrail
fi

# ── Connect + introspect ────────────────────────────────────────────────────
say ""
say "${c_bld}Connecting to $BASE_URL${c_rst}"
health=$(curl -sS "$BASE_URL/health" 2>&1) || {
  err "Cannot reach $BASE_URL/health: $health"
  exit 1
}
status=$(printf '%s' "$health" | jget '["status"]') || {
  err "/health returned non-JSON or unexpected shape: $health"
  exit 1
}
if [[ "$status" != "ok" ]]; then
  err "/health status is '$status', not 'ok'"
  exit 1
fi
detector_mode=$(printf '%s' "$health" | jget '["detector_mode"]')
say "  detector_mode = ${c_grn}${detector_mode}${c_rst}"

has_regex=false; has_llm=false; has_pf=false
[[ ",${detector_mode}," == *",regex,"* ]]          && has_regex=true
[[ ",${detector_mode}," == *",llm,"* ]]            && has_llm=true
[[ ",${detector_mode}," == *",privacy_filter,"* ]] && has_pf=true

# ── 1. Health ──
say ""
say "${c_bld}1. Health${c_rst}"
pass "GET /health → status: ok"

# ── 2. Anonymize / deanonymize round-trip ──
say ""
say "${c_bld}2. Anonymize / deanonymize round-trip${c_rst}"
ORIG="Email me at alice.smith@example.com about ticket #42."
CALL_ID="rt-$$"
B1=$(mkbody "$ORIG" request "$CALL_ID")
R1=$(post "$B1" 2>&1)
ACTION1=$(printf '%s' "$R1" | jget '["action"]')
MOD1=$(printf '%s' "$R1" | jget '["texts"][0]')
if [[ "$ACTION1" != "GUARDRAIL_INTERVENED" ]]; then
  ffail "anonymize" "expected GUARDRAIL_INTERVENED, got $ACTION1"
elif [[ "$MOD1" == "$ORIG" ]]; then
  ffail "anonymize" "text unchanged"
elif [[ "$MOD1" == *"alice.smith@example.com"* ]]; then
  ffail "anonymize" "PII still in response"
else
  pass "anonymize"
  B2=$(mkbody "$MOD1" response "$CALL_ID")
  R2=$(post "$B2" 2>&1)
  RESTORED=$(printf '%s' "$R2" | jget '["texts"][0]')
  if [[ "$RESTORED" == "$ORIG" ]]; then
    pass "deanonymize → original restored"
  else
    ffail "deanonymize" "got '$RESTORED'"
  fi
fi

# ── 3. No match → action: NONE ──
say ""
say "${c_bld}3. No matches → action NONE${c_rst}"
expect_none "no-pii" "What is the capital of France?"

# ── 4. Regex layer ──
say ""
say "${c_bld}4. Regex layer${c_rst}"
if $has_regex; then
  expect_intervened "email+phone" \
    "Reach me at jane.doe+work@corp.example or +1-415-555-0142." \
    "jane.doe+work@corp.example" "+1-415-555-0142"

  expect_intervened "ip+cidr+host" \
    "The cluster runs on 10.0.42.7 inside 10.0.0.0/16, fronted by api.acme.internal." \
    "10.0.42.7" "10.0.0.0/16" "api.acme.internal"

  expect_intervened "cloud-creds" \
    "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz OPENAI_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWx SLACK_TOKEN=xoxb-1234567890-0987654321-aBcDeFgHiJkLmNoPqRsTuVwX" \
    "AKIAIOSFODNN7EXAMPLE" \
    "ghp_1234567890abcdefghijklmnopqrstuvwxyz" \
    "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx" \
    "xoxb-1234567890-0987654321-aBcDeFgHiJkLmNoPqRsTuVwX"

  # JWT regex matches the full three-segment string. Assert all three
  # segments are individually gone — checking just the header would
  # miss a partial-redaction bug.
  expect_intervened "jwt" \
    "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" \
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" \
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0" \
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"

  expect_intervened "hashes" \
    "MD5 5d41402abc4b2a76b9719d911017c592, SHA-1 aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d, SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855." \
    "5d41402abc4b2a76b9719d911017c592" \
    "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d" \
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

  expect_intervened "uuid" \
    "Order id 550e8400-e29b-41d4-a716-446655440000 failed." \
    "550e8400-e29b-41d4-a716-446655440000"

  expect_intervened "card+iban" \
    "Card 4111-1111-1111-1111 charged to IBAN DE89370400440532013000." \
    "4111-1111-1111-1111" "DE89370400440532013000"

  # DOB pattern uses a capture group → only the date part is redacted,
  # the "DOB:" label stays. So we assert the date is gone.
  expect_intervened "dob" \
    "DOB: 1985-04-15. Patient lookup pending." \
    "1985-04-15"
else
  skip "regex layer tests" "regex not in DETECTOR_MODE"
fi

# ── 5. Privacy-filter (NER) ──
say ""
say "${c_bld}5. Privacy-filter — NER${c_rst}"
if $has_pf; then
  expect_intervened "ner-person+address" \
    "Alice Smith lives at 123 Main Street, Springfield, and her DOB is 15 April 1985." \
    "Alice Smith"
else
  skip "privacy-filter test" "privacy_filter not in DETECTOR_MODE"
fi

# ── 6. LLM detector ──
say ""
say "${c_bld}6. LLM detector${c_rst}"
if $has_llm; then
  # Don't assert a specific PII substring — what the LLM extracts depends
  # on the model + prompt. fake-llm + bundled rules.example.yaml will
  # catch AcmeCorp; a real LLM may pick AcmeCorp / Project Zephyr /
  # Northstar in any combination. Pass condition: action=INTERVENED and
  # the text changed.
  expect_intervened "llm-orgs" \
    "AcmeCorp engineering is rolling out Project Zephyr to the Northstar customer fleet next quarter."
else
  skip "LLM detector test" "llm not in DETECTOR_MODE"
fi

# ── 7. Multi-text batch (cross-entry surrogate consistency) ──
say ""
say "${c_bld}7. Multi-text batch${c_rst}"
if $has_regex; then
  CID="mt-$$"
  B=$(mkbody_multi request "$CID" \
    "You are a helpful assistant. The current user is bob@corp.example." \
    "Hi, can you summarize my last email exchange with bob@corp.example?")
  R=$(post "$B" 2>&1)
  A=$(printf '%s' "$R" | jget '["action"]')
  T0=$(printf '%s' "$R" | jget '["texts"][0]')
  T1=$(printf '%s' "$R" | jget '["texts"][1]')
  if [[ "$A" != "GUARDRAIL_INTERVENED" ]]; then
    ffail "multi-text" "got action $A"
  elif [[ "$T0" == *"bob@corp.example"* ]] || [[ "$T1" == *"bob@corp.example"* ]]; then
    ffail "multi-text" "PII still present in one of the texts"
  else
    # Pull the email-shaped surrogate (Faker) or [EMAIL_ADDRESS_…] token
    # from each modified text. They should be the same — that's the
    # cross-text consistency invariant.
    re='\[EMAIL_ADDRESS_[0-9A-Fa-f]+\]|[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
    s0=$(printf '%s' "$T0" | grep -oE "$re" | head -1)
    s1=$(printf '%s' "$T1" | grep -oE "$re" | head -1)
    if [[ -n "$s0" && "$s0" == "$s1" ]]; then
      pass "multi-text → same surrogate '$s0' across both texts"
    elif [[ -n "$s0" || -n "$s1" ]]; then
      ffail "multi-text" "surrogates differ across texts: '$s0' vs '$s1'"
    else
      # Couldn't extract a surrogate via the regex — soft-pass since the
      # action+removal already matched. This happens with exotic locales
      # where Faker produces non-ASCII addresses we don't pattern-match.
      pass "multi-text (action OK; surrogate extraction skipped)"
    fi
  fi
else
  skip "multi-text test" "regex not in DETECTOR_MODE"
fi

# ── 8. Kitchen-sink ──
say ""
say "${c_bld}8. Kitchen-sink request${c_rst}"
# Only assert PII guaranteed to be caught by the regex layer. The
# privacy-filter and LLM detectors are context-dependent — e.g.
# privacy-filter has been observed dropping a `.example`-TLD email
# in dense prose even though it catches the same email in isolation.
# Skip the whole section when regex isn't in DETECTOR_MODE; sections
# 5 and 6 already cover what pf / llm contribute on their own inputs.
if $has_regex; then
  KS="Incident report: AcmeCorp engineer Alice Smith (alice.smith@acme.example, +1-415-555-0142, DOB: 1985-04-15) noticed unusual traffic from 10.0.42.7 to api.acme.internal at 03:14 UTC. The attacker appears to have lifted GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz from a public Gist. Affected resource id 550e8400-e29b-41d4-a716-446655440000. Card 4111-1111-1111-1111 was used on the linked account. Suspected internal codename: Project Zephyr."
  expect_intervened "kitchen-sink" "$KS" \
    "alice.smith@acme.example" \
    "+1-415-555-0142" \
    "1985-04-15" \
    "10.0.42.7" \
    "api.acme.internal" \
    "ghp_1234567890abcdefghijklmnopqrstuvwxyz" \
    "550e8400-e29b-41d4-a716-446655440000" \
    "4111-1111-1111-1111"
else
  skip "kitchen-sink test" "regex not in DETECTOR_MODE"
fi

# ── Summary ──
say ""
TOTAL=$((PASS+FAIL+SKIP))
say "${c_bld}Summary:${c_rst} ${c_grn}${PASS} passed${c_rst}, ${c_red}${FAIL} failed${c_rst}, ${c_ylw}${SKIP} skipped${c_rst} of $TOTAL"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
