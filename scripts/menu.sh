#!/usr/bin/env bash
# Interactive launcher for the anonymizer-guardrail container.
# Single-screen `dialog` UI: every setting is visible at once, pick a
# row to edit it, hit "Launch" to run. For non-interactive / scripted
# use, see scripts/cli.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

if ! command -v dialog >/dev/null 2>&1; then
  err "dialog not found in PATH."
  err "Install it (apt install dialog / dnf install dialog)"
  err "or use scripts/cli.sh for flag-driven invocation."
  exit 1
fi

# Override dialog's color theme to taste via DIALOGRC=/path/to/.dialogrc
# — see `dialog --create-rc /tmp/dialogrc-default` for the defaults.

detect_engine

# ── Sane defaults — operator can change any of these in the menu ────────────
TYPE="slim"
DETECTOR_MODE="regex"
REGEX_OVERLAP_CHOICE="longest"
LOG_LEVEL_CHOICE="info"
USE_FAKER_CHOICE="false"
LOCALE_CHOICE=""
LLM_BACKEND="fake-llm"           # only used if DETECTOR_MODE has llm
LLM_API_BASE_OVERRIDE="http://litellm:4000/v1"
LLM_API_KEY_OVERRIDE=""
LLM_MODEL_OVERRIDE="anonymize"
HF_OFFLINE=false
RULES_FILE=""
REGEX_PATTERNS_PATH=""
LLM_SYSTEM_PROMPT_PATH=""
DENYLIST_PATH=""
DENYLIST_BACKEND=""              # empty → server default (regex)
PRIVACY_FILTER_URL=""
PRIVACY_FILTER_BACKEND=""        # empty | service | external
GLINER_PII_URL=""
GLINER_PII_BACKEND=""            # empty | service | external
GLINER_PII_LABELS=""             # empty → server's DEFAULT_LABELS apply
GLINER_PII_THRESHOLD=""          # empty → server's DEFAULT_THRESHOLD applies
GLINER_PII_FAIL_CLOSED=true
LLM_USE_FORWARDED_KEY=false
LLM_FAIL_CLOSED=true
PRIVACY_FILTER_FAIL_CLOSED=true
SURROGATE_SALT=""
EXTRA_ARGS=()
NAME="$CONTAINER_NAME_DEFAULT"
PORT=8000

# ── on-state helper ─────────────────────────────────────────────────────────
# dialog radiolists need an "on"/"off" tag per choice. Tiny helper so
# the per-dialog code below stays readable.
on() { [[ "$1" == "$2" ]] && echo on || echo off; }

# ── Sub-dialogs (each modifies one global; returns 1 on Cancel) ─────────────

ws_pick_flavour() {
  # Labels match the README's "Run it" table. Tag values
  # (slim/pf/pf-baked) stay the same — they're the keys cli.sh /
  # build-image.sh / Containerfile already understand.
  local r
  r=$(dialog --title "Image flavour" --no-tags --radiolist \
    "Pick the guardrail image" 12 72 3 \
    "slim"     "slim"                                  "$(on "$TYPE" slim)" \
    "pf"       "privacy-filter (runtime download)"     "$(on "$TYPE" pf)" \
    "pf-baked" "privacy-filter (model baked in)"       "$(on "$TYPE" pf-baked)" \
    3>&1 1>&2 2>&3) || return 1
  TYPE="$r"

  # Switching to slim with the in-process privacy-filter backend
  # selected → auto-correct to `service` with the canonical URL.
  # The in-process path needs torch + the model, which slim doesn't
  # ship; without this the launch-time validation would block and
  # bounce the operator back to the menu anyway. Cheaper UX to fix
  # silently here.
  if [[ "$TYPE" == "slim" \
        && "${DETECTOR_MODE},"  == *"privacy_filter,"* \
        && -z "$PRIVACY_FILTER_BACKEND" ]]; then
    PRIVACY_FILTER_BACKEND="service"
    [[ -z "$PRIVACY_FILTER_URL" ]] && PRIVACY_FILTER_URL="$PF_SERVICE_URL_LOCAL"
  fi
}

