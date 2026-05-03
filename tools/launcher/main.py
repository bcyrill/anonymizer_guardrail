"""Flag-driven launcher.

Plain Click, no Typer / rich. Each option carries a `_help_section`
attribute (set via the `grouped_option` decorator) that the custom
`GroupedCommand` reads to print options in named sections under
`--help`. Column widths are computed globally across all sections so
every section's option-name column lines up.

`--ui` / `--interactive` hands off to the Textual menu in
`tools/launcher/menu.py` via an eager callback — same entry point
covers both modes. The bash wrapper (`scripts/launcher.sh`) just
exec's into this module; flag dispatch is Python-side.

Dev-only — installed via `pip install -e ".[dev]"`, never shipped in
the production image.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Click is a dev-only dep (declared in pyproject's [dev] extras). The
# bash wrapper (scripts/launcher.sh) execs into us; if dev wasn't installed,
# we want a clear ImportError pointing at the install command.
try:
    import click
except ImportError as exc:
    print(
        "tools/launcher requires Click (dev dependency). Install with:\n"
        "  pip install -e \".[dev]\"\n"
        f"  ({exc})",
        file=sys.stderr,
    )
    sys.exit(1)

from .engine import detect_engine
from .runner import LaunchConfig, run_guardrail
from .services import auto_start_services, register_atexit_cleanup


# ── Section-grouped help formatting ──────────────────────────────────────
class GroupedCommand(click.Command):
    """Click Command that prints options in named sections in --help.

    Each `@grouped_option(group="…")` tags its option with a
    `_help_section` attribute we read here. Sections appear in
    declaration order; column widths are computed across ALL sections
    so the "option name" column lines up everywhere (the visual
    jaggedness that bothered us in Typer/rich came from per-panel
    width optimisation).
    """

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Group options by their _help_section attr (default: "Options").
        # Preserve declaration order: track the order each section first
        # appears in self.params.
        sections: dict[str, list[click.Option]] = {}
        section_order: list[str] = []
        for param in self.get_params(ctx):
            if not isinstance(param, click.Option):
                continue
            section = getattr(param, "_help_section", "Options")
            if section not in sections:
                sections[section] = []
                section_order.append(section)
            sections[section].append(param)

        # Compute help records (col1, col2) per option, then derive
        # the global col1 width so every section's columns line up.
        section_rows: dict[str, list[tuple[str, str]]] = {}
        all_rows: list[tuple[str, str]] = []
        for section_name in section_order:
            rows: list[tuple[str, str]] = []
            for param in sections[section_name]:
                record = param.get_help_record(ctx)
                if record is not None:
                    rows.append(record)
            section_rows[section_name] = rows
            all_rows.extend(rows)
        if not all_rows:
            return

        # Click's write_dl computes col1 width from the rows it gets
        # (`min(widths[0], col_max) + col_spacing`). col_max is a CAP,
        # not a TARGET — sections with a narrower max width get a
        # narrower column even if we pass a larger col_max. So we
        # left-pad every row's col1 to the global width: that forces
        # `widths[0] == global_col_max` for every section, and the
        # description column starts at the same offset everywhere.
        global_col_max = max(len(col1) for col1, _ in all_rows)

        for section_name in section_order:
            rows = section_rows[section_name]
            if not rows:
                continue
            padded = [(col1.ljust(global_col_max), col2) for col1, col2 in rows]
            with formatter.section(section_name):
                formatter.write_dl(padded, col_max=global_col_max, col_spacing=2)


# Sentinel attribute name for options' help section. Set via
# `grouped_option(group="…")` below; read by `GroupedCommand.format_options`.
_HELP_SECTION_ATTR = "_help_section"


def grouped_option(*args: Any, group: str = "Options", **kwargs: Any):
    """`click.option` wrapper that tags the resulting parameter with a
    `_help_section` attribute. The custom `GroupedCommand` reads that
    attr to bucket options into named sections in --help.

    Wraps Click's decorator so we don't lose any Click features
    (envvar, callback, multiple, etc.). The tagging happens after
    the underlying decorator has registered the param on the function:
    Click appends the new option to `__click_params__`, so the freshly-
    added one is at index -1.
    """
    underlying = click.option(*args, **kwargs)

    def decorator(f):
        f = underlying(f)
        params = getattr(f, "__click_params__", [])
        if params:
            setattr(params[-1], _HELP_SECTION_ATTR, group)
        return f

    return decorator


# ── Presets ────────────────────────────────────────────────────────────────
# Definitions live in `tools/launcher/presets/default.yaml` (bundled)
# plus an optional operator file via `--presets-file PATH` or
# `LAUNCHER_PRESETS_FILE`. See `preset_loader.py` for the schema and
# resolution order.

from .preset_loader import (
    LoadedPreset,
    load_presets,
    set_operator_presets_file,
)


def _apply_preset(cfg: LaunchConfig, preset: str) -> dict[str, str | None]:
    """Apply a named preset to `cfg` in-place. Returns a dict of the
    preset's backend choices (`llm`, `privacy_filter`, `gliner_pii`)
    so the caller can wire auto-start. Keys are detector names;
    values are backend strings or None."""
    try:
        presets = load_presets()
    except RuntimeError as exc:
        # Operator file schema / parse error. Click-format it the
        # same way `_show_presets` does so the surface stays uniform
        # across both reading paths.
        raise click.UsageError(str(exc)) from exc
    if preset not in presets:
        valid = ", ".join(presets)
        raise click.BadParameter(f"Unknown preset {preset!r}. Valid: {valid}")
    spec = presets[preset].spec
    cfg.flavour = spec.flavour
    cfg.detector_mode = spec.detector_mode
    cfg.log_level = spec.log_level
    cfg.use_faker = spec.use_faker
    cfg.env_overrides.update(spec.env_overrides)
    # Service-variant selection (e.g. `privacy_filter` → `hf`). Merge
    # into LaunchConfig.service_variants so the runner's auto-start
    # path picks the variant container. Same shape the menu's
    # variant-edit modal writes; no other plumbing needed.
    cfg.service_variants.update(spec.service_variants)
    # Redis infrastructure backend — only set if the preset declares
    # one. An empty preset value preserves any explicit `--redis-backend`
    # the operator passed before `--preset` on the CLI.
    if spec.redis_backend:
        cfg.redis_backend = spec.redis_backend
    return {
        "llm": spec.llm_backend or None,
        "privacy_filter": spec.pf_backend or None,
        "gliner_pii": spec.gliner_backend or None,
    }


def _render_presets_table() -> "Table":
    """Build a rich Table comparing every loaded preset. One row per
    preset, one column per setting — earlier versions used the inverse
    layout (preset-per-column) but that scaled poorly past ~3 presets
    and made "find the preset that auto-starts gliner" a horizontal-
    scroll exercise. Reading down a name column is the operator's
    typical entry point.

    A "Source" column indicates whether each row was loaded from the
    bundled YAML or from the operator's `LAUNCHER_PRESETS_FILE`. Useful
    when an operator's file replaces a bundled preset by name — the
    operator can verify their override actually took effect.
    """
    from rich.table import Table

    presets = load_presets()

    table = Table(
        title="Launcher presets",
        title_style="bold",
        header_style="bold",
        padding=(0, 1),
    )
    # First column = preset name with an inline source annotation
    # (`(bundled)` / `(operator)`). Cheaper than a dedicated Source
    # column — keeps the most operator-relevant info on the same row
    # head, and reclaims one column of horizontal space for the
    # value cells. Remaining columns = settings; `overflow="fold"`
    # on every value column so long values (multi-detector lists,
    # bundle paths) wrap inside the cell instead of ellipsis-truncating.
    table.add_column("Preset", style="bold cyan", no_wrap=True)
    table.add_column("Detector mode", style="green", overflow="fold")
    table.add_column("Log", style="green", no_wrap=True)
    table.add_column("Faker", style="green", no_wrap=True)
    table.add_column("LLM", style="green", no_wrap=True)
    table.add_column("PF", style="green", no_wrap=True)
    table.add_column("GLiNER", style="green", no_wrap=True)
    table.add_column("Redis", style="green", no_wrap=True)
    table.add_column("Service variants", style="green", overflow="fold")
    table.add_column("Env overrides", style="green", overflow="fold")

    def _backend_cell(spec, attr: str) -> str:
        v = getattr(spec, attr)
        if not v:
            return "[dim]—[/dim]"
        if v == "service":
            return "service"  # `(auto-start)` annotation removed —
                              # column is named "LLM"/"PF"/"GLiNER"
                              # so backend type is the only signal
                              # the cell needs to carry. The plan
                              # printer (print_plan) shows the
                              # auto-start status at run-time.
        return str(v)

    def _env_overrides_cell(spec) -> str:
        if not spec.env_overrides:
            return "[dim]—[/dim]"
        return "\n".join(f"{k}={v}" for k, v in spec.env_overrides.items())

    def _service_variants_cell(spec) -> str:
        if not spec.service_variants:
            return "[dim]—[/dim]"
        return "\n".join(f"{k}={v}" for k, v in spec.service_variants.items())

    def _name_cell(name: str, loaded: LoadedPreset) -> str:
        # Bundled is the common case → no prefix, just the name.
        # Operator-supplied entries get a yellow `operator:` prefix so
        # an override is conspicuously NOT a bundled default — the
        # only signal an operator needs ("did my file's entry land?").
        if loaded.source == "operator":
            return f"[yellow]operator:[/yellow] {name}"
        return name

    def _faker_cell(spec) -> str:
        return "enabled" if spec.use_faker else "opaque"

    for name, loaded in presets.items():
        spec = loaded.spec
        table.add_row(
            _name_cell(name, loaded),
            spec.detector_mode,
            spec.log_level,
            _faker_cell(spec),
            _backend_cell(spec, "llm_backend"),
            _backend_cell(spec, "pf_backend"),
            _backend_cell(spec, "gliner_backend"),
            _backend_cell(spec, "redis_backend"),
            _service_variants_cell(spec),
            _env_overrides_cell(spec),
        )

    return table


def _show_presets(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Eager `--show-presets` callback — renders the loaded presets as
    a rich Table and exits cleanly. Doesn't run any container, doesn't
    engage the engine; pure introspection.

    `expose_value=False` so the flag never reaches the cli() function;
    `is_eager=True` so we exit before required-arg validation triggers
    (operators discovering the launcher should be able to run
    `--show-presets` without first specifying `--type` or `--detector-mode`).

    Reads `--presets-file` if it was passed earlier in the argv (Click
    eager-callback ordering — `--presets-file` is also eager and
    declared before this in the option chain). When unset, falls back
    to `LAUNCHER_PRESETS_FILE`. When neither is set, only bundled
    presets appear in the table.

    `resilient_parsing` is true during shell-completion; skip the
    rendering in that case so completion doesn't accidentally print
    the table into the operator's shell."""
    if not value or ctx.resilient_parsing:
        return
    from rich.console import Console
    # Schema-validation errors from the operator file surface here as
    # RuntimeError. Convert to a Click error so the operator sees a
    # clean "Usage:" header + message instead of a Python traceback —
    # matches how missing-file is reported by `_set_presets_file`.
    try:
        table = _render_presets_table()
    except RuntimeError as exc:
        raise click.UsageError(str(exc)) from exc
    # stderr → keep the table off any stdout pipeline operators might
    # have set up. Same console destination `services.py` uses for
    # status messages.
    Console(stderr=True).print(table)
    ctx.exit(0)


