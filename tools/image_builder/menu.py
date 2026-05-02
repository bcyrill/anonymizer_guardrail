"""Textual TUI for the image builder.

Single-screen menuconfig-style picker. Two interlocking widgets:

  * **Preset radio** (top): mutually exclusive list of named subsets
    (`all`, `guardrail`, `privacy-filter`, `gliner-pii`, `minimal`,
    `minimal-fakellm`) plus a `custom` sentinel that lights up when
    the operator hand-edits the selection so it no longer matches a
    named preset.

  * **Checkbox grid** (below): every flavour in `FLAVOURS`, grouped
    by `Flavour.group` and laid out two columns wide so the catalog
    fits on a normal terminal without scrolling. Checking/unchecking
    a box updates the live selection set; the preset radio re-snaps
    to whatever named preset matches (or to `custom`).

The two are kept in sync via a `_syncing` re-entrancy guard — without
it, programmatic widget updates trigger the same `Checkbox.Changed` /
`RadioSet.Changed` handlers that drove them, looping forever.

Keyboard model: arrow up/down move focus between widgets (priority
bindings, so they trump the RadioSet's own up/down which would
otherwise trap focus inside the preset row). Left/right still cycles
within the horizontal RadioSet. Tab works as a fallback. Build is
ctrl+b (Enter would clash with checkbox toggling); Cancel is q/Esc.
"""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    RadioButton,
    RadioSet,
    Static,
)

from .runner import print_plan, resolve_plans, run_all
from .specs import (
    FLAVOURS,
    FLAVOURS_BY_NAME,
    GROUP_COMPANION,
    GROUP_GLINER,
    GROUP_GUARDRAIL,
    GROUP_PF,
    GROUP_PF_HF,
    PRESETS,
    flavours_in_group,
    match_preset,
    preset_names,
)


# Display order for group section headers. Adding a new group means
# inserting the constant here (and one in specs.py). Missing a group
# here is a hard bug: any flavour in that group is in FLAVOURS but
# its checkbox is never rendered, so the preset "check these
# flavours" path fails with `NoMatches` when it tries to query the
# checkbox by id.
_GROUP_ORDER: tuple[tuple[str, str], ...] = (
    (GROUP_GUARDRAIL, "anonymizer_guardrail"),
    (GROUP_PF, "privacy-filter"),
    (GROUP_PF_HF, "privacy-filter (HF variant)"),
    (GROUP_GLINER, "gliner-pii"),
    (GROUP_COMPANION, "companion"),
)


# Sentinel name used by the preset RadioSet for "no preset matches the
# current selection" — surfaces as a separate radio item so the user
# can see they're in custom mode rather than guessing.
_CUSTOM_PRESET = "custom"

# Default preset on first open. Matches the recommended development
# setup (slim guardrail + CPU runtime-download services). Operators
# almost always want this for local iteration.
_DEFAULT_PRESET = "minimal"


class _PresetRadioSet(RadioSet):
    """RadioSet variant where arrow nav commits the new highlight as
    the selection AND skips past the `custom` sentinel.

    Two deviations from stock Textual RadioSet:

      * Stock treats left/right as highlight movement only — the
        operator must then press Enter to commit. For a horizontal
        preset row that *is* the selection, that's a confusing extra
        step: the operator sees the highlight move but the checkboxes
        below don't update until they hit Enter. Auto-committing on
        navigation makes left/right feel like "switch preset".
      * `custom` is a state indicator (lights up when the operator's
        manual checkbox toggling no longer matches any named preset),
        not a destination. Selecting it explicitly is a no-op
        (`_on_preset_changed` early-returns), so arrowing onto it
        and committing would be a dead keypress. The action methods
        skip past it in whichever direction the operator was going.
    """

    def action_next_button(self) -> None:
        super().action_next_button()
        if self._highlight_is_custom():
            super().action_next_button()
        self._commit_highlight()

    def action_previous_button(self) -> None:
        super().action_previous_button()
        if self._highlight_is_custom():
            super().action_previous_button()
        self._commit_highlight()

    def _highlight_is_custom(self) -> bool:
        idx = getattr(self, "_selected", None)
        if idx is None:
            return False
        buttons = list(self.query(RadioButton))
        return 0 <= idx < len(buttons) and buttons[idx].name == _CUSTOM_PRESET

    def _commit_highlight(self) -> None:
        # `_selected` is RadioSet's internal highlight index. Setting
        # value=True on that RadioButton triggers a Changed event the
        # outer RadioSet handles to deselect the others — same path
        # an Enter would take, just without the operator having to
        # press it.
        idx = getattr(self, "_selected", None)
        if idx is None:
            return
        buttons = list(self.query(RadioButton))
        if 0 <= idx < len(buttons):
            buttons[idx].value = True


