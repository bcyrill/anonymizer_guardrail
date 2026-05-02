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
from .services import register_atexit_cleanup, start_service
from .spec_extras import LAUNCHER_METADATA


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
# Mirror the bash --preset values. Each preset partially populates a
# LaunchConfig; flags applied after the preset override.
_PRESETS: dict[str, dict[str, object]] = {
    "uuid-debug": {
        "flavour": "slim",
        "detector_mode": "regex,llm",
        "log_level": "debug",
        "use_faker": False,
        "_llm_backend": "service",
    },
    "pentest": {
        "flavour": "slim",
        "detector_mode": "regex,privacy_filter,llm",
        "log_level": "debug",
        "use_faker": False,
        "_llm_backend": "service",
        "_pf_backend": "service",
        "_env_overrides": {
            "REGEX_PATTERNS_PATH": "bundled:regex_pentest.yaml",
            "LLM_SYSTEM_PROMPT_PATH": "bundled:llm_pentest.md",
        },
    },
    "regex-only": {
        "flavour": "slim",
        "detector_mode": "regex",
        "log_level": "info",
        "use_faker": True,
    },
}


def _apply_preset(cfg: LaunchConfig, preset: str) -> dict[str, str | None]:
    """Apply a named preset to `cfg` in-place. Returns a dict of the
    preset's backend choices (`llm`, `privacy_filter`, …) so the caller
    can wire auto-start. Keys are detector names; values are backend
    strings or None."""
    if preset not in _PRESETS:
        valid = ", ".join(_PRESETS)
        raise click.BadParameter(f"Unknown preset {preset!r}. Valid: {valid}")
    p = _PRESETS[preset]
    for k, v in p.items():
        if k.startswith("_"):
            continue
        setattr(cfg, k, v)
    cfg.env_overrides.update(p.get("_env_overrides", {}))  # type: ignore[arg-type]
    return {
        "llm": p.get("_llm_backend"),  # type: ignore[arg-type]
        "privacy_filter": p.get("_pf_backend"),  # type: ignore[arg-type]
    }


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
# ── Required ──────────────────────────────────────────────────────────────
@grouped_option(
    "--type", "-t", "type_",
    type=str, default=None, group=_S_REQUIRED,
    help="Image flavour: slim (the only flavour). Reserved for future expansion.",
)
@grouped_option(
    "--detector-mode", "-d",
    type=str, default=None, group=_S_REQUIRED,
    help="Comma-separated detectors (regex, denylist, privacy_filter, gliner_pii, llm).",
)
@grouped_option(
    "--preset",
    type=str, default=None, group=_S_REQUIRED,
    help=f"Bundled preset: {', '.join(_PRESETS)}. Sets type, detector mode, and reasonable defaults.",
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
@grouped_option(
    "--hf-offline",
    is_flag=True, default=False, group=_S_CONTAINER,
    help="Pass HF_HUB_OFFLINE=1 to the guardrail (pf flavour only).",
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
    hf_offline: bool,
    log_level: str,
    faker: bool | None,
    locale: str | None,
    surrogate_salt: str | None,
    regex_overlap_strategy: str | None,
    regex_patterns: str | None,
    denylist_path: str | None,
    denylist_backend: str | None,
    pf_backend: str | None,
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
    cfg.hf_offline = hf_offline
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
    auto_started_detectors: list[str] = []
    for det_name, backend in cfg.backends.items():
        if backend == "service":
            spec = LAUNCHER_METADATA.get(det_name)
            if spec is None or spec.service is None:
                raise click.BadParameter(
                    f"Detector {det_name!r} has no service to auto-start. "
                    f"Use --{det_name.replace('_', '-')}-backend external."
                )
            extra_volumes: list[tuple[str, str]] = []
            # Mount the operator's --rules YAML into the auto-started
            # fake-llm. Other services don't currently consume extra
            # bind mounts; if a second one ever does, generalise via a
            # ServiceSpec field rather than stacking detector names here.
            if det_name == "llm" and cfg.fake_llm_rules_file:
                rules_path = os.path.abspath(cfg.fake_llm_rules_file)
                if not os.path.isfile(rules_path):
                    raise click.BadParameter(
                        f"--rules path {cfg.fake_llm_rules_file!r} doesn't "
                        f"exist (resolved to {rules_path}).",
                        param_hint="--rules",
                    )
                extra_volumes.append((rules_path, "/app/rules.yaml:ro"))
            start_service(
                engine, det_name,
                log_level=cfg.log_level,
                extra_volumes=extra_volumes or None,
            )
            auto_started_detectors.append(det_name)

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
