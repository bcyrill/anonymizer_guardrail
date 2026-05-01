"""Click CLI for the detector quality benchmark.

Two modes, mirroring scripts/test-examples.sh:

  default            connect to $BASE_URL (caller manages the
                     guardrail).
  --preset NAME      spawn a fresh test guardrail via scripts/cli.sh,
                     run the benchmark, tear it down on exit.

Output is a rich table per case + an aggregate summary. Operators
copy/paste runs side-by-side to compare detector mixes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

try:
    import click
except ImportError as exc:
    print(
        "tools/detector_bench requires Click (dev dependency). Install with:\n"
        '  pip install -e ".[dev]"\n'
        f"  ({exc})",
        file=sys.stderr,
    )
    sys.exit(1)

from rich.console import Console
from rich.table import Table

from .corpus import CorpusError, load
from .runner import Summary, run


# stderr console so the table doesn't interleave with the
# auto-started guardrail's stdout when --preset spawns one.
_console = Console(stderr=True)


_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_PRESET_PORT = 8001
_DEFAULT_PRESET_NAME = "anonymizer-bench"


def _fetch_health(base_url: str, timeout_s: float = 5.0) -> dict[str, Any]:
    """Probe /health and return the parsed JSON. Raises RuntimeError
    when the server isn't reachable or returns non-OK."""
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            payload = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc}") from exc
    data = json.loads(payload)
    if data.get("status") != "ok":
        raise RuntimeError(f"{url} reports status={data.get('status')!r}, expected 'ok'.")
    return data