ws_pick_detector_mode() {
  # Checklist: pick which detection layers to enable. We assemble the
  # canonical priority order (regex → denylist → privacy_filter → llm)
  # from the selected boxes; operators wanting an unusual order can use
  # cli.sh. privacy_filter is offered when the image ships the model
  # (pf / pf-baked) OR when the operator pointed at a remote
  # privacy-filter service (PRIVACY_FILTER_URL set on the slim image).
  local cur=",${DETECTOR_MODE},"
  local has_regex=off has_denylist=off has_pf=off has_gliner=off has_llm=off
  [[ "$cur" == *",regex,"*          ]] && has_regex=on
  [[ "$cur" == *",denylist,"*       ]] && has_denylist=on
  [[ "$cur" == *",privacy_filter,"* ]] && has_pf=on
  [[ "$cur" == *",gliner_pii,"*     ]] && has_gliner=on
  [[ "$cur" == *",llm,"*            ]] && has_llm=on

  local opts=()
  opts+=("regex"    "Regex"           "$has_regex")
  opts+=("denylist" "Denylist"        "$has_denylist")
  # `privacy_filter` is offered on every flavour. On pf / pf-baked the
  # in-process detector handles it; on slim the user has to set
  # PRIVACY_FILTER_URL (Detectors → Privacy-filter) so the remote
  # detector can take over. The launch-time validation catches the
  # slim+no-URL combination with a clear pointer.
  opts+=("privacy_filter" "Privacy-Filter" "$has_pf")
  # `gliner_pii` is remote-only — there's no in-process variant in any
  # guardrail image, so the operator must point at a gliner-pii-service
  # via Detectors → GLiNER-PII. Same launch-time validation catches
  # the no-URL case.
  opts+=("gliner_pii" "GLiNER-PII (experimental)" "$has_gliner")
  opts+=("llm"     "LLM"             "$has_llm")

  local selected
  selected=$(dialog --title "Enabled detectors" --no-tags \
    --separate-output --checklist \
    "Space toggles a detector. The order of type-resolution priority is regex → denylist → privacy_filter → gliner_pii → llm." \
    16 72 6 "${opts[@]}" 3>&1 1>&2 2>&3) || return 1

  # Build the comma-separated list in canonical priority order.
  local mode=""
  for d in regex denylist privacy_filter gliner_pii llm; do
    if printf '%s\n' "$selected" | grep -qx "$d"; then
      [[ -z "$mode" ]] && mode="$d" || mode="${mode},${d}"
    fi
  done
  if [[ -z "$mode" ]]; then
    dialog --title "No detector selected" --msgbox \
      "At least one detector must be enabled." 8 60
    return 1
  fi
  DETECTOR_MODE="$mode"

  # Ticking privacy_filter on slim with no backend chosen → default to
  # `service`. The in-process backend isn't valid on slim, and leaving
  # the backend empty would render the submenu as "in-process" (which
  # the launch-time check would then reject). Picking service here
  # makes the submenu / plan output reflect a launchable state.
  if [[ "$TYPE" == "slim" \
        && ",${DETECTOR_MODE},"  == *",privacy_filter,"* \
        && -z "$PRIVACY_FILTER_BACKEND" ]]; then
    PRIVACY_FILTER_BACKEND="service"
    [[ -z "$PRIVACY_FILTER_URL" ]] && PRIVACY_FILTER_URL="$PF_SERVICE_URL_LOCAL"
  fi

  # gliner_pii has no in-process variant on ANY flavour, so the same
  # auto-default-to-service pattern applies whenever it gets ticked
  # without a backend already chosen. Saves the operator one drill-in
  # to reach a launchable state.
  if [[ ",${DETECTOR_MODE}," == *",gliner_pii,"* \
        && -z "$GLINER_PII_BACKEND" ]]; then
    GLINER_PII_BACKEND="service"
    [[ -z "$GLINER_PII_URL" ]] && GLINER_PII_URL="$GLINER_PII_URL_LOCAL"
  fi
}

ws_pick_overlap_strategy() {
  local r
  r=$(dialog --title "Regex overlap strategy" --no-tags --radiolist \
    "Resolution when two regex patterns match overlapping spans" 12 70 2 \
    "longest"  "longest match span wins (default)"   "$(on "$REGEX_OVERLAP_CHOICE" longest)" \
    "priority" "first pattern in YAML order wins"    "$(on "$REGEX_OVERLAP_CHOICE" priority)" \
    3>&1 1>&2 2>&3) || return 1
  REGEX_OVERLAP_CHOICE="$r"
}

ws_pick_llm_backend() {
  local r
  r=$(dialog --title "LLM detector backend" --no-tags --radiolist \
    "Where the LLM detector sends its requests" 12 70 2 \
    "fake-llm" "fake-llm — auto-started, deterministic test backend" "$(on "$LLM_BACKEND" fake-llm)" \
    "custom"   "custom   — your own OpenAI-compatible endpoint"      "$(on "$LLM_BACKEND" custom)" \
    3>&1 1>&2 2>&3) || return 1
  LLM_BACKEND="$r"

  if [[ "$LLM_BACKEND" == "custom" ]]; then
    local out
    out=$(dialog --title "Custom LLM endpoint" --form \
      "Connection details (leave key empty for unauthenticated dev backends)" \
      14 70 3 \
      "API base" 1 1 "${LLM_API_BASE_OVERRIDE}" 1 12 50 0 \
      "API key " 2 1 "${LLM_API_KEY_OVERRIDE}"  2 12 50 0 \
      "Model   " 3 1 "${LLM_MODEL_OVERRIDE}"     3 12 50 0 \
      3>&1 1>&2 2>&3) || return 1
    LLM_API_BASE_OVERRIDE="$(printf '%s\n' "$out" | sed -n '1p')"
    LLM_API_KEY_OVERRIDE="$(printf '%s\n' "$out" | sed -n '2p')"
    LLM_MODEL_OVERRIDE="$(printf '%s\n' "$out" | sed -n '3p')"
  fi
}

ws_pick_log_level() {
  local r
  r=$(dialog --title "LOG_LEVEL" --no-tags --radiolist \
    "Container log verbosity" 12 70 4 \
    "info"    "info    — request line per call (default)"             "$(on "$LOG_LEVEL_CHOICE" info)" \
    "debug"   "debug   — per-detector matches, parsed entities, dedup" "$(on "$LOG_LEVEL_CHOICE" debug)" \
    "warning" "warning — warnings and errors only"                     "$(on "$LOG_LEVEL_CHOICE" warning)" \
    "error"   "error   — errors only"                                   "$(on "$LOG_LEVEL_CHOICE" error)" \
    3>&1 1>&2 2>&3) || return 1
  LOG_LEVEL_CHOICE="$r"
}

ws_toggle_faker() {
  # dialog requires common options (e.g. --defaultno) BEFORE the widget
  # type (--yesno) — putting it after the TEXT/HEIGHT/WIDTH triplet
  # silently no-ops the flag, which made the dialog default to Yes
  # while looking like it defaulted to No.
  if dialog --title "Use Faker" --defaultno --yesno \
       "Generate realistic surrogates with Faker?" \
       8 70; then
    USE_FAKER_CHOICE="true"
  else
    USE_FAKER_CHOICE="false"
  fi
}

