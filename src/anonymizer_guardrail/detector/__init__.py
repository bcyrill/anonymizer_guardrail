"""Detector registry — single source of truth for which detectors exist.

`REGISTERED_SPECS` is the ordered list the pipeline iterates to build
its semaphores, in-flight counters, factory dispatch table, exception
handlers, and `/health` stats payload. `main.py` iterates the same
list to build the typed-error → BLOCKED response map.

`LAUNCHER_METADATA` is the parallel mapping the dev-only launcher
(`tools/launcher/`) reads to know which detectors have an auto-startable
service and which env vars to forward onto the guardrail container.
Each detector module owns its `LAUNCHER_SPEC` constant (next to
`CONFIG` and `SPEC`); this module just aggregates them by name.

Adding a detector: append its module's `SPEC` here, and — if the
detector has a service or detector-specific env vars — also import its
`LAUNCHER_SPEC` and add it to `LAUNCHER_METADATA`. Removing one: delete
both entries. Reordering doesn't matter for the runtime — the
DETECTOR_MODE-derived order in `Pipeline._build_detectors` is what
governs match priority — but keeping the canonical priority order
here as well makes the registry easier to scan.
"""

from __future__ import annotations

from .regex import LAUNCHER_SPEC as _REGEX_LAUNCHER_SPEC, SPEC as _REGEX_SPEC
from .denylist import LAUNCHER_SPEC as _DENYLIST_LAUNCHER_SPEC, SPEC as _DENYLIST_SPEC
from .remote_privacy_filter import (
    LAUNCHER_SPEC as _PRIVACY_FILTER_LAUNCHER_SPEC,
    SPEC as _PRIVACY_FILTER_SPEC,
)
from .remote_gliner_pii import (
    LAUNCHER_SPEC as _GLINER_PII_LAUNCHER_SPEC,
    SPEC as _GLINER_PII_SPEC,
)
from .llm import LAUNCHER_SPEC as _LLM_LAUNCHER_SPEC, SPEC as _LLM_SPEC
from .launcher import LauncherSpec, ServiceSpec
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

# Per-detector launcher metadata, keyed by `SPEC.name`. Built from each
# detector module's `LAUNCHER_SPEC` constant — keeping the launcher data
# colocated with the rest of the detector definition (CONFIG, SPEC,
# detector class). Read by `tools/launcher/spec_extras.py` (which
# re-exports it for the launcher's CLI / menu / runner).
#
# Detectors without a `LAUNCHER_SPEC` would simply be absent from this
# mapping; the launcher tolerates absent entries (it just won't
# auto-start anything for them and won't pass detector-specific env
# vars). Today every registered detector ships a LAUNCHER_SPEC, so the
# mapping is complete.
LAUNCHER_METADATA: dict[str, LauncherSpec] = {
    _REGEX_SPEC.name: _REGEX_LAUNCHER_SPEC,
    _DENYLIST_SPEC.name: _DENYLIST_LAUNCHER_SPEC,
    _PRIVACY_FILTER_SPEC.name: _PRIVACY_FILTER_LAUNCHER_SPEC,
    _GLINER_PII_SPEC.name: _GLINER_PII_LAUNCHER_SPEC,
    _LLM_SPEC.name: _LLM_LAUNCHER_SPEC,
}


__all__ = [
    "DetectorSpec",
    "LAUNCHER_METADATA",
    "LauncherSpec",
    "REGISTERED_SPECS",
    "ServiceSpec",
    "SPECS_BY_NAME",
    "SPECS_WITH_CACHE",
    "SPECS_WITH_SEMAPHORE",
    "TYPED_UNAVAILABLE_ERRORS",
]
