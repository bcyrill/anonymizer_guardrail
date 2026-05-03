"""Markdown report generator + JSON dump for the cache benchmark.

The bench runs many cells and produces a forest of TurnResult
objects. The report flattens that into operator-friendly summaries:

  1. Headline table per detector mix — total wall time and total
     cache hit rates across cache configs at each conversation length.
  2. Per-cell drilldowns — turn-by-turn latency and cache
     hit/miss deltas (only printed in `--verbose` JSON mode; the
     markdown stays summary-level for readability).
  3. Override-mode comparison — how much override variation degrades
     cache hits.
  4. Backend comparison — memory vs redis when both ran.

JSON dump preserves every per-turn observation so analysts can
re-aggregate however they want without re-running the bench.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .harness import BenchCell, CellResult


# ── Aggregation helpers ──────────────────────────────────────────────


def _median(values: Iterable[float]) -> float:
    vs = list(values)
    return statistics.median(vs) if vs else 0.0


def _mean(values: Iterable[float]) -> float:
    vs = list(values)
    return statistics.mean(vs) if vs else 0.0


def _hits_misses(result: CellResult) -> tuple[int, int, int, int]:
    """Sum cache hits/misses across all repeats and turns of one
    cell. Returns (pipe_hits, pipe_misses, det_hits, det_misses).
    Per-detector hits/misses are summed across detectors."""
    pipe_h = pipe_m = det_h = det_m = 0
    for repeat in result.repeats:
        for turn in repeat:
            pipe_h += turn.pipeline_hits_delta
            pipe_m += turn.pipeline_misses_delta
            for v in turn.detector_hits_delta.values():
                det_h += v
            for v in turn.detector_misses_delta.values():
                det_m += v
    return pipe_h, pipe_m, det_h, det_m


def _hit_rate(hits: int, misses: int) -> float:
    """Hits / (hits + misses), expressed as a ratio in [0, 1]. 0
    when neither fired (e.g. no-cache run)."""
    total = hits + misses
    return hits / total if total else 0.0


def _cell_label(cell: BenchCell) -> str:
    """Compact label for tables — drops the detector mix (which is
    the row group) and just shows the cache shape + override mode."""
    cache_part = (
        cell.cache_mode if cell.cache_mode == "none"
        else f"{cell.cache_mode}-{cell.backend}"
    )
    return f"{cache_part}/{cell.overrides_mode}"


# ── Markdown rendering ───────────────────────────────────────────────


_BANNER = """\
# Cache benchmark report

In-process driver across the matrix of (detector mix × cache
config × conversation length × overrides mode). Each cell ran the
same multi-turn conversation `{repeats}` times; numbers are medians
across repeats.

  * **Anon ms** — total wall time across all turns of the
    conversation, anonymize side only. Lower is better.
  * **Pipe hits / misses** — pipeline-cache `get_or_compute` hits
    summed across all turns. A hit means no detectors ran.
  * **Det hits / misses** — per-detector cache hits summed across
    all active cache-using detectors and all turns.
  * **Cells marked `(skipped)`** — couldn't run because a required
    service URL or Redis URL wasn't configured.

The "no-cache" cell is the baseline for each (detector mix, length,
overrides) row. Speed-up is total-anon-ms vs that baseline.
"""

_LAYOUT_NOTES = """
## Reading the tables

  * **Each table groups one detector mix.** The headline number is
    the median total `anonymize` wall time per conversation
    (anonymize is what the cache short-circuits; deanonymize is
    nearly free in all configurations).
  * **The override column** shows whether that cell ran with
    `overrides=none` (every call uses defaults) or `overrides=varied`
    (every other call rotates `gliner_threshold=0.7` so the cache
    keys fragment). Compare the two to see the cost of override
    fragmentation.
  * **Pipeline-cache and per-detector-cache hit counts** report the
    sum across all turns + repeats. A pipeline hit short-circuits
    detector dispatch entirely; a per-detector hit only avoids that
    one detector's dispatch.
