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
LLM_USE_FORWARDED_KEY=false
FAIL_CLOSED=true
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
}

ws_pick_detector_mode() {
  # Checklist: pick which detection layers to enable. We assemble the
  # canonical priority order (regex → privacy_filter → llm) from the
  # selected boxes; operators wanting an unusual order can use cli.sh.
  # privacy_filter is only offered on images that ship the model.
  local cur=",${DETECTOR_MODE},"
  local has_regex=off has_pf=off has_llm=off
  [[ "$cur" == *",regex,"*          ]] && has_regex=on
  [[ "$cur" == *",privacy_filter,"* ]] && has_pf=on
  [[ "$cur" == *",llm,"*            ]] && has_llm=on

  local opts=()
  opts+=("regex"          "Regex"           "$has_regex")
  if [[ "$TYPE" == "pf" || "$TYPE" == "pf-baked" ]]; then
    opts+=("privacy_filter" "Privacy-Filter" "$has_pf")
  fi
  opts+=("llm"            "LLM"             "$has_llm")

  local selected
  selected=$(dialog --title "Enabled detectors" --no-tags \
    --separate-output --checklist \
    "Space toggles a detector. The order of type-resolution priority is regex → privacy_filter → llm." \
    14 72 4 "${opts[@]}" 3>&1 1>&2 2>&3) || return 1

  # Build the comma-separated list in canonical priority order.
  local mode=""
  for d in regex privacy_filter llm; do
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

ws_toggle_forwarded_key() {
  if dialog --title "Forward LLM key" --defaultno --yesno \
       "Send the caller's Authorization header to the detection LLM (LLM_USE_FORWARDED_KEY=true)?\n\nOnly meaningful with a custom backend that authenticates against a per-user virtual key (e.g. LiteLLM with extra_headers: [authorization])." \
       12 70; then
    LLM_USE_FORWARDED_KEY=true
  else
    LLM_USE_FORWARDED_KEY=false
  fi
}

ws_toggle_fail_closed() {
  # Default-yes (closed) so the safer choice is the default if the
  # operator just hits Enter.
  if dialog --title "Fail-closed" --yesno \
       "Block requests when the LLM detector errors (FAIL_CLOSED=true)?\n\nNo → fall back to regex-only redaction on LLM failure (useful when iterating on the LLM, dangerous in real deployments)." \
       11 70; then
    FAIL_CLOSED=true
  else
    FAIL_CLOSED=false
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
    [[ "$FAIL_CLOSED" == "false" ]] && fc_disp="open (regex fallback)"
    items+=("failmode" "$(fmt_row "Fail mode          $fc_disp")")

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
          failmode)   ws_toggle_fail_closed ;;
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
    if [[ "$DETECTOR_HAS_LLM" == "true" ]]; then
      items+=("llm_sub"   "$(fmt_row "LLM options        →")")
    fi

    local choice rc=0
    choice=$(dialog --title "Detectors" --no-tags --clear \
      --ok-label "Open" --cancel-label "Back" \
      --menu "" 14 78 6 "${items[@]}" 3>&1 1>&2 2>&3) || rc=$?

    case "$rc" in
      0)
        case "$choice" in
          mode)      ws_pick_detector_mode ;;
          regex_sub) submenu_regex ;;
          llm_sub)   submenu_llm ;;
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

print_plan
run_guardrail