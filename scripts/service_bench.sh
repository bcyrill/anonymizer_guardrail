#!/usr/bin/env bash
# Latency benchmark across the three detector services.
#
# For each (mode, service) pair where mode is "cpu" and/or "gpu" and
# service is one of gliner-pii-service / privacy-filter-service /
# privacy-filter-hf-service: start the matching image with podman
# (passing the GPU device through for gpu mode), wait for /health=ok,
# run two timed phases against /detect — "short" (30 mixed-PII
# payloads) and "long" (the same 30 each repeated LONG_REPEAT_FACTOR
# times so the forward pass actually has work to do), stop the
# container, then print a side-by-side timing summary. The two
# phases tell you whether the GPU advantage that's invisible on
# short inputs (dominated by HTTP / Python / tokenisation overhead)
# shows up once the model has real compute to chew on.
#
# Requirements on the host:
#   - podman (plus the nvidia container runtime for gpu mode)
#   - curl
#   - the relevant images already pulled (or network access to
#     ghcr.io). With default tags:
#       ghcr.io/bcyrill/privacy-filter-service:{0.5-cpu,0.5-cu130}
#       ghcr.io/bcyrill/gliner-pii-service:{0.5-cpu,0.5-cu130}
#       ghcr.io/bcyrill/privacy-filter-hf-service:{0.5-cpu,0.5-cu130}
#
# Usage:
#   scripts/service_bench.sh             # both modes (cpu then gpu)
#   scripts/service_bench.sh cpu         # cpu only
#   scripts/service_bench.sh gpu         # gpu only
#   scripts/service_bench.sh both        # explicit; same as default
#
# Subset of services (env var, comma-separated):
#   SERVICE_FILTER=gliner-pii,privacy-filter-hf scripts/service_bench.sh
#
# Larger long-phase repetition (env var):
#   LONG_REPEAT_FACTOR=30 scripts/service_bench.sh

set -uo pipefail

# --- Configuration ----------------------------------------------------

IMAGE_TAG_CPU="${IMAGE_TAG_CPU:-0.5-cpu}"
IMAGE_TAG_GPU="${IMAGE_TAG_GPU:-0.5-cu130}"
GPU_FLAG="${GPU_FLAG:---device=nvidia.com/gpu=all}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-600}"
REQUEST_TIMEOUT_SEC="${REQUEST_TIMEOUT_SEC:-120}"

# Optional comma-separated subset of service names to run. Empty
# means "run all". Names must match the first field in $SERVICES
# below: privacy-filter | gliner-pii | privacy-filter-hf.
SERVICE_FILTER="${SERVICE_FILTER:-}"

service_enabled() {
    local svc="$1"
    [ -z "$SERVICE_FILTER" ] && return 0
    local IFS=','
    local f
    for f in $SERVICE_FILTER; do
        f="${f// /}"
        [ "$f" = "$svc" ] && return 0
    done
    return 1
}

# Mode selection — first positional arg picks cpu/gpu/both. Default
# both so a bare invocation produces a full CPU vs GPU comparison.
MODE_ARG="${1:-both}"
case "$MODE_ARG" in
    cpu)  MODES=("cpu") ;;
    gpu)  MODES=("gpu") ;;
    both) MODES=("cpu" "gpu") ;;
    -h|--help)
        sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "ERROR: unknown mode '$MODE_ARG' (expected: cpu | gpu | both)" >&2
        exit 2
        ;;
esac

# name | image-base | port | volume | mountpath
# image-base has the tag appended at run time based on the mode.
SERVICES=(
    "privacy-filter|ghcr.io/bcyrill/privacy-filter-service|8001|privacy-filter-opf-cache|/app/.opf"
    "gliner-pii|ghcr.io/bcyrill/gliner-pii-service|8002|gliner-hf-cache|/app/.cache/huggingface"
    "privacy-filter-hf|ghcr.io/bcyrill/privacy-filter-hf-service|8003|privacy-filter-hf-cache|/app/.cache/huggingface"
)

image_for_mode() {
    local base="$1" mode="$2"
    case "$mode" in
        cpu) printf '%s:%s' "$base" "$IMAGE_TAG_CPU" ;;
        gpu) printf '%s:%s' "$base" "$IMAGE_TAG_GPU" ;;
    esac
}

