"""Click CLI for the image builder.

Mirrors the launcher's shape: `GroupedCommand` formatter with options
bucketed into named sections, an `--ui` eager flag that hands off to
the Textual TUI in `tools.image_builder.menu`, and a single
`scripts/image_builder.sh` bash wrapper that just exec's into us.

CLI surface (cleaned up from the old bash script's `-t`/`-T`):

  --flavour / -f  NAME    Single flavour; repeatable for multi-build.
  --preset NAME           Named subset (all, guardrail, privacy-filter,
                          gliner-pii, minimal, minimal-fakellm).
  --list                  Print every flavour with its default tag and exit.
  --tag / -T TAG          Override the default image tag (single-flavour only).
  --engine podman|docker  Force the engine. Without it: ENGINE env, then podman, then docker.
  --yes / -y              Skip the confirmation prompt.
  --ui / --interactive    Open the Textual menu instead.
  -- ARGS...              Passed verbatim to `<engine> build` (e.g. `--no-cache`).

Dev-only — installed via `pip install -e ".[dev]"`, never shipped in
the production wheel.
"""

from __future__ import annotations

import os
import sys
from typing import Any

try:
    import click
except ImportError as exc:
    print(
        "tools/image_builder requires Click (dev dependency). Install with:\n"
        '  pip install -e ".[dev]"\n'
        f"  ({exc})",
        file=sys.stderr,
    )
    sys.exit(1)

from rich.console import Console
from rich.table import Table

from ..launcher.engine import detect_engine
from .runner import print_plan, resolve_plans, run_all
from .specs import FLAVOURS, FLAVOURS_BY_NAME, PRESETS, preset_names


# ── Section-grouped help formatting ──────────────────────────────────────
# Lifted from `tools.launcher.main.GroupedCommand` — same idea, same
# implementation. Kept inline rather than imported from the launcher
# so the two tools stay independently maintainable (a launcher-side
# tweak shouldn't have to think about the builder's help layout).
class GroupedCommand(click.Command):
    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
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

        global_col_max = max(len(col1) for col1, _ in all_rows)
        for section_name in section_order:
            rows = section_rows[section_name]
            if not rows:
                continue
            padded = [(col1.ljust(global_col_max), col2) for col1, col2 in rows]
            with formatter.section(section_name):
                formatter.write_dl(padded, col_max=global_col_max, col_spacing=2)


_HELP_SECTION_ATTR = "_help_section"


def grouped_option(*args: Any, group: str = "Options", **kwargs: Any):
    underlying = click.option(*args, **kwargs)

    def decorator(f):
        f = underlying(f)
        params = getattr(f, "__click_params__", [])
        if params:
            setattr(params[-1], _HELP_SECTION_ATTR, group)
        return f
    return decorator


_S_MODE = "Mode"
_S_SELECT = "Selection"
_S_BUILD = "Build"


