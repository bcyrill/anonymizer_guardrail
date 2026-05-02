"""Subprocess wrapper around the engine's `build` invocation.

Single responsibility: take a `Flavour` + a tag + the operator's
passthrough args, and run

    <engine> build [--format=docker] -t TAG [--build-arg ...]
        [extra...] -f Containerfile <context>

once per flavour in sequence. The plan summary, per-build heading,
and final tally are rendered with rich so the bash script's coloured
status lines have a direct port.

Why subprocess and not a podman/docker SDK: the surface we use is
a single `build` call. A lib dep (libpod / docker-py) buys nothing
and adds non-trivial dependency mass (libpod has C extensions, docker-py
brings requests). subprocess.run with check=False + an explicit
returncode test stays portable.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from rich.console import Console

from .specs import Flavour


_console = Console(stderr=False)
_err_console = Console(stderr=True, style="red")


@dataclass(frozen=True)
class BuildPlan:
    """Resolved per-flavour build invocation. Pre-computed so the plan
    table can show exact tags before the operator confirms, and so a
    --tag override is validated up front rather than mid-run."""
    flavour: Flavour
    tag: str


def resolve_plans(
    flavours: list[Flavour],
    *,
    tag_override: str | None,
) -> list[BuildPlan]:
    """Pair each flavour with its final tag.

    `tag_override` is only meaningful for a single flavour — when
    multiple are queued, every build would land on the same tag and
    the last write would silently win. The CLI rejects this earlier;
    the runtime check is a defence-in-depth assert.
    """
    if tag_override and len(flavours) > 1:
        raise ValueError(
            "--tag is only supported when building a single flavour "
            f"(got {len(flavours)})."
        )
    return [
        BuildPlan(flavour=f, tag=tag_override or f.default_tag)
        for f in flavours
    ]


def _argv_for(engine: str, plan: BuildPlan, extra: list[str]) -> list[str]:
    """Build the engine's argv. Podman gets `--format=docker` so the
    HEALTHCHECK in our Containerfiles isn't silently dropped (OCI
    image format, podman's default, has no HEALTHCHECK field). Docker
    builds always emit Docker-format images so the flag is omitted
    (it's not a valid `docker build` flag)."""
    argv: list[str] = [engine, "build"]
    if engine == "podman":
        argv.append("--format=docker")
    argv += ["-t", plan.tag]
    for k, v in plan.flavour.build_args.items():
        argv += ["--build-arg", f"{k}={v}"]
    argv += list(extra)
    argv += ["-f", plan.flavour.containerfile, plan.flavour.context]
    return argv


def print_plan(
    engine: str,
    plans: list[BuildPlan],
    *,
    extra: list[str],
) -> None:
    """Pre-build summary. Mirrors the bash plan output."""
    _console.print()
    _console.print(f"Engine:    [green]{engine}[/green]")
    if len(plans) == 1:
        p = plans[0]
        _console.print(f"Flavour:   [green]{p.flavour.name} → {p.tag}[/green]")
    else:
        _console.print("Flavours:")
        for p in plans:
            _console.print(f"  [green]{p.flavour.name} → {p.tag}[/green]")
    if extra:
        _console.print(f"Passthrough: [dim]{' '.join(extra)}[/dim]")

    if any(p.flavour.bakes_model for p in plans):
        _console.print()
        _console.print(
            "[yellow]At least one flavour bakes the model into the image at "
            "build time (openai/privacy-filter or nvidia/gliner-pii). The "
            "first build of a baked flavour pulls multi-GB weights from "
            "HuggingFace; subsequent rebuilds reuse the layer cache.[/yellow]"
        )
    _console.print()


def run_one(engine: str, plan: BuildPlan, extra: list[str]) -> int:
    """Execute a single build. Streams the engine's stdout/stderr to
    the operator's terminal (no capture) — long builds need to show
    progress in real time, and the engine's own progress UI is what
    operators expect to see.
    """
    argv = _argv_for(engine, plan, extra)
    _console.print()
    _console.print(
        f"[bold]── Building {plan.flavour.name} → {plan.tag} ──[/bold]"
    )
    _console.print(f"[dim]{' '.join(argv)}[/dim]")
    try:
        result = subprocess.run(argv, check=False)
    except FileNotFoundError:
        _err_console.print(
            f"Engine binary {engine!r} not found in PATH."
        )
        return 127
    if result.returncode != 0:
        _err_console.print(
            f"Build of {plan.flavour.name} failed (exit {result.returncode})."
        )
        return result.returncode
    _console.print(f"[green]Built {plan.tag}.[/green]")
    return 0


def run_all(
    engine: str,
    plans: list[BuildPlan],
    *,
    extra: list[str],
) -> int:
    """Build each plan in sequence; bail on the first failure.

    Returning the failing build's exit code (rather than always 1)
    preserves the engine's exit semantics — useful when a CI script
    wraps the builder and wants to distinguish e.g. a build failure
    from "engine not found".
    """
    for plan in plans:
        rc = run_one(engine, plan, extra)
        if rc != 0:
            return rc
    if len(plans) > 1:
        _console.print()
        _console.print(f"[green]Built {len(plans)} images.[/green]")
    return 0


__all__ = [
    "BuildPlan",
    "resolve_plans",
    "print_plan",
    "run_one",
    "run_all",
]