class _NoFocusScroll(VerticalScroll):
    """VerticalScroll that doesn't take keyboard focus so `down` from
    the preset radio lands on the first Checkbox directly. The stock
    VerticalScroll is focusable so operators can page-scroll long
    content — but our flavour grid is short enough to fit on one
    screen, and putting the scroll wrapper in the focus chain forces
    a no-op `down` press before the operator can navigate flavours.
    """

    can_focus = False


class BuilderApp(App):
    """Single-screen flavour picker. State is the selected-flavour
    set; the preset radio is a derived view of that state."""

    CSS = """
    Screen { layout: vertical; }

    #presets {
        height: auto;
        padding: 0 2;
        border: solid $primary;
        margin: 1 1 0 1;
    }
    #presets RadioSet {
        layout: horizontal;
        height: auto;
        border: none;
        padding: 0;
        background: transparent;
    }
    #presets RadioButton { margin-right: 2; }

    #flavour-grid {
        height: 1fr;
        padding: 0 2;
        margin: 0 1;
    }

    .group-header {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }

    .flavour-cols {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-rows: auto;
        grid-gutter: 0 2;
        height: auto;
    }

    Checkbox { margin: 0 1; height: auto; }

    #buttons {
        height: auto;
        align: center middle;
        padding: 1;
    }
    #buttons Button { margin: 0 1; }
    """

    # Up/down are `priority=True` so they fire BEFORE focused-widget
    # bindings — RadioSet's own up/down would otherwise cycle radio
    # buttons and trap focus inside the preset row. Left/right are
    # left to RadioSet's defaults so the operator can still scroll
    # horizontally through presets. ctrl+b for Build instead of enter
    # because enter on a focused Checkbox toggles it; a global enter
    # binding would surprise the operator mid-selection.
    BINDINGS = [
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+b", "build", "Build"),
        Binding("up", "focus_previous_widget", show=False, priority=True),
        Binding("down", "focus_next_widget", show=False, priority=True),
    ]

    TITLE = "anonymizer-guardrail image builder"

    def __init__(self) -> None:
        super().__init__()
        self._selected: set[str] = set(PRESETS[_DEFAULT_PRESET])
        # Re-entrancy guard: when we programmatically toggle a Checkbox
        # or set the RadioSet's pressed item, the matching Changed
        # event fires. Without the guard, the handler interprets that
        # as user input and drives a second update, etc.
        self._syncing = False
        # Set by action_build; consumed by run_interactive() below.
        self._build_requested = False

    # ── Layout ─────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header()

        # Preset section — `border_title` puts the label inline with
        # the top border so we don't burn a row on a separate Static.
        with Container(id="presets"):
            with _PresetRadioSet(id="preset-radio"):
                for name in preset_names():
                    yield RadioButton(name, name=name)
                yield RadioButton(_CUSTOM_PRESET, name=_CUSTOM_PRESET)

        # Flavour catalog — one Container per group so each group's
        # checkboxes live in their own 2-column grid (a single grid
        # spanning all groups would let column 1 of one group sit
        # next to column 2 of the next, which is confusing).
        with _NoFocusScroll(id="flavour-grid"):
            for group_const, group_label in _GROUP_ORDER:
                yield Static(group_label, classes="group-header")
                with Container(classes="flavour-cols"):
                    for f in flavours_in_group(group_const):
                        yield Checkbox(
                            f.label,
                            value=(f.name in self._selected),
                            # Carry the flavour name as the widget's
                            # `name` so the Changed handler can map back.
                            name=f.name,
                            id=f"cb-{f.name}",
                        )

        with Horizontal(id="buttons"):
            yield Button("Build", id="build-btn", variant="primary")
            yield Button("Cancel", id="cancel-btn")

        yield Footer()

    def on_mount(self) -> None:
        # `border_title` is set after mount so the title lands on the
        # actual rendered border (assignment before mount can race
        # with widget creation in Textual 0.50+). Same reasoning as
        # the preset-radio sync below.
        self.query_one("#presets", Container).border_title = "Preset"
        self._sync_preset_radio()
        # Anchor initial focus on the preset radio. Textual's default
        # auto-focus picks the first focusable in layout order, which
        # can land on the VerticalScroll wrapping the flavour grid —
        # the operator's first arrow keypress then walks checkboxes
        # before they've even seen the preset row. Forcing focus to
        # the radio here gives a predictable starting point: left/
        # right cycles presets, down drops into the grid.
        self.query_one("#preset-radio", RadioSet).focus()

    # ── Focus actions ─────────────────────────────────────────────────────
    # Bound to up/down at priority so they short-circuit the RadioSet's
    # built-in up/down (which would otherwise re-cycle radio buttons
    # and never let focus leave the preset row).
    def action_focus_next_widget(self) -> None:
        self.screen.focus_next()

    def action_focus_previous_widget(self) -> None:
        self.screen.focus_previous()

    # ── Event handlers ─────────────────────────────────────────────────────
    @on(RadioSet.Changed, "#preset-radio")
    def _on_preset_changed(self, event: RadioSet.Changed) -> None:
        if self._syncing:
            return
        if event.pressed is None:
            return
        choice = event.pressed.name
        if choice == _CUSTOM_PRESET or choice is None:
            # `custom` isn't a real preset — picking it just acknowledges
            # the operator is happy with whatever boxes are checked.
            return
        members = PRESETS.get(choice)
        if members is None:
            return
        self._selected = set(members)
        self._sync_checkboxes()

    @on(Checkbox.Changed)
    def _on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._syncing:
            return
        cb = event.checkbox
        flavour_name = cb.name
        if flavour_name is None or flavour_name not in FLAVOURS_BY_NAME:
            return
        if cb.value:
            self._selected.add(flavour_name)
        else:
            self._selected.discard(flavour_name)
        self._sync_preset_radio()

    @on(Button.Pressed, "#build-btn")
    def _on_build(self) -> None:
        self.action_build()

    @on(Button.Pressed, "#cancel-btn")
    def _on_cancel(self) -> None:
        self.action_cancel()

    # ── Bindings ───────────────────────────────────────────────────────────
    def action_build(self) -> None:
        if not self._selected:
            self.notify(
                "Pick at least one flavour before building.",
                severity="warning",
            )
            return
        self._build_requested = True
        self.exit()

    def action_cancel(self) -> None:
        self._build_requested = False
        self.exit()

    # ── Sync helpers ───────────────────────────────────────────────────────
    def _sync_checkboxes(self) -> None:
        """Programmatically tick every checkbox to match `_selected`."""
        self._syncing = True
        try:
            for f in FLAVOURS:
                cb = self.query_one(f"#cb-{f.name}", Checkbox)
                cb.value = f.name in self._selected
        finally:
            self._syncing = False

    def _sync_preset_radio(self) -> None:
        """Update the preset radio to reflect `_selected` (snap to a
        named preset when the set matches one, otherwise `custom`)."""
        match = match_preset(frozenset(self._selected)) or _CUSTOM_PRESET
        self._syncing = True
        try:
            radio = self.query_one("#preset-radio", RadioSet)
            for i, button in enumerate(radio.query(RadioButton)):
                if button.name == match:
                    # `_selected` index drives the pressed state; setting
                    # `value` on the matching button leaves the others
                    # alone (RadioSet enforces single-selection).
                    radio._selected = i  # noqa: SLF001 — Textual API
                    button.value = True
                else:
                    button.value = False
        finally:
            self._syncing = False


def run_interactive() -> int:
    """Open the TUI; on Build, dispatch to the runner. Return code is
    the runner's exit (or 0 on cancel).

    Mirrors `tools.launcher.menu.run_interactive` — keeps the unified
    `--ui` entry point in `main.py` symmetric across both tools.
    """
    app = BuilderApp()
    app.run()
    if not app._build_requested:
        return 0

    # Resolve selection in catalog order so the build sequence is
    # deterministic regardless of when each box was checked. Operators
    # rebuilding "all" repeatedly should see the same flavour order.
    resolved = [f for f in FLAVOURS if f.name in app._selected]
    plans = resolve_plans(resolved, tag_override=None)

    # The TUI doesn't expose --engine / --tag / passthrough, so the
    # runner gets defaults: detected engine, no extras. Operators with
    # those needs use the CLI form directly (see scripts/image_builder.sh
    # --help).
    from ..launcher.engine import detect_engine
    engine = detect_engine()

    print_plan(engine.name, plans, extra=[])
    return run_all(engine.name, plans, extra=[])


__all__ = ["BuilderApp", "run_interactive"]
