#!/usr/bin/env bash
# End-to-end tests for the per-request override mechanism
# (additional_provider_specific_params).
#
# Each test sends a guardrail request with overrides set and asserts
# the response shape. Tests that require a specific feature are
# skipped when the running guardrail's DETECTOR_MODE doesn't include
# the relevant detector.
#
# Default preset is `uuid-debug` (default flavour + regex,llm + fake-llm,
# USE_FAKER=false at startup). The override tests then flip these
# per call to verify each path.
#
# Usage:
#   scripts/test-overrides.sh                       # against $BASE_URL
#   scripts/test-overrides.sh --preset uuid-debug   # self-host

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

BASE_URL="${BASE_URL:-http://localhost:8000}"
PRESET=""
KEEP=false
TEST_PORT=8001
TEST_NAME="anonymizer-test"
TEST_LOG="/tmp/anonymizer-test-overrides.log"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--preset NAME [--port N] [--keep]] [-h]

Run the per-request override tests.

Without --preset:
  Connects to \$BASE_URL (default http://localhost:8000).

With --preset NAME:
  Boots a self-contained guardrail via scripts/launcher.sh, runs the
  tests, tears it down. Default port ${TEST_PORT}, container name "${TEST_NAME}".

Options:
  --preset NAME   uuid-debug (recommended) | pentest | regex-only.
  --port N        Host port (default ${TEST_PORT}).
  --keep          Don't tear down on exit.
  -h, --help      Show this help.
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

post() {
  curl -sS -X POST "$BASE_URL/beta/litellm_basic_guardrail_api" \
    -H 'Content-Type: application/json' \
    --data-raw "$1"
}

# Build a request body with optional overrides. Args:
#   $1  text (string)
#   $2  call_id
#   $3  json overrides dict (string), e.g. '{"use_faker": false}' — or empty
mkbody() {
  python3 - "$1" "$2" "${3:-}" <<'PY'
import json, sys
text, call_id, overrides_raw = sys.argv[1:4]
body = {"texts": [text], "input_type": "request"}
if call_id:
    body["litellm_call_id"] = call_id
if overrides_raw:
    body["additional_provider_specific_params"] = json.loads(overrides_raw)
print(json.dumps(body))
PY
}

jget() {
  python3 -c "import json,sys; print(json.load(sys.stdin)$1)"
}

jgetdef() {
  python3 -c "import json,sys; print(json.load(sys.stdin).get($1))"
}

# ── Engine + container lifecycle (same as test-examples.sh) ─────────────────
detect_engine() {
  if [[ -n "${ENGINE:-}" ]]; then return; fi
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  fi
}

GUARDRAIL_PID=""
TEARDOWN_REGISTERED=false

start_test_guardrail() {
  detect_engine
  BASE_URL="http://localhost:${TEST_PORT}"
  say ""
  say "${c_bld}Setting up test environment via launcher.sh --preset ${PRESET}${c_rst}"
  say "  port=${TEST_PORT}  name=${TEST_NAME}  log=${TEST_LOG}"
  # Inject the named-alternative registries via launcher.sh's `--` passthrough.
  # Tests for regex_patterns / llm_prompt then reference these names.
  # `pentest` is a name we made up; the operator picks any name they want.
  "$SCRIPT_DIR/launcher.sh" --preset "$PRESET" \
    --port "$TEST_PORT" --name "$TEST_NAME" --replace \
    -- \
    -e "REGEX_PATTERNS_REGISTRY=pentest=bundled:regex_pentest.yaml" \
    -e "LLM_SYSTEM_PROMPT_REGISTRY=pentest=bundled:llm_pentest.md" \
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
      err "launcher.sh exited before guardrail came up. Tail of $TEST_LOG:"
      tail -30 "$TEST_LOG" >&2
      exit 1
    fi
    sleep 1
  done
  err "Guardrail did not become ready within 90s."
  tail -30 "$TEST_LOG" >&2
  exit 1
}

stop_test_guardrail() {
  [[ "$TEARDOWN_REGISTERED" != "true" ]] && return
  if [[ "$KEEP" == "true" ]]; then
    say ""
    say "${c_dim}--keep set; leaving guardrail running at ${BASE_URL} (PID ${GUARDRAIL_PID})${c_rst}"
    return
  fi
  say ""
  say "${c_dim}Stopping test guardrail...${c_rst}"
  if [[ -n "$GUARDRAIL_PID" ]] && kill -0 "$GUARDRAIL_PID" 2>/dev/null; then
    kill "$GUARDRAIL_PID" 2>/dev/null || true
    wait "$GUARDRAIL_PID" 2>/dev/null || true
  fi
  if [[ -n "${ENGINE:-}" ]]; then
    "$ENGINE" rm -f "$TEST_NAME" >/dev/null 2>&1 || true
    "$ENGINE" rm -f fake-llm >/dev/null 2>&1 || true
  fi
}

if [[ -n "$PRESET" ]]; then
  start_test_guardrail
fi

# ── Connect + capability detect ─────────────────────────────────────────────
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
[[ "$status" == "ok" ]] || { err "/health status is '$status'"; exit 1; }
detector_mode=$(printf '%s' "$health" | jget '["detector_mode"]')
say "  detector_mode = ${c_grn}${detector_mode}${c_rst}"

has_regex=false; has_llm=false; has_pf=false
[[ ",${detector_mode}," == *",regex,"* ]]          && has_regex=true
[[ ",${detector_mode}," == *",llm,"* ]]            && has_llm=true
[[ ",${detector_mode}," == *",privacy_filter,"* ]] && has_pf=true

# ── 1. Baseline: no overrides → request works ──────────────────────────────
say ""
say "${c_bld}1. Baseline (no overrides)${c_rst}"
r=$(post "$(mkbody 'Email me at alice@example.com.' 'baseline-1' '')")
a=$(printf '%s' "$r" | jget '["action"]')
m=$(printf '%s' "$r" | jget '["texts"][0]')
if [[ "$a" == "GUARDRAIL_INTERVENED" && "$m" != *"alice@example.com"* ]]; then
  pass "default config anonymizes the email"
else
  ffail "baseline" "action=$a, response='$m'"
fi

# ── 2. use_faker override ──────────────────────────────────────────────────
say ""
say "${c_bld}2. use_faker override${c_rst}"

# uuid-debug preset starts with USE_FAKER=false. Override to true should
# produce a realistic surrogate (no [EMAIL_ADDRESS_…] opaque marker).
r=$(post "$(mkbody 'Email me at bob@corp.example.' 'uf-true' '{"use_faker": true}')")
m=$(printf '%s' "$r" | jget '["texts"][0]')
if [[ "$m" != *"bob@corp.example"* && "$m" != *"[EMAIL_ADDRESS_"* && "$m" == *"@"* ]]; then
  pass "use_faker=true → realistic email surrogate"
else
  ffail "use_faker=true" "got: $m"
fi

# Override to false (no-op for uuid-debug since it's already false, but
# the path runs). Should produce an opaque token.
r=$(post "$(mkbody 'Email me at carol@corp.example.' 'uf-false' '{"use_faker": false}')")
m=$(printf '%s' "$r" | jget '["texts"][0]')
if [[ "$m" == *"[EMAIL_ADDRESS_"* ]]; then
  pass "use_faker=false → opaque [EMAIL_ADDRESS_…] token"
else
  ffail "use_faker=false" "expected opaque token, got: $m"
fi

# Verify the two overrides produced different surrogate buckets in the
# cache: same email under different override values should yield
# different surrogates.
r1=$(post "$(mkbody 'Email me at dave@corp.example.' 'uf-cache-true'  '{"use_faker": true}')")
r2=$(post "$(mkbody 'Email me at dave@corp.example.' 'uf-cache-false' '{"use_faker": false}')")
s1=$(printf '%s' "$r1" | jget '["texts"][0]')
s2=$(printf '%s' "$r2" | jget '["texts"][0]')
if [[ "$s1" != "$s2" ]]; then
  pass "same input × different use_faker → different surrogates (separate cache buckets)"
else
  ffail "use_faker bucket separation" "both yielded '$s1'"
fi

# ── 3. faker_locale override ────────────────────────────────────────────────
say ""
say "${c_bld}3. faker_locale override${c_rst}"

# Same email under two different locales → two different surrogates
# (different cache buckets, distinct Faker instances).
r1=$(post "$(mkbody 'Contact: erin@corp.example.' 'loc-pt' '{"use_faker": true, "faker_locale": "pt_BR"}')")
r2=$(post "$(mkbody 'Contact: erin@corp.example.' 'loc-de' '{"use_faker": true, "faker_locale": "de_DE"}')")
s1=$(printf '%s' "$r1" | jget '["texts"][0]')
s2=$(printf '%s' "$r2" | jget '["texts"][0]')
if [[ "$s1" != "$s2" && "$s1" != *"erin@corp.example"* && "$s2" != *"erin@corp.example"* ]]; then
  pass "different locales → different surrogates"
else
  ffail "locale bucket separation" "pt='$s1' de='$s2'"
fi

# Multi-locale chain (string form): "pt_BR,en_US"
r=$(post "$(mkbody 'Hi frank@corp.example.' 'loc-multi' '{"use_faker": true, "faker_locale": "pt_BR,en_US"}')")
a=$(printf '%s' "$r" | jget '["action"]')
m=$(printf '%s' "$r" | jget '["texts"][0]')
if [[ "$a" == "GUARDRAIL_INTERVENED" && "$m" != *"frank@corp.example"* ]]; then
  pass "multi-locale chain (string form) accepted"
else
  ffail "multi-locale string" "action=$a, m='$m'"
fi

# Multi-locale chain (array form): ["pt_BR", "en_US"]
r=$(post "$(mkbody 'Hi gina@corp.example.' 'loc-arr' '{"use_faker": true, "faker_locale": ["pt_BR", "en_US"]}')")
a=$(printf '%s' "$r" | jget '["action"]')
if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
  pass "multi-locale chain (array form) accepted"
else
  ffail "multi-locale array" "action=$a"
fi

# Locale chain over the cap (3) → override dropped, request still succeeds.
huge=$(python3 -c 'print(",".join("en_%02d" % i for i in range(50)))')
r=$(post "$(mkbody 'Try harry@corp.example.' 'loc-huge' "{\"use_faker\": true, \"faker_locale\": \"$huge\"}")")
a=$(printf '%s' "$r" | jget '["action"]')
if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
  pass "oversize locale chain → request still succeeds (override dropped)"
else
  ffail "oversize locale" "action=$a"
fi

# ── 4. detector_mode override (subset) ──────────────────────────────────────
say ""
say "${c_bld}4. detector_mode override${c_rst}"

if $has_llm; then
  # Restrict to regex only — anything only the LLM would catch (e.g.
  # "AcmeCorp") should pass through unredacted, but a regex-detectable
  # PII (email) still gets caught.
  r=$(post "$(mkbody 'AcmeCorp engineer ivan@corp.example reports a fault.' \
    'dm-regex' '{"detector_mode": ["regex"]}')")
  m=$(printf '%s' "$r" | jget '["texts"][0]')
  # Email should be gone, AcmeCorp should remain (regex doesn't catch it).
  if [[ "$m" != *"ivan@corp.example"* && "$m" == *"AcmeCorp"* ]]; then
    pass "detector_mode=[regex] → email redacted, AcmeCorp preserved"
  else
    ffail "detector_mode=[regex]" "got: $m"
  fi
else
  skip "detector_mode subset" "llm not in DETECTOR_MODE"
fi

# Unknown detector name → warning logged, fall through to all configured.
r=$(post "$(mkbody 'Email jane@corp.example.' 'dm-bad' '{"detector_mode": ["unknown_thing"]}')")
a=$(printf '%s' "$r" | jget '["action"]')
if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
  pass "unknown detector in override → fall back to all (request still succeeds)"
else
  ffail "unknown detector fallback" "action=$a"
fi

# Oversize detector list (>3) → override dropped.
r=$(post "$(mkbody 'Email kate@corp.example.' 'dm-huge' \
  '{"detector_mode": ["a", "b", "c", "d", "e"]}')")
a=$(printf '%s' "$r" | jget '["action"]')
if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
  pass "oversize detector list → request still succeeds (override dropped)"
else
  ffail "oversize detector list" "action=$a"
fi

# ── 5. regex_patterns override (named alternative) ──────────────────────────
say ""
say "${c_bld}5. regex_patterns override (named alternative)${c_rst}"
if $has_regex; then
  # CNPJ (Brazilian tax ID, format `\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}`)
  # is matched by regex_pentest.yaml's CNPJ pattern but NOT by
  # regex_default.yaml — confirmed via direct detector probing. So
  # switching to the "pentest" registry entry at request time changes
  # whether the value gets redacted, with no overlap from PHONE or
  # other shape-similar patterns in the default set.
  TEXT="Tax ID 12.345.678/0001-99 in the report."
  CNPJ="12.345.678/0001-99"

  # No-detection responses come back as {action: NONE, texts: null} —
  # LiteLLM passes the original text through unchanged, so for the
  # "CNPJ left alone" assertion, action=NONE is success.
  r=$(post "$(mkbody "$TEXT" 'rp-default' '')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "NONE" ]]; then
    pass "default patterns leave the CNPJ alone (action=NONE)"
  else
    m=$(printf '%s' "$r" | jget '["texts"][0]')
    ffail "rp default" "expected NONE, got action=$a, response: $m"
  fi

  r=$(post "$(mkbody "$TEXT" 'rp-pentest' '{"regex_patterns": "pentest"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
    m=$(printf '%s' "$r" | jget '["texts"][0]')
    if [[ "$m" != *"$CNPJ"* ]]; then
      pass "regex_patterns=pentest → CNPJ redacted (registry switch end-to-end)"
    else
      ffail "rp pentest" "intervened but CNPJ still present, got: $m"
    fi
  else
    ffail "rp pentest" "expected GUARDRAIL_INTERVENED, got action=$a"
  fi

  # Unknown name → warn-and-fall-back. Same as default: NONE.
  r=$(post "$(mkbody "$TEXT" 'rp-bogus' '{"regex_patterns": "nonexistent"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "NONE" ]]; then
    pass "unknown regex_patterns name → fallback to default (CNPJ untouched)"
  else
    ffail "rp unknown name" "expected NONE (fallback), got action=$a"
  fi
else
  skip "regex_patterns tests" "regex not in DETECTOR_MODE"
fi

# ── 6. llm_prompt override (named alternative) ──────────────────────────────
say ""
say "${c_bld}6. llm_prompt override (named alternative)${c_rst}"
if $has_llm; then
  # End-to-end: fake-llm has a rule keyed on match_system_prompt="NORDVENTO"
  # (a substring unique to bundled:llm_pentest.md). When the guardrail
  # forwards that prompt as the system message, fake-llm fires the rule
  # and returns 503 → LLM_FAIL_CLOSED → outer BLOCKED. The default prompt
  # doesn't contain NORDVENTO, so without the override the request flows
  # normally.
  r=$(post "$(mkbody 'Some random user input.' 'lp-pentest' '{"llm_prompt": "pentest"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "BLOCKED" ]]; then
    pass "llm_prompt=pentest reaches fake-llm (NORDVENTO rule → 503 → BLOCKED)"
  else
    ffail "lp pentest" "expected BLOCKED, got action=$a"
  fi

  # Negative control: same payload without the override — default prompt,
  # no NORDVENTO, no rule match.
  r=$(post "$(mkbody 'Some random user input.' 'lp-default' '')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" != "BLOCKED" ]]; then
    pass "without llm_prompt override: not BLOCKED (action=$a) — confirms isolation"
  else
    ffail "lp isolation" "default prompt also BLOCKED?"
  fi

  # Unknown name → warn-and-fall-back; same as the negative control.
  r=$(post "$(mkbody 'Some random user input.' 'lp-bogus' '{"llm_prompt": "nonexistent"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" != "BLOCKED" ]]; then
    pass "unknown llm_prompt name → fallback to default"
  else
    ffail "lp unknown name" "unknown name routed to a registered prompt?"
  fi
else
  skip "llm_prompt tests" "llm not in DETECTOR_MODE"
fi

# ── 7. regex_overlap_strategy override ──────────────────────────────────────
say ""
say "${c_bld}7. regex_overlap_strategy override${c_rst}"
if $has_regex; then
  # On the default image with the bundled default YAML, regex_pentest's
  # \d{12} pattern isn't present, so this test only meaningfully
  # demonstrates that the override is plumbed and accepted. We still
  # verify both values come back as INTERVENED.
  r=$(post "$(mkbody 'Order id 550e8400-e29b-41d4-a716-446655440000.' \
    'rs-longest' '{"regex_overlap_strategy": "longest"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
    pass "regex_overlap_strategy=longest accepted"
  else
    ffail "rs longest" "action=$a"
  fi

  r=$(post "$(mkbody 'Order id 550e8400-e29b-41d4-a716-446655440000.' \
    'rs-priority' '{"regex_overlap_strategy": "priority"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
    pass "regex_overlap_strategy=priority accepted"
  else
    ffail "rs priority" "action=$a"
  fi

  # Bogus value → warned + dropped, request still proceeds with default.
  r=$(post "$(mkbody 'Order id 550e8400-e29b-41d4-a716-446655440000.' \
    'rs-bogus' '{"regex_overlap_strategy": "best"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
    pass "invalid strategy → warn-and-drop (request still succeeds)"
  else
    ffail "rs bogus" "action=$a"
  fi
else
  skip "regex strategy tests" "regex not in DETECTOR_MODE"
fi

# ── 8. llm_model override ──────────────────────────────────────────────────
say ""
say "${c_bld}8. llm_model override${c_rst}"
if $has_llm; then
  # End-to-end verification: fake-llm's bundled rules.example.yaml
  # has a rule keyed on match_model="invalid-model" that returns 503.
  # When the override propagates through the LLMDetector to the
  # outgoing chat-completions request, fake-llm fires that rule, the
  # guardrail's LLMUnavailableError trips LLM_FAIL_CLOSED, and the
  # response comes back BLOCKED. That's only possible if the override
  # actually reached fake-llm — proves the plumbing is real.
  r=$(post "$(mkbody 'AcmeCorp uses ml-platform.' 'lm-bad' '{"llm_model": "invalid-model"}')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" == "BLOCKED" ]]; then
    pass "llm_model override reaches fake-llm (invalid-model → 503 → BLOCKED)"
  else
    ffail "llm_model" "expected BLOCKED, got action=$a"
  fi

  # And the negative control: the same payload without the bad model
  # override should NOT be BLOCKED — confirms the previous failure
  # came from the override, not some other path.
  r=$(post "$(mkbody 'AcmeCorp uses ml-platform.' 'lm-default' '')")
  a=$(printf '%s' "$r" | jget '["action"]')
  if [[ "$a" != "BLOCKED" ]]; then
    pass "without llm_model override: not BLOCKED (action=$a) — confirms isolation"
  else
    ffail "llm_model isolation" "default path also BLOCKED?"
  fi
else
  skip "llm_model test" "llm not in DETECTOR_MODE"
fi

# ── 9. Unknown / invalid override keys ─────────────────────────────────────
say ""
say "${c_bld}9. Invalid override values${c_rst}"

# Bad type (string for a bool) → dropped, request still works.
r=$(post "$(mkbody 'Email at lily@corp.example.' 'inv-1' '{"use_faker": "yes"}')")
a=$(printf '%s' "$r" | jget '["action"]')
m=$(printf '%s' "$r" | jget '["texts"][0]')
if [[ "$a" == "GUARDRAIL_INTERVENED" && "$m" != *"lily@corp.example"* ]]; then
  pass "bad-type override dropped, request anonymized normally"
else
  ffail "bad-type override" "action=$a"
fi

# Unknown key → silently ignored.
r=$(post "$(mkbody 'Email at mark@corp.example.' 'inv-2' '{"future_param": 42}')")
a=$(printf '%s' "$r" | jget '["action"]')
if [[ "$a" == "GUARDRAIL_INTERVENED" ]]; then
  pass "unknown override key silently ignored"
else
  ffail "unknown key" "action=$a"
fi

# ── Summary ────────────────────────────────────────────────────────────────
say ""
TOTAL=$((PASS+FAIL+SKIP))
say "${c_bld}Summary:${c_rst} ${c_grn}${PASS} passed${c_rst}, ${c_red}${FAIL} failed${c_rst}, ${c_ylw}${SKIP} skipped${c_rst} of $TOTAL"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1