def _start_preset_guardrail(
    preset: str, port: int, name: str, log_path: str,
) -> subprocess.Popen:
    """Background a `scripts/cli.sh --preset PRESET` and wait for
    /health. Mirrors test-examples.sh's start_test_guardrail.
    Returns the Popen handle so the caller can tear it down."""
    repo_root = _repo_root()
    cli_sh = repo_root / "scripts" / "cli.sh"
    if not cli_sh.is_file():
        raise click.UsageError(f"{cli_sh} not found — are you in a checkout?")

    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        [
            str(cli_sh),
            "--preset", preset,
            "--port", str(port),
            "--name", name,
            "--replace",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(repo_root),
        # Detach into a new session so a Ctrl+C in the bench doesn't
        # nuke cli.sh before its own EXIT trap fires (it stops the
        # auto-started fake-llm / pf-service).
        start_new_session=True,
    )

    base_url = f"http://localhost:{port}"
    _console.print(
        f"[dim]Spawning test guardrail: scripts/cli.sh --preset {preset} "
        f"(port {port}, name {name}, log {log_path})[/dim]"
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = _tail(log_path, 30)
            raise click.ClickException(
                f"cli.sh exited (code {proc.returncode}) before guardrail came up.\n"
                f"Last lines of {log_path}:\n{tail}"
            )
        try:
            _fetch_health(base_url, timeout_s=2.0)
            _console.print("[green]ready[/green]")
            return proc
        except RuntimeError:
            time.sleep(1)

    tail = _tail(log_path, 30)
    raise click.ClickException(
        f"Guardrail did not become ready within 90s.\n"
        f"Last lines of {log_path}:\n{tail}"
    )


def _stop_preset_guardrail(proc: subprocess.Popen, name: str, keep: bool) -> None:
    """SIGTERM the cli.sh process group so its EXIT trap fires and
    tears down auto-started services. Belt-and-braces removes the
    container by name in case the signal got lost."""
    if keep:
        _console.print(
            f"[dim]--keep set; leaving guardrail running (PID {proc.pid}, container {name}).[/dim]"
        )
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()

    # Last-resort container removal — same logic as test-examples.sh's
    # belt-and-braces. We don't fail the benchmark if this errors;
    # operators can rm by hand.
    for engine in ("podman", "docker"):
        for container in (name, "fake-llm", "privacy-filter-service",
                          "gliner-pii-service"):
            subprocess.run(
                [engine, "rm", "-f", container],
                capture_output=True, check=False,
            )
        # Only need to fire one engine's cleanup successfully.
        if subprocess.run(
            [engine, "--version"], capture_output=True, check=False
        ).returncode == 0:
            break


def _tail(path: str, n: int) -> str:
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return "(could not read log)"
    return "".join(lines[-n:])


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


def _print_results(corpus_name: str, base_url: str, summary: Summary) -> None:
    """Per-case table + aggregate summary."""
    table = Table(title=f"Corpus: {corpus_name} (against {base_url})")
    table.add_column("case", overflow="fold")
    table.add_column("recall", justify="right")
    table.add_column("type acc.", justify="right")
    table.add_column("precision", justify="right")
    table.add_column("latency", justify="right")
    table.add_column("notes", overflow="fold")

    for c in summary.cases:
        if c.skipped:
            table.add_row(
                c.case_id,
                "[yellow]—[/yellow]", "[yellow]—[/yellow]", "[yellow]—[/yellow]",
                "[yellow]—[/yellow]",
                f"[yellow]skipped: {c.skip_reason}[/yellow]",
            )
            continue
        if c.blocked:
            table.add_row(
                c.case_id,
                "[red]BLOCKED[/red]", "—", "—",
                f"{c.latency_ms:.0f}ms",
                f"[red]{c.blocked_reason}[/red]",
            )
            continue
        recall_str = (
            f"{c.redacted_count}/{c.expected_total}"
            if c.expected_total else "—"
        )
        type_str = (
            f"{c.type_correct}/{c.type_expected}"
            if c.type_expected else "—"
        )
        prec_str = (
            f"{c.must_keep_kept}/{c.must_keep_total}"
            if c.must_keep_total else "—"
        )
        notes_parts: list[str] = []
        if c.missed:
            shown = ", ".join(_truncate(e.text, 30) for e in c.missed[:3])
            extra = f" (+{len(c.missed)-3} more)" if len(c.missed) > 3 else ""
            notes_parts.append(f"[red]missed:[/red] {shown}{extra}")
        if c.leaked:
            shown = ", ".join(_truncate(t, 30) for t in c.leaked[:3])
            extra = f" (+{len(c.leaked)-3} more)" if len(c.leaked) > 3 else ""
            notes_parts.append(f"[red]leaked:[/red] {shown}{extra}")
        table.add_row(
            c.case_id, recall_str, type_str, prec_str,
            f"{c.latency_ms:.0f}ms",
            "  ".join(notes_parts) or "[green]✓[/green]",
        )

    _console.print()
    _console.print(table)

    _console.print()
    _console.print("[bold]Aggregate[/bold]")
    # Width chosen for the longest label ("strict recall" = 13 chars).
    # If you add a longer aggregate, bump the column.
    _print_aggregate("recall",         summary.recall)
    _print_aggregate("strict recall",  summary.recall_excluding_tolerated)
    _print_aggregate("type accuracy",  summary.type_accuracy)
    _print_aggregate("precision",      summary.precision)
    if summary.scored:
        _console.print(f"  {'avg latency':<14} {summary.avg_latency_ms:>4.0f} ms")
    if summary.skipped:
        _console.print(f"  [yellow]{len(summary.skipped)} case(s) skipped[/yellow]")
    if summary.blocked:
        # Distinct from skipped because the request DID dispatch — the
        # guardrail just refused to respond. Excluded from the metric
        # averages because BLOCKED measures error policy, not detection
        # quality (a flaky LLM would otherwise look like a recall bug).
        _console.print(
            f"  [red]{len(summary.blocked)} case(s) blocked[/red] "
            f"[dim](excluded from aggregate metrics; see per-case rows)[/dim]"
        )


def _print_aggregate(label: str, value: float | None) -> None:
    """Aggregate row: label left-padded to a fixed column, value
    right-padded so single- and triple-digit percentages line up
    on the right edge."""
    if value is None:
        _console.print(f"  {label:<14} [dim]   n/a[/dim]")
    else:
        colour = "green" if value >= 0.9 else ("yellow" if value >= 0.6 else "red")
        _console.print(f"  {label:<14} [{colour}]{value:>5.0%}[/{colour}]")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _run_comparison(
    base_url: str,
    corpus,
    active: list[str],
    timeout_s: float,
) -> None:
    """Score the corpus once per individual active detector + once
    with the full active set. Prints a side-by-side metric table.

    Uses the per-request `detector_mode` override (a SUBSET filter
    over the detectors built at boot) — start the guardrail with
    every detector you want to compare, the override narrows from
    there.
    """
    if not active:
        _console.print("[red]No detectors active in DETECTOR_MODE — nothing to compare.[/red]")
        return
    if len(active) < 2:
        _console.print(
            f"[yellow]Only one detector active ({active[0]}); --compare needs "
            f"at least two for the table to be meaningful. Restart the guardrail "
            f"with a wider DETECTOR_MODE.[/yellow]"
        )
        return

    # Variants: each individual detector, then the full set as a
    # baseline. Order: detectors in their REGISTERED_SPECS order
    # (which is what /health already gives us), then "all".
    variants: list[tuple[str, list[str] | None]] = [
        (det, [det]) for det in active
    ]
    variants.append(("all", None))  # None → no override → use everything

    results: list[tuple[str, "Summary"]] = []
    for label, override_mode in variants:
        _console.print(f"\n[bold]Running variant: {label}[/bold]")
        extras: dict[str, object] | None = (
            {"detector_mode": override_mode} if override_mode is not None else None
        )
        summary = run(
            base_url,
            corpus,
            active_detectors=active,
            timeout_s=timeout_s,
            extra_overrides=extras,
        )
        results.append((label, summary))

    _print_comparison(corpus.name, results)


def _print_comparison(corpus_name: str, results: list[tuple[str, "Summary"]]) -> None:
    """Side-by-side metric table. Rows are metrics, columns are
    variants. Best value per row gets a green tint so the winner
    pops without scanning."""
    table = Table(title=f"Comparison (corpus: {corpus_name})")
    table.add_column("metric")
    for label, _ in results:
        table.add_column(label, justify="right")

    metric_rows: list[tuple[str, str, list[float | None]]] = [
        ("recall",         "higher", [s.recall for _, s in results]),
        ("strict recall",  "higher", [s.recall_excluding_tolerated for _, s in results]),
        ("type accuracy",  "higher", [s.type_accuracy for _, s in results]),
        ("precision",      "higher", [s.precision for _, s in results]),
        ("avg latency ms", "lower",  [s.avg_latency_ms if s.scored else None
                                      for _, s in results]),
    ]

    for label, direction, values in metric_rows:
        # Pick the best value (skipping Nones) so we can highlight it.
        # Latency is "lower is better"; everything else is "higher is better".
        numeric = [v for v in values if v is not None]
        if numeric:
            best = min(numeric) if direction == "lower" else max(numeric)
        else:
            best = None
        row = [label]
        for v in values:
            if v is None:
                row.append("[dim]n/a[/dim]")
                continue
            if label == "avg latency ms":
                cell = f"{v:.0f}"
            else:
                cell = f"{v:.0%}"
            if best is not None and v == best:
                cell = f"[green]{cell}[/green]"
            row.append(cell)
        table.add_row(*row)

    # Operational counts row — distinct from quality metrics. A
    # variant with everything blocked would otherwise show n/a across
    # the metric rows with no clue why; this makes the cause obvious.
    blocked_row = ["blocked / scored"]
    for _, s in results:
        b = len(s.blocked)
        sc = len(s.scored)
        if b == 0:
            blocked_row.append(f"[dim]0/{sc}[/dim]")
        else:
            blocked_row.append(f"[red]{b}[/red]/{sc}")
    table.add_row(*blocked_row)

    _console.print()
    _console.print(table)
    _console.print(
        "[dim]Highlighted cells = best value per row. "
        "Latency is 'lower wins'; the other metrics are 'higher wins'. "
        "Blocked cases are excluded from the quality metrics — see the "
        "blocked / scored row.[/dim]"
    )


@click.command(
    no_args_is_help=True,
    context_settings={"max_content_width": 200},
)
@click.option(
    "--config", "-c", "config",
    type=str, required=True,
    help="Path to a corpus YAML, or `bundled:NAME` for a starter corpus under tests/corpus/.",
)
@click.option(
    "--preset",
    type=str, default=None,
    help="cli.sh preset (uuid-debug | pentest | regex-only). When set, spawn a test guardrail and tear it down on exit.",
)
@click.option(
    "--port",
    type=int, default=_DEFAULT_PRESET_PORT, show_default=True,
    help="Host port for the --preset test guardrail.",
)
@click.option(
    "--name",
    type=str, default=_DEFAULT_PRESET_NAME, show_default=True,
    help="Container name for the --preset test guardrail.",
)
@click.option(
    "--keep",
    is_flag=True, default=False,
    help="Don't tear down the --preset test guardrail on exit.",
)
@click.option(
    "--base-url",
    type=str, default=None,
    help=f"Guardrail URL (only used without --preset). Default: $BASE_URL or {_DEFAULT_BASE_URL}.",
)
@click.option(
    "--timeout-s",
    type=float, default=30.0, show_default=True,
    help="Per-request HTTP timeout when calling the guardrail.",
)
@click.option(
    "--compare",
    is_flag=True, default=False,
    help=(
        "Run the corpus once per active detector (using the per-request "
        "`detector_mode` override to filter the active set), plus once "
        "with all detectors as a baseline, and print a side-by-side "
        "metric table. Start the guardrail with EVERY detector you want "
        "to compare in DETECTOR_MODE; the override narrows from there. "
        "Exits 0 always — this is an exploratory comparison, not a CI gate."
    ),
)
def cli(
    config: str,
    preset: str | None,
    port: int,
    name: str,
    keep: bool,
    base_url: str | None,
    timeout_s: float,
    compare: bool,
) -> None:
    """Score a guardrail's detector mix against a labelled corpus.

    Examples:

    \b
      # Against a guardrail you're managing yourself:
      scripts/benchmark.sh --config bundled:pentest

      # Spawn one for the run, tear it down at exit:
      scripts/benchmark.sh --config bundled:pentest --preset pentest

      # Your own corpus:
      scripts/benchmark.sh --config tests/corpus/legal.yaml

      # Compare every active detector individually + the full mix:
      scripts/benchmark.sh --config bundled:pentest --compare
    """
    try:
        corpus = load(config)
    except CorpusError as exc:
        raise click.ClickException(str(exc))

    _console.print(f"[bold]Loading corpus:[/bold] {corpus.path}")
    if corpus.description:
        _console.print(f"[dim]{corpus.description.strip()}[/dim]")

    proc = None
    log_path = "/tmp/anonymizer-bench-guardrail.log"
    try:
        if preset:
            proc = _start_preset_guardrail(preset, port, name, log_path)
            base_url_resolved = f"http://localhost:{port}"
        else:
            base_url_resolved = (
                base_url or os.environ.get("BASE_URL") or _DEFAULT_BASE_URL
            )

        try:
            health = _fetch_health(base_url_resolved)
        except RuntimeError as exc:
            raise click.ClickException(str(exc))

        active = _parse_detector_mode(health.get("detector_mode", ""))
        _console.print(
            f"[bold]Guardrail:[/bold] {base_url_resolved}  "
            f"[bold]DETECTOR_MODE:[/bold] [green]{','.join(active) or '(none)'}[/green]"
        )

        if compare:
            _run_comparison(base_url_resolved, corpus, active, timeout_s)
            sys.exit(0)

        summary = run(
            base_url_resolved,
            corpus,
            active_detectors=active,
            timeout_s=timeout_s,
        )
        _print_results(corpus.name, base_url_resolved, summary)

        # Exit code reflects pass/fail so this can sit in CI alongside
        # test-examples.sh. Three things count as failure:
        #   1. any non-tolerated miss in a scored case (recall regression),
        #   2. any leaked must_keep substring in a scored case (precision regression),
        #   3. any BLOCKED case (the guardrail refused — needs investigation
        #      even if it's an LLM availability issue rather than a detection bug).
        scored_failure = any(
            (c.leaked or (c.expected_total - c.expected_tolerated > c.redacted_excluding_tolerated))
            for c in summary.scored
        )
        any_failure = scored_failure or bool(summary.blocked)
        sys.exit(1 if any_failure else 0)

    finally:
        if proc is not None:
            _stop_preset_guardrail(proc, name, keep)


def _parse_detector_mode(mode: str) -> list[str]:
    return [d.strip() for d in mode.split(",") if d.strip()]


def main() -> None:
    """Entry point invoked by `python -m tools.detector_bench` and
    the scripts/benchmark.sh wrapper."""
    prog_name = os.environ.get("BENCH_PROG_NAME")
    if prog_name:
        cli.main(prog_name=prog_name)
    else:
        cli.main()


if __name__ == "__main__":
    main()
