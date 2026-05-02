"""Textual TUI for the image builder.

Single-screen menuconfig-style picker. Two interlocking widgets:

  * **Preset radio** (top): mutually exclusive list of named subsets
    (`all`, `guardrail`, `privacy-filter`, `gliner-pii`, `minimal`,
    `minimal-fakellm`) plus a `custom` sentinel that lights up when
    the operator hand-edits the selection so it no longer matches a
    named preset.

  * **Checkbox grid** (below): every flavour in `FLAVOURS`, grouped
    by `Flavour.group`. Checking/unchecking a box updates the live
    selection set; the preset radio re-snaps to whatever named
    preset matches (or to `custom`).

The two are kept in sync via a `_syncing` re-entrancy guard — without
it, programmatic widget updates trigger the same `Checkbox.Changed` /
`RadioSet.Changed` handlers that drove them, looping forever.

Hitting Build (or Enter) exits the app with the selected set; the
caller (`run_interactive`) resolves it into BuildPlans and calls the
runner. Cancel/Esc/Q exits with no selection (return code 0).
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
    PRESETS,
    flavours_in_group,
    match_preset,
    preset_names,
)


# Display order for group section headers. Mirrors the bash menu's
# layout the user sketched: guardrail → privacy-filter → gliner-pii →
# companion. Adding a new group means inserting the constant here
# (and one in specs.py).
_GROUP_ORDER: tuple[tuple[str, str], ...] = (
    (GROUP_GUARDRAIL, "anonymizer_guardrail"),
    (GROUP_PF, "privacy-filter"),
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


class BuilderApp(App):
    """Single-screen flavour picker. State is the selected-flavour
    set; the preset radio is a derived view of that state."""

    CSS = """
    Screen { layout: vertical; }

    #presets {
        height: auto;
        padding: 1 2;
        border: solid $primary;
        margin: 1 1 0 1;
    }
    #presets RadioSet { layout: horizontal; height: auto; }
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

    Checkbox { margin: 0 1; height: auto; }

    #buttons {
        height: 3;
        align: center middle;
        padding: 1;
    }
    #buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "build", "Build"),
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

        with Container(id="presets"):
            yield Static("Preset:", classes="group-header")
            with RadioSet(id="preset-radio"):
                for name in preset_names():
                    yield RadioButton(name, name=name)
                yield RadioButton(_CUSTOM_PRESET, name=_CUSTOM_PRESET)

        with VerticalScroll(id="flavour-grid"):
            for group_const, group_label in _GROUP_ORDER:
                yield Static(group_label, classes="group-header")
                for f in flavours_in_group(group_const):
                    yield Checkbox(
                        f.label,
                        value=(f.name in self._selected),
                        # Carry the flavour name as the widget's `name`
                        # so the Changed handler can map back.
                        name=f.name,
                        id=f"cb-{f.name}",
                    )

        with Horizontal(id="buttons"):
            yield Button("Build", id="build-btn", variant="primary")
            yield Button("Cancel", id="cancel-btn")

        yield Footer()

    def on_mount(self) -> None:
        # RadioSet's pressed-item is set after compose so the pressed
        # state lands on the actual mounted RadioButton (setting it
        # before mount races with widget creation in Textual 0.50+).
        self._sync_preset_radio()

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
