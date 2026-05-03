"""Per-cell benchmark harness.

A "cell" is one combination of (detector mix, cache mode, backend,
overrides mode, conversation). For each cell:

  1. Build a fresh `Pipeline` with the cell's cache config (in-process
     monkeypatch of the central `config` + each detector's CONFIG —
     same mechanism the test suite uses).
  2. Walk the conversation turn-by-turn, simulating the LiteLLM
     Generic Guardrail flow:
       - Pre-call (`anonymize`) on the growing conversation history.
       - Post-call (`deanonymize`) on the assistant response (with
         surrogates for any entity the assistant mentions).
  3. Capture per-turn timings + cache-stat deltas.
  4. Tear down (`pipeline.aclose()` + restore the original config).

Cells are independent so the harness can repeat each cell N times
(median aggregation) and run cells in any order. Redis cells use a
fresh `CACHE_SALT` per cell so their cached entries don't leak across
cells (different salt → different digest → different Redis key).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from anonymizer_guardrail import config as config_mod
from anonymizer_guardrail import pipeline as pipeline_mod
from anonymizer_guardrail import surrogate as surrogate_mod
from anonymizer_guardrail.api import Overrides
from anonymizer_guardrail.detector import (
    llm as llm_mod,
    regex as regex_mod,
    denylist as denylist_mod,
    remote_gliner_pii as gliner_mod,
    remote_privacy_filter as pf_mod,
)
from anonymizer_guardrail.pipeline import Pipeline

from .payloads import Conversation


log = logging.getLogger("cache_bench.harness")


# ── Cell shape ────────────────────────────────────────────────────────


CacheMode = Literal["none", "detector", "pipeline", "both"]
Backend = Literal["memory", "redis"]
OverridesMode = Literal["none", "varied"]


@dataclass(frozen=True)
class BenchCell:
    """One benchmark configuration. The cell is the unit of comparison
    — all results are reported per-cell + per-conversation."""

    detector_mode: tuple[str, ...]
    """Active detectors as a tuple, e.g. `("regex", "gliner_pii")`.
    Order matches DETECTOR_MODE; first-listed wins type-resolution
    conflicts."""

    cache_mode: CacheMode
    """Which cache layer(s) are enabled:
       * `none`     — no cache anywhere.
       * `detector` — per-detector caches only.
       * `pipeline` — pipeline-level cache only.
       * `both`     — both layers active."""

    backend: Backend
    """`memory` or `redis`. Ignored when `cache_mode=none` (we still
    record it for label uniqueness so the matrix counts are clear)."""

    overrides_mode: OverridesMode
    """`none` (every call uses defaults) or `varied` (every other
    call switches gliner_threshold, simulating per-tenant config
    rotation)."""

    @property
    def label(self) -> str:
        """Human-readable cell label for tables."""
        dets = "+".join(self.detector_mode)
        cache_part = self.cache_mode if self.cache_mode == "none" else f"{self.cache_mode}-{self.backend}"
        return f"{dets} | {cache_part} | overrides={self.overrides_mode}"


@dataclass
class TurnResult:
    """One turn of one repeat."""

    turn_idx: int                 # 0-based
    history_len: int              # number of texts sent to anonymize this turn
    anon_ms: float
    deanon_ms: float
    pipeline_hits_delta: int
    pipeline_misses_delta: int
    detector_hits_delta: dict[str, int] = field(default_factory=dict)
    detector_misses_delta: dict[str, int] = field(default_factory=dict)


@dataclass
class CellResult:
    """All repeats for one (cell, conversation) pair."""

    cell: BenchCell
    conversation_name: str
    conversation_length: int
    repeats: list[list[TurnResult]] = field(default_factory=list)
    """Outer list: one entry per repeat. Inner list: one TurnResult
    per turn within that repeat."""

    total_anon_ms: list[float] = field(default_factory=list)
    """One total per repeat (sum of all turns' anon_ms)."""

    total_deanon_ms: list[float] = field(default_factory=list)
    """One total per repeat (sum of all turns' deanon_ms)."""

    skipped: bool = False
    skip_reason: str = ""


# ── Config patching ──────────────────────────────────────────────────


_DETECTOR_CONFIG_MODULES = {
    "llm": llm_mod,
    "regex": regex_mod,
    "denylist": denylist_mod,
    "gliner_pii": gliner_mod,
    "privacy_filter": pf_mod,
}


@dataclass
class ServiceUrls:
    """Externally-provided service URLs. The harness skips cells
    whose detectors would require an unreachable service."""

    gliner_pii_url: str = ""
    privacy_filter_url: str = ""
    cache_redis_url: str = ""

    def detector_available(self, name: str) -> bool:
        """True iff this detector can run with the configured URLs.
        regex/denylist are always available (in-process); LLM is only
        available with a configured `LLM_API_BASE` (which we don't
        bench) — so the harness reports LLM as unavailable here."""
        if name in {"regex", "denylist"}:
            return True
        if name == "gliner_pii":
            return bool(self.gliner_pii_url)
        if name == "privacy_filter":
            return bool(self.privacy_filter_url)
        if name == "llm":
            # The bench deliberately doesn't exercise LLM — too costly
            # for repeatable runs. Marked unavailable so cells using it
            # are skipped.
            return False
        return False

    def redis_available(self) -> bool:
        return bool(self.cache_redis_url)


def _build_pipeline_config(cell: BenchCell, urls: ServiceUrls):
    """Compute the central `Config` to use for this cell."""
    cache_redis_url = urls.cache_redis_url if cell.backend == "redis" else ""
    # Fresh random salt per cell so Redis keys don't collide across
    # cells (different salt → different BLAKE2b digest → different
    # Redis key). For memory cells the salt doesn't matter.
    cache_salt = secrets.token_hex(16)

    pipeline_backend = (
        cell.backend if cell.cache_mode in ("pipeline", "both") else "none"
    )
    # Pipeline cache LRU cap: 0 disables, anything > 0 enables.
    pipeline_max_size = 2000 if pipeline_backend == "memory" else 0

    return config_mod.config.model_copy(update={
        "detector_mode": ",".join(cell.detector_mode),
        "cache_redis_url": cache_redis_url,
        "cache_salt": cache_salt,
        "pipeline_cache_backend": pipeline_backend,
        "pipeline_cache_max_size": pipeline_max_size,
        # Pipeline cache TTL is generous so entries don't expire
        # mid-bench. Each conversation finishes in seconds.
        "pipeline_cache_ttl_s": 3600,
    })


def _build_detector_configs(cell: BenchCell, urls: ServiceUrls) -> dict:
    """Compute per-detector CONFIG instances for this cell. Returns
    a dict of `module → patched CONFIG`."""
    out: dict = {}

    # Per-detector cache enablement: only if cache_mode includes
    # the per-detector layer.
    detector_cache_active = cell.cache_mode in ("detector", "both")

    cache_max_size = 1000 if detector_cache_active and cell.backend == "memory" else 0
    cache_backend = cell.backend if detector_cache_active else "memory"
    # When detector caches are off (cache_mode in {none, pipeline}),
    # the backend selector doesn't matter — we leave it at memory
    # so the validator doesn't demand CACHE_REDIS_URL.

    if "regex" in cell.detector_mode:
        # Regex has no cache config — nothing to patch.
        pass

    if "denylist" in cell.detector_mode:
        # Same — no cache.
        pass

    if "privacy_filter" in cell.detector_mode:
        out[pf_mod] = pf_mod.CONFIG.model_copy(update={
            "url": urls.privacy_filter_url,
            "cache_max_size": cache_max_size,
            "cache_backend": cache_backend,
            "cache_ttl_s": 3600,
            # fail_open while benching so a one-off service blip
            # doesn't kill the run; cell-level skip handles the
            # complete-unavailable case.
            "fail_closed": False,
        })

    if "gliner_pii" in cell.detector_mode:
        out[gliner_mod] = gliner_mod.CONFIG.model_copy(update={
            "url": urls.gliner_pii_url,
            "cache_max_size": cache_max_size,
            "cache_backend": cache_backend,
            "cache_ttl_s": 3600,
            "fail_closed": False,
        })

    return out


@asynccontextmanager
async def _configured_pipeline(cell: BenchCell, urls: ServiceUrls):
    """Build a fresh Pipeline with the cell's cache config patched in.
    Yields the Pipeline; restores the original config on exit."""
    # Snapshot originals.
    orig_central = {
        "config_mod": config_mod.config,
        "pipeline_mod": pipeline_mod.config,
        "surrogate_mod": surrogate_mod.config,
    }
    orig_detectors = {mod: mod.CONFIG for mod in _DETECTOR_CONFIG_MODULES.values()}

    try:
        # Apply patches.
        new_central = _build_pipeline_config(cell, urls)
        config_mod.config = new_central
        pipeline_mod.config = new_central
        surrogate_mod.config = new_central

        for mod, new_config in _build_detector_configs(cell, urls).items():
            setattr(mod, "CONFIG", new_config)

        pipeline = Pipeline()
        try:
            yield pipeline
        finally:
            await pipeline.aclose()
    finally:
        # Restore central.
        config_mod.config = orig_central["config_mod"]
        pipeline_mod.config = orig_central["pipeline_mod"]
        surrogate_mod.config = orig_central["surrogate_mod"]
        # Restore per-detector.
        for mod, orig_cfg in orig_detectors.items():
            setattr(mod, "CONFIG", orig_cfg)


# ── Per-turn execution ────────────────────────────────────────────────


def _build_overrides(turn_idx: int, mode: OverridesMode) -> Overrides:
    """For `varied` mode, alternate gliner_threshold between turns to
    simulate per-tenant override rotation. Even turns: defaults. Odd
    turns: gliner_threshold=0.7."""
    if mode == "none":
        return Overrides.empty()
    if turn_idx % 2 == 1:
        return Overrides(gliner_threshold=0.7)
    return Overrides.empty()


def _stat_deltas(
    before: dict, after: dict, detectors: tuple[str, ...],
) -> tuple[int, int, dict[str, int], dict[str, int]]:
    """Compute (pipeline_hits, pipeline_misses, det_hits, det_misses)
    deltas between two `pipeline.stats()` snapshots."""
    pipe_h = after.get("pipeline_cache_hits", 0) - before.get("pipeline_cache_hits", 0)
    pipe_m = after.get("pipeline_cache_misses", 0) - before.get("pipeline_cache_misses", 0)
    det_h: dict[str, int] = {}
    det_m: dict[str, int] = {}
    # Map detector names to their stats prefixes — same lookup the
    # pipeline uses internally.
    prefix_by_name = {"llm": "llm", "privacy_filter": "pf", "gliner_pii": "gliner_pii"}
    for det in detectors:
        prefix = prefix_by_name.get(det)
        if prefix is None:
            continue
        det_h[det] = (
            after.get(f"{prefix}_cache_hits", 0)
            - before.get(f"{prefix}_cache_hits", 0)
        )
        det_m[det] = (
            after.get(f"{prefix}_cache_misses", 0)
            - before.get(f"{prefix}_cache_misses", 0)
        )
    return pipe_h, pipe_m, det_h, det_m


async def _run_one_repeat(
    pipeline: Pipeline, cell: BenchCell, conversation: Conversation,
) -> list[TurnResult]:
    """Walk the conversation once. Simulates the LiteLLM Generic
    Guardrail flow: each turn anonymizes the full chat history (prior
    user msgs + deanonymized prior assistant responses + new user
    msg), then deanonymizes the new assistant response (with
    surrogates substituted in for any entity the response references).
    """
    history: list[str] = []
    turns: list[TurnResult] = []
    # Track originals → surrogates *across the whole conversation* so
    # the assistant response uses the same surrogates the model
    # would have seen in its prompt. Real LiteLLM flow rebuilds this
    # via the vault on each call; for the simulation we just
    # accumulate it locally.
    sur_by_orig: dict[str, str] = {}

    for i in range(conversation.length):
        user_msg = conversation.user_msgs[i]
        assistant_raw = conversation.assistant_resps[i]
        history.append(user_msg)
        call_id = f"bench-{cell.label}-{conversation.name}-t{i}"
        overrides = _build_overrides(i, cell.overrides_mode)

        before = pipeline.stats()
        t0 = time.perf_counter()
        modified, mapping = await pipeline.anonymize(
            list(history), call_id=call_id, overrides=overrides,
        )
        anon_ms = (time.perf_counter() - t0) * 1000.0

        # Update the running surrogate map. `mapping` is
        # `surrogate → original`; flip for our local lookup.
        for sur, orig in mapping.items():
            sur_by_orig[orig] = sur

        # Build the assistant text the model would produce: substitute
        # known originals with their surrogates so deanonymize has
        # something to restore.
        assistant_for_post = assistant_raw
        # Sort by length desc so longer originals replace before
        # shorter substrings (mirrors `_build_replacer`'s sorting).
        for orig in sorted(sur_by_orig, key=len, reverse=True):
            if orig in assistant_for_post:
                assistant_for_post = assistant_for_post.replace(
                    orig, sur_by_orig[orig],
                )

        t0 = time.perf_counter()
        restored = await pipeline.deanonymize(
            [assistant_for_post], call_id=call_id,
        )
        deanon_ms = (time.perf_counter() - t0) * 1000.0

        # Append the deanonymized assistant text to history for the
        # next turn — that's what the user-facing chat history holds
        # at turn N+1.
        history.append(restored[0])

        after = pipeline.stats()
        pipe_h, pipe_m, det_h, det_m = _stat_deltas(before, after, cell.detector_mode)
        turns.append(TurnResult(
            turn_idx=i,
            history_len=len(history) - 1,  # -1 because we appended assistant after measurement
            anon_ms=anon_ms,
            deanon_ms=deanon_ms,
            pipeline_hits_delta=pipe_h,
            pipeline_misses_delta=pipe_m,
            detector_hits_delta=det_h,
            detector_misses_delta=det_m,
        ))
    return turns


async def run_cell(
    cell: BenchCell,
    conversation: Conversation,
    urls: ServiceUrls,
    repeats: int = 3,
) -> CellResult:
    """Run one (cell, conversation) pair `repeats` times. Each repeat
    uses a fresh Pipeline so cache state is isolated."""
    result = CellResult(
        cell=cell,
        conversation_name=conversation.name,
        conversation_length=conversation.length,
    )

    # Skip-checks: if any detector in the mix isn't available, skip
    # the cell with a clear reason. Keeps regex-only cells running
    # even when the operator hasn't started gliner/PF.
    for det in cell.detector_mode:
        if not urls.detector_available(det):
            result.skipped = True
            result.skip_reason = f"{det} service not configured"
            return result
    if cell.backend == "redis" and cell.cache_mode != "none" and not urls.redis_available():
        result.skipped = True
        result.skip_reason = "redis URL not configured"
        return result

    for repeat_idx in range(repeats):
        async with _configured_pipeline(cell, urls) as pipeline:
            turns = await _run_one_repeat(pipeline, cell, conversation)
        result.repeats.append(turns)
        result.total_anon_ms.append(sum(t.anon_ms for t in turns))
        result.total_deanon_ms.append(sum(t.deanon_ms for t in turns))
    return result


# ── Cell matrix builder ───────────────────────────────────────────────


_DETECTOR_COMBOS: tuple[tuple[str, ...], ...] = (
    ("regex",),
    ("gliner_pii",),
    ("privacy_filter",),
    ("regex", "gliner_pii"),
    ("regex", "gliner_pii", "privacy_filter"),
)


def build_cells(
    overrides_modes: tuple[OverridesMode, ...] = ("none", "varied"),
    backends: tuple[Backend, ...] = ("memory", "redis"),
) -> list[BenchCell]:
    """The full bench matrix. Cells with `cache_mode=none` only
    appear once per (detectors, overrides) pair (backend doesn't
    affect a no-cache run). Other cache_modes get one cell per
    backend."""
    cells: list[BenchCell] = []
    seen_no_cache: set[tuple] = set()

    for detector_mode in _DETECTOR_COMBOS:
        for overrides_mode in overrides_modes:
            # No-cache once per (detectors, overrides).
            key = (detector_mode, overrides_mode, "none")
            if key not in seen_no_cache:
                cells.append(BenchCell(
                    detector_mode=detector_mode,
                    cache_mode="none",
                    backend="memory",  # arbitrary, not used
                    overrides_mode=overrides_mode,
                ))
                seen_no_cache.add(key)
            # Cached cells per backend.
            for cache_mode in ("detector", "pipeline", "both"):
                for backend in backends:
                    cells.append(BenchCell(
                        detector_mode=detector_mode,
                        cache_mode=cache_mode,
                        backend=backend,
                        overrides_mode=overrides_mode,
                    ))
    return cells


__all__ = [
    "BenchCell",
    "Backend",
    "CacheMode",
    "CellResult",
    "OverridesMode",
    "ServiceUrls",
    "TurnResult",
    "build_cells",
    "run_cell",
]