ws_pick_locale() {
  # FAKER_LOCALE accepts a comma-separated list — the first locale is
  # primary, later ones are fallbacks for providers the primary doesn't
  # implement. The checklist exposes that: tick one or more presets, or
  # tick "custom" alone to enter an arbitrary string.
  local cur=",${LOCALE_CHOICE},"
  # "custom" is on iff LOCALE_CHOICE has any token that isn't a preset.
  local any_custom=off
  if [[ -n "$LOCALE_CHOICE" ]]; then
    local IFS=','
    for tok in $LOCALE_CHOICE; do
      case "$tok" in
        en_US|pt_BR|de_CH|fr_FR|es_ES|ja_JP) ;;
        *) any_custom=on ;;
      esac
    done
  fi
  has() { [[ "$cur" == *",$1,"* ]] && echo on || echo off; }

  local opts=()
  # The selected boxes are joined in list order; that order IS the
  # primary→fallback order written to FAKER_LOCALE. So en_US lives
  # at the end — ticking pt_BR + en_US yields "pt_BR,en_US" (pt_BR
  # primary, en_US fallback), which is the common shape.
  opts+=("pt_BR"  "pt_BR  — Brazilian Portuguese"       "$(has pt_BR)")
  opts+=("de_CH"  "de_CH  — Switzerland"                "$(has de_CH)")
  opts+=("fr_FR"  "fr_FR  — French"                     "$(has fr_FR)")
  opts+=("es_ES"  "es_ES  — Spanish"                    "$(has es_ES)")
  opts+=("ja_JP"  "ja_JP  — Japanese"                   "$(has ja_JP)")
  opts+=("en_US"  "en_US  — US English (Faker default)" "$(has en_US)")
  opts+=("custom" "custom — type your own"              "$any_custom")

  local selected
  selected=$(dialog --title "Faker locale" --no-tags \
    --separate-output --checklist \
    "Tick one or more. Empty selection means en_US (default). Order is primary → fallbacks." \
    16 72 7 "${opts[@]}" 3>&1 1>&2 2>&3) || return 1

  # If "custom" is among the picks, the inputbox replaces everything —
  # arbitrary text wouldn't compose meaningfully with preset tags.
  if printf '%s\n' "$selected" | grep -qx custom; then
    local r
    r=$(dialog --title "Custom Faker locale" --inputbox \
      "Comma-separated locale list (e.g. pt_BR,en_US):" 8 70 "$LOCALE_CHOICE" \
      3>&1 1>&2 2>&3) || return 1
    LOCALE_CHOICE="${r//[[:space:]]/}"
  else
    # Join the ticked presets in YAML order. Empty selection → unset.
    local result=""
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      [[ -z "$result" ]] && result="$line" || result="${result},${line}"
    done <<< "$selected"
    LOCALE_CHOICE="$result"
  fi
}

ws_pick_rules_file() {
  local r
  r=$(dialog --title "fake-llm rules file" --inputbox \
    "Host path to a YAML file (mounted at /app/rules.yaml). Empty = use the bundled rules.example.yaml." \
    10 70 "$RULES_FILE" \
    3>&1 1>&2 2>&3) || return 1
  RULES_FILE="$r"
}

ws_pick_regex_patterns() {
  local cur="$REGEX_PATTERNS_PATH"
  local on_default=off on_pentest=off on_custom=off
  case "$cur" in
    "")                              on_default=on ;;
    "bundled:regex_pentest.yaml")    on_pentest=on ;;
    *)                               on_custom=on ;;
  esac
  local r
  r=$(dialog --title "Regex patterns" --no-tags --radiolist \
    "Pattern set used by the regex detector" 12 72 3 \
    "default" "default — bundled regex_default.yaml"          "$on_default" \
    "pentest" "pentest — bundled regex_pentest.yaml"          "$on_pentest" \
    "custom"  "custom  — filesystem path or bundled:filename" "$on_custom" \
    3>&1 1>&2 2>&3) || return 1
  case "$r" in
    default) REGEX_PATTERNS_PATH="" ;;
    pentest) REGEX_PATTERNS_PATH="bundled:regex_pentest.yaml" ;;
    custom)
      r=$(dialog --title "Custom regex patterns path" --inputbox \
        "Path or 'bundled:NAME':" 8 70 "$cur" \
        3>&1 1>&2 2>&3) || return 1
      REGEX_PATTERNS_PATH="$r"
      ;;
  esac
}

ws_pick_llm_prompt() {
  local cur="$LLM_SYSTEM_PROMPT_PATH"
  local on_default=off on_pentest=off on_custom=off
  case "$cur" in
    "")                          on_default=on ;;
    "bundled:llm_pentest.md")    on_pentest=on ;;
    *)                           on_custom=on ;;
  esac
  local r
  r=$(dialog --title "LLM detection prompt" --no-tags --radiolist \
    "System prompt used by the LLM detector" 12 72 3 \
    "default" "default — bundled llm_default.md"               "$on_default" \
    "pentest" "pentest — bundled llm_pentest.md"               "$on_pentest" \
    "custom"  "custom  — filesystem path or bundled:filename"  "$on_custom" \
    3>&1 1>&2 2>&3) || return 1
  case "$r" in
    default) LLM_SYSTEM_PROMPT_PATH="" ;;
    pentest) LLM_SYSTEM_PROMPT_PATH="bundled:llm_pentest.md" ;;
    custom)
      r=$(dialog --title "Custom LLM prompt path" --inputbox \
        "Path or 'bundled:NAME':" 8 70 "$cur" \
        3>&1 1>&2 2>&3) || return 1
      LLM_SYSTEM_PROMPT_PATH="$r"
      ;;
  esac
}