# ── Eager callbacks (run before main validation) ──────────────────────────
def _open_menu(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Hand off to the Textual TUI when --ui/--interactive is set."""
    if not value or ctx.resilient_parsing:
        return
    from .menu import run_interactive
    rc = run_interactive()
    ctx.exit(rc)


def _list_flavours(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """`--list`: print the flavour catalog as a rich table and exit.
    Eager so it doesn't trip the require-at-least-one-flavour check."""
    if not value or ctx.resilient_parsing:
        return
    console = Console()
    table = Table(title="Image flavours", show_lines=False)
    table.add_column("Flavour", style="green", no_wrap=True)
    table.add_column("Group")
    table.add_column("Default tag")
    table.add_column("Notes", style="dim")
    for f in FLAVOURS:
        notes = []
        if f.bakes_model:
            notes.append("downloads model at build time")
        if f.build_args:
            notes.append(", ".join(f"{k}={v}" for k, v in f.build_args.items()))
        table.add_row(f.name, f.group, f.default_tag, "; ".join(notes))
    console.print(table)
    console.print()
    console.print("[bold]Presets:[/bold]")
    for name in preset_names():
        members = ", ".join(PRESETS[name])
        console.print(f"  [green]{name}[/green] — {members}")
    ctx.exit(0)


# ── Click command ─────────────────────────────────────────────────────────
@click.command(
    cls=GroupedCommand,
    no_args_is_help=True,
    context_settings={"max_content_width": 200},
)
# ── Mode ──────────────────────────────────────────────────────────────────
@grouped_option(
    "--ui", "--interactive",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_open_menu,
    group=_S_MODE,
    help="Open the Textual interactive menu (preset radio + checkbox grid).",
)
# ── Selection ─────────────────────────────────────────────────────────────
@grouped_option(
    "--flavour", "-f", "flavours",
    multiple=True,
    type=str,
    group=_S_SELECT,
    help="Image flavour to build. Repeatable. See --list.",
)
@grouped_option(
    "--preset",
    type=str,
    default=None,
    group=_S_SELECT,
    help=(
        "Named subset of flavours: "
        f"{', '.join(PRESETS.keys())}. Composes with --flavour (union)."
    ),
)
@grouped_option(
    "--list", "list_flavours",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_list_flavours,
    group=_S_SELECT,
    help="Print every flavour + preset with default tags and exit.",
)
# ── Build ─────────────────────────────────────────────────────────────────
@grouped_option(
    "--tag", "-T",
    type=str,
    default=None,
    group=_S_BUILD,
    help="Override the default image tag. Only valid with a single flavour.",
)
@grouped_option(
    "--engine",
    type=click.Choice(["podman", "docker"], case_sensitive=False),
    default=None,
    group=_S_BUILD,
    help="Force the engine. Without it: ENGINE env, then podman, then docker.",
)
@grouped_option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    group=_S_BUILD,
    help="Skip the pre-build confirmation prompt.",
)
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def cli(
    flavours: tuple[str, ...],
    preset: str | None,
    tag: str | None,
    engine: str | None,
    yes: bool,
    extra: tuple[str, ...],
) -> None:
    """Build one or more anonymizer-guardrail container images.

    Anything after `--` is forwarded to the underlying `<engine> build`
    invocation (e.g. `-- --no-cache --pull`). For an interactive
    walkthrough instead of flags, pass `--ui`.
    """
    # Resolve --engine override BEFORE flavour validation: the engine
    # picker can fail loud (binary missing), and we want that error
    # before the operator picked a wrong flavour.
    if engine:
        os.environ["ENGINE"] = engine
    try:
        engine_obj = detect_engine()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    # Compose flavour list: --preset bundle ∪ --flavour singletons,
    # de-duplicated, in (preset-order, then --flavour-order). Empty
    # union is rejected — running the builder with no targets would
    # silently exit 0 which is confusing.
    selected: list[str] = []
    seen: set[str] = set()
    if preset:
        if preset not in PRESETS:
            valid = ", ".join(PRESETS)
            raise click.BadParameter(
                f"Unknown preset {preset!r}. Valid: {valid}.",
                param_hint="--preset",
            )
        for name in PRESETS[preset]:
            if name not in seen:
                selected.append(name)
                seen.add(name)
    for name in flavours:
        if name not in seen:
            selected.append(name)
            seen.add(name)

    if not selected:
        raise click.UsageError(
            "Pick at least one flavour with --flavour or --preset "
            "(or use --ui for the interactive picker, --list to see options)."
        )

    # Translate names → Flavour objects, surfacing typos with a
    # clear "valid: …" hint.
    resolved = []
    for name in selected:
        f = FLAVOURS_BY_NAME.get(name)
        if f is None:
            valid = ", ".join(sorted(FLAVOURS_BY_NAME))
            raise click.BadParameter(
                f"Unknown flavour {name!r}. Valid: {valid}.",
                param_hint="--flavour",
            )
        resolved.append(f)

    # --tag with multi-flavour would tag every image identically; the
    # last build would silently overwrite the others. resolve_plans
    # also asserts this; raise as a UsageError up here so the message
    # surfaces as `Usage: ...` rather than an unhandled ValueError.
    if tag and len(resolved) > 1:
        raise click.UsageError(
            "--tag is only supported with a single flavour "
            "(otherwise N builds would clobber the same tag)."
        )

    plans = resolve_plans(resolved, tag_override=tag)
    extra_args = list(extra)
    print_plan(engine_obj.name, plans, extra=extra_args)

    if not yes:
        confirm = click.confirm("Proceed?", default=True)
        if not confirm:
            click.echo("Aborted.")
            ctx = click.get_current_context()
            ctx.exit(0)

    rc = run_all(engine_obj.name, plans, extra=extra_args)
    sys.exit(rc)


def main() -> None:
    """Entry point invoked by `python -m tools.image_builder` and the
    scripts/image_builder.sh wrapper.

    `IMAGE_BUILDER_PROG_NAME` is set by the bash wrapper so the
    --help "Usage:" line reads `scripts/image_builder.sh` rather than
    `python -m tools.image_builder`. Without the env var we fall back
    to Click's default (sys.argv[0])."""
    prog_name = os.environ.get("IMAGE_BUILDER_PROG_NAME")
    if prog_name:
        cli.main(prog_name=prog_name)
    else:
        cli.main()


if __name__ == "__main__":
    main()
