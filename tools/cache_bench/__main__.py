"""Click CLI for the cache benchmark.

Default invocation:

    scripts/cache_bench.sh

Reads service URLs from CLI flags / env vars, builds the cell matrix,
runs each cell, and writes `cache-bench-results/{report.md,results.json}`.

The bench needs:
  * `gliner-pii-service` and `privacy-filter-hf-service` containers
    running (see `scripts/launcher.sh --gliner-pii-backend service
    --privacy-filter-backend service --privacy-filter-variant hf`).
    Cells using detectors whose service URL isn't reachable are
    skipped with a clear message.
  * Redis (any single-instance deployment) for the `redis` backend
    cells. Skip if the URL is unreachable.

Operators can run a partial benchmark (`--quick`, `--detector-mix`,
`--length`) when iterating, or the full matrix (default) for the
formal report.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import click
except ImportError as exc:
    print(
        "tools/cache_bench requires Click (dev dependency). Install with:\n"
        '  pip install -e ".[dev]"\n'
        f"  ({exc})",
        file=sys.stderr,
    )
    sys.exit(1)

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .harness import BenchCell, CellResult, ServiceUrls, build_cells, run_cell
from .payloads import CONVERSATION_LENGTHS, SCENARIOS, build_conversation
from .report import write_outputs


# stderr console so the progress bar doesn't interleave with anything
# the bench writes to stdout (notably JSON if the operator pipes us).
_console = Console(stderr=True)


_PROG_NAME = os.environ.get("BENCH_PROG_NAME", "python -m tools.cache_bench")
_DEFAULT_OUT_DIR = "cache-bench-results"


# ── Service health checks ────────────────────────────────────────────


def _check_http_service(url: str, timeout_s: float = 3.0) -> bool:
    """Probe the service's `/health` endpoint (gliner-pii-service and
    privacy-filter-service both expose it). Returns True on a 200,
    False otherwise. Connection timeouts and refusals → False."""
    if not url:
        return False
    health_url = url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _check_redis(url: str, timeout_s: float = 3.0) -> bool:
    """TCP-connect probe for the Redis URL — we don't ping Redis with
    redis-py here to keep this CLI dependency-light. A reachable
    socket plus a working configuration should be enough; the bench
    cells that actually use Redis will fail loudly if the URL is
    bogus beyond TCP reachability."""
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (socket.error, OSError):
        return False


# ── CLI ──────────────────────────────────────────────────────────────


def _filter_cells(
    cells: list[BenchCell],
    detector_mix_filter: tuple[str, ...] | None,
    cache_mode_filter: tuple[str, ...] | None,
    backend_filter: tuple[str, ...] | None,
    overrides_filter: tuple[str, ...] | None,
) -> list[BenchCell]:
    """Apply the operator's `--detector-mix`/`--cache-mode`/etc.
    filters to the cell matrix. Each filter is a comma-separated
    list passed via Click; the cell must match every active filter
    to survive."""
    out = list(cells)
    if detector_mix_filter:
        wanted = {tuple(m.split("+")) for m in detector_mix_filter}
        out = [c for c in out if c.detector_mode in wanted]
    if cache_mode_filter:
        out = [c for c in out if c.cache_mode in set(cache_mode_filter)]
    if backend_filter:
        out = [c for c in out if c.cache_mode == "none" or c.backend in set(backend_filter)]
    if overrides_filter:
        out = [c for c in out if c.overrides_mode in set(overrides_filter)]
    return out


@click.command(name=_PROG_NAME)
@click.option(
    "--gliner-pii-url", envvar="GLINER_PII_URL", default="",
    help="URL of a running gliner-pii-service (e.g. http://localhost:8002). "
         "Cells using gliner_pii are skipped if unset/unreachable.",
)
@click.option(
    "--privacy-filter-url", envvar="PRIVACY_FILTER_URL", default="",
    help="URL of a running privacy-filter(-hf)-service (e.g. http://localhost:8003). "
         "Cells using privacy_filter are skipped if unset/unreachable.",
)
@click.option(
    "--redis-url", envvar="CACHE_REDIS_URL", default="",
    help="Redis URL (e.g. redis://localhost:6379/2). Cells with backend=redis "
         "are skipped if unset/unreachable.",
)
@click.option(
    "--repeats", default=3, show_default=True,
    help="Number of times to run each cell. Median across repeats is reported.",
)
@click.option(
    "--scenario", default="support", show_default=True,
    type=click.Choice(SCENARIOS),
    help="Conversation scenario template (persona + dialogue style).",
)
@click.option(
    "--length", "lengths", multiple=True, type=int,
    help="Conversation lengths to bench (default: 5, 10, 20, 30). Pass multiple "
         "times for a custom set.",
)
@click.option(
    "--detector-mix", multiple=True,
    help="Filter to specific detector mixes. Pass each as a `+`-joined name "
         "(e.g. `--detector-mix regex --detector-mix regex+gliner_pii`).",
)
@click.option(
    "--cache-mode", multiple=True,
    type=click.Choice(["none", "detector", "pipeline", "both"]),
    help="Filter to specific cache modes.",
)
@click.option(
    "--backend", "backends", multiple=True,
    type=click.Choice(["memory", "redis"]),
    help="Filter to specific backends.",
)
@click.option(
    "--overrides-mode", multiple=True,
    type=click.Choice(["none", "varied"]),
    help="Filter to specific override modes.",
)
@click.option(
    "--quick", is_flag=True,
    help="Shortcut: run only L=5 + L=10 against `regex` and `regex+gliner_pii`. "
         "For fast iteration during development.",
)
@click.option(
    "-o", "--output-dir", default=_DEFAULT_OUT_DIR, show_default=True,
    type=click.Path(),
    help="Directory to write report.md + results.json.",
)
def main(
    gliner_pii_url: str,
    privacy_filter_url: str,
    redis_url: str,
    repeats: int,
    scenario: str,
    lengths: tuple[int, ...],
    detector_mix: tuple[str, ...],
    cache_mode: tuple[str, ...],
    backends: tuple[str, ...],
    overrides_mode: tuple[str, ...],
    quick: bool,
    output_dir: str,
) -> None:
    """Run the cache-effectiveness benchmark and write a report.

    The bench is in-process: it builds a Pipeline against the
    configured services and exercises it directly (no HTTP layer
    between the bench and the pipeline). That isolates cache
    behaviour from FastAPI / serialisation noise.

    See `docs/cache-bench.md` for setup and interpretation.
    """
    if quick:
        if not lengths:
            lengths = (5, 10)
        if not detector_mix:
            detector_mix = ("regex", "regex+gliner_pii")

    if not lengths:
        lengths = CONVERSATION_LENGTHS

    urls = ServiceUrls(
        gliner_pii_url=gliner_pii_url.strip(),
        privacy_filter_url=privacy_filter_url.strip(),
        cache_redis_url=redis_url.strip(),
    )

    # Health-check services up front so the operator sees the picture
    # before we burn time on the matrix.
    _console.print("[bold]Pre-flight service checks[/bold]")
    if urls.gliner_pii_url:
        ok = _check_http_service(urls.gliner_pii_url)
        _console.print(
            f"  gliner_pii  → {'[green]reachable[/green]' if ok else '[yellow]unreachable[/yellow]'} "
            f"({urls.gliner_pii_url})"
        )
        if not ok:
            urls = ServiceUrls(
                gliner_pii_url="",
                privacy_filter_url=urls.privacy_filter_url,
                cache_redis_url=urls.cache_redis_url,
            )
    else:
        _console.print("  gliner_pii  → [dim]not configured (cells using gliner_pii will skip)[/dim]")

    if urls.privacy_filter_url:
        ok = _check_http_service(urls.privacy_filter_url)
        _console.print(
            f"  privacy_filter → {'[green]reachable[/green]' if ok else '[yellow]unreachable[/yellow]'} "
            f"({urls.privacy_filter_url})"
        )
        if not ok:
            urls = ServiceUrls(
                gliner_pii_url=urls.gliner_pii_url,
                privacy_filter_url="",
                cache_redis_url=urls.cache_redis_url,
            )
    else:
        _console.print("  privacy_filter → [dim]not configured (cells using privacy_filter will skip)[/dim]")

    if urls.cache_redis_url:
        ok = _check_redis(urls.cache_redis_url)
        _console.print(
            f"  redis → {'[green]reachable[/green]' if ok else '[yellow]unreachable[/yellow]'} "
            f"({urls.cache_redis_url})"
        )
        if not ok:
            urls = ServiceUrls(
                gliner_pii_url=urls.gliner_pii_url,
                privacy_filter_url=urls.privacy_filter_url,
                cache_redis_url="",
            )
    else:
        _console.print("  redis → [dim]not configured (redis-backend cells will skip)[/dim]")

    # Build cell matrix.
    backend_filter = backends or None
    cells = build_cells(
        overrides_modes=overrides_mode or ("none", "varied"),
        backends=backends or ("memory", "redis"),
    )
    cells = _filter_cells(
        cells,
        detector_mix_filter=detector_mix or None,
        cache_mode_filter=cache_mode or None,
        backend_filter=backend_filter,
        overrides_filter=overrides_mode or None,
    )

    if not cells:
        _console.print("[red]No cells match the filters — exiting.[/red]")
        sys.exit(2)

    # Conversations: one per length × scenario. (Single scenario by
    # default to keep the matrix manageable; operators wanting
    # multi-scenario can run the bench multiple times with different
    # `--scenario`.)
    conversations = [build_conversation(scenario, length) for length in lengths]
    total = len(cells) * len(conversations)
    _console.print(
        f"\n[bold]Running {total} (cell × conversation) combinations[/bold] "
        f"({len(cells)} cells × {len(conversations)} lengths) × {repeats} repeats"
    )
    started = time.perf_counter()

    results: list[CellResult] = []

    async def _run_all() -> None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=_console,
            transient=False,
        ) as progress:
            task = progress.add_task("running cells", total=total)
            for cell in cells:
                for conv in conversations:
                    progress.update(
                        task,
                        description=f"{cell.label} | {conv.name}",
                    )
                    result = await run_cell(cell, conv, urls, repeats=repeats)
                    results.append(result)
                    progress.advance(task)

    asyncio.run(_run_all())

    elapsed = time.perf_counter() - started
    _console.print(
        f"\n[bold green]Bench complete[/bold green] in {elapsed:.1f}s. "
        f"Cells run: {sum(1 for r in results if not r.skipped)}, "
        f"skipped: {sum(1 for r in results if r.skipped)}."
    )

    out_path = Path(output_dir)
    md_path, json_path = write_outputs(results, out_path, repeats=repeats)
    _console.print(f"  → markdown: [bold]{md_path}[/bold]")
    _console.print(f"  → json:     [bold]{json_path}[/bold]")


if __name__ == "__main__":
    main()
