"""Compose and exec the guardrail container run command.

Takes a `LaunchConfig` (built by either the typer CLI or the
questionary TUI), assembles the `podman/docker run` argv from per-
detector LAUNCHER_METADATA, prints the operator-facing plan, and
exec's into the engine. Replaces the bash `build_run_args` +
`print_plan` + `run_guardrail` triple.

Adding a new detector with new env vars: add the var names to
`LAUNCHER_METADATA[name].guardrail_env_passthroughs` — this composer
picks them up automatically. No edit needed here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Sequence

from rich.console import Console
from rich.table import Table

from .engine import Engine
from .services import started_services
from .spec_extras import LAUNCHER_METADATA, CONTAINER_NAME_DEFAULT


# stderr console so plan/status output doesn't interleave with the
# guardrail container's own stdout once `subprocess.call` takes over.
# rich auto-detects TTY/colour and respects NO_COLOR.
_console = Console(stderr=True)


# ── Per-flavour image map ─────────────────────────────────────────────────
# Slim is the only guardrail flavour — privacy-filter and gliner-pii
# ship as standalone services. Tag override comes from `TAG_SLIM` so
# image_builder.sh and the launcher agree on naming.
_FLAVOUR_TAG_DEFAULTS = {"slim": "anonymizer-guardrail:latest"}
_FLAVOUR_TAG_ENVS = {"slim": "TAG_SLIM"}


def resolve_image(flavour: str) -> str:
    """Pick the operator-resolved image tag for the given flavour.
    `TAG_<FLAVOUR>` env wins; otherwise the bundled default."""
    env = _FLAVOUR_TAG_ENVS.get(flavour)
    if env is None:
        raise RuntimeError(f"Unknown flavour {flavour!r}. Valid: slim.")
    return os.environ.get(env) or _FLAVOUR_TAG_DEFAULTS[flavour]


@dataclass
class LaunchConfig:
    """Operator-resolved choices fed to the run-command composer.

    Built by either the typer CLI (from flags + presets) or the
    questionary TUI (from interactive prompts). The composer reads
    this and assembles the final `podman run` argv — no further
    decisions, just translation.
    """

    flavour: str = "slim"
    detector_mode: str = "regex"
    log_level: str = "info"

    # Container settings
    name: str = CONTAINER_NAME_DEFAULT
    port: int = 8000
    replace_existing: bool = False

    # Surrogate / Faker
    use_faker: bool = False
    faker_locale: str = ""
    surrogate_salt: str = ""

    # Per-detector env vars — operator can pre-populate any of these
    # via flags / interactive prompts. Each detector's launcher spec
    # declares which vars to forward. Empty values are skipped.
    env_overrides: dict[str, str] = field(default_factory=dict)

    # Per-detector backend selection (service / external) for the
    # detectors that have a service. Stored alongside env_overrides
    # so the runner can decide which auto-start dispatches to fire.
    # Keys are detector names; values are "service" / "external" / "" (skip).
    backends: dict[str, str] = field(default_factory=dict)

    # Optional path to a fake-llm rules file (mounted into the auto-
    # started fake-llm container at /app/rules.yaml). Empty = use the
    # bundled rules.example.yaml.
    fake_llm_rules_file: str = ""

    # Pass-through extras for `podman run` (operator's `--` after the
    # subcommand). Lets advanced operators add `-v`, `--memory`, etc.
    extra_run_args: list[str] = field(default_factory=list)

    @property
    def detector_names(self) -> list[str]:
        return [n.strip() for n in self.detector_mode.split(",") if n.strip()]


def build_run_argv(engine: Engine, cfg: LaunchConfig) -> list[str]:
    """Assemble the `podman/docker run` argv for the guardrail.

    Reads LAUNCHER_METADATA to pick up per-detector env passthroughs
    so a new detector with new env vars Just Works without edits here.
    """
    image = resolve_image(cfg.flavour)
    network_args: list[str] = []
    env_args: list[str] = []

    # Network: join the shared net whenever any auto-started service
    # is in play (the guardrail dials services by container name).
    auto_started = started_services()
    if auto_started:
        from .spec_extras import SHARED_NETWORK
        network_args = ["--network", SHARED_NETWORK]

    # Per-detector env passthroughs. Walk active detectors in
    # priority order so the env var ordering is deterministic.
    for det_name in cfg.detector_names:
        spec = LAUNCHER_METADATA.get(det_name)
        if spec is None:
            continue

        # Step 1: pass through operator-set env vars.
        for var in spec.guardrail_env_passthroughs:
            val = cfg.env_overrides.get(var) or os.environ.get(var, "")
            if val:
                env_args.extend(["-e", f"{var}={val}"])

        # Step 2: if we auto-started this detector's service, set the
        # service-pointing env vars. These OVERRIDE any operator-set
        # values from Step 1 because the auto-start URL is canonical
        # (the operator picked `service` backend → they want the
        # auto-started service, not whatever URL they had in their env).
        if det_name in auto_started and spec.service:
            # Drop any earlier `-e <K>=...` for the same K to avoid
            # duplicate flags (the engine resolves to the LAST -e but
            # readability suffers).
            already_set_keys = {
                env_args[i + 1].split("=", 1)[0]
                for i in range(0, len(env_args), 2)
                if i + 1 < len(env_args) and env_args[i] == "-e"
            }
            for k, v in spec.service.guardrail_env_when_started.items():
                if k in already_set_keys:
                    # Replace the previous binding.
                    for i in range(0, len(env_args) - 1, 2):
                        if env_args[i] == "-e" and env_args[i + 1].startswith(f"{k}="):
                            env_args[i + 1] = f"{k}={v}"
                            break
                else:
                    env_args.extend(["-e", f"{k}={v}"])

    # Faker / locale knobs live on the central Config, not per-detector.
    if not cfg.use_faker:
        env_args.extend(["-e", "USE_FAKER=false"])
    if cfg.faker_locale:
        env_args.extend(["-e", f"FAKER_LOCALE={cfg.faker_locale}"])
    if cfg.surrogate_salt:
        env_args.extend(["-e", f"SURROGATE_SALT={cfg.surrogate_salt}"])

    return [
        engine.name, "run", "--rm",
        "--name", cfg.name,
        "-p", f"{cfg.port}:8000",
        *network_args,
        "-e", f"DETECTOR_MODE={cfg.detector_mode}",
        "-e", f"LOG_LEVEL={cfg.log_level}",
        *env_args,
        *cfg.extra_run_args,
        image,
    ]


def print_plan(engine: Engine, cfg: LaunchConfig) -> None:
    """Operator-facing summary of what we're about to run.

    Renders as a rich Table so column alignment is correct regardless
    of the longest value, and color/no-color is auto-detected. Old
    versions used hand-tuned ANSI codes; rich replaces them with a
    proper renderer that respects NO_COLOR / non-TTY contexts.
    """
    image = resolve_image(cfg.flavour)
    auto_started = started_services()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()

    table.add_row("Engine",    f"[green]{engine.name}[/green]")
    table.add_row("Flavour",   f"[green]{cfg.flavour}[/green]")
    table.add_row("Image",     f"[green]{image}[/green]")
    table.add_row("Container", f"[green]{cfg.name}[/green]")
    table.add_row("Port",      f"[green]{cfg.port}:8000[/green]")
    table.add_row("Detectors", f"[green]{cfg.detector_mode}[/green]")
    table.add_row("Log level", f"[green]{cfg.log_level}[/green]")

    # Per-detector backend / service indicator.
    for det_name in cfg.detector_names:
        spec = LAUNCHER_METADATA.get(det_name)
        if spec is None or spec.service is None:
            continue
        backend = cfg.backends.get(det_name, "")
        if det_name in auto_started:
            table.add_row(
                det_name,
                f"[green]service[/green] "
                f"[dim](auto-started, {spec.service.container_name}:{spec.service.port})[/dim]",
            )
        elif backend == "external":
            url = cfg.env_overrides.get(
                next(
                    (v for v in spec.guardrail_env_passthroughs if v.endswith("_URL")),
                    "",
                ),
                "",
            )
            table.add_row(det_name, f"[green]external[/green] [dim]({url})[/dim]")

    # Surrogate / Faker summary.
    if cfg.surrogate_salt:
        table.add_row(
            "Salt",
            "[green]set[/green] [dim](stable surrogates across restarts)[/dim]",
        )
    if cfg.use_faker:
        loc = cfg.faker_locale or "default"
        table.add_row("Faker", f"[green]enabled[/green], locale=[green]{loc}[/green]")
    else:
        table.add_row(
            "Faker",
            "[green]disabled[/green] [dim](opaque [TYPE_HEX] surrogates)[/dim]",
        )

    if cfg.extra_run_args:
        table.add_row("Passthrough", f"[dim]{' '.join(cfg.extra_run_args)}[/dim]")

    _console.print()
    _console.print(table)
    _console.print()


def run_guardrail(engine: Engine, cfg: LaunchConfig) -> int:
    """Print the plan, exec the engine. Returns the engine's exit code.

    Validates the image exists first — same fail-loud-with-build-hint
    behaviour as the bash version.
    """
    image = resolve_image(cfg.flavour)
    if not engine.image_exists(image):
        _console.print(
            f"[red]Image \"{image}\" not found locally.[/red]\n"
            f"Build it with:  scripts/image_builder.sh -f {cfg.flavour}",
        )
        return 1

    if engine.container_exists(cfg.name):
        if cfg.replace_existing:
            engine.remove_container(cfg.name)
        else:
            _console.print(
                f"[red]A container named \"{cfg.name}\" already exists. "
                f"Pass --replace to remove it.[/red]",
            )
            return 1

    print_plan(engine, cfg)

    argv = build_run_argv(engine, cfg)
    # Foreground run; engine receives SIGINT directly so Ctrl-C
    # tears down the container cleanly. We still need to catch
    # KeyboardInterrupt here because Python translates SIGINT to
    # an exception in the parent — without the catch, hitting Ctrl+C
    # surfaces a noisy traceback even though the container itself
    # exited cleanly.
    import subprocess
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        return 130  # standard SIGINT exit code


__all__ = ["LaunchConfig", "build_run_argv", "run_guardrail", "print_plan", "resolve_image"]