def _set_presets_file(
    ctx: click.Context, _param: click.Parameter, value: str | None,
) -> str | None:
    """Eager callback for `--presets-file`. Stores the path on the
    preset_loader module so `_show_presets` and `_apply_preset` see it.
    Returns the value unchanged so Click's normal flow continues.

    Eager-ordering matters: this callback runs before `--show-presets`
    and before `--preset`, so by the time those fire, the operator
    file is already wired in. Click invokes eager callbacks in the
    order options are declared, and `--presets-file` is declared
    above `--show-presets` and `--preset` in the decorator chain
    below.

    Validates that the file exists at callback time (rather than at
    `load_presets()` time) so the operator's mistake surfaces with
    a clean Click error message tagged to the right flag."""
    if value and not ctx.resilient_parsing:
        if not os.path.isfile(value):
            raise click.BadParameter(
                f"presets file {value!r} does not exist or isn't a file.",
                param_hint="--presets-file",
            )
        set_operator_presets_file(value)
    return value


# ── Section names ─────────────────────────────────────────────────────────
# Constants so a typo in any one --help-section reference shows up at
# import time as a NameError rather than as a "wrong section in help"
# silent bug.
_S_MODE = "Mode"
_S_REQUIRED = "Required"
_S_CONTAINER = "Container"
_S_SURROGATE = "Logging & surrogates"
_S_REGEX = "Regex detector"
_S_DENYLIST = "Denylist detector"
_S_PF = "Privacy-filter detector"
_S_GLINER = "GLiNER-PII detector"
_S_LLM = "LLM detector"
_S_REDIS = "Redis (vault + cache)"
_S_OTHER = "Other"