# Container names are scoped by mode so the EXIT trap can clean up
# whichever combination we may have started.
container_name() { printf 'bench-%s-%s' "$1" "$2"; }

# 30 distinct short payloads. Mixed PII shapes and lengths so the
# benchmark exercises a representative variety of inputs rather
# than the same string 30 times.
SHORT_PAYLOADS=(
    "Email alice.smith@example.com about the renewal next week."
    "Call Bob Roberts at +1 415-555-0181 to confirm the delivery address."
    "Customer Jane Doe (DOB 1987-03-22) lives at 742 Evergreen Terrace, Springfield, IL 62701."
    "The IBAN DE89 3704 0044 0532 0130 00 has been linked to the household account."
    "Visa card 4111-1111-1111-1111 was declined; please retry with a different method."
    "SSN 123-45-6789 was verified during the identity check on 2026-04-17."
    "Reset link: https://support.acmebank.com/reset/8e5a2f1c — expires in 24h."
    "Marcus Chen (agent ID 88421) closed the ticket after the customer confirmed access."
    "Patient John Miller, MRN MR-007841, was admitted on 2026-01-15 with chest pain."
    "Forward to bob.roberts@firstnationalbank.com and cc legal@acme.com."
    "Account number 9876543210 belongs to the holding company registered in Delaware."
    "Please update the shipping address from 123 Main St, Boston MA 02108 to the new office."
    "Passport US12345678 was scanned at the border on 2026-02-09 at 14:32 UTC."
    "Server logs show repeated logins from 192.168.1.42 and 10.0.0.55 between 2am and 4am."
    "Ms. Emily Tanaka (emily.tanaka@globex.co.jp) requested a copy of the GDPR report."
    "Driver license CA-D1234567 expires 2030-07-12; renewal reminder sent."
    "Wire transfer of EUR 12,500 from FR76 3000 6000 0112 3456 7890 189 completed."
    "Contact the on-call engineer at +44 20 7946 0958 if the alert pages overnight."
    "Username @nightowl42 posted from device fingerprint 7c9e6679f5dea9e1c73f at 02:14."
    "The new HR partner, Priya Subramanian, joins on 2026-06-01 — set up payroll."
    "Backup credentials: admin / S3cur3P@ssw0rd! (rotate after the migration)."
    "Health insurance member ID HIX-887766554, group GRP-2026-NA, effective 2026-01-01."
    "Private key fingerprint SHA256:abcd1234ef56... should never appear in chat logs."
    "Refund of \$89.50 issued to card ending 4242 on 2026-04-19."
    "Schedule the on-site visit at 555 Cypress Ave, Apt 12B, Brooklyn NY 11215 for next Tuesday."
    "Loan application 2026-LN-44291 references SSN 987-65-4321 and credit score 742."
    "Bug reproduces under user_id=u_8842f1a3 with API token tk_live_9c4f...e21d."
    "Confidential: M&A target Helios Systems Ltd, valuation \$420M, due 2026-Q3."
    "From: ceo@acme.com — please process the bonus for Carlos Rivera (employee 2287)."
    "Audit log entry 2026-04-20T09:14:32Z: file /etc/passwd accessed by uid=0 from 203.0.113.7."
)

# Long-payload set is built by repeating each short payload
# LONG_REPEAT_FACTOR times so the model's forward pass actually
# has work to do. Short inputs are dominated by HTTP / Python /
# tokenisation overhead, which the GPU can't accelerate; the long
# set is what tells you whether GPU pays off when the compute is
# real. The default 50× sits comfortably inside the typical
# 2048-token max-position limit for these models so the bench
# measures actual length scaling rather than silently-truncated
# max-context throughput. Bump higher if you specifically want to
# stress the truncation regime.
LONG_REPEAT_FACTOR="${LONG_REPEAT_FACTOR:-50}"

declare -a LONG_PAYLOADS=()
for _p in "${SHORT_PAYLOADS[@]}"; do
    _long=""
    for (( _i=0; _i<LONG_REPEAT_FACTOR; _i++ )); do
        _long+="${_p} "
    done
    LONG_PAYLOADS+=("${_long% }")