ws_pick_denylist_path() {
  # No bundled denylist files ship with the package — the use case is
  # org-specific names. So we offer "none" (loaded but empty) and
  # "custom" (filesystem path or bundled:NAME if the operator has a
  # baked-in image variant).
  local cur="$DENYLIST_PATH"
  local on_none=off on_custom=off
  if [[ -z "$cur" ]]; then
    on_none=on
  else
    on_custom=on
  fi
  local r
  r=$(dialog --title "Denylist path" --no-tags --radiolist \
    "DENYLIST_PATH for the denylist detector" 12 72 2 \
    "none"   "none — detector loads with no entries (matches nothing)" "$on_none" \
    "custom" "custom — filesystem path or bundled:NAME"                "$on_custom" \
    3>&1 1>&2 2>&3) || return 1
  case "$r" in
    none)
      DENYLIST_PATH=""
      ;;
    custom)
      r=$(dialog --title "Denylist path" --inputbox \
        "Path or 'bundled:NAME' (e.g. /etc/anonymizer/deny.yaml):" 8 70 "$cur" \
        3>&1 1>&2 2>&3) || return 1
      DENYLIST_PATH="$r"
      ;;
  esac
}

ws_pick_denylist_backend() {
  # Empty = "use the server default" (regex). Offer the two real values
  # plus the no-override pseudo-option so an operator who's just
  # exploring the menu doesn't accidentally pin the env var.
  local on_default=off on_regex=off on_aho=off
  case "$DENYLIST_BACKEND" in
    "")     on_default=on ;;
    regex)  on_regex=on   ;;
    aho)    on_aho=on     ;;
  esac
  local r
  r=$(dialog --title "Denylist backend" --no-tags --radiolist \
    "Matching backend used by the denylist detector" 12 72 3 \
    "default" "default — leave DENYLIST_BACKEND unset (server picks regex)" "$on_default" \
    "regex"   "regex   — Python re alternation (stdlib)"                    "$on_regex" \
    "aho"     "aho     — Aho-Corasick via pyahocorasick (sub-linear)"       "$on_aho" \
    3>&1 1>&2 2>&3) || return 1
  case "$r" in
    default) DENYLIST_BACKEND="" ;;
    *)       DENYLIST_BACKEND="$r" ;;
  esac
}

ws_pick_privacy_filter_backend() {
  # The in-process option is only valid on the pf / pf-baked images
  # (they ship torch + the model). slim has no in-process path, so
  # offering "in-process" there would be a footgun — the launch-time
  # validation would reject it. Hide it instead.
  local supports_inproc=false
  if [[ "$TYPE" == "pf" || "$TYPE" == "pf-baked" ]]; then
    supports_inproc=true
  fi

  local on_inproc=off on_service=off on_external=off
  case "$PRIVACY_FILTER_BACKEND" in
    "")       on_inproc=on   ;;
    service)  on_service=on  ;;
    external) on_external=on ;;
  esac

  # Build the option list per flavour. dialog needs at least one
  # entry pre-selected; the radiolist below ensures that holds.
  local opts=()
  if [[ "$supports_inproc" == "true" ]]; then
    opts+=("in-process" "in-process — model loaded in this container" "$on_inproc")
  elif [[ "$on_inproc" == "on" ]]; then
    # Operator had in-process selected but switched to slim. Default
    # the radiolist to `service` so they have a valid pick on submit.
    on_service=on
  fi
  opts+=("service"    "service    — auto-start service" "$on_service")
  opts+=("external"   "external   — custom URL"         "$on_external")

  local height=12
  [[ "$supports_inproc" == "true" ]] && height=13

  local r
  r=$(dialog --title "Privacy-filter backend" --no-tags --radiolist \
    "Where the privacy_filter detector runs" "$height" 76 3 \
    "${opts[@]}" \
    3>&1 1>&2 2>&3) || return 1
  case "$r" in
    in-process)
      PRIVACY_FILTER_BACKEND=""
      PRIVACY_FILTER_URL=""
      ;;
    service)
      PRIVACY_FILTER_BACKEND="service"
      # Default URL to the canonical local container — the operator
      # can override later via "Service URL" if they want a different
      # host on the shared network.
      [[ -z "$PRIVACY_FILTER_URL" ]] && PRIVACY_FILTER_URL="$PF_SERVICE_URL_LOCAL"
      ;;
    external)
      PRIVACY_FILTER_BACKEND="external"
      # Force the URL field so the operator can fill it in next.
      ws_pick_privacy_filter_url || return 1
      if [[ -z "$PRIVACY_FILTER_URL" ]]; then
        dialog --title "URL required" --msgbox \
          "External backend needs a URL. Reverting to previous setting." 8 60
        # Don't drop to in-process on slim — that's invalid there.
        if [[ "$supports_inproc" == "true" ]]; then
          PRIVACY_FILTER_BACKEND=""
        else
          PRIVACY_FILTER_BACKEND="service"
          PRIVACY_FILTER_URL="$PF_SERVICE_URL_LOCAL"
        fi
      fi
      ;;
  esac
}

ws_pick_privacy_filter_url() {
  # When set, the guardrail's RemotePrivacyFilterDetector talks to a
  # standalone privacy-filter service over HTTP and the in-process model
  # load is skipped — slim image becomes sufficient. The detector itself
  # isn't implemented yet; the env var is plumbed now so the menu is
  # ready when it lands.
  local r
  r=$(dialog --title "Remote privacy-filter URL" --inputbox \
    "PRIVACY_FILTER_URL — empty = in-process detector (current behaviour). Set a URL to talk to a privacy-filter-service container instead. Example: http://privacy-filter-service:8001" \
    12 70 "$PRIVACY_FILTER_URL" \
    3>&1 1>&2 2>&3) || return 1
  PRIVACY_FILTER_URL="$r"
}

