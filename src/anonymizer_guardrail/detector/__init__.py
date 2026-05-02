"""Detector registry — single source of truth for which detectors exist.

`REGISTERED_SPECS` is the ordered list the pipeline iterates to build
its semaphores, in-flight counters, factory dispatch table, exception
handlers, and `/health` stats payload. `main.py` iterates the same
list to build the typed-error → BLOCKED response map.

Adding a detector: append its module's `SPEC` here. Removing one:
delete the entry. Reordering doesn't matter for the runtime — the
DETECTOR_MODE-derived order in `Pipeline._build_detectors` is what
governs match priority — but keeping the canonical priority order
here as well makes the registry easier to scan.
"""

from __future__ import annotations

from .regex import SPEC as _REGEX_SPEC
from .denylist import SPEC as _DENYLIST_SPEC
from .remote_privacy_filter import SPEC as _PRIVACY_FILTER_SPEC
from .remote_gliner_pii import SPEC as _GLINER_PII_SPEC
from .llm import SPEC as _LLM_SPEC
from .spec import DetectorSpec

# Canonical priority order — same shape DETECTOR_MODE accepts when
# constructed left-to-right. Read-only after import; pipeline /
# main.py treat it as immutable.
REGISTERED_SPECS: tuple[DetectorSpec, ...] = (
    _REGEX_SPEC,
    _DENYLIST_SPEC,
    _PRIVACY_FILTER_SPEC,
    _GLINER_PII_SPEC,
    _LLM_SPEC,
)

# Lookup table — pipeline's `_run` dispatches via `det.name → spec`,
# so the detector instance only needs its `.name` attribute set
# correctly (already true via the Detector Protocol).
SPECS_BY_NAME: dict[str, DetectorSpec] = {s.name: s for s in REGISTERED_SPECS}

# Tuple of typed errors — used in `except*` clauses and main.py's
# top-level except so all detectors with availability concerns are
# handled uniformly. Empty for detectors without an unavailable_error.
TYPED_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = tuple(
    s.unavailable_error for s in REGISTERED_SPECS if s.unavailable_error is not None
)

# Subset of specs with a concurrency cap — pipeline iterates this
# narrower list when allocating semaphores / counters. Same order
# as REGISTERED_SPECS so iteration is deterministic.
SPECS_WITH_SEMAPHORE: tuple[DetectorSpec, ...] = tuple(
    s for s in REGISTERED_SPECS if s.has_semaphore
)

# Subset of specs that surface a per-detector result cache. Pipeline
# iterates this narrower list when emitting cache stats on /health.
# Membership ≠ "cache is currently active" — an operator can set
# CONFIG.cache_max_size=0 to keep the spec wired for stats while the
# detector runs uncached. The stats payload reflects that as size=0,
# max=0, hits=0, misses=0.
SPECS_WITH_CACHE: tuple[DetectorSpec, ...] = tuple(
    s for s in REGISTERED_SPECS if s.has_cache
)


__all__ = [
    "DetectorSpec",
    "REGISTERED_SPECS",
    "SPECS_BY_NAME",
    "SPECS_WITH_CACHE",
    "SPECS_WITH_SEMAPHORE",
    "TYPED_UNAVAILABLE_ERRORS",
]
