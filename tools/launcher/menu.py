"""Interactive launcher (Textual TUI).

Single-screen menuconfig-style app. All current settings are visible
at once on the main screen; each row drills into a modal for editing.
Section layout (General, Detectors, Faker) keeps the same conceptual
groupings the operator already knows.

Invoked via `scripts/launcher.sh --ui` — the Click CLI in `main.py`
catches that flag in an eager callback and hands off to
`run_interactive()` here. There's no separate entry point for the
TUI; the unified launcher always goes through `tools.launcher.main`.

Per-detector submenus stay hand-written for the detectors with unique
fields (gliner labels/threshold, llm prompt, etc.); the boilerplate
around them — checklist, backend selection, fail-mode toggles — is
generic and driven by `LAUNCHER_METADATA`.

Adding a new detector with a service: add a `LauncherSpec(...)` entry
in `spec_extras.py`. Its checklist row, backend prompt, fail-mode
toggle, and auto-start hook all light up automatically. Detector-
specific fields (like gliner labels) need a per-detector function
in this module — there's no way to generalize "what fields does THIS
detector configure" without giving up the unique-prompt UX.

The Textual app exits cleanly when the operator hits Launch (or Q).
On Launch we hand the resolved `LaunchConfig` to `run_guardrail()`
which exec's the engine in the foreground, replacing this process.
"""

from __future__ import annotations

from typing import Callable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
)
from textual.widgets.option_list import Option

# Separator() was a top-level export in older Textual; current versions
# (8.x) accept None as a separator in add_options/__init__. Single-name
# alias keeps the call sites readable.
Separator = lambda: None  # noqa: E731 — intentional sentinel-as-callable

from .engine import detect_engine
from .runner import LaunchConfig, run_guardrail
from .services import register_atexit_cleanup, start_service
from .spec_extras import LAUNCHER_METADATA