"""


def _render_headline_table(
    detector_mode: tuple[str, ...],
    cells: list[CellResult],
) -> str:
    """Emit one big table for one detector mix.

    Rows: cache config × overrides mode.
    Columns: each conversation length's median total-anon ms +
    cache-hit summary."""
    # Sort cells by (cache_mode_rank, backend, overrides_mode, length).
    cache_rank = {"none": 0, "detector": 1, "pipeline": 2, "both": 3}
    ovr_rank = {"none": 0, "varied": 1}

    def sort_key(c: CellResult):
        return (
            cache_rank[c.cell.cache_mode],
            c.cell.backend if c.cell.cache_mode != "none" else "",
            ovr_rank[c.cell.overrides_mode],
            c.conversation_length,
        )
    cells = sorted(cells, key=sort_key)

    # Pivot into rows by (cache_mode, backend, overrides_mode) and
    # columns by length.
    by_row: dict[tuple, dict[int, CellResult]] = {}
    lengths: set[int] = set()
    for c in cells:
        key = (c.cell.cache_mode, c.cell.backend, c.cell.overrides_mode)
        by_row.setdefault(key, {})[c.conversation_length] = c
        lengths.add(c.conversation_length)
    sorted_lengths = sorted(lengths)

    title = " + ".join(detector_mode)
    out = [f"## Detector mix: `{title}`", ""]

    header = ["Cache", "Backend", "Overrides"] + [f"L={L} anon ms / hits" for L in sorted_lengths]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "|".join(["---"] * len(header)) + "|")

    # Find baseline anon ms per (length, overrides) for speed-up calc.
    baselines: dict[tuple, float] = {}
    for c in cells:
        if c.cell.cache_mode == "none" and not c.skipped and c.total_anon_ms:
            baselines[(c.conversation_length, c.cell.overrides_mode)] = _median(c.total_anon_ms)

    for (cache_mode, backend, ovr), per_length in by_row.items():
        row = [
            cache_mode,
            backend if cache_mode != "none" else "—",
            ovr,
        ]
        for L in sorted_lengths:
            c = per_length.get(L)
            if c is None:
                row.append("—")
                continue
            if c.skipped:
                row.append(f"_(skipped: {c.skip_reason})_")
                continue
            anon = _median(c.total_anon_ms)
            base = baselines.get((L, ovr))
            speedup = f"  ({base/anon:.1f}×)" if base and anon > 0 else ""
            pipe_h, pipe_m, det_h, det_m = _hits_misses(c)
            row.append(
                f"{anon:.0f} ms{speedup} · pipe {pipe_h}/{pipe_h+pipe_m} · det {det_h}/{det_h+det_m}"
            )
        out.append("| " + " | ".join(row) + " |")

    out.append("")
    return "\n".join(out)


def _render_summary_observations(results: list[CellResult]) -> str:
    """Higher-level call-outs — the three or four observations
    operators most need at a glance."""
    out = ["## Summary observations", ""]

    # (1) Best speed-up overall vs no-cache, holding overrides=none.
    best_speedup: tuple[float, CellResult, CellResult] | None = None
    by_baseline: dict[tuple, CellResult] = {}
    for r in results:
        if r.skipped or not r.total_anon_ms:
            continue
        if r.cell.cache_mode == "none":
            by_baseline[(r.cell.detector_mode, r.conversation_length, r.cell.overrides_mode)] = r
    for r in results:
        if r.skipped or r.cell.cache_mode == "none" or not r.total_anon_ms:
            continue
        base = by_baseline.get((r.cell.detector_mode, r.conversation_length, r.cell.overrides_mode))
        if base is None or not base.total_anon_ms:
            continue
        ratio = _median(base.total_anon_ms) / _median(r.total_anon_ms)
        if best_speedup is None or ratio > best_speedup[0]:
            best_speedup = (ratio, r, base)

    if best_speedup is not None:
        ratio, winner, base = best_speedup
        out.append(
            f"- **Largest speed-up vs no-cache**: `{winner.cell.label}` on "
            f"`{winner.conversation_name}` — "
            f"{_median(base.total_anon_ms):.0f} ms → "
            f"{_median(winner.total_anon_ms):.0f} ms ({ratio:.1f}×)."
        )

    # (2) Override-fragmentation cost: same cell, none vs varied.
    pairs: dict[tuple, dict[str, CellResult]] = {}
    for r in results:
        if r.skipped:
            continue
        key = (r.cell.detector_mode, r.cell.cache_mode, r.cell.backend, r.conversation_length)
        pairs.setdefault(key, {})[r.cell.overrides_mode] = r
    deltas: list[tuple[float, str]] = []
    for key, by_ovr in pairs.items():
        none = by_ovr.get("none")
        varied = by_ovr.get("varied")
        if none and varied and none.total_anon_ms and varied.total_anon_ms:
            n = _median(none.total_anon_ms)
            v = _median(varied.total_anon_ms)
            if n > 0:
                deltas.append(((v - n) / n, _cell_label(none.cell) + f" {none.cell.detector_mode}/L={none.conversation_length}"))
    if deltas:
        worst = max(deltas, key=lambda d: d[0])
        out.append(
            f"- **Override fragmentation cost**: worst case "
            f"{worst[0]:+.0%} more anon time under `varied` overrides "
            f"({worst[1]}). Override changes scatter the pipeline-cache "
            f"key — per-detector caches absorb the partial mismatch."
        )

    # (3) Pipeline cache hit rate at length 30 (best-case reuse).
    for r in results:
        if r.skipped or r.conversation_length != 30:
            continue
        if r.cell.cache_mode not in ("pipeline", "both"):
            continue
        if r.cell.overrides_mode != "none":
            continue
        pipe_h, pipe_m, _, _ = _hits_misses(r)
        if pipe_h + pipe_m == 0:
            continue
        rate = _hit_rate(pipe_h, pipe_m)
        out.append(
            f"- **Pipeline-cache hit rate at L=30** for "
            f"`{r.cell.label}`: {rate:.0%} ({pipe_h}/{pipe_h+pipe_m})."
        )
        break

    # (4) Memory vs redis cost (same cache_mode, same detectors, same
    # length, same overrides — different backend).
    backend_pairs: dict[tuple, dict[str, CellResult]] = {}
    for r in results:
        if r.skipped or r.cell.cache_mode == "none":
            continue
        key = (r.cell.detector_mode, r.cell.cache_mode, r.cell.overrides_mode, r.conversation_length)
        backend_pairs.setdefault(key, {})[r.cell.backend] = r
    overheads: list[float] = []
    for by_backend in backend_pairs.values():
        mem = by_backend.get("memory")
        red = by_backend.get("redis")
        if mem and red and mem.total_anon_ms and red.total_anon_ms:
            m = _median(mem.total_anon_ms)
            if m > 0:
                overheads.append((_median(red.total_anon_ms) - m) / m)
    if overheads:
        avg = _mean(overheads)
        out.append(
            f"- **Redis vs memory backend**: redis adds an average "
            f"of {avg:+.0%} anon time across cells (RTT cost on every "
            f"cache lookup). Memory is the right default unless you "
            f"need cross-replica or restart-persistent cache state."
        )

    out.append("")
    return "\n".join(out)


def render_markdown(results: list[CellResult], repeats: int) -> str:
    """Build the full markdown report."""
    out = [_BANNER.format(repeats=repeats), _LAYOUT_NOTES]

    # Group by detector mix.
    by_detector: dict[tuple[str, ...], list[CellResult]] = {}
    for r in results:
        by_detector.setdefault(r.cell.detector_mode, []).append(r)

    out.append(_render_summary_observations(results))

    # One section per detector mix.
    detector_keys = sorted(by_detector.keys(), key=lambda k: (len(k), k))
    for det_mode in detector_keys:
        out.append(_render_headline_table(det_mode, by_detector[det_mode]))

    return "\n".join(out)


# ── JSON dump ─────────────────────────────────────────────────────────


def render_json(results: list[CellResult]) -> str:
    """Machine-readable dump of every per-turn observation. Pretty-
    printed (indent=2) so it's diffable in version control."""
    serial = []
    for r in results:
        serial.append({
            "cell": asdict(r.cell),
            "conversation_name": r.conversation_name,
            "conversation_length": r.conversation_length,
            "skipped": r.skipped,
            "skip_reason": r.skip_reason,
            "total_anon_ms_per_repeat": r.total_anon_ms,
            "total_deanon_ms_per_repeat": r.total_deanon_ms,
            "repeats": [
                [asdict(turn) for turn in repeat]
                for repeat in r.repeats
            ],
        })
    return json.dumps(serial, indent=2)


def write_outputs(
    results: list[CellResult],
    out_dir: Path,
    repeats: int,
) -> tuple[Path, Path]:
    """Write `report.md` + `results.json` to `out_dir`. Returns the
    two paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"
    json_path = out_dir / "results.json"
    md_path.write_text(render_markdown(results, repeats), encoding="utf-8")
    json_path.write_text(render_json(results), encoding="utf-8")
    return md_path, json_path


__all__ = ["render_json", "render_markdown", "write_outputs"]