ws_pick_gliner_pii_backend() {
  # gliner_pii has no in-process variant — the choice is between
  # auto-starting a local container (`service`) or pointing at one
  # the operator already has running (`external`). Both feed
  # GLINER_PII_URL to the guardrail; the difference is who manages
  # the inference container's lifecycle.
  local on_service=off on_external=off
  case "$GLINER_PII_BACKEND" in
    service)  on_service=on  ;;
    external) on_external=on ;;
    *)        on_service=on  ;;     # default: auto-start
  esac

  local r
  r=$(dialog --title "GLiNER-PII backend" --no-tags --radiolist \
    "Where the gliner_pii detector runs (no in-process variant exists)" 12 76 2 \
    "service"  "service  — auto-start a local gliner-pii-service container" "$on_service" \
    "external" "external — operator-supplied URL"                            "$on_external" \
    3>&1 1>&2 2>&3) || return 1
  case "$r" in
    service)
      GLINER_PII_BACKEND="service"
      [[ -z "$GLINER_PII_URL" ]] && GLINER_PII_URL="$GLINER_PII_URL_LOCAL"
      ;;
    external)
      GLINER_PII_BACKEND="external"
      ws_pick_gliner_pii_url || return 1
      if [[ -z "$GLINER_PII_URL" ]]; then
        dialog --title "URL required" --msgbox \
          "External backend needs a URL. Reverting to service." 8 60
        GLINER_PII_BACKEND="service"
        GLINER_PII_URL="$GLINER_PII_URL_LOCAL"
      fi
      ;;
  esac
}

ws_pick_gliner_pii_url() {
  local r
  r=$(dialog --title "GLiNER-PII URL" --inputbox \
    "GLINER_PII_URL — base URL of a gliner-pii-service. Example: http://gliner-pii-service:8002" \
    10 70 "$GLINER_PII_URL" \
    3>&1 1>&2 2>&3) || return 1
  GLINER_PII_URL="$r"
}

ws_pick_gliner_pii_labels() {
  # Empty = "use whatever DEFAULT_LABELS the gliner-pii-service has
  # configured." Setting this client-side overrides the server-side
  # default, which is useful when one inference container serves
  # several guardrail deployments with different vocabularies.
  local r
  r=$(dialog --title "GLiNER-PII labels" --inputbox \
    "GLINER_PII_LABELS — comma-separated zero-shot labels (e.g. 'person,email,ssn,credit_card'). Empty = use the gliner-pii-service's DEFAULT_LABELS." \
    11 70 "$GLINER_PII_LABELS" \
    3>&1 1>&2 2>&3) || return 1
  # Trim spaces so commas don't inherit incidental whitespace from
  # paste sources.
  GLINER_PII_LABELS="$(printf '%s' "$r" | sed 's/ *, */,/g; s/^ *//; s/ *$//')"
}

ws_pick_gliner_pii_threshold() {
  local r
  r=$(dialog --title "GLiNER-PII threshold" --inputbox \
    "GLINER_PII_THRESHOLD — confidence cutoff (0..1). Empty = use the gliner-pii-service's DEFAULT_THRESHOLD (0.5)." \
    10 70 "$GLINER_PII_THRESHOLD" \
    3>&1 1>&2 2>&3) || return 1
  # Validate at edit time so the operator gets immediate feedback
  # rather than a confusing failure at boot. Empty is allowed
  # (server default applies).
  if [[ -n "$r" ]]; then
    if ! awk -v v="$r" 'BEGIN { exit !(v ~ /^[0-9]+(\.[0-9]+)?$/ && v+0 >= 0 && v+0 <= 1) }' </dev/null; then
      dialog --title "Invalid threshold" --msgbox \
        "Threshold must be a number in [0, 1]. Got: $r" 8 60
      return 1
    fi
  fi
  GLINER_PII_THRESHOLD="$r"
}

ws_toggle_gliner_pii_fail_closed() {
  if dialog --title "GLiNER-PII fail-closed" --yesno \
       "Block requests when the gliner_pii detector errors (GLINER_PII_FAIL_CLOSED=true)?\n\nNo → degrade to no gliner_pii matches and proceed with the other detectors." \
       11 70; then
    GLINER_PII_FAIL_CLOSED=true
  else
    GLINER_PII_FAIL_CLOSED=false
  fi
}

ws_toggle_forwarded_key() {
  if dialog --title "Forward LLM key" --defaultno --yesno \
       "Send the caller's Authorization header to the detection LLM (LLM_USE_FORWARDED_KEY=true)?\n\nOnly meaningful with a custom backend that authenticates against a per-user virtual key (e.g. LiteLLM with extra_headers: [authorization])." \
       12 70; then
    LLM_USE_FORWARDED_KEY=true
  else
    LLM_USE_FORWARDED_KEY=false
  fi
}

ws_toggle_llm_fail_closed() {
  # Default-yes (closed) so the safer choice is the default if the
  # operator just hits Enter.
  if dialog --title "LLM fail-closed" --yesno \
       "Block requests when the LLM detector errors (LLM_FAIL_CLOSED=true)?\n\nNo → coverage falls back to the other detectors on LLM failure (useful when iterating on the LLM, dangerous in real deployments)." \
       11 70; then
    LLM_FAIL_CLOSED=true
  else
    LLM_FAIL_CLOSED=false
  fi
}

ws_toggle_pf_fail_closed() {
  if dialog --title "Privacy-filter fail-closed" --yesno \
       "Block requests when the privacy_filter detector errors (PRIVACY_FILTER_FAIL_CLOSED=true)?\n\nNo → degrade to no privacy_filter matches and proceed with the other detectors (regex / denylist / llm)." \
       11 70; then
    PRIVACY_FILTER_FAIL_CLOSED=true
  else
    PRIVACY_FILTER_FAIL_CLOSED=false
  fi
}

ws_set_surrogate_salt() {
  local r
  r=$(dialog --title "Surrogate salt" --inputbox \
    "SURROGATE_SALT — empty leaves a random 16-byte salt picked at process start (strongest privacy). Set to a stable string for cross-restart consistency." \
    11 70 "$SURROGATE_SALT" \
    3>&1 1>&2 2>&3) || return 1
  SURROGATE_SALT="$r"
}

# ── Main menu loop ──────────────────────────────────────────────────────────
# All menu rows are padded to the same width so dialog doesn't
# auto-resize between renders when the right-hand value (e.g. "enabled"
# vs "disabled (opaque ...)") changes length. The width is chosen so
# the longest realistic value still fits without truncation.
_MENU_ROW_WIDTH=60
fmt_row() { printf '%-*s' "$_MENU_ROW_WIDTH" "$*"; }