# ── Modal screens (one per editor type) ───────────────────────────────────
class TextEditScreen(ModalScreen[str | None]):
    """Single-line text input modal. Returns the entered string, or
    None if the operator cancels."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "ok", "OK"),
    ]

    DEFAULT_CSS = """
    TextEditScreen {
        align: center middle;
    }
    TextEditScreen > Container {
        width: 78;
        height: auto;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    TextEditScreen Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    /* width: 1fr + height: auto are what make Label wrap multi-line
       inside the container. Without them long help/title text gets
       single-line truncated at the right edge. */
    TextEditScreen Label.help {
        width: 1fr;
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    TextEditScreen Input { margin-bottom: 1; }
    TextEditScreen Horizontal { height: auto; align-horizontal: right; }
    TextEditScreen Button { margin-left: 1; }
    """

    def __init__(self, title: str, current: str, help_text: str = "") -> None:
        super().__init__()
        self._title = title
        self._current = current
        self._help_text = help_text

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(self._title, classes="title")
            if self._help_text:
                yield Label(self._help_text, classes="help")
            yield Input(value=self._current, id="value")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    @on(Button.Pressed, "#ok")
    def action_ok(self) -> None:
        self.dismiss(self.query_one("#value", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    # Input consumes Enter to fire its own Submitted event before the
    # modal-level `enter` binding fires; route Submitted to action_ok
    # so Enter confirms regardless of which path Textual takes.
    @on(Input.Submitted, "#value")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_ok()


class SelectEditScreen(ModalScreen[str | None]):
    """RadioSet-backed picker. `choices` is `(value, label)` pairs.

    Textual's `RadioButton` binds both Enter and Space to "toggle";
    that conflicts with the conventional "Space toggles, Enter
    confirms" UX. We use `priority=True` on the Enter binding so the
    modal-level OK action fires before the RadioButton's toggle
    handler sees the key.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "ok", "OK", priority=True, show=False),
    ]

    DEFAULT_CSS = """
    SelectEditScreen {
        align: center middle;
    }
    SelectEditScreen > Container {
        width: 70;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    SelectEditScreen Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    SelectEditScreen RadioSet { margin-bottom: 1; height: auto; }
    SelectEditScreen Horizontal { height: auto; align-horizontal: right; }
    SelectEditScreen Button { margin-left: 1; }
    """

    def __init__(
        self, title: str, choices: list[tuple[str, str]], current: str
    ) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._current = current

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(self._title, classes="title")
            with RadioSet(id="choices"):
                for value, label in self._choices:
                    yield RadioButton(label, value=(value == self._current), name=value)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    @on(Button.Pressed, "#ok")
    def action_ok(self) -> None:
        rs = self.query_one("#choices", RadioSet)
        # RadioSet exposes .pressed_button when an item is checked.
        # The RadioButton's name carries our payload value.
        if rs.pressed_button is not None:
            self.dismiss(rs.pressed_button.name)
        else:
            self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class DetectorOrderScreen(ModalScreen[list[str] | None]):
    """Combined enable + reorder for the detector list.

    The list shows ALL detectors. Each row has an enable state (via
    space) and a position (via Ctrl+↑ / Ctrl+↓). The OK result is the
    enabled subset in the displayed visual order — disabled rows are
    dropped from the result but their position in the list is
    preserved across edits in case the operator re-enables one.

    Order matters because `pipeline._dedup` keeps the first-seen
    entity_type for duplicate text matches, so the detector listed
    first wins type-resolution conflicts. Earlier UIs forced canonical
    order; this gives operators full control without leaving the menu.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("space", "toggle", "Toggle"),
        Binding("ctrl+up", "move_up", "Move ↑"),
        Binding("ctrl+down", "move_down", "Move ↓"),
        Binding("enter", "ok", "OK", show=False),
    ]

    DEFAULT_CSS = """
    DetectorOrderScreen { align: center middle; }
    DetectorOrderScreen > Container {
        width: 78;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    DetectorOrderScreen Label.title { text-style: bold; margin-bottom: 1; }
    DetectorOrderScreen Label.hint {
        width: 1fr;
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    DetectorOrderScreen OptionList {
        height: auto;
        max-height: 16;
        margin-bottom: 1;
    }
    DetectorOrderScreen Horizontal { height: auto; align-horizontal: right; }
    DetectorOrderScreen Button { margin-left: 1; }
    """

    def __init__(
        self,
        all_detectors: list[str],
        current_order: list[str],
    ) -> None:
        super().__init__()
        # Working state: the visual list (all detectors, in order)
        # plus the per-detector enable flag. We start with the
        # operator's current enabled list (in their order), then
        # append any detector they haven't enabled at the end so the
        # picker exposes them too.
        enabled = set(current_order)
        self._items: list[str] = list(current_order) + [
            d for d in all_detectors if d not in enabled
        ]
        self._enabled: dict[str, bool] = {d: (d in enabled) for d in self._items}

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("Enabled detectors", classes="title")
            yield Label(
                "Space toggles, Ctrl+↑/↓ moves, Enter confirms. Order = "
                "type-resolution priority (first-listed wins on duplicate matches).",
                classes="hint",
            )
            yield OptionList(*self._render_items(), id="order_list")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("OK", id="ok", variant="primary")

    # ── Rendering ─────────────────────────────────────────────────────────
    def _render_items(self) -> list:
        """Build the OptionList items from current state. Each row
        shows `[x]` or `[ ]` followed by the detector name; the id
        is the detector name so action handlers can locate the row.

        Only the opening `[` needs escaping (Rich treats `[name]` as
        a markup tag); the closing `]` is meaningful only after an
        unescaped `[`, so it stays literal. Without the escape, `[x]`
        would be parsed as a non-existent style and silently dropped,
        leaving the row visually unmarked. `[ ]` with a space
        happened to work since space isn't a valid tag name — that's
        why only the checked state was missing."""
        out: list = []
        for d in self._items:
            mark = r"\[x]" if self._enabled[d] else r"\[ ]"
            out.append(Option(f"{mark}  {d}", id=f"det:{d}"))
        return out

    def _refresh(self, focus_index: int | None = None) -> None:
        """Rebuild the OptionList in place. Preserves the highlight
        position so toggling/moving feels continuous — without this
        the highlight would snap back to the top after every change."""
        ol = self.query_one("#order_list", OptionList)
        if focus_index is None:
            focus_index = ol.highlighted if ol.highlighted is not None else 0
        ol.clear_options()
        ol.add_options(self._render_items())
        if 0 <= focus_index < len(self._items):
            ol.highlighted = focus_index

    # ── Actions ───────────────────────────────────────────────────────────
    def _highlighted_index(self) -> int | None:
        """Index of the currently-highlighted detector in self._items.
        None when nothing is highlighted (e.g. empty list — shouldn't
        happen in practice)."""
        ol = self.query_one("#order_list", OptionList)
        return ol.highlighted

    def action_toggle(self) -> None:
        idx = self._highlighted_index()
        if idx is None:
            return
        det = self._items[idx]
        self._enabled[det] = not self._enabled[det]
        self._refresh(focus_index=idx)

    def action_move_up(self) -> None:
        idx = self._highlighted_index()
        if idx is None or idx == 0:
            return
        self._items[idx - 1], self._items[idx] = self._items[idx], self._items[idx - 1]
        self._refresh(focus_index=idx - 1)

    def action_move_down(self) -> None:
        idx = self._highlighted_index()
        if idx is None or idx >= len(self._items) - 1:
            return
        self._items[idx + 1], self._items[idx] = self._items[idx], self._items[idx + 1]
        self._refresh(focus_index=idx + 1)

    def action_ok(self) -> None:
        result = [d for d in self._items if self._enabled[d]]
        if not result:
            # Empty selection would render a detectors-section with
            # zero rows in the main menu — treat as cancel rather
            # than overwrite cfg.
            self.dismiss(None)
            return
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # OptionList consumes Enter to fire OptionSelected, which would
    # otherwise short-circuit our `enter` binding. Dispatch it to
    # `action_ok` so Enter behaves as the help text says (confirm),
    # not toggle. Toggle stays Space-only.
    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_ok()

    @on(Button.Pressed, "#ok")
    def _ok_button(self) -> None:
        self.action_ok()

    @on(Button.Pressed, "#cancel")
    def _cancel_button(self) -> None:
        self.action_cancel()


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no toggle with operator-facing description.

    The body Label uses `width: 1fr` (full container width) and
    `height: auto` so multi-line text wraps cleanly — without those
    Textual would truncate at the right edge instead of wrapping.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Container {
        width: 78;
        height: auto;
        padding: 1 2;
        border: solid $primary;
        background: $surface;
    }
    ConfirmScreen Label.title { text-style: bold; margin-bottom: 1; }
    ConfirmScreen Label.body {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
    }
    ConfirmScreen Label.hint {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    ConfirmScreen Horizontal { height: auto; align-horizontal: right; }
    ConfirmScreen Button { margin-left: 1; }
    """

    def __init__(
        self, title: str, body: str, default: bool = False, hint: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._default = default
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(self._title, classes="title")
            yield Label(self._body, classes="body")
            if self._hint:
                yield Label(self._hint, classes="hint")
            with Horizontal():
                yield Button("No", id="no", variant=("primary" if not self._default else "default"))
                yield Button("Yes", id="yes", variant=("primary" if self._default else "default"))

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(False)


# ── Option list helpers ──────────────────────────────────────────────────
# We build a single OptionList containing every settings row plus
# disabled "section header" options between them. OptionList gives us:
#
#   * Native arrow-key navigation (↑/↓ moves between selectable rows;
#     Enter triggers the OptionSelected event).
#   * Mouse support (click a row to select).
#   * `disabled=True` for header rows: visible but skipped by ↑/↓.
#   * `Separator()` for blank-line breathing room between sections.
#
# Each settings row gets `id=f"set:{key}"`; section headers get
# `id=f"hdr:{name}"` (we ignore those in the OptionSelected handler).

# Width for the label column in each row's display string. Pinned
# rather than computed so all rows align even when the visible row
# set changes (toggling detectors adds/removes rows).
_LABEL_WIDTH = 26


def _row(key: str, label: str, value: str, indent: int = 0) -> Option:
    """Build one selectable settings row. The display string is two
    columns: dotted label, value (bold). The id encodes which setting
    this row edits; on_option_selected uses it for dispatch."""
    pad = " " * indent
    label_padded = f"{pad}{label}".ljust(_LABEL_WIDTH, ".")
    return Option(f"{label_padded} [b]{value}[/b]", id=f"set:{key}")


def _header(name: str, hint: str = "") -> Option:
    """Build a section-header row. Disabled so arrow keys skip it.
    The hint column lets us put a summary of the section's current
    state (e.g. enabled detectors list) inline with the title."""
    title = f"[bold $accent]{name}[/bold $accent]"
    if hint:
        title = f"{title}  [dim]{hint}[/dim]"
    return Option(title, id=f"hdr:{name}", disabled=True)


# ── Main app ──────────────────────────────────────────────────────────────
class LauncherApp(App):
    """The launcher's main TUI screen.

    State lives in `self.cfg` (a LaunchConfig). Each setting row reads
    from cfg on render; editing a row dispatches a modal that updates
    cfg and refreshes the visible rows.

    On Launch we exit the app and let `run_interactive`'s caller hand
    the cfg to `run_guardrail` — that way the foreground engine
    process replaces us cleanly without the TUI fighting for the TTY.
    """

    TITLE = "anonymizer-guardrail launcher"
    SUB_TITLE = "press L to launch, Q to quit"

    BINDINGS = [
        Binding("l", "launch", "Launch", show=True, priority=True),
        Binding("q", "quit_app", "Quit", show=True, priority=True),
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    Screen { background: $surface; }
    OptionList { height: 1fr; padding: 1 1; border: none; }
    OptionList:focus { border: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.cfg = LaunchConfig(flavour="default", detector_mode="regex")
        self._launch_requested = False

    # ── Compose ───────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header()
        # OptionList with the entire settings list. Arrow keys / Page
        # keys / mouse all work natively; section headers are disabled
        # Options that ↑/↓ skip over.
        yield OptionList(*self._build_options(), id="settings")
        yield Footer()

    def _build_options(self) -> list:
        """Return the flat list of Option / Separator items rendered
        inside the OptionList. Per-detector sections appear only when
        the detector is enabled, so toggling DETECTOR_MODE adds/removes
        whole sections in-place."""
        cfg = self.cfg
        items: list = []

        # ── General ───────────────────────────────────────────────────────
        items.append(_header("General"))
        items.append(_row("flavour", "Image flavour", cfg.flavour))
        items.append(_row("log_level", "Log level", cfg.log_level))
        salt_label = "set (stable)" if cfg.surrogate_salt else "random per restart"
        items.append(_row("surrogate_salt", "Surrogate salt", salt_label))

        # ── Detectors meta ────────────────────────────────────────────────
        items.append(Separator())
        items.append(_header("Detectors", hint=cfg.detector_mode))
        items.append(_row("detector_mode", "Enabled", cfg.detector_mode))

        active = set(cfg.detector_names)

        # ── Per-detector sections ────────────────────────────────────────
        # Each enabled detector gets its own header + its own settings
        # block. Replaces the previous "Detectors" mega-section with
        # all sub-rows under one header — easier to scan when 3+
        # detectors are active.
        if "regex" in active:
            items.append(Separator())
            items.append(_header("Regex"))
            items.append(_row(
                "regex_overlap_strategy", "Strategy",
                cfg.env_overrides.get("REGEX_OVERLAP_STRATEGY", "longest"),
            ))
            items.append(_row(
                "regex_patterns", "Patterns",
                cfg.env_overrides.get("REGEX_PATTERNS_PATH", "(default)"),
            ))

        if "denylist" in active:
            items.append(Separator())
            items.append(_header("Denylist"))
            items.append(_row(
                "denylist_path", "Path",
                cfg.env_overrides.get("DENYLIST_PATH", "(none)"),
            ))
            items.append(_row(
                "denylist_backend", "Backend",
                cfg.env_overrides.get("DENYLIST_BACKEND", "(default — regex)"),
            ))

        if "privacy_filter" in active:
            items.append(Separator())
            backend = cfg.backends.get("privacy_filter") or "service"
            items.append(_header("Privacy-filter", hint=backend))
            items.append(_row("privacy_filter_backend", "Backend", backend))
            # Variant picker — only meaningful for backend=service. With
            # backend=external the operator's URL pins which sidecar is
            # in play, so the variant choice is silently ignored
            # there; we hide the row to avoid surfacing a knob that
            # does nothing.
            if backend == "service":
                items.append(_row(
                    "privacy_filter_variant", "Variant",
                    cfg.service_variants.get("privacy_filter") or "opf (default)",
                ))
            if cfg.backends.get("privacy_filter") == "external":
                items.append(_row(
                    "privacy_filter_url", "URL",
                    cfg.env_overrides.get("PRIVACY_FILTER_URL", "(unset)"),
                ))
            items.append(_row(
                "privacy_filter_fail", "Fail mode",
                "open" if cfg.env_overrides.get("PRIVACY_FILTER_FAIL_CLOSED") == "false" else "closed",
            ))

        if "gliner_pii" in active:
            items.append(Separator())
            backend = cfg.backends.get("gliner_pii") or "(unset)"
            items.append(_header("GLiNER-PII", hint=backend))
            items.append(_row("gliner_pii_backend", "Backend", backend))
            if cfg.backends.get("gliner_pii") == "external":
                items.append(_row(
                    "gliner_pii_url", "URL",
                    cfg.env_overrides.get("GLINER_PII_URL", "(unset)"),
                ))
            items.append(_row(
                "gliner_pii_labels", "Labels",
                cfg.env_overrides.get("GLINER_PII_LABELS", "(server default)"),
            ))
            items.append(_row(
                "gliner_pii_threshold", "Threshold",
                cfg.env_overrides.get("GLINER_PII_THRESHOLD", "(server default)"),
            ))
            items.append(_row(
                "gliner_pii_fail", "Fail mode",
                "open" if cfg.env_overrides.get("GLINER_PII_FAIL_CLOSED") == "false" else "closed",
            ))

        if "llm" in active:
            items.append(Separator())
            backend = cfg.backends.get("llm") or "(unset)"
            items.append(_header("LLM", hint=backend))
            items.append(_row("llm_backend", "Backend", backend))
            if cfg.backends.get("llm") == "external":
                items.append(_row(
                    "llm_api_base", "API base",
                    cfg.env_overrides.get("LLM_API_BASE", "(default)"),
                ))
                items.append(_row(
                    "llm_model", "Model",
                    cfg.env_overrides.get("LLM_MODEL", "(default)"),
                ))
            items.append(_row(
                "llm_prompt", "Prompt",
                cfg.env_overrides.get("LLM_SYSTEM_PROMPT_PATH", "(default)"),
            ))
            items.append(_row(
                "forward_llm_key", "Forward key",
                cfg.env_overrides.get("LLM_USE_FORWARDED_KEY", "false"),
            ))
            items.append(_row(
                "llm_fail", "Fail mode",
                "open" if cfg.env_overrides.get("LLM_FAIL_CLOSED") == "false" else "closed",
            ))

        # ── Faker ─────────────────────────────────────────────────────────
        items.append(Separator())
        items.append(_header("Faker"))
        items.append(_row("use_faker", "Use Faker", "yes" if cfg.use_faker else "no"))
        if cfg.use_faker:
            items.append(_row(
                "faker_locale", "Faker locale",
                cfg.faker_locale or "en_US (default)",
            ))

        return items

    def _refresh_rows(self) -> None:
        """Rebuild the OptionList in place. Preserves the current
        selection by id where possible — without this, every edit
        snaps the selection back to the top of the list."""
        ol = self.query_one("#settings", OptionList)
        # Capture the currently-highlighted option's id so we can
        # restore it after rebuild. None when the list is empty or
        # nothing is highlighted yet.
        prev_id: str | None = None
        if ol.highlighted is not None:
            try:
                prev_id = ol.get_option_at_index(ol.highlighted).id
            except IndexError:
                prev_id = None
        ol.clear_options()
        ol.add_options(self._build_options())
        if prev_id is not None:
            try:
                idx = ol.get_option_index(prev_id)
                ol.highlighted = idx
            except Exception:
                # Option was removed (e.g. detector got disabled);
                # fall back to the first selectable row.
                pass

    # ── OptionList → modal dispatch ───────────────────────────────────────
    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Fired when the operator hits Enter on a selectable row.
        Disabled headers don't fire this event (OptionList skips them
        on selection too)."""
        opt_id = event.option.id or ""
        if not opt_id.startswith("set:"):
            return
        self._edit_setting(opt_id[4:])

    def _edit_setting(self, key: str) -> None:
        """Dispatch the right modal for `key`. Each branch is small —
        the modal returns a value, we update cfg, rows refresh."""
        cfg = self.cfg
        if key == "flavour":
            self._pick(
                "Image flavour",
                [("default", "default")],
                cfg.flavour,
                self._set_flavour,
            )
        elif key == "log_level":
            self._pick(
                "LOG_LEVEL",
                [(v, v) for v in ("info", "debug", "warning", "error")],
                cfg.log_level,
                lambda v: self._set("log_level", v),
            )
        elif key == "surrogate_salt":
            self._text(
                "Surrogate salt", cfg.surrogate_salt,
                lambda v: self._set("surrogate_salt", v),
                help_text="Empty = random per process start. Set for cross-restart consistency.",
            )
        elif key == "detector_mode":
            self._detector_order(cfg)
        elif key == "regex_overlap_strategy":
            self._pick(
                "Regex overlap strategy",
                [("longest", "longest match span wins (default)"),
                 ("priority", "first pattern in YAML order wins")],
                cfg.env_overrides.get("REGEX_OVERLAP_STRATEGY", "longest"),
                lambda v: self._set_env("REGEX_OVERLAP_STRATEGY", v),
            )
        elif key == "regex_patterns":
            self._text(
                "REGEX_PATTERNS_PATH",
                cfg.env_overrides.get("REGEX_PATTERNS_PATH", ""),
                lambda v: self._set_env("REGEX_PATTERNS_PATH", v),
                help_text="Empty = bundled default. Use `bundled:NAME` for shipped pattern sets.",
            )
        elif key == "denylist_path":
            self._text(
                "DENYLIST_PATH",
                cfg.env_overrides.get("DENYLIST_PATH", ""),
                lambda v: self._set_env("DENYLIST_PATH", v),
                help_text="Empty = detector loads with no entries.",
            )
        elif key == "denylist_backend":
            self._pick(
                "Denylist backend",
                [("", "(default — regex)"), ("regex", "regex"), ("aho", "aho")],
                cfg.env_overrides.get("DENYLIST_BACKEND", ""),
                lambda v: self._set_env("DENYLIST_BACKEND", v),
            )
        elif key == "privacy_filter_backend":
            self._pick(
                "Privacy-filter backend",
                [
                    ("service", "service (auto-start the inference container)"),
                    ("external", "external (operator-supplied URL)"),
                ],
                cfg.backends.get("privacy_filter", "service"),
                lambda v: self._set_backend("privacy_filter", v),
            )
        elif key == "privacy_filter_variant":
            # The "opf" choice clears the entry rather than storing it
            # so the printed plan elides "(opf)" from the row label.
            # Mirrors the CLI's `--privacy-filter-variant opf` semantics.
            self._pick(
                "Privacy-filter variant",
                [
                    ("opf", "opf (default — privacy-filter-service)"),
                    ("hf", "hf (experimental — privacy-filter-hf-service, ~7x faster on CPU)"),
                ],
                cfg.service_variants.get("privacy_filter", "opf"),
                lambda v: self._set_pf_variant(v),
            )
        elif key == "privacy_filter_url":
            self._text(
                "PRIVACY_FILTER_URL",
                cfg.env_overrides.get("PRIVACY_FILTER_URL", ""),
                lambda v: self._set_env("PRIVACY_FILTER_URL", v),
                help_text="Required when --privacy-filter-backend=external.",
            )
        elif key == "privacy_filter_fail":
            self._toggle_failmode(cfg, "PRIVACY_FILTER_FAIL_CLOSED", "Privacy-filter")
        elif key == "gliner_pii_backend":
            self._pick(
                "GLiNER-PII backend",
                [("service", "service (auto-start)"), ("external", "external (operator URL)")],
                cfg.backends.get("gliner_pii", ""),
                lambda v: self._set_backend("gliner_pii", v),
            )
        elif key == "gliner_pii_url":
            self._text(
                "GLINER_PII_URL",
                cfg.env_overrides.get("GLINER_PII_URL", ""),
                lambda v: self._set_env("GLINER_PII_URL", v),
            )
        elif key == "gliner_pii_labels":
            self._text(
                "GLINER_PII_LABELS",
                cfg.env_overrides.get("GLINER_PII_LABELS", ""),
                lambda v: self._set_env("GLINER_PII_LABELS", v),
                help_text="Comma-separated zero-shot labels (e.g. 'person,email,ssn'). Empty = server default.",
            )
        elif key == "gliner_pii_threshold":
            self._text(
                "GLINER_PII_THRESHOLD",
                cfg.env_overrides.get("GLINER_PII_THRESHOLD", ""),
                lambda v: self._set_env("GLINER_PII_THRESHOLD", v),
                help_text="Confidence cutoff (0..1). Empty = server default.",
            )
        elif key == "gliner_pii_fail":
            self._toggle_failmode(cfg, "GLINER_PII_FAIL_CLOSED", "GLiNER-PII")
        elif key == "llm_backend":
            self._pick(
                "LLM backend",
                [("service", "service (auto-start fake-llm)"),
                 ("external", "external (operator URL)")],
                cfg.backends.get("llm", ""),
                lambda v: self._set_backend("llm", v),
            )
        elif key == "llm_api_base":
            self._text(
                "LLM_API_BASE",
                cfg.env_overrides.get("LLM_API_BASE", "http://litellm:4000/v1"),
                lambda v: self._set_env("LLM_API_BASE", v),
            )
        elif key == "llm_model":
            self._text(
                "LLM_MODEL",
                cfg.env_overrides.get("LLM_MODEL", "anonymize"),
                lambda v: self._set_env("LLM_MODEL", v),
            )
        elif key == "llm_prompt":
            self._text(
                "LLM_SYSTEM_PROMPT_PATH",
                cfg.env_overrides.get("LLM_SYSTEM_PROMPT_PATH", ""),
                lambda v: self._set_env("LLM_SYSTEM_PROMPT_PATH", v),
                help_text="Empty = bundled default. Use `bundled:NAME` for shipped prompts.",
            )
        elif key == "forward_llm_key":
            self._confirm(
                "Forward LLM key",
                "Forward the caller's Authorization header to the detection LLM (LLM_USE_FORWARDED_KEY=true)?",
                cfg.env_overrides.get("LLM_USE_FORWARDED_KEY", "false") == "true",
                lambda yes: self._set_env(
                    "LLM_USE_FORWARDED_KEY", "true" if yes else "false",
                ),
            )
        elif key == "llm_fail":
            self._toggle_failmode(cfg, "LLM_FAIL_CLOSED", "LLM")
        elif key == "use_faker":
            self._confirm(
                "Use Faker",
                "Generate realistic surrogates with Faker (vs opaque [TYPE_HEX] tokens)?",
                cfg.use_faker,
                self._set_use_faker,
            )
        elif key == "faker_locale":
            self._text(
                "FAKER_LOCALE",
                cfg.faker_locale,
                lambda v: self._set("faker_locale", v),
                help_text="Comma-separated, e.g. 'pt_BR,en_US'. Empty = en_US default.",
            )

    # ── Modal helpers ─────────────────────────────────────────────────────
    def _text(
        self, title: str, current: str, on_ok: Callable[[str], None], help_text: str = ""
    ) -> None:
        def handler(result: str | None) -> None:
            if result is not None:
                on_ok(result)
                self._refresh_rows()

        self.push_screen(TextEditScreen(title, current, help_text), handler)

    def _pick(
        self, title: str, choices: list[tuple[str, str]], current: str,
        on_ok: Callable[[str], None],
    ) -> None:
        def handler(result: str | None) -> None:
            if result is not None:
                on_ok(result)
                self._refresh_rows()

        self.push_screen(SelectEditScreen(title, choices, current), handler)

    def _confirm(
        self, title: str, body: str, default: bool, on_ok: Callable[[bool], None],
        hint: str = "",
    ) -> None:
        def handler(result: bool) -> None:
            on_ok(result)
            self._refresh_rows()

        self.push_screen(ConfirmScreen(title, body, default, hint=hint), handler)

    def _detector_order(self, cfg: LaunchConfig) -> None:
        """Open the combined enable + reorder modal. Result is a list
        of detector names in the operator's chosen order; we apply it
        as cfg.detector_mode and update per-detector backend defaults."""
        canonical = ["regex", "denylist", "privacy_filter", "gliner_pii", "llm"]

        def handler(result: list[str] | None) -> None:
            if result is None:
                return
            self._set_detector_order(result)
            self._refresh_rows()

        self.push_screen(
            DetectorOrderScreen(canonical, cfg.detector_names),
            handler,
        )

    # ── Setter callbacks ──────────────────────────────────────────────────
    # Each setter applies a value to cfg + handles cross-cutting
    # consequences (e.g. enabling privacy_filter auto-defaults its
    # backend to service so the operator doesn't have to pick).

    def _set(self, attr: str, value: str) -> None:
        setattr(self.cfg, attr, value)

    def _set_env(self, var: str, value: str) -> None:
        if value:
            self.cfg.env_overrides[var] = value
        else:
            self.cfg.env_overrides.pop(var, None)

    def _set_backend(self, det: str, value: str) -> None:
        if value:
            self.cfg.backends[det] = value
        else:
            self.cfg.backends.pop(det, None)

    def _set_pf_variant(self, value: str) -> None:
        """Store the privacy-filter variant on the LaunchConfig. The
        "opf" choice (the default) is stored as an empty entry — the
        runner / printed plan distinguish the default from explicitly
        chosen variants by the dict's presence, not its value. Same
        normalisation as the CLI flag's handler."""
        if value and value != "opf":
            self.cfg.service_variants["privacy_filter"] = value
        else:
            self.cfg.service_variants.pop("privacy_filter", None)

    def _set_flavour(self, value: str) -> None:
        self.cfg.flavour = value
        # Slim is the only flavour now; default privacy_filter to the
        # service backend if it's active and the operator hasn't picked.
        if "privacy_filter" in self.cfg.detector_names \
                and not self.cfg.backends.get("privacy_filter"):
            self.cfg.backends["privacy_filter"] = "service"

    def _set_detector_order(self, ordered: list[str]) -> None:
        """Apply the operator's ordered detector list. Order is
        preserved verbatim (no canonical re-sort) — the whole point
        of the reorder modal is to let the operator override the
        default priority. Backend auto-defaults still kick in for
        newly-enabled service detectors."""
        if not ordered:
            return
        self.cfg.detector_mode = ",".join(ordered)
        selected = set(ordered)
        for det in ("privacy_filter", "gliner_pii", "llm"):
            if det not in selected:
                # Un-enabled detector → drop its backend so the next
                # enable starts clean (no stale "external + URL" from
                # a previous session).
                self.cfg.backends.pop(det, None)
                continue
            if self.cfg.backends.get(det):
                continue
            self.cfg.backends[det] = "service"

    def _set_use_faker(self, yes: bool) -> None:
        self.cfg.use_faker = yes

    def _toggle_failmode(self, cfg: LaunchConfig, env_var: str, label: str) -> None:
        current_closed = cfg.env_overrides.get(env_var, "true") != "false"
        # Split body + env-var hint so neither line overflows the 78-
        # wide modal even when env_var is the long PRIVACY_FILTER_FAIL_CLOSED.
        self._confirm(
            f"{label} fail mode",
            f"Block requests when the {label} detector errors?",
            current_closed,
            lambda yes: self._set_env(env_var, "true" if yes else "false"),
            hint=f"(sets {env_var}=true on Yes, =false on No)",
        )

    # ── Actions ───────────────────────────────────────────────────────────
    def action_launch(self) -> None:
        """Validate and exit so the caller can run_guardrail. We don't
        run the engine from inside the TUI — Textual would fight for
        the TTY with the streaming container logs."""
        ok, msg = _validate(self.cfg)
        if not ok:
            self.notify(msg, severity="error", timeout=10)
            return
        self._launch_requested = True
        self.exit()

    def action_quit_app(self) -> None:
        self._launch_requested = False
        self.exit()


def _validate(cfg: LaunchConfig) -> tuple[bool, str]:
    """Pre-launch invariants. Same as the CLI subcommand's checks."""
    detectors = cfg.detector_names
    if "llm" in detectors and not cfg.backends.get("llm"):
        return False, (
            "DETECTOR_MODE includes 'llm' but no backend is set. "
            "Open 'LLM backend' and pick service or external."
        )
    if "privacy_filter" in detectors and cfg.flavour == "default" \
            and not cfg.backends.get("privacy_filter"):
        return False, (
            "Privacy-filter needs a remote backend. "
            "Open 'PF backend' and pick service or external."
        )
    if "gliner_pii" in detectors and not cfg.backends.get("gliner_pii"):
        return False, (
            "GLiNER-PII has no in-process variant — pick a backend."
        )
    return True, ""


def run_interactive() -> int:
    """Drive the TUI, then start services + run the guardrail.
    Returns the engine's exit code (or 0 on operator quit)."""
    app = LauncherApp()
    app.run()
    if not app._launch_requested:
        return 0

    cfg = app.cfg
    engine = detect_engine()

    auto_started: list[str] = []
    for det_name, backend in cfg.backends.items():
        if backend == "service":
            try:
                start_service(engine, det_name, log_level=cfg.log_level)
                auto_started.append(det_name)
            except SystemExit as exc:
                return int(exc.code) if isinstance(exc.code, int) else 1
    if auto_started:
        register_atexit_cleanup(engine, auto_started)

    return run_guardrail(engine, cfg)




__all__ = ["run_interactive", "main", "LauncherApp"]
