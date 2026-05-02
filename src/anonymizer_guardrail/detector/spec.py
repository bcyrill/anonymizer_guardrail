"""Per-detector wiring descriptor.

Adding a new detector to the pipeline historically required edits in
~7 files: `config.py` (env vars), `pipeline.py` (factory + semaphore +
in-flight counter + `_run` branch + exception handler + TaskGroup
`except*` clause + `stats()` payload + log line), `main.py` (BLOCKED
handler), tests, plus the bash launchers.

`DetectorSpec` collapses the Python-side fan-out into one constant per
detector, AND each detector module owns its own `CONFIG` BaseSettings
model so the env-var fields live alongside the code that reads them.
Adding a new detector is now: write the Detector class, define
`CONFIG = …`, define `SPEC = DetectorSpec(...)`, append the SPEC to
`REGISTERED_SPECS` in `detector/__init__.py`. Pipeline iterates the
registry to build all the wiring above; `main.py` iterates to build
the typed-error → BLOCKED response map.

What the registry deliberately does NOT cover:

  * **Cross-cutting config** (vault, surrogate, http server, faker
    locale): still on the central `Config` in `config.py`. These
    aren't per-detector, so there's no spec to attach them to.
  * **Bash wrapper** (`scripts/launcher.sh`): bash can't consume
    Python state, so it stays hand-wired. The Python launcher
    (`tools/launcher/`) consumes `LAUNCHER_METADATA` to build CLI
    flags and menu rows automatically — that's where new detectors
    get their UX wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..api import Overrides
    from .base import Detector


def _no_call_kwargs(_overrides: "Overrides", _api_key: str | None) -> dict[str, Any]:
    """Default `prepare_call_kwargs` for detectors whose `detect()`
    only needs the text. Lets the pipeline always call
    `**spec.prepare_call_kwargs(overrides, api_key)` without a None
    check — fewer branches in the hot path."""
    return {}


@dataclass(frozen=True)
class DetectorSpec:
    """Declarative wiring for one DETECTOR_MODE token.

    Fields fall into four groups:

      * **identity** — `name`, `factory`, `module`
      * **per-call kwargs** — `prepare_call_kwargs`
      * **concurrency cap** — `has_semaphore` + `stats_prefix`. The
        cap value itself comes from `spec.config.max_concurrency`,
        which is a live lookup of the detector module's `CONFIG`
        attribute (so test monkeypatches are visible).
      * **availability failure** — `unavailable_error`, `blocked_reason`.
        The fail-closed flag comes from `spec.config.fail_closed`,
        same live-lookup pattern.

    Post-init validation catches misconfiguration (e.g. `has_semaphore=True`
    with no `stats_prefix`) at module load rather than at first request,
    mirroring the project's "fail loud at boot" stance everywhere else.
    """

    name: str
    """DETECTOR_MODE token. Also matches the detector instance's
    `.name` attribute — pipeline dispatches via `det.name → spec`
    lookup, so the two must agree."""

    factory: Callable[[], "Detector"]
    """Constructs the detector instance. May raise RuntimeError at
    boot when the detector can't be deployed in the current process
    (e.g. `gliner_pii` without `GLINER_PII_URL`)."""

    module: ModuleType
    """The detector's own module — captured at SPEC construction via
    `sys.modules[__name__]`. The `config` property reads
    `module.CONFIG` on every access so test monkeypatching of
    `module.CONFIG` is immediately visible to all consumers."""

    prepare_call_kwargs: Callable[["Overrides", str | None], dict[str, Any]] = _no_call_kwargs
    """Builds the kwargs dict passed to `det.detect(text, **kwargs)`.
    Receives the per-call `Overrides` and the forwarded `api_key`
    (only LLM uses the latter today). Default is the no-op
    `_no_call_kwargs` so the pipeline can call it unconditionally."""

    # ── Concurrency cap (optional) ─────────────────────────────────────
    has_semaphore: bool = False
    """When True, the pipeline allocates a semaphore + in-flight
    counter (both keyed by `name`) and gates `_run` calls behind
    them. CPU-cheap detectors (regex, denylist) skip this — gating
    them would only serialize work that wasn't a problem.

    Cap size comes from `spec.config.max_concurrency`."""

    stats_prefix: str | None = None
    """Prefix for `/health` stats keys: `<prefix>_in_flight`,
    `<prefix>_max_concurrency`. Required when `has_semaphore=True`.
    Distinct from `name` because `privacy_filter` shortens to `pf`
    in the stats payload (legacy compatibility — operators have
    Grafana queries pinned to the short name)."""

    # ── Availability failure mode (optional) ───────────────────────────
    unavailable_error: type[Exception] | None = None
    """Typed error raised on availability failure (e.g. service
    unreachable, timeout, non-200). None when the detector has no
    availability concept (regex, denylist — they always succeed
    or raise programmer-error exceptions).

    Fail-closed flag comes from `spec.config.fail_closed`."""

    blocked_reason: str | None = None
    """Operator-facing message for `main.py`'s BLOCKED response when
    the typed error escapes the pipeline. Required when
    `unavailable_error` is set. Should NOT include the underlying
    exception message — that gets logged separately, and BLOCKED
    reasons are surfaced to the upstream LLM client (no leakage)."""

    # ── Result caching (optional) ─────────────────────────────────────
    has_cache: bool = False
    """When True, the pipeline pulls cache stats from the detector and
    surfaces them on /health. Setting this without the detector
    actually owning a `DetectionCache` (or without a `cache_stats()`
    method) is a programmer error caught at request time, not at
    boot. Cap size comes from `spec.config.cache_max_size`; 0 disables
    the cache at runtime even when `has_cache=True`."""

    @property
    def config(self) -> Any:
        """Live lookup of the detector module's `CONFIG` attribute.

        Returning a property (rather than caching the instance at
        SPEC construction) lets tests monkeypatch `module.CONFIG`
        — typically via `CONFIG.model_copy(update=...)` — and have the
        change take effect immediately for all consumers
        (Pipeline, main.py, the spec validators below).
        """
        return self.module.CONFIG

    def __post_init__(self) -> None:
        """Validate field combinations. Frozen-dataclass __post_init__
        can't mutate self but can raise — which is what we want here
        because misconfiguration should crash the import that defined
        the bad spec, not the request that first hit it."""
        if self.has_semaphore and self.stats_prefix is None:
            raise ValueError(
                f"DetectorSpec(name={self.name!r}): "
                f"has_semaphore=True requires stats_prefix to be set."
            )
        if self.unavailable_error is not None and self.blocked_reason is None:
            raise ValueError(
                f"DetectorSpec(name={self.name!r}): "
                f"unavailable_error requires blocked_reason to be set."
            )
        # The module must define a CONFIG attribute that satisfies the
        # spec's needs. We can't fully validate the shape (each detector
        # has its own fields), but we can at least check CONFIG exists
        # and has the well-known fields the pipeline / main.py read.
        if not hasattr(self.module, "CONFIG"):
            raise ValueError(
                f"DetectorSpec(name={self.name!r}): "
                f"module {self.module.__name__} has no CONFIG attribute."
            )
        cfg = self.module.CONFIG
        if self.has_semaphore and not hasattr(cfg, "max_concurrency"):
            raise ValueError(
                f"DetectorSpec(name={self.name!r}): has_semaphore=True "
                f"requires CONFIG.max_concurrency on {self.module.__name__}."
            )
        if self.unavailable_error is not None and not hasattr(cfg, "fail_closed"):
            raise ValueError(
                f"DetectorSpec(name={self.name!r}): unavailable_error "
                f"requires CONFIG.fail_closed on {self.module.__name__}."
            )
        if self.has_cache:
            # The cap lives on the detector module's CONFIG so operators
            # can tune it via env var alongside the rest of the
            # detector's settings — same shape as max_concurrency.
            if not hasattr(cfg, "cache_max_size"):
                raise ValueError(
                    f"DetectorSpec(name={self.name!r}): has_cache=True "
                    f"requires CONFIG.cache_max_size on {self.module.__name__}."
                )
            # stats_prefix is reused for cache stat keys
            # (`<prefix>_cache_size`, `<prefix>_cache_hits`, etc.) so
            # has_cache without it would mean stats can't be emitted.
            if self.stats_prefix is None:
                raise ValueError(
                    f"DetectorSpec(name={self.name!r}): has_cache=True "
                    f"requires stats_prefix to be set."
                )


__all__ = ["DetectorSpec"]