# ── --ui / --interactive eager callback ───────────────────────────────────
def _open_menu(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Hand off to the Textual TUI when --ui/--interactive is set.

    Eager so it fires before any required-arg validation in the main
    command (the TUI collects everything itself, no CLI flags needed).
    `resilient_parsing` is true during shell-completion; skip the
    handoff in that case so completion doesn't accidentally launch
    a full TUI session.
    """
    if not value or ctx.resilient_parsing:
        return
    # Imported lazily so a CLI-only invocation doesn't pay the
    # textual import cost (~50ms) on startup.
    from .menu import run_interactive
    rc = run_interactive()
    ctx.exit(rc)


# ── Click command ─────────────────────────────────────────────────────────
# Decorator stack: applied bottom-up. Sections are declared per-option
# via `grouped_option(group=...)`. Adding a new flag means picking the
# right group; nothing else to wire.
@click.command(
    cls=GroupedCommand,
    # No args → show this command's help. Without this, `scripts/launcher.sh`
    # (no args) would call the function with all defaults and trip
    # validation deep inside, which is a noisier UX than just showing
    # the help up front.
    no_args_is_help=True,
    # Click's default max_content_width is 80 cols regardless of the
    # actual terminal — that's what was wrapping help text mid-line on
    # wider terminals. 200 is a generous ceiling that lets the actual
    # terminal width drive layout up to that cap.
    context_settings={"max_content_width": 200},
)
# ── Mode ──────────────────────────────────────────────────────────────────
# Eager + expose_value=False: fires before required-arg validation,
# never reaches the cli() function as a kwarg. Two flag spellings so
# both `--ui` (canonical, short) and `--interactive` (verbose) work.
@grouped_option(
    "--ui", "--interactive",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_open_menu,
    group=_S_MODE,
    help="Open the Textual interactive menu instead of CLI mode.",
)
# --presets-file MUST be declared before --show-presets and --preset
# so its eager callback fires first — those downstream callbacks read
# the operator-file path via preset_loader's module-level state.
@grouped_option(
    "--presets-file",
    type=str, default=None,
    expose_value=False,
    is_eager=True,
    callback=_set_presets_file,
    group=_S_MODE,
    help=(
        "Path to a YAML file with operator-supplied launcher presets. "
        "Operator entries with names that collide with bundled presets "
        "replace the bundled entry; new names are appended. Same "
        "behaviour as setting LAUNCHER_PRESETS_FILE in the environment."
    ),
)
@grouped_option(
    "--show-presets", "--show-preset",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_show_presets,
    group=_S_MODE,
    help=(
        "Print a table of bundled (and operator-supplied) --preset "
        "values and the configuration each one applies, then exit. "
        "Doesn't engage the container engine."
    ),
)
# ── Required ──────────────────────────────────────────────────────────────
@grouped_option(
    "--type", "-t", "type_",
    type=str, default=None, group=_S_REQUIRED,
    help="Image flavour. Reserved for future expansion.",
)
@grouped_option(
    "--detector-mode", "-d",
    type=str, default=None, group=_S_REQUIRED,
    help="Comma-separated detectors (regex, denylist, privacy_filter, gliner_pii, llm).",
)
@grouped_option(
    "--preset",
    type=str, default=None, group=_S_REQUIRED,
    help=(
        "Bundled preset name (run `--show-presets` to list). Sets "
        "type, detector mode, and reasonable defaults. Operators can "
        "extend the bundled set via `--presets-file` or "
        "LAUNCHER_PRESETS_FILE."
    ),
)
# ── Container ─────────────────────────────────────────────────────────────
@grouped_option(
    "--name", "-n",
    type=str, default=None, group=_S_CONTAINER,
    help="Container name.",
)
@grouped_option(
    "--port", "-p",
    type=int, default=8000, show_default=True, group=_S_CONTAINER,
    help="Host port to publish.",
)
@grouped_option(
    "--replace",
    is_flag=True, default=False, group=_S_CONTAINER,
    help="Remove a stale container of the same name before starting.",
)
# ── Logging & surrogates ──────────────────────────────────────────────────
@grouped_option(
    "--log-level", "-l",
    type=str, default="info", show_default=True, group=_S_SURROGATE,
    help="debug | info | warning | error.",
)
@grouped_option(
    "--faker/--no-faker",
    default=None, group=_S_SURROGATE,
    help="Realistic Faker surrogates vs opaque [TYPE_HEX] tokens.",
)
@grouped_option(
    "--locale",
    type=str, default=None, group=_S_SURROGATE,
    help="Faker locale, e.g. pt_BR. Implies --faker.",
)
@grouped_option(
    "--surrogate-salt",
    type=str, default=None, group=_S_SURROGATE,
    help="Stable salt across restarts (default: random per process).",
)
# ── Regex detector ────────────────────────────────────────────────────────
@grouped_option(
    "--regex-overlap-strategy",
    type=str, default=None, group=_S_REGEX,
    help="longest (default) | priority.",
)
@grouped_option(
    "--regex-patterns",
    type=str, default=None, group=_S_REGEX,
    help="REGEX_PATTERNS_PATH override.",
)
# ── Denylist detector ─────────────────────────────────────────────────────
@grouped_option(
    "--denylist-path",
    type=str, default=None, group=_S_DENYLIST,
    help="DENYLIST_PATH.",
)
@grouped_option(
    "--denylist-backend",
    type=str, default=None, group=_S_DENYLIST,
    help="regex (default) | aho.",
)
# ── Privacy-filter detector ───────────────────────────────────────────────
@grouped_option(
    "--privacy-filter-backend", "pf_backend",
    type=str, default=None, group=_S_PF,
    help="service (auto-start the sidecar) | external (operator-managed URL). Default service.",
)
@grouped_option(
    "--privacy-filter-variant", "pf_variant",
    type=click.Choice(["opf", "hf"]), default=None, group=_S_PF,
    help=(
        "opf (default) → privacy-filter-service (opf forward + Viterbi). "
        "hf → privacy-filter-hf-service (HF forward + opf Viterbi; ~7x "
        "faster on CPU; experimental). Only applies when --privacy-filter-backend=service."
    ),
)
@grouped_option(
    "--privacy-filter-url", "pf_url",
    type=str, default=None, group=_S_PF,
    help="PRIVACY_FILTER_URL — pointing at an externally-managed privacy-filter-service. Implies --privacy-filter-backend external.",
)
@grouped_option(
    "--privacy-filter-fail-open", "pf_fail_open",
    is_flag=True, default=False, group=_S_PF,
    help="PRIVACY_FILTER_FAIL_CLOSED=false (degrade to no PF matches on error).",
)
# ── GLiNER-PII detector ───────────────────────────────────────────────────
@grouped_option(
    "--gliner-pii-backend", "gliner_backend",
    type=str, default=None, group=_S_GLINER,
    help="service (auto-start) | external (operator URL). No in-process variant.",
)
@grouped_option(
    "--gliner-pii-url", "gliner_url",
    type=str, default=None, group=_S_GLINER,
    help="GLINER_PII_URL — implies --gliner-pii-backend external.",
)
@grouped_option(
    "--gliner-pii-labels", "gliner_labels",
    type=str, default=None, group=_S_GLINER,
    help="GLINER_PII_LABELS (comma-separated zero-shot labels).",
)
@grouped_option(
    "--gliner-pii-threshold", "gliner_threshold",
    type=str, default=None, group=_S_GLINER,
    help="GLINER_PII_THRESHOLD (0..1).",
)
@grouped_option(
    "--gliner-pii-fail-open", "gliner_fail_open",
    is_flag=True, default=False, group=_S_GLINER,
    help="GLINER_PII_FAIL_CLOSED=false (degrade to no gliner matches on error).",
)
# ── LLM detector ──────────────────────────────────────────────────────────
@grouped_option(
    "--llm-backend",
    type=str, default=None, group=_S_LLM,
    help="service (auto-start fake-llm) | external (operator URL).",
)
@grouped_option(
    "--llm-api-base",
    type=str, default=None, group=_S_LLM,
    help="LLM_API_BASE — only meaningful with --llm-backend external.",
)
@grouped_option(
    "--llm-api-key",
    type=str, default=None, group=_S_LLM,
    help="LLM_API_KEY — empty allowed for unauthenticated dev backends.",
)
@grouped_option(
    "--llm-model",
    type=str, default=None, group=_S_LLM,
    help="LLM_MODEL — model alias for the detection LLM.",
)
@grouped_option(
    "--llm-prompt",
    type=str, default=None, group=_S_LLM,
    help="LLM_SYSTEM_PROMPT_PATH override (bundled:NAME or filesystem path).",
)
@grouped_option(
    "--forward-llm-key",
    is_flag=True, default=False, group=_S_LLM,
    help="LLM_USE_FORWARDED_KEY=true (forward caller's Authorization to LLM).",
)
@grouped_option(
    "--llm-fail-open",
    is_flag=True, default=False, group=_S_LLM,
    help="LLM_FAIL_CLOSED=false (LLM errors degrade vs block).",
)
# ── Redis (vault + cache) ────────────────────────────────────────────────
@grouped_option(
    "--redis-backend",
    type=str, default=None, group=_S_REDIS,
    help=(
        "service (auto-start the shared anonymizer-redis container "
        "and inject VAULT_REDIS_URL / CACHE_REDIS_URL) | external "
        "(operator supplies URLs via env). When unset, no Redis is "
        "wired and VAULT_BACKEND=redis / *_CACHE_BACKEND=redis "
        "would fail at boot for missing URLs."
    ),
)
# ── Other ─────────────────────────────────────────────────────────────────
@grouped_option(
    "--rules",
    type=str, default=None, group=_S_OTHER,
    help="Path to fake-llm rules YAML (mounted at /app/rules.yaml).",
)
@click.argument("extra", nargs=-1)
def cli(
    type_: str | None,
    detector_mode: str | None,
    preset: str | None,
    name: str | None,
    port: int,
    replace: bool,
    log_level: str,
    faker: bool | None,
    locale: str | None,
    surrogate_salt: str | None,
    regex_overlap_strategy: str | None,
    regex_patterns: str | None,
    denylist_path: str | None,
    denylist_backend: str | None,
    pf_backend: str | None,
    pf_variant: str | None,
    pf_url: str | None,
    pf_fail_open: bool,
    gliner_backend: str | None,
    gliner_url: str | None,
    gliner_labels: str | None,
    gliner_threshold: str | None,
    gliner_fail_open: bool,
    llm_backend: str | None,
    llm_api_base: str | None,
    llm_api_key: str | None,
    llm_model: str | None,
    llm_prompt: str | None,
    forward_llm_key: bool,
    llm_fail_open: bool,
    redis_backend: str | None,
    rules: str | None,
    extra: tuple[str, ...],
) -> None:
    """Launch the anonymizer-guardrail container with the given configuration.

    Anything after `--` is passed straight through to `podman/docker run`.
    For an interactive walkthrough instead of flags, pass `--ui`.
    """

    cfg = LaunchConfig()

    # Apply preset first so explicit flags can override.
    preset_backends: dict[str, str | None] = {}
    if preset:
        preset_backends = _apply_preset(cfg, preset)

    if type_:
        cfg.flavour = type_
    if detector_mode:
        cfg.detector_mode = detector_mode
    if not cfg.flavour:
        raise click.BadParameter("Missing --type or --preset.")
    if not cfg.detector_mode:
        raise click.BadParameter("Missing --detector-mode or --preset.")

    if name:
        cfg.name = name
    cfg.port = port
    cfg.replace_existing = replace
    cfg.log_level = log_level
    if faker is not None:
        cfg.use_faker = faker
    if locale is not None:
        cfg.faker_locale = locale
        cfg.use_faker = True
    if surrogate_salt is not None:
        cfg.surrogate_salt = surrogate_salt
    if rules:
        cfg.fake_llm_rules_file = rules
    if extra:
        cfg.extra_run_args = list(extra)

    # ── Per-detector overrides ────────────────────────────────────────────
    # Each --foo-XXX flag maps to one env var in the matching detector's
    # guardrail_env_passthroughs. Validation lives below; the env var
    # forwarding is data-driven by LAUNCHER_METADATA in the runner.
    if regex_overlap_strategy is not None:
        cfg.env_overrides["REGEX_OVERLAP_STRATEGY"] = regex_overlap_strategy
    if regex_patterns is not None:
        cfg.env_overrides["REGEX_PATTERNS_PATH"] = regex_patterns
    if denylist_path is not None:
        cfg.env_overrides["DENYLIST_PATH"] = denylist_path
    if denylist_backend is not None:
        cfg.env_overrides["DENYLIST_BACKEND"] = denylist_backend
    if llm_api_base is not None:
        cfg.env_overrides["LLM_API_BASE"] = llm_api_base
    if llm_api_key is not None:
        cfg.env_overrides["LLM_API_KEY"] = llm_api_key
    if llm_model is not None:
        cfg.env_overrides["LLM_MODEL"] = llm_model
    if llm_prompt is not None:
        cfg.env_overrides["LLM_SYSTEM_PROMPT_PATH"] = llm_prompt
    if forward_llm_key:
        cfg.env_overrides["LLM_USE_FORWARDED_KEY"] = "true"
    if llm_fail_open:
        cfg.env_overrides["LLM_FAIL_CLOSED"] = "false"
    if pf_url is not None:
        cfg.env_overrides["PRIVACY_FILTER_URL"] = pf_url
        if not pf_backend:
            pf_backend = "external"
    # `--privacy-filter-variant` is only meaningful when the launcher
    # auto-starts the sidecar (backend=service). For external backends
    # the operator's URL already pins which service is in play, so we
    # don't set the variant at all in that case (the variant flag
    # would be silently ignored — fine, but worth noting).
    if pf_variant and pf_variant != "opf":
        cfg.service_variants["privacy_filter"] = pf_variant
    if pf_fail_open:
        cfg.env_overrides["PRIVACY_FILTER_FAIL_CLOSED"] = "false"
    if gliner_url is not None:
        cfg.env_overrides["GLINER_PII_URL"] = gliner_url
        if not gliner_backend:
            gliner_backend = "external"
    if gliner_labels is not None:
        cfg.env_overrides["GLINER_PII_LABELS"] = gliner_labels
    if gliner_threshold is not None:
        cfg.env_overrides["GLINER_PII_THRESHOLD"] = gliner_threshold
    if gliner_fail_open:
        cfg.env_overrides["GLINER_PII_FAIL_CLOSED"] = "false"

    # ── Backend selection per detector ────────────────────────────────────
    if llm_backend or preset_backends.get("llm"):
        cfg.backends["llm"] = llm_backend or preset_backends.get("llm") or ""
    # privacy_filter is remote-only. Default to "service" (auto-start
    # the sidecar) unless the operator explicitly picked "external" —
    # matches gliner_pii's UX.
    pf_resolved = pf_backend or preset_backends.get("privacy_filter")
    if pf_resolved:
        cfg.backends["privacy_filter"] = pf_resolved
    elif "privacy_filter" in cfg.detector_names:
        cfg.backends["privacy_filter"] = "service"
    if gliner_backend:
        cfg.backends["gliner_pii"] = gliner_backend

    # Redis infrastructure backend. Explicit CLI flag wins over the
    # preset's value (the preset's redis_backend was already applied
    # by `_apply_preset` above; the explicit flag overrides only when
    # set). Same precedence pattern as `llm_backend or preset_backends.get(…)`
    # immediately above.
    if redis_backend:
        cfg.redis_backend = redis_backend

    # ── Validation ───────────────────────────────────────────────────────
    detectors = cfg.detector_names
    if "llm" in detectors and not cfg.backends.get("llm"):
        raise click.BadParameter(
            "DETECTOR_MODE includes 'llm' but --llm-backend isn't set "
            "(use 'service' to auto-start fake-llm or 'external' with "
            "--llm-api-base for a real LLM)."
        )
    if "gliner_pii" in detectors and not cfg.backends.get("gliner_pii"):
        raise click.BadParameter(
            "DETECTOR_MODE includes 'gliner_pii' but "
            "--gliner-pii-backend isn't set (gliner_pii is remote-"
            "only — pass 'service' or 'external')."
        )

    # ── Auto-start services ──────────────────────────────────────────────
    engine = detect_engine()
    # Validate the operator's `--rules` path here (CLI-side concern),
    # then let `auto_start_services` do the loop. The TUI doesn't
    # expose `--rules` so it skips this block and passes no extras.
    extra_volumes: dict[str, list[tuple[str, str]]] = {}
    if cfg.backends.get("llm") == "service" and cfg.fake_llm_rules_file:
        rules_path = os.path.abspath(cfg.fake_llm_rules_file)
        if not os.path.isfile(rules_path):
            raise click.BadParameter(
                f"--rules path {cfg.fake_llm_rules_file!r} doesn't "
                f"exist (resolved to {rules_path}).",
                param_hint="--rules",
            )
        extra_volumes["llm"] = [(rules_path, "/app/rules.yaml:ro")]

    auto_started_detectors = auto_start_services(
        engine, cfg, extra_volumes=extra_volumes,
    )
    if auto_started_detectors:
        register_atexit_cleanup(engine, auto_started_detectors)

    # ── Run ──────────────────────────────────────────────────────────────
    rc = run_guardrail(engine, cfg)
    sys.exit(rc)


def main() -> None:
    """Entry point invoked by `python -m tools.launcher` and the
    scripts/launcher.sh wrapper.

    `LAUNCHER_PROG_NAME` is set by the bash wrapper so the Usage line
    in --help reads `scripts/launcher.sh` rather than
    `python -m tools.launcher`. Without the env var we fall back to
    Click's default (sys.argv[0])."""
    prog_name = os.environ.get("LAUNCHER_PROG_NAME")
    if prog_name:
        cli.main(prog_name=prog_name)
    else:
        cli.main()


if __name__ == "__main__":
    main()