done
unset _p _long _i

NUM_SHORT=${#SHORT_PAYLOADS[@]}
NUM_LONG=${#LONG_PAYLOADS[@]}

# --- Helpers ----------------------------------------------------------

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

cleanup() {
    # Best-effort: stop any container we may have left running, across
    # both modes (only the ones we actually started exist).
    local mode entry name cname
    for mode in "${MODES[@]}"; do
        for entry in "${SERVICES[@]}"; do
            IFS='|' read -r name _ _ _ _ <<< "$entry"
            cname="$(container_name "$name" "$mode")"
            podman stop --time 10 "$cname" >/dev/null 2>&1 || true
            podman rm   --force   "$cname" >/dev/null 2>&1 || true
        done
    done
}
trap cleanup EXIT INT TERM

ensure_volume() {
    local vol="$1"
    if ! podman volume exists "$vol" 2>/dev/null; then
        podman volume create "$vol" >/dev/null
    fi
}

start_service() {
    local name="$1" image="$2" port="$3" volume="$4" mount="$5" mode="$6"
    local cname
    cname="$(container_name "$name" "$mode")"

    log "starting ${name} [${mode}] (${image}) on port ${port}"
    podman rm --force "$cname" >/dev/null 2>&1 || true
    ensure_volume "$volume"

    # GPU flag is only injected for gpu mode; cpu runs are plain podman.
    local -a extra_flags=()
    if [ "$mode" = "gpu" ]; then
        # Word-split GPU_FLAG so multi-token values still work.
        # shellcheck disable=SC2206
        extra_flags=( ${GPU_FLAG} )
    fi

    podman run -d --rm \
        "${extra_flags[@]}" \
        -p "${port}:${port}" \
        -v "${volume}:${mount}" \
        --name "$cname" \
        "$image" >/dev/null
}

wait_for_health() {
    local port="$1" name="$2" mode="$3"
    local cname
    cname="$(container_name "$name" "$mode")"
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SEC ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        local body
        body="$(curl -fsS --max-time 3 "http://localhost:${port}/health" 2>/dev/null || true)"
        if printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
            log "${name} [${mode}] healthy"
            return 0
        fi
        sleep 2
    done
    log "ERROR: ${name} [${mode}] did not reach status=ok within ${HEALTH_TIMEOUT_SEC}s"
    podman logs "$cname" 2>&1 | tail -50 || true
    return 1
}

stop_service() {
    local name="$1" mode="$2"
    log "stopping ${name} [${mode}]"
    podman stop --time 15 "$(container_name "$name" "$mode")" >/dev/null 2>&1 || true
}

# Send one POST /detect request and print its wall-clock seconds
# (curl's %{time_total}). Returns non-zero on HTTP error.
time_one_request() {
    local port="$1" payload_json="$2"
    curl -fsS -o /dev/null \
        -w '%{time_total}' \
        --max-time "$REQUEST_TIMEOUT_SEC" \
        -H 'Content-Type: application/json' \
        -d "$payload_json" \
        "http://localhost:${port}/detect"
}

# Escape a raw text string into a JSON string literal (no jq dep).
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

# Run one timed phase against $port, drawing payloads from the
# named array (passed via nameref so the caller can pick
# SHORT_PAYLOADS or LONG_PAYLOADS). One warmup (not counted)
# absorbs first-request kernel-tune cost; the warmup uses a payload
# from the same set so it tunes for the right shape.
# Echoes one line: "<total_sec> <min_sec> <max_sec> <ok> <fail>"
run_phase() {
    local port="$1"
    local -n _payloads="$2"
    local n=${#_payloads[@]}

    local warm_payload
    warm_payload="{\"text\":\"$(json_escape "${_payloads[0]}")\"}"
    curl -fsS -o /dev/null --max-time "$REQUEST_TIMEOUT_SEC" \
        -H 'Content-Type: application/json' \
        -d "$warm_payload" \
        "http://localhost:${port}/detect" >/dev/null 2>&1 || true

    local total=0 min="" max="" ok=0 fail=0 i
    for (( i=0; i<n; i++ )); do
        local body
        body="{\"text\":\"$(json_escape "${_payloads[$i]}")\"}"
        local t
        if t="$(time_one_request "$port" "$body" 2>/dev/null)" && [ -n "$t" ]; then
            total="$(awk -v a="$total" -v b="$t" 'BEGIN{printf "%.6f", a+b}')"
            if [ -z "$min" ] || awk -v a="$t" -v b="$min" 'BEGIN{exit !(a<b)}'; then min="$t"; fi
            if [ -z "$max" ] || awk -v a="$t" -v b="$max" 'BEGIN{exit !(a>b)}'; then max="$t"; fi
            ok=$((ok+1))
        else
            fail=$((fail+1))
        fi
    done

    [ -z "$min" ] && min=0
    [ -z "$max" ] && max=0
    printf '%s %s %s %s %s\n' "$total" "$min" "$max" "$ok" "$fail"
}

# --- Main -------------------------------------------------------------

declare -a RESULTS

PHASES=("short" "long")

# Short rows always come before long rows for the same (mode, name)
# so the summary table groups naturally. FAILED_START / UNHEALTHY
# emit one row per phase so the table stays rectangular.
record_failure() {
    local mode="$1" name="$2" status="$3" phase
    for phase in "${PHASES[@]}"; do
        RESULTS+=("${mode}|${name}|${phase}|${status}|0|0|0|0|0|0")
    done
}

for mode in "${MODES[@]}"; do
    log "=== mode: ${mode} ==="
    for entry in "${SERVICES[@]}"; do
        IFS='|' read -r name image_base port volume mount <<< "$entry"
        if ! service_enabled "$name"; then
            log "skipping ${name} [${mode}] (not in SERVICE_FILTER)"
            continue
        fi
        image="$(image_for_mode "$image_base" "$mode")"

        start_service "$name" "$image" "$port" "$volume" "$mount" "$mode" || {
            log "ERROR: failed to start ${name} [${mode}], skipping"
            record_failure "$mode" "$name" "FAILED_START"
            continue
        }

        if ! wait_for_health "$port" "$name" "$mode"; then
            stop_service "$name" "$mode"
            record_failure "$mode" "$name" "UNHEALTHY"
            continue
        fi

        for phase in "${PHASES[@]}"; do
            if [ "$phase" = "short" ]; then
                arr_name="SHORT_PAYLOADS"; n_payloads=$NUM_SHORT
            else
                arr_name="LONG_PAYLOADS";  n_payloads=$NUM_LONG
            fi
            log "benchmarking ${name} [${mode}] ${phase}: ${n_payloads} payloads"
            read -r total min max ok fail <<< "$(run_phase "$port" "$arr_name")"
            avg="$(awk -v t="$total" -v c="$ok" 'BEGIN{ if (c>0) printf "%.4f", t/c; else printf "0.0000" }')"
            log "${name} [${mode}] ${phase}: total=${total}s avg=${avg}s min=${min}s max=${max}s ok=${ok} fail=${fail}"
            RESULTS+=("${mode}|${name}|${phase}|OK|${total}|${avg}|${min}|${max}|${ok}|${fail}")
        done

        stop_service "$name" "$mode"
    done
done

# --- Summary ----------------------------------------------------------

echo
echo "================================================================"
echo " Service benchmark — short=${NUM_SHORT} payloads, long=${NUM_LONG}×${LONG_REPEAT_FACTOR} (per service, per mode)"
echo "================================================================"
printf '%-5s %-22s %-6s %-12s %10s %10s %10s %10s %4s %4s\n' \
    "mode" "service" "size" "status" "total(s)" "avg(s)" "min(s)" "max(s)" "ok" "err"
printf '%-5s %-22s %-6s %-12s %10s %10s %10s %10s %4s %4s\n' \
    "-----" "----------------------" "------" "------------" "----------" "----------" "----------" "----------" "----" "----"
for r in "${RESULTS[@]}"; do
    IFS='|' read -r mode name size status total avg min max ok fail <<< "$r"
    printf '%-5s %-22s %-6s %-12s %10s %10s %10s %10s %4s %4s\n' \
        "$mode" "$name" "$size" "$status" "$total" "$avg" "$min" "$max" "$ok" "$fail"
done
echo