# Same labels as the flavour radio dialog (see ws_pick_flavour) so the
# main menu row reads the same as the option that produced it.
flavour_label() {
  case "$1" in
    slim)     echo "slim" ;;
    pf)       echo "privacy-filter (runtime download)" ;;
    pf-baked) echo "privacy-filter (model baked in)" ;;
    *)        echo "$1" ;;
  esac
}

# Display labels for the path-style overrides — match the radio
# dialog choices when the value is one of the known bundled presets,
# otherwise show the literal path.
regex_patterns_label() {
  case "$REGEX_PATTERNS_PATH" in
    "")                              echo "default (bundled)" ;;
    "bundled:regex_pentest.yaml")    echo "pentest (bundled)" ;;
    *)                               echo "$REGEX_PATTERNS_PATH" ;;
  esac
}

llm_prompt_label() {
  case "$LLM_SYSTEM_PROMPT_PATH" in
    "")                          echo "default (bundled)" ;;
    "bundled:llm_pentest.md")    echo "pentest (bundled)" ;;
    *)                           echo "$LLM_SYSTEM_PROMPT_PATH" ;;
  esac
}

denylist_path_label() {
  if [[ -z "$DENYLIST_PATH" ]]; then
    echo "(none — detector loads empty)"
  else
    echo "$DENYLIST_PATH"
  fi
}

denylist_backend_label() {
  if [[ -z "$DENYLIST_BACKEND" ]]; then
    echo "default (regex)"
  else
    echo "$DENYLIST_BACKEND"
  fi
}

privacy_filter_url_label() {
  if [[ -z "$PRIVACY_FILTER_URL" ]]; then
    echo "(unset — in-process detector)"
  else
    echo "$PRIVACY_FILTER_URL"
  fi
}

privacy_filter_backend_label() {
  case "$PRIVACY_FILTER_BACKEND" in
    "")
      # Empty means in-process. On slim that's not a valid configuration
      # (no torch / no model in the image) — flag it as unset instead so
      # the row reflects what the launch-time validation will say.
      if [[ "$TYPE" == "slim" ]]; then
        echo "(unset — pick service or external)"
      else
        echo "in-process (model loaded in this container)"
      fi
      ;;
    service)  echo "service (auto-start privacy-filter-service)" ;;
    external) echo "external (operator-supplied URL)" ;;
    *)        echo "$PRIVACY_FILTER_BACKEND" ;;
  esac
}

gliner_pii_url_label() {
  if [[ -z "$GLINER_PII_URL" ]]; then
    echo "(unset — required)"
  else
    echo "$GLINER_PII_URL"
  fi
}

gliner_pii_backend_label() {
  case "$GLINER_PII_BACKEND" in
    "")       echo "(unset — pick service or external)" ;;
    service)  echo "service (auto-start gliner-pii-service)" ;;
    external) echo "external (operator-supplied URL)" ;;
    *)        echo "$GLINER_PII_BACKEND" ;;
  esac
}

gliner_pii_labels_label() {
  if [[ -z "$GLINER_PII_LABELS" ]]; then
    echo "(server default)"
  else
    echo "$GLINER_PII_LABELS"
  fi
}

gliner_pii_threshold_label() {
  if [[ -z "$GLINER_PII_THRESHOLD" ]]; then
    echo "(server default — 0.5)"
  else
    echo "$GLINER_PII_THRESHOLD"
  fi
}

# ── Sub-menus (menuconfig-style) ────────────────────────────────────────────
# Each submenu is its own loop. `Cancel` (or Esc) returns to the
# parent menu — same convention as `make menuconfig`. Submenus only
# expose the OK and Cancel buttons; the top-level "Launch" lives only
# on `main_menu`.

submenu_general() {
  while true; do
    local items=()
    items+=("flavour" "$(fmt_row "Flavour            $(flavour_label "$TYPE")")")
    items+=("log"     "$(fmt_row "Log level          $LOG_LEVEL_CHOICE")")
    local salt_disp="random per restart"
    [[ -n "$SURROGATE_SALT" ]] && salt_disp="set (stable)"
    items+=("salt"    "$(fmt_row "Surrogate salt     $salt_disp")")

    local choice rc=0
    choice=$(dialog --title "General" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 14 78 6 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          flavour) ws_pick_flavour ;;
          log)     ws_pick_log_level ;;
          salt)    ws_set_surrogate_salt ;;
        esac
        ;;
      *) return 0 ;;     # Back / Esc → up to parent
    esac
  done
}

submenu_regex() {
  while true; do
    local items=()
    items+=("strat"    "$(fmt_row "Regex strategy     $REGEX_OVERLAP_CHOICE")")
    items+=("patterns" "$(fmt_row "Regex patterns     $(regex_patterns_label)")")

    local choice rc=0
    choice=$(dialog --title "Detectors → Regex" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 12 78 4 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          strat)    ws_pick_overlap_strategy ;;
          patterns) ws_pick_regex_patterns ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

submenu_denylist() {
  while true; do
    local items=()
    items+=("path"    "$(fmt_row "Denylist path      $(denylist_path_label)")")
    items+=("backend" "$(fmt_row "Denylist backend   $(denylist_backend_label)")")

    local choice rc=0
    choice=$(dialog --title "Detectors → Denylist" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 12 78 4 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          path)    ws_pick_denylist_path ;;
          backend) ws_pick_denylist_backend ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

