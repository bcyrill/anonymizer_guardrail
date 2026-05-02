"""Shared infrastructure for the remote detectors.

The three slow detectors (LLM, privacy_filter, gliner_pii) share the
same plumbing: an httpx async client gated by a per-detector
concurrency cap (in `Pipeline._run_detector`), a result cache that
opts in via the detector's `*_CACHE_MAX_SIZE` env var, plus the
trivial `cache_stats()` / `aclose()` accessors and the empty-text
short-circuit / `get_or_compute` template.

They diverge on:

  * **construction args** — LLM has `api_base/api_key/model`, PF has
    `url`, gliner has `url + labels + threshold`.
  * **`detect()` signature** — LLM accepts `api_key/model/prompt_name`,
    gliner accepts `labels/threshold`, PF takes nothing.
  * **cache key shape** — driven by which overrides change the
    detector's output.
  * **`_do_detect()` body** — completely different HTTP requests.

This module owns the *shared* part. Subclasses keep their own
`__init__` signature (calling `super().__init__()` for the shared
bits), their own `detect()` signature (calling `_detect_via_cache()`
for the cache-wrap template), and their own `_do_detect()` body.
The diverging signatures stay subclass-specific so callers and the
type checker keep their IDE-friendly per-detector views.

Why a base class instead of a mixin or composition: subclasses want
`isinstance(det, CachingDetector)` to satisfy the typing contract
in `Pipeline.stats()`, and `aclose()` is genuinely the same
implementation across all three. A mixin or has-a `RemoteCore`
helper would force every subclass to forward those calls
explicitly. Inheritance is the lighter weight here.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Hashable

import httpx

from .base import Match
from .cache import DetectorResultCache, InMemoryDetectionCache


class BaseRemoteDetector:
    """Shared base for httpx-backed detectors that opt into result
    caching. Owns the httpx client, the cache instance, and the
    cache-wrap helper. Subclasses set their own `name = "..."`
    class attribute and provide `detect()` + `_do_detect()`.

    Stub-construction caveat: tests sometimes build a "bare" detector
    via `Class.__new__(Class)` to bypass httpx wiring. Those stubs
    must still set `name` and `_cache` manually (see
    `tests/_pipeline_helpers.py`); they don't need `_client` because
    the test never reaches the network or `aclose()`. If your test
    *does* reach `aclose()`, attach a stub `_client` with a no-op
    async `aclose()` of your own.
    """

    name: str  # Set by subclasses to their DETECTOR_MODE token.

    def __init__(self, *, timeout_s: int, cache_max_size: int) -> None:
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(timeout=timeout_s)
        # Annotated as the Protocol so a future swap to a different
        # cache backend (e.g. a Redis-backed implementation when
        # multi-replica lands — see TASKS.md) is a one-line factory
        # change here, with no edits to the subclasses.
        self._cache: DetectorResultCache = InMemoryDetectionCache(cache_max_size)

    async def _detect_via_cache(
        self,
        text: str,
        cache_key: Hashable,
        do_call: Callable[[], Awaitable[list[Match]]],
    ) -> list[Match]:
        """Empty-text short-circuit + cache wrap. Subclass `detect()`
        builds the cache key (encoding which per-call overrides matter)
        and a closure capturing the diverging kwargs for `_do_detect`,
        then calls this. The shape is shared because every detector's
        contract on empty input is the same: return `[]` without
        touching the network or the cache."""
        if not text or not text.strip():
            return []
        return await self._cache.get_or_compute(cache_key, do_call)

    def cache_stats(self) -> dict[str, int]:
        """Result cache snapshot for `Pipeline.stats()` / `/health`.
        Always defined (even when caching is disabled) so the spec's
        `has_cache=True` flag is the single switch the pipeline reads.
        Satisfies the `CachingDetector` Protocol contract."""
        return self._cache.stats()

    async def aclose(self) -> None:
        """Drain the httpx connection pool. Wired to `Pipeline.aclose()`
        which fires on FastAPI shutdown so the connection pool drains
        cleanly across all configured remote detectors."""
        await self._client.aclose()


__all__ = ["BaseRemoteDetector"]
