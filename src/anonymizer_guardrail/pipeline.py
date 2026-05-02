"""
Anonymization pipeline.

Wires the detectors, surrogate generator, and vault together. Two entry points:

  * anonymize(texts, call_id) → modified texts. Detects entities, generates
    surrogates, replaces in-place, persists the reverse mapping under call_id.

  * deanonymize(texts, call_id) → restored texts. Looks up the mapping and
    substitutes surrogates back to originals.

Detector wiring is driven by `detector.REGISTERED_SPECS` — a list of
`DetectorSpec` constants, one per DETECTOR_MODE token. The pipeline
iterates the registry to build its semaphores, in-flight counters,
factory dispatch table, exception handlers, and `/health` stats
payload. Adding a new detector is a one-line append to the registry
plus the spec definition in the detector's own module — no edits in
this file. See `detector/spec.py` for the descriptor shape.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Iterable

from .api import Overrides
from .config import config
from .detector import (
    REGISTERED_SPECS,
    SPECS_BY_NAME,
    SPECS_WITH_CACHE,
    SPECS_WITH_SEMAPHORE,
    TYPED_UNAVAILABLE_ERRORS,
)
from .detector.base import CachingDetector, Detector, Match
from .surrogate import SurrogateGenerator
from .vault import VaultBackend, build_vault

log = logging.getLogger("anonymizer.pipeline")


# Sentinel separator inserted between texts when a detector runs in
# `input_mode="merged"`. Properties we need:
#
#   1. **Implausibly part of any real document or entity.** The
#      "ANONYMIZER-SEGMENT-BREAK" tag is unique enough that real
#      content shouldn't contain it; the surrounding `\n\n` and
#      `---` give a visual paragraph break the LLM is likely to
#      treat as a section divider rather than as content to
#      classify.
#   2. **Stable as a literal string.** We filter detected matches
#      whose text contains the sentinel — that's how we guard
#      against the model classifying the separator itself as an
#      entity, or returning a span that crosses a boundary
#      ("Alice<SEP>Smith" as PERSON). Both filtered out by the
#      contains-check.
#
# Don't change the literal without updating the contains-check
# logic below. Don't put it in operator-controllable config — the
# point is that it's a known, stable, non-content marker; making
# it tunable just opens a footgun where two operators pick
# different sentinels and wonder why detections drift.
_MERGE_SENTINEL = "\n\n--- ANONYMIZER-SEGMENT-BREAK ---\n\n"
# Distinctive substring used to filter sentinel-bearing matches. We
# could check for the full sentinel, but a hallucinated match might
# contain only the trimmed core (e.g. "--- ANONYMIZER-SEGMENT-BREAK ---"
# or just "ANONYMIZER-SEGMENT-BREAK") rather than the surrounding
# newlines. Checking the distinctive tag catches all such cases —
# cross-boundary spans, classifications of the separator itself, and
# any partial match the model invents around the marker.
_MERGE_SENTINEL_MARKER = "ANONYMIZER-SEGMENT-BREAK"

# Per-resource budget for `Pipeline.aclose`. A wedged backend (Redis
# hung, httpx pool stuck on a slow socket) shouldn't be able to hang
# FastAPI shutdown indefinitely. 5 seconds is generous for any healthy
# pool drain and short enough that operators don't notice on a clean
# stop. Not tunable via env — this is a shutdown safety budget, not
# operator-facing behaviour.
_ACLOSE_TIMEOUT_S = 5.0


def _build_detectors() -> list[Detector]:
    """Parse DETECTOR_MODE as a comma-separated list of detector names.

    Order is preserved — it determines the order detectors run in (parallel
    via TaskGroup, but `_dedup` keeps first-seen entity_type, so a
    duplicate match will adopt the type from the detector listed earlier).
    Whitespace around names is tolerated; duplicates are collapsed with
    a warning. Unknown names log a warning and are skipped; if nothing
    valid remains, we fall back to regex-only so the service still boots.

    Factory lookup goes via the registry — the per-detector `SPEC.factory`
    is the single source of truth for how to construct a given token.
    """
    raw = config.detector_mode or ""
    names = [n.strip() for n in raw.split(",") if n.strip()]

    detectors: list[Detector] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            log.warning("DETECTOR_MODE: duplicate detector %r — collapsed", name)
            continue
        spec = SPECS_BY_NAME.get(name)
        if spec is None:
            log.warning(
                "DETECTOR_MODE: unknown detector %r (valid names: %s)",
                name, ", ".join(sorted(SPECS_BY_NAME)),
            )
            continue
        seen.add(name)
        detectors.append(spec.factory())

    if not detectors:
        log.warning(
            "DETECTOR_MODE=%r produced no valid detectors — falling back to regex.",
            raw,
        )
        detectors = [SPECS_BY_NAME["regex"].factory()]
    return detectors


def _dedup(matches: Iterable[Match]) -> list[Match]:
    """Keep one Match per unique text, preferring the first-seen entity_type.

    Keying on text alone (not `(text, type)`) is intentional: the same
    span getting flagged by two detectors with different types — e.g.
    regex says EMAIL_ADDRESS, llm says PERSON — is the *normal* case,
    and we need exactly one surrogate per substring. The detector
    listed earlier in DETECTOR_MODE wins type-resolution, which is
    why operators put deterministic detectors (regex, denylist) first
    so their shape-based classifications beat the LLM/NER layers'
    interpretive ones. See `docs/detectors/index.md` →
    "When detectors disagree" for the worked example.

    Don't "fix" this by keying on `(text, entity_type)` — that
    produces two surrogates for the same substring, and the
    replacement regex then can't tell which to use.
    """
    seen: dict[str, Match] = {}
    for m in matches:
        if m.text and m.text not in seen:
            seen[m.text] = m
    return list(seen.values())


def _build_replacer(mapping: dict[str, str]) -> Callable[[str], str]:
    """Compile the alternation pattern ONCE and return a closure that
    applies it to a text. Callers process N texts with one compile, instead
    of recompiling per text.

    The pattern alternates over all keys, longest-first so a shorter key
    that's a prefix of a longer one doesn't shadow it. A single regex pass
    avoids transitive replacements (where a surrogate generated for Entity
    A matches the original of Entity B and gets replaced again).
    """
    if not mapping:
        return lambda text: text
    pattern = re.compile(
        "|".join(re.escape(k) for k in sorted(mapping.keys(), key=len, reverse=True))
    )

    def apply(text: str) -> str:
        return pattern.sub(lambda m: mapping[m.group(0)], text)

    return apply


class Pipeline:
    def __init__(self) -> None:
        self._detectors: list[Detector] = _build_detectors()
        self._surrogates = SurrogateGenerator()
        # Vault backend is config-driven (memory by default; redis when
        # operators select multi-replica). Pipeline holds the Protocol
        # type so callers don't depend on the concrete implementation —
        # see `vault.build_vault()` for the dispatch.
        self._vault: VaultBackend = build_vault()

        # Concurrency caps and in-flight counters live in dicts keyed
        # by spec name — one entry per detector that opted into a
        # semaphore. Reading via `spec.config.max_concurrency` so test
        # monkeypatches of the detector's CONFIG take effect on
        # Pipeline rebuild.
        self._semaphores: dict[str, asyncio.Semaphore] = {
            spec.name: asyncio.Semaphore(spec.config.max_concurrency)
            for spec in SPECS_WITH_SEMAPHORE
        }
        # Per-detector in-flight counters. Single-threaded asyncio means
        # plain int mutation between await points is safe — no lock
        # needed. Surface via `stats()` for the /health probe.
        self._inflight_counters: dict[str, int] = {
            spec.name: 0 for spec in SPECS_WITH_SEMAPHORE
        }

        # Build the startup log line dynamically so adding a detector
        # doesn't require touching the format string. Keeps the message
        # operator-readable (`fail_closed=…` per detector, `max_conc=…`
        # per detector).
        fc_parts = [
            f"{spec.name}_fail_closed={spec.config.fail_closed}"
            for spec in REGISTERED_SPECS
            if spec.unavailable_error is not None
        ]
        cap_parts = [
            f"{spec.name}_max_conc={spec.config.max_concurrency}"
            for spec in SPECS_WITH_SEMAPHORE
        ]
        log.info(
            "Pipeline ready — detectors=[%s], vault_ttl=%ds, %s, %s",
            ", ".join(d.name for d in self._detectors),
            config.vault_ttl_s,
            ", ".join(fc_parts),
            ", ".join(cap_parts),
        )

        # Surface mutually-exclusive (cache + merge) configurations as
        # a startup warning. Every merged blob is unique by construction
        # (the new user message is always different), so the cache is
        # 100% miss in merged mode and entries only consume storage.
        # Two ways the cache is "live": memory backend with cap > 0, or
        # the redis backend at all (it has no per-detector cap to gate
        # on — Redis-side maxmemory is the only bound). Warn but don't
        # fail — a typo in env vars shouldn't crash the service, and
        # the detector still runs correctly (just stores 100%-miss
        # cache entries until they evict).
        for spec in REGISTERED_SPECS:
            mode = getattr(spec.config, "input_mode", "per_text")
            cache_max = getattr(spec.config, "cache_max_size", 0)
            cache_backend = getattr(spec.config, "cache_backend", "memory")
            cache_active = cache_max > 0 or cache_backend == "redis"
            if mode == "merged" and cache_active:
                upper = spec.name.upper()
                log.warning(
                    "%s detector configured with input_mode=merged AND "
                    "a live cache (backend=%s, cache_max_size=%d). "
                    "Every merged blob is unique by construction, so "
                    "cache entries are 100%% miss and only consume "
                    "storage. Either switch to %s_INPUT_MODE=per_text "
                    "to use the cache, or set %s_CACHE_BACKEND=memory "
                    "+ %s_CACHE_MAX_SIZE=0 to silence this warning.",
                    spec.name, cache_backend, cache_max,
                    upper, upper, upper,
                )

    def stats(self) -> dict[str, object]:
        """Snapshot of pipeline-internal counters for the /health probe.
        All reads are cheap and lock-free.

        Mostly `int` values, plus per-detector `<prefix>_cache_backend`
        (`"memory"` / `"redis"`) string fields so operators can verify
        their backend selection landed without consulting logs.
        """
        cache_size, cache_max = self._surrogates.cache_stats()
        out: dict[str, object] = {
            "vault_size": self._vault.size(),
            "surrogate_cache_size": cache_size,
            "surrogate_cache_max": cache_max,
        }
        # Per-detector concurrency snapshot. Key names use the spec's
        # `stats_prefix` (e.g. `pf_in_flight`) rather than the raw
        # detector name (`privacy_filter_in_flight`) for legacy
        # compatibility — operators have Grafana dashboards pinned to
        # the short names.
        for spec in SPECS_WITH_SEMAPHORE:
            out[f"{spec.stats_prefix}_in_flight"] = self._inflight_counters[spec.name]
            out[f"{spec.stats_prefix}_max_concurrency"] = spec.config.max_concurrency
        # Per-detector cache snapshot. Mirrors the SPECS_WITH_SEMAPHORE
        # loop above: emit a fixed set of keys for every spec that opted
        # into has_cache, regardless of whether the detector is
        # currently active under DETECTOR_MODE. Inactive detectors
        # report the configured cap with zero size/hits/misses — that
        # keeps /health key sets stable so dashboards can pin to them.
        active_by_name = {d.name: d for d in self._detectors}
        for spec in SPECS_WITH_CACHE:
            det = active_by_name.get(spec.name)
            # Type-side contract: SPECS_WITH_CACHE membership means the
            # detector class implements `cache_stats()` (see
            # `detector/base.py:CachingDetector`). The runtime-checkable
            # isinstance narrowing both satisfies type checkers AND
            # acts as a defensive guard if a future detector sets
            # has_cache=True on its spec but forgets the method.
            if det is not None and isinstance(det, CachingDetector):
                cs = det.cache_stats()
            else:
                # Inactive (DETECTOR_MODE narrowed it out) OR — under
                # the defensive guard — a misconfigured detector. Emit
                # the configured cap with zero counters so /health key
                # sets stay stable.
                cs = {
                    "size": 0,
                    "max": int(spec.config.cache_max_size),
                    "hits": 0,
                    "misses": 0,
                }
            out[f"{spec.stats_prefix}_cache_size"]   = cs["size"]
            out[f"{spec.stats_prefix}_cache_max"]    = cs["max"]
            out[f"{spec.stats_prefix}_cache_hits"]   = cs["hits"]
            out[f"{spec.stats_prefix}_cache_misses"] = cs["misses"]
            # `<prefix>_cache_backend` lets operators confirm their
            # `<DETECTOR>_CACHE_BACKEND` env var landed without grepping
            # logs. Read live from spec.config — same pattern as the
            # other config-derived fields above.
            out[f"{spec.stats_prefix}_cache_backend"] = (
                getattr(spec.config, "cache_backend", "memory")
            )
        return out

    @property
    def vault(self) -> VaultBackend:
        return self._vault

    def _resolve_active_detectors(
        self, overrides: Overrides
    ) -> list[Detector]:
        """Apply the per-request `detector_mode` filter as a SUBSET over
        the detectors configured at startup.

        Operators can narrow the active set per call (e.g. "skip the LLM
        for this request") but cannot expand it: every detector has
        boot-time setup that isn't safe to run mid-request — the remote
        detectors set up httpx clients against configured URLs, and the
        LLM detector needs the configured api_base / model. Names that
        aren't currently configured are dropped with a warning.
        """
        if not overrides.detector_mode:
            return self._detectors
        configured = {d.name: d for d in self._detectors}
        active: list[Detector] = []
        unknown: list[str] = []
        for name in overrides.detector_mode:
            d = configured.get(name)
            if d is None:
                unknown.append(name)
            elif d not in active:
                active.append(d)
        if unknown:
            log.warning(
                "detector_mode override mentions detector(s) %s that aren't "
                "configured (configured: %s) — ignored.",
                unknown, list(configured),
            )
        if not active:
            log.warning(
                "detector_mode override resolved to an empty set; falling "
                "back to all configured detectors for this request."
            )
            return self._detectors
        return active

    async def _run_detector(
        self,
        det: Detector,
        text: str,
        *,
        api_key: str | None,
        overrides: Overrides,
    ) -> list[Match]:
        """Run a single detector against `text` with the per-spec semaphore
        + error-policy wrapper. Used by both per-text and merged dispatch
        paths so the policy lives in exactly one place.

        Per-detector exceptions are handled here so one detector crashing
        doesn't tear down the surrounding TaskGroup. Typed unavailable
        errors still propagate under fail-closed — the TaskGroup then
        cancels the in-flight siblings (saving wasted work since the
        request is about to BLOCK) and re-raises the typed error so
        main.py's BLOCKED handler matches.
        """
        spec = SPECS_BY_NAME[det.name]
        kwargs = spec.prepare_call_kwargs(overrides, api_key)
        try:
            if spec.has_semaphore:
                # Gate the call behind the per-spec semaphore. The
                # in-flight counter increment MUST be the first
                # statement after acquisition, before the inner `try`
                # — putting anything between `async with` and
                # `+= 1` (or worse, between the increment and `try`)
                # would silently leak counter slots if that statement
                # raised: the with-block would still release the
                # semaphore on the way out, but the matching `-= 1`
                # in the `finally` would never fire. `/health` would
                # show in_flight stuck above 0 forever.
                async with self._semaphores[spec.name]:
                    self._inflight_counters[spec.name] += 1
                    try:
                        return await det.detect(text, **kwargs)
                    finally:
                        self._inflight_counters[spec.name] -= 1
            return await det.detect(text, **kwargs)
        except Exception as exc:
            # Single error path. Two cases:
            #   1. Typed unavailable error → fail-closed re-raises
            #      so main.py BLOCKs; fail-open logs + degrades to [].
            #   2. Anything else (programmer error, transient bug)
            #      under fail-closed → re-wrap as the typed error so
            #      the BLOCKED path fires. The asymmetry is on
            #      purpose: an operator who set fail-closed wanted
            #      to BLOCK on any problem from this detector, not
            #      just availability ones.
            if (
                spec.unavailable_error is not None
                and isinstance(exc, spec.unavailable_error)
            ):
                if spec.config.fail_closed:
                    raise
                log.warning(
                    "%s detector unavailable, proceeding fail-open: %s",
                    spec.name, exc,
                )
                return []

            log.exception("Detector %s crashed: %s", det.name, exc)
            if (
                spec.unavailable_error is not None
                and spec.config.fail_closed
            ):
                raise spec.unavailable_error(
                    f"Unexpected failure in {spec.name} detector "
                    f"({type(exc).__name__}): {exc}"
                ) from exc
            # Detectors with no availability concept (regex,
            # denylist) just degrade to []. A buggy regex pattern
            # shouldn't take the request down — the LLM/PF/etc.
            # layers still run and provide coverage.
            return []

    async def _detect_one(
        self,
        text: str,
        *,
        api_key: str | None = None,
        overrides: Overrides = Overrides.empty(),
        detectors: list[Detector] | None = None,
    ) -> list[Match]:
        """Run a per-text fan-out: every detector in `detectors` against
        the same text in parallel, then dedup.

        `detectors=None` → resolve from `overrides.detector_mode` (the
        default behaviour for direct callers). The merged-dispatch path
        in `anonymize` passes an explicit subset so it doesn't double-
        count detectors that already ran in merged mode.

        Regex is CPU-bound and fast; the LLM call dominates total latency.
        Running them concurrently means the per-text floor is `max(regex,
        llm)` instead of `regex + llm`. Cheap regardless — and if a second
        slow detector ever gets added, the win compounds.

        Concurrency caps (LLM, privacy_filter, gliner_pii) are independent
        per-detector semaphores keyed by spec name; throttling one doesn't
        starve the others. CPU-cheap detectors (regex, denylist) skip
        gating entirely.
        """
        active = (
            self._resolve_active_detectors(overrides)
            if detectors is None else detectors
        )
        if not active:
            return []

        # TaskGroup (vs asyncio.gather) cancels in-flight sibling tasks
        # the moment one detector raises a fail-closed error. Matters
        # because the request is about to BLOCK; a still-running
        # privacy_filter call (hundreds of ms locally, seconds remotely)
        # would just be wasted work. gather() left siblings running
        # until completion and discarded their results.
        #
        # The except* unwrap re-raises the first matching typed exception
        # so main.py's typed-error handler matches — it wouldn't catch
        # the BaseExceptionGroup TaskGroup raises by default. The tuple
        # comes from the registry, so adding a new detector with its
        # own typed error doesn't require an edit here.
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(
                        self._run_detector(d, text, api_key=api_key, overrides=overrides)
                    )
                    for d in active
                ]
        except* TYPED_UNAVAILABLE_ERRORS as eg:
            raise eg.exceptions[0] from None

        results = [t.result() for t in tasks]
        if log.isEnabledFor(logging.DEBUG):
            for det, matches in zip(active, results):
                log.debug(
                    "Detector %s returned %d matches: %s",
                    det.name, len(matches),
                    [(m.text, m.entity_type) for m in matches],
                )
        flat = [m for sub in results for m in sub]
        deduped = _dedup(flat)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "After dedup: %d matches: %s",
                len(deduped),
                [(m.text, m.entity_type) for m in deduped],
            )
        return deduped

    @staticmethod
    def _partition_by_input_mode(
        detectors: list[Detector],
    ) -> tuple[list[Detector], list[Detector]]:
        """Split active detectors into (per_text, merged) by their
        configured `input_mode`. A detector whose CONFIG has no
        `input_mode` field defaults to `per_text` — the per-text
        fan-out is the safe default that every detector supports."""
        per_text: list[Detector] = []
        merged: list[Detector] = []
        for det in detectors:
            spec = SPECS_BY_NAME[det.name]
            mode = getattr(spec.config, "input_mode", "per_text")
            if mode == "merged":
                merged.append(det)
            else:
                per_text.append(det)
        return per_text, merged

    def _apply_merge_size_fallback(
        self,
        merged_detectors: list[Detector],
        merged_blob: str,
    ) -> tuple[list[Detector], list[Detector]]:
        """For merged-mode detectors that declare a `max_chars` cap on
        their config, demote to per_text dispatch when the merged blob
        exceeds that cap. Returns (still_merged, demoted_to_per_text).

        Opportunistic: the optimisation degrades gracefully rather than
        raising. An operator's merged-mode preference is honoured when
        the blob fits and silently falls back when it doesn't, so a
        single oversized request doesn't break detection coverage.
        Detectors without a `max_chars` field are passed through
        unchanged — the remote service decides its own size policy.
        """
        still_merged: list[Detector] = []
        demoted: list[Detector] = []
        for det in merged_detectors:
            spec = SPECS_BY_NAME[det.name]
            max_chars = getattr(spec.config, "max_chars", None)
            if max_chars is not None and len(merged_blob) > max_chars:
                log.debug(
                    "Merged blob (%d chars) exceeds %s.max_chars (%d); "
                    "falling back to per_text dispatch for this request.",
                    len(merged_blob), det.name, max_chars,
                )
                demoted.append(det)
            else:
                still_merged.append(det)
        return still_merged, demoted

    async def aclose(self) -> None:
        """Release per-detector resources (httpx connection pools, etc)
        plus the vault backend (Redis connection pool when applicable).
        Wired to FastAPI's lifespan in main.py so shutdown is clean.

        Order: detectors first, vault last. The pipeline's own request
        path is `detect → vault.put` (`anonymize`) and `vault.pop`
        (`deanonymize`). If a request is mid-anonymize when shutdown
        fires, the detector path needs the vault to still be live for
        the put; closing the vault first would leave that put hitting
        a closed Redis client. Uvicorn drains in-flight requests
        before the lifespan teardown fires today, but pinning the
        order defensively here doesn't hurt and removes the implicit
        dependency on graceful-shutdown ordering.

        Each `aclose` is wrapped in `asyncio.wait_for` with a 5-second
        budget so a wedged backend (Redis hung, httpx pool stuck on a
        slow socket) can't hang FastAPI shutdown indefinitely.
        """
        for det in self._detectors:
            close = getattr(det, "aclose", None)
            if close is None:
                continue
            try:
                await asyncio.wait_for(close(), timeout=_ACLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning(
                    "Detector %s aclose timed out after %ds; abandoning.",
                    det.name, _ACLOSE_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001 — never fail shutdown
                log.warning("Detector %s aclose failed: %s", det.name, exc)

        try:
            await asyncio.wait_for(self._vault.aclose(), timeout=_ACLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning(
                "Vault aclose timed out after %ds; abandoning.",
                _ACLOSE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — never fail shutdown
            log.warning("Vault aclose failed: %s", exc)

    async def _run_detection(
        self,
        texts: list[str],
        *,
        api_key: str | None,
        overrides: Overrides,
    ) -> tuple[list[list[Match]], list[list[Match]]]:
        """Dispatch detection across all active detectors and return
        their match lists, partitioned by dispatch mode.

        Returns `(per_text_matches, merged_matches)` where:

          * `per_text_matches[i]` is the deduped matches for `texts[i]`
            from the per_text-mode detectors (one inner list per text).
          * `merged_matches[j]` is the sentinel-filtered matches from
            the j-th merged-mode detector (one inner list per detector,
            *not* per text — each merged detector ran once against the
            sentinel-joined blob).

        Both lists are consumed by the caller for mapping construction;
        cross-source dedup happens at that layer (by value, first-seen
        wins on entity type).

        Why this lives in its own method: `anonymize` was getting long
        and doing five jobs. The dispatch concern (partition → blob →
        size fallback → unified TaskGroup → sentinel filter) is one
        cohesive concern; mapping/replacement/vault is another.
        """
        # Resolve the active set once, then partition by input_mode so
        # we can dispatch per-text and merged detectors in a single
        # TaskGroup. Doing both in one group is what gives us unified
        # cancellation: a fail-closed error in either mode tears down
        # ALL in-flight detector calls (per-text and merged), which
        # would otherwise burn LLM/PF/gliner concurrency slots on a
        # request that's about to BLOCK.
        active = self._resolve_active_detectors(overrides)
        per_text_dets, merged_dets = self._partition_by_input_mode(active)

        # Build the merged blob once (and only if any detector wants
        # merged mode). Size fallback runs against this blob: any
        # merged detector whose `max_chars` cap is exceeded is demoted
        # to per_text dispatch for THIS request only — no permanent
        # mode change on the detector instance.
        merged_blob: str | None = None
        if merged_dets:
            merged_blob = _MERGE_SENTINEL.join(texts)
            merged_dets, demoted = self._apply_merge_size_fallback(
                merged_dets, merged_blob,
            )
            if demoted:
                # Demoted detectors join the per_text fan-out for this
                # request. The original per_text_dets list comes first
                # so detector-mode order (which governs _dedup type
                # priority) is preserved — a detector configured as
                # merged but demoted today still ranks where it would
                # have ranked under DETECTOR_MODE order.
                per_text_dets = per_text_dets + demoted
            if not merged_dets:
                # Every merged detector fell back; no need for the blob.
                merged_blob = None

        # Same TaskGroup-not-gather rationale as `_detect_one` one level
        # down: when one text trips fail-closed (e.g. the LLM detector
        # times out), TaskGroup cancels the in-flight sibling text-tasks
        # AND every detector call inside them. asyncio.gather would
        # propagate the exception immediately but leak the in-flight
        # work, burning compute and pinning detector concurrency slots
        # for the duration of the doomed request. The except* unwrap
        # re-raises the first matching typed exception so main.py's
        # typed-error handler matches — it wouldn't catch the
        # BaseExceptionGroup TaskGroup raises by default.
        try:
            async with asyncio.TaskGroup() as tg:
                per_text_tasks = [
                    tg.create_task(
                        self._detect_one(
                            t,
                            api_key=api_key,
                            overrides=overrides,
                            detectors=per_text_dets,
                        )
                    )
                    for t in texts
                ]
                merged_tasks = [
                    tg.create_task(
                        self._run_detector(
                            d, merged_blob, api_key=api_key, overrides=overrides,
                        )
                    )
                    for d in merged_dets
                ] if merged_blob is not None else []
        except* TYPED_UNAVAILABLE_ERRORS as eg:
            raise eg.exceptions[0] from None

        per_text_matches = [t.result() for t in per_text_tasks]

        # Filter sentinel-spanning matches from each merged detector's
        # output. This guards against (a) the model classifying the
        # sentinel literal as an entity, and (b) returning a span that
        # crosses a segment boundary ("Alice<SEP>Smith" as PERSON) —
        # both filtered out by the contains-check. Done before the
        # caller builds the surrogate mapping so the generator never
        # sees a sentinel-bearing match.
        merged_matches: list[list[Match]] = []
        for det, task in zip(merged_dets, merged_tasks):
            results = task.result()
            kept = [m for m in results if _MERGE_SENTINEL_MARKER not in m.text]
            if log.isEnabledFor(logging.DEBUG):
                dropped = len(results) - len(kept)
                if dropped:
                    log.debug(
                        "merged dispatch: dropped %d sentinel-spanning matches "
                        "from %s output", dropped, det.name,
                    )
            merged_matches.append(kept)

        return per_text_matches, merged_matches

    async def anonymize(
        self,
        texts: list[str],
        call_id: str | None,
        *,
        api_key: str | None = None,
        overrides: Overrides = Overrides.empty(),
    ) -> tuple[list[str], dict[str, str]]:
        """Anonymize a batch of texts, persist the reverse mapping, return modified texts.

        Returns (modified_texts, mapping) so the API layer can include the
        mapping size in logs/responses without re-reading the vault.

        `api_key`, if given, overrides config.llm_api_key for the LLM detector
        on this call only — used to forward the caller's own key per-request.

        `overrides` carries the `additional_provider_specific_params`
        knobs (use_faker, faker_locale, detector_mode,
        regex_overlap_strategy, llm_model). Each is None by default;
        the detectors and the surrogate generator pick up only the
        ones relevant to them.

        High-level shape:

          1. `_run_detection` → dispatch all active detectors and
             collect their match lists (per-text + merged).
          2. Build the process-wide `original → surrogate` mapping by
             walking both match lists in DETECTOR_MODE order. Same
             entity in multiple sources collapses to one surrogate.
          3. Apply replacements, persist the reverse mapping under
             `call_id` for the post-call deanonymize lookup.
        """
        if not texts:
            return [], {}

        per_text_matches, merged_matches = await self._run_detection(
            texts, api_key=api_key, overrides=overrides,
        )

        # Build one process-wide mapping (original → surrogate) so the same
        # entity gets the same surrogate across all texts in this request.
        # Surrogate-side overrides (use_faker, faker_locale) are passed
        # to for_match — the cache key includes them so different combos
        # coexist with their own consistency.
        original_to_surrogate: dict[str, str] = {}
        # Iterate per-text first, then merged, so cross-source dedup
        # follows DETECTOR_MODE priority (per-text-first detectors win
        # type-resolution over later merged-only detectors when the same
        # value is matched by both).
        for matches in (*per_text_matches, *merged_matches):
            for m in matches:
                if m.text not in original_to_surrogate:
                    original_to_surrogate[m.text] = self._surrogates.for_match(
                        m,
                        use_faker=overrides.use_faker,
                        locale=overrides.faker_locale,
                    )

        # The vault stores the *reverse* direction (surrogate → original) since
        # that's what deanonymize needs.
        reverse_mapping = {v: k for k, v in original_to_surrogate.items()}

        replace = _build_replacer(original_to_surrogate)
        modified = [replace(t) for t in texts]

        if reverse_mapping and call_id:
            await self._vault.put(call_id, reverse_mapping)
        elif reverse_mapping and not call_id:
            log.warning(
                "Anonymized %d entities but no call_id was provided — "
                "deanonymization will not work for this request",
                len(reverse_mapping),
            )

        log.info(
            "anonymize call_id=%s entities=%d texts=%d",
            call_id, len(reverse_mapping), len(texts),
        )
        return modified, reverse_mapping

    async def deanonymize(self, texts: list[str], call_id: str | None) -> list[str]:
        """Reverse the substitution using the stored mapping for this call_id."""
        if not texts:
            return []
        mapping = await self._vault.pop(call_id) if call_id else {}
        if not mapping:
            log.debug("deanonymize call_id=%s — nothing to restore", call_id)
            return texts
        replace = _build_replacer(mapping)
        restored = [replace(t) for t in texts]
        log.info(
            "deanonymize call_id=%s entities=%d texts=%d",
            call_id, len(mapping), len(texts),
        )
        return restored


__all__ = ["Pipeline"]