submenu_privacy_filter() {
  while true; do
    local items=()
    items+=("backend" "$(fmt_row "Backend            $(privacy_filter_backend_label)")")
    # The URL row matters for `external` (operator-supplied) and as a
    # display-only hint for `service` (auto-derived from the canonical
    # container name). For in-process (empty backend) the URL is unused.
    if [[ "$PRIVACY_FILTER_BACKEND" == "external" || -n "$PRIVACY_FILTER_URL" ]]; then
      items+=("url" "$(fmt_row "Service URL        $(privacy_filter_url_label)")")
    fi
    local pf_fc_disp="closed (block on PF error)"
    [[ "$PRIVACY_FILTER_FAIL_CLOSED" == "false" ]] && pf_fc_disp="open (degrade to no PF matches)"
    items+=("failmode" "$(fmt_row "Fail mode          $pf_fc_disp")")

    local choice rc=0
    choice=$(dialog --title "Detectors → Privacy-Filter" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 13 78 5 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          backend)  ws_pick_privacy_filter_backend ;;
          url)      ws_pick_privacy_filter_url ;;
          failmode) ws_toggle_pf_fail_closed ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

submenu_gliner_pii() {
  while true; do
    local items=()
    items+=("backend"   "$(fmt_row "Backend            $(gliner_pii_backend_label)")")
    # URL row only when external (operator-supplied) or already set;
    # for service-backend it's auto-derived from the canonical
    # container name and not editable.
    if [[ "$GLINER_PII_BACKEND" == "external" || -n "$GLINER_PII_URL" ]]; then
      items+=("url" "$(fmt_row "Service URL        $(gliner_pii_url_label)")")
    fi
    items+=("labels"    "$(fmt_row "Labels             $(gliner_pii_labels_label)")")
    items+=("threshold" "$(fmt_row "Threshold          $(gliner_pii_threshold_label)")")
    local fc_disp="closed (block on gliner_pii error)"
    [[ "$GLINER_PII_FAIL_CLOSED" == "false" ]] && fc_disp="open (degrade to no gliner matches)"
    items+=("failmode" "$(fmt_row "Fail mode          $fc_disp")")

    local choice rc=0
    choice=$(dialog --title "Detectors → GLiNER-PII" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 15 78 7 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          backend)   ws_pick_gliner_pii_backend ;;
          url)       ws_pick_gliner_pii_url ;;
          labels)    ws_pick_gliner_pii_labels ;;
          threshold) ws_pick_gliner_pii_threshold ;;
          failmode)  ws_toggle_gliner_pii_fail_closed ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

submenu_llm() {
  while true; do
    local items=()
    local lbl
    case "$LLM_BACKEND" in
      custom) lbl="LLM backend        custom ($LLM_API_BASE_OVERRIDE)" ;;
      *)      lbl="LLM backend        $LLM_BACKEND" ;;
    esac
    items+=("llm"    "$(fmt_row "$lbl")")
    # The system-prompt override only applies when the LLM backend
    # actually consults a real LLM. fake-llm matches rules.yaml and
    # ignores the system prompt entirely, so we hide that row to avoid
    # implying it has any effect.
    if [[ "$LLM_BACKEND" != "fake-llm" ]]; then
      items+=("prompt" "$(fmt_row "LLM prompt         $(llm_prompt_label)")")
    fi
    if [[ "$LLM_BACKEND" == "custom" ]]; then
      local fk_disp="no"; [[ "$LLM_USE_FORWARDED_KEY" == "true" ]] && fk_disp="yes"
      items+=("forwardkey" "$(fmt_row "Forward LLM key    $fk_disp")")
    fi
    if [[ "$LLM_BACKEND" == "fake-llm" ]]; then
      local rules_disp="${RULES_FILE:-bundled rules.example.yaml}"
      items+=("rules" "$(fmt_row "fake-llm rules     $rules_disp")")
    fi
    local fc_disp="closed (block on LLM error)"
    [[ "$LLM_FAIL_CLOSED" == "false" ]] && fc_disp="open (fall back to other detectors)"
    items+=("failmode" "$(fmt_row "LLM fail mode      $fc_disp")")

    local choice rc=0
    choice=$(dialog --title "Detectors → LLM" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 16 78 8 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          llm)        ws_pick_llm_backend ;;
          prompt)     ws_pick_llm_prompt ;;
          forwardkey) ws_toggle_forwarded_key ;;
          rules)      ws_pick_rules_file ;;
          failmode)   ws_toggle_llm_fail_closed ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

submenu_detectors() {
  while true; do
    resolve_predicates
    local items=()
    items+=("mode" "$(fmt_row "Enabled detectors  $DETECTOR_MODE")")
    if [[ "$DETECTOR_HAS_REGEX" == "true" ]]; then
      items+=("regex_sub" "$(fmt_row "Regex options      →")")
    fi
    if [[ "$DETECTOR_HAS_DENYLIST" == "true" ]]; then
      items+=("denylist_sub" "$(fmt_row "Denylist options   →")")
    fi
    if [[ "$DETECTOR_HAS_PRIVACY_FILTER" == "true" ]]; then
      items+=("pf_sub" "$(fmt_row "Privacy-filter     →")")
    fi
    if [[ "$DETECTOR_HAS_GLINER_PII" == "true" ]]; then
      items+=("gliner_sub" "$(fmt_row "GLiNER-PII         →")")
    fi
    if [[ "$DETECTOR_HAS_LLM" == "true" ]]; then
      items+=("llm_sub"   "$(fmt_row "LLM options        →")")
    fi

    local choice rc=0
    choice=$(dialog --title "Detectors" --no-tags --clear \
      --ok-label "Open" --cancel-label "Back" \
      --menu "" 18 78 9 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          mode)         ws_pick_detector_mode ;;
          regex_sub)    submenu_regex ;;
          denylist_sub) submenu_denylist ;;
          pf_sub)       submenu_privacy_filter ;;
          gliner_sub)   submenu_gliner_pii ;;
          llm_sub)      submenu_llm ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

