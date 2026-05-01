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
    SPECS_WITH_SEMAPHORE,
    TYPED_UNAVAILABLE_ERRORS,
)
from .detector.base import Detector, Match
from .surrogate import SurrogateGenerator
from .vault import Vault

log = logging.getLogger("anonymizer.pipeline")


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
    """Keep one Match per unique text, preferring the first-seen entity_type."""
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
        self._vault = Vault()

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

    def stats(self) -> dict[str, int]:
        """Snapshot of pipeline-internal counters for the /health probe.
        All reads are cheap and lock-free."""
        cache_size, cache_max = self._surrogates.cache_stats()
        out: dict[str, int] = {
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
        return out

    @property
    def vault(self) -> Vault:
        return self._vault

    def _resolve_active_detectors(
        self, overrides: Overrides
    ) -> list[Detector]:
        """Apply the per-request `detector_mode` filter as a SUBSET over
        the detectors configured at startup.

        Operators can narrow the active set per call (e.g. "skip the LLM
        for this request") but cannot expand it: privacy_filter requires
        torch + the model to be loaded, and the LLM detector requires the
        configured api_base / model — neither are constructable at
        request time. Names that aren't currently configured are dropped
        with a warning.
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

    async def _detect_one(
        self,
        text: str,
        *,
        api_key: str | None = None,
        overrides: Overrides = Overrides.empty(),
    ) -> list[Match]:
        """Run all configured detectors against one text in parallel and dedup.

        Regex is CPU-bound and fast; the LLM call dominates total latency.
        Running them concurrently means the per-text floor is `max(regex,
        llm)` instead of `regex + llm`. Cheap regardless — and if a second
        slow detector ever gets added, the win compounds.

        Concurrency caps (LLM, privacy_filter, gliner_pii) are independent
        per-detector semaphores keyed by spec name; throttling one doesn't
        starve the others. CPU-cheap detectors (regex, denylist) skip
        gating entirely.

        Per-detector exceptions are handled inside the inner runner so one
        detector crashing doesn't poison the gather. Typed unavailable
        errors still propagate under fail-closed — the TaskGroup then
        cancels the in-flight siblings (saving wasted work since the
        request is about to BLOCK) and re-raises the typed error so
        main.py's BLOCKED handler matches.
        """
        active = self._resolve_active_detectors(overrides)

        async def _run(det: Detector) -> list[Match]:
            spec = SPECS_BY_NAME[det.name]
            kwargs = spec.prepare_call_kwargs(overrides, api_key)
            try:
                if spec.has_semaphore:
                    # Gate the call behind the per-spec semaphore. The
                    # in-flight counter increment is the first statement
                    # after acquisition — no statement between acquire
                    # and `try` can raise, so the finally always fires.
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
                tasks = [tg.create_task(_run(d)) for d in active]
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

    async def aclose(self) -> None:
        """Release per-detector resources (httpx connection pools, etc).
        Wired to FastAPI's lifespan in main.py so shutdown is clean."""
        for det in self._detectors:
            close = getattr(det, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 — never fail shutdown
                    log.warning("Detector %s aclose failed: %s", det.name, exc)

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
        """
        if not texts:
            return [], {}

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
                tasks = [
                    tg.create_task(
                        self._detect_one(t, api_key=api_key, overrides=overrides)
                    )
                    for t in texts
                ]
        except* TYPED_UNAVAILABLE_ERRORS as eg:
            raise eg.exceptions[0] from None

        per_text_matches = [t.result() for t in tasks]

        # Build one process-wide mapping (original → surrogate) so the same
        # entity gets the same surrogate across all texts in this request.
        # Surrogate-side overrides (use_faker, faker_locale) are passed
        # to for_match — the cache key includes them so different combos
        # coexist with their own consistency.
        original_to_surrogate: dict[str, str] = {}
        for matches in per_text_matches:
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
            self._vault.put(call_id, reverse_mapping)
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
        mapping = self._vault.pop(call_id) if call_id else {}
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