submenu_faker() {
  while true; do
    local items=()
    if [[ "$USE_FAKER_CHOICE" == "true" ]]; then
      items+=("faker"  "$(fmt_row "Use Faker          yes")")
      items+=("locale" "$(fmt_row "Faker locale       ${LOCALE_CHOICE:-en_US (default)}")")
    else
      items+=("faker"  "$(fmt_row "Use Faker          no")")
    fi

    local choice rc=0
    choice=$(dialog --title "Faker" --no-tags --clear \
      --ok-label "Edit" --cancel-label "Back" \
      --menu "" 12 78 4 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          faker)  ws_toggle_faker ;;
          locale) ws_pick_locale ;;
        esac
        ;;
      *) return 0 ;;
    esac
  done
}

# ── Top-level menu ──────────────────────────────────────────────────────────
main_menu() {
  while true; do
    local items=()
    items+=("general"   "$(fmt_row "General settings   →")")
    items+=("detectors" "$(fmt_row "Detector settings  →")")
    items+=("faker"     "$(fmt_row "Faker settings     →")")

    # Three buttons via `dialog --extra-button`:
    #   <Open>    (OK,    rc=0) → enter the highlighted submenu
    #   <Launch>  (extra, rc=3) → exit loop, run
    #   <Quit>    (cancel,rc=1) → abort
    #   Esc                rc=255 → abort
    local choice rc=0
    choice=$(dialog --title "anonymizer-guardrail" --no-tags --clear \
      --ok-label "Open" \
      --extra-button --extra-label "Launch" \
      --cancel-label "Quit" \
      --menu "↑↓ to highlight a section, Open to drill in." \
      14 78 6 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          general)   submenu_general ;;
          detectors) submenu_detectors ;;
          faker)     submenu_faker ;;
        esac
        ;;
      3)   break ;;             # Launch button (extra)
      *)   reset_screen; warn "Aborted."; exit 0 ;;
    esac
  done
  reset_screen
}

# Some terminals don't fully restore after dialog without an explicit
# alternate-screen exit + clear. Belt-and-braces: try `tput rmcup`
# (exit alt buffer), fall through to a hard ANSI reset, then clear.
reset_screen() {
  tput rmcup 2>/dev/null || true
  printf '\033[H\033[2J'
}

main_menu

resolve_flavour
resolve_predicates

# ── privacy_filter on slim requires a remote backend ───────────────────────
# slim doesn't ship torch + transformers, so the in-process detector
# can't load. The operator must pick a remote backend (service /
# external) before launching. Block with a clear pointer rather than
# letting the container crash at startup with a torch ImportError.
if [[ "$DETECTOR_HAS_PRIVACY_FILTER" == "true" \
      && "$TYPE" == "slim" \
      && -z "$PRIVACY_FILTER_BACKEND" ]]; then
  dialog --title "Privacy-filter backend required" --msgbox \
    "DETECTOR_MODE includes 'privacy_filter' on the slim image, but no remote backend is selected.\n\nThe slim image doesn't ship torch + the model, so the in-process detector can't load.\n\nOpen Detectors → Privacy-filter and pick:\n  • service  → auto-start a privacy-filter-service container\n  • external → talk to a service you supply via URL\n\nOr go back and pick the pf / pf-baked flavour instead." \
    16 72
  exec "$0"   # restart the menu so the operator can fix it
fi

# ── gliner_pii always requires a remote backend ────────────────────────────
# Unlike privacy_filter, there is no in-process variant in any
# guardrail flavour — gliner_pii is remote-only. The detector's
# factory crashes loud at boot with the same message, but blocking
# at the menu instead saves the operator a container start cycle.
if [[ "$DETECTOR_HAS_GLINER_PII" == "true" \
      && ( -z "$GLINER_PII_BACKEND" || -z "$GLINER_PII_URL" ) ]]; then
  dialog --title "GLiNER-PII backend required" --msgbox \
    "DETECTOR_MODE includes 'gliner_pii' but no service URL is configured.\n\nThe gliner_pii detector is remote-only — there's no in-process variant.\n\nOpen Detectors → GLiNER-PII and pick:\n  • service  → auto-start a gliner-pii-service container\n  • external → talk to a service you supply via URL" \
    14 72
  exec "$0"
fi

# ── Image must exist ─────────────────────────────────────────────────────────
if ! image_exists "$IMAGE"; then
  if dialog --title "Image not found" --yesno \
       "Image \"${IMAGE}\" is not built.\n\nRun scripts/build-image.sh -t ${TYPE} now?" \
       10 70; then
    "$SCRIPT_DIR/build-image.sh" -t "$TYPE"
    if ! image_exists "$IMAGE"; then
      err "Build did not produce \"${IMAGE}\"."
      exit 1
    fi
  else
    err "Cannot run without an image."
    exit 1
  fi
fi

# ── Volume + HF offline (pf only) ────────────────────────────────────────────
if [[ "$USE_VOLUME" == "true" ]]; then
  ensure_hf_volume
  if [[ "$JUST_CREATED_VOLUME" == "false" ]]; then
    if dialog --title "HF Hub revalidation" --yesno \
         "The HuggingFace cache volume already has the model.\n\nCheck Hub for an updated model on every start?\n\nNo (default) → set HF_HUB_OFFLINE=1 (faster, no warning)." \
         12 70 \
         --defaultno; then
      HF_OFFLINE=false
    else
      HF_OFFLINE=true
    fi
  fi
fi

# ── Container conflict ──────────────────────────────────────────────────────
if container_exists "$NAME"; then
  if dialog --title "Container exists" --yesno \
       "A container named \"${NAME}\" already exists.\nRemove it and proceed?" \
       9 70; then
    "$ENGINE" rm -f "$NAME" >/dev/null
  else
    err "Aborted."
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

# ── Auto-start gliner-pii-service if needed ─────────────────────────────────
if [[ "$DETECTOR_HAS_GLINER_PII" == "true" \
      && "$GLINER_PII_BACKEND" == "service" ]]; then
  start_gliner_pii
fi

print_plan
run_guardrail