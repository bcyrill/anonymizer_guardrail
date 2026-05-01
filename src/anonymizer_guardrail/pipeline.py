"""
Anonymization pipeline.

Wires the detectors, surrogate generator, and vault together. Two entry points:

  * anonymize(texts, call_id) → modified texts. Detects entities, generates
    surrogates, replaces in-place, persists the reverse mapping under call_id.

  * deanonymize(texts, call_id) → restored texts. Looks up the mapping and
    substitutes surrogates back to originals.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Iterable

from .api import Overrides
from .config import config
from .detector.base import Detector, Match
from .detector.denylist import DenylistDetector
from .detector.llm import LLMDetector, LLMUnavailableError
from .detector.privacy_filter import PrivacyFilterDetector
from .detector.regex import RegexDetector
from .detector.remote_privacy_filter import (
    PrivacyFilterUnavailableError,
    RemotePrivacyFilterDetector,
)
from .surrogate import SurrogateGenerator
from .vault import Vault

log = logging.getLogger("anonymizer.pipeline")


def _privacy_filter_factory() -> Detector:
    """Pick in-process or remote based on PRIVACY_FILTER_URL.

    Empty (default) → in-process PrivacyFilterDetector (torch +
    transformers loaded in this container; image must be `pf` /
    `pf-baked`). URL set → RemotePrivacyFilterDetector talks to a
    standalone privacy-filter-service over HTTP and the slim image
    is sufficient.

    The branch is evaluated at instantiation, not at module import,
    so a test that monkeypatches `config.privacy_filter_url` then
    rebuilds the pipeline picks up the new value.
    """
    if config.privacy_filter_url:
        return RemotePrivacyFilterDetector()
    return PrivacyFilterDetector()


_DETECTOR_FACTORIES: dict[str, Callable[[], Detector]] = {
    "regex":          RegexDetector,
    "llm":            LLMDetector,
    # Literal-string match against an operator-supplied YAML denylist.
    # Loads with no entries when DENYLIST_PATH is unset, so registering
    # it here unconditionally is safe.
    "denylist":       DenylistDetector,
    # Dispatches between in-process (torch in this container) and
    # remote (HTTP to a privacy-filter-service) at instantiation time.
    "privacy_filter": _privacy_filter_factory,
}


def _build_detectors() -> list[Detector]:
    """Parse DETECTOR_MODE as a comma-separated list of detector names.

    Order is preserved — it determines the order detectors run in (parallel
    via asyncio.gather, but `_dedup` keeps first-seen entity_type, so a
    duplicate match will adopt the type from the detector listed earlier).
    Whitespace around names is tolerated; duplicates are collapsed with
    a warning.

    The historical `both` value is no longer accepted — use `regex,llm`.
    Unknown names log a warning and are skipped; if nothing valid remains,
    we fall back to regex-only so the service still boots.
    """
    raw = config.detector_mode or ""
    names = [n.strip() for n in raw.split(",") if n.strip()]

    detectors: list[Detector] = []
    seen: set[str] = set()
    for name in names:
        if name == "both":
            log.error(
                "DETECTOR_MODE=both has been removed. Use 'regex,llm' for the "
                "same behaviour, or pick a single detector ('regex' or 'llm')."
            )
            continue
        if name in seen:
            log.warning("DETECTOR_MODE: duplicate detector %r — collapsed", name)
            continue
        factory = _DETECTOR_FACTORIES.get(name)
        if factory is None:
            log.warning(
                "DETECTOR_MODE: unknown detector %r (valid names: %s)",
                name, ", ".join(sorted(_DETECTOR_FACTORIES)),
            )
            continue
        seen.add(name)
        detectors.append(factory())

    if not detectors:
        log.warning(
            "DETECTOR_MODE=%r produced no valid detectors — falling back to regex.",
            raw,
        )
        detectors = [RegexDetector()]
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
        self._llm_semaphore = asyncio.Semaphore(config.llm_max_concurrency)
        # Privacy-filter shares the same throttling rationale as LLM:
        # in-process inference is CPU/GPU-bound, remote inference is one
        # HTTP call per text and a chatty request can fan out far past
        # the service's safe concurrency. Independent semaphore so a
        # saturated PF queue doesn't reduce LLM headroom (or vice versa).
        self._pf_semaphore = asyncio.Semaphore(config.privacy_filter_max_concurrency)
        # In-flight call counters. We increment/decrement around the
        # detect() call rather than reading Semaphore._value (private)
        # because the asyncio.Semaphore API doesn't expose a stable
        # "current waiters" attribute. Single-threaded asyncio means
        # plain int mutation is safe between await points.
        self._llm_in_flight = 0
        self._pf_in_flight = 0
        log.info(
            "Pipeline ready — detectors=[%s], vault_ttl=%ds, "
            "llm_fail_closed=%s, pf_fail_closed=%s, "
            "llm_max_concurrency=%d, pf_max_concurrency=%d",
            ", ".join(d.name for d in self._detectors),
            config.vault_ttl_s,
            config.llm_fail_closed,
            config.privacy_filter_fail_closed,
            config.llm_max_concurrency,
            config.privacy_filter_max_concurrency,
        )

    def stats(self) -> dict[str, int]:
        """Snapshot of pipeline-internal counters for the /health probe.
        All reads are cheap and lock-free."""
        cache_size, cache_max = self._surrogates.cache_stats()
        return {
            "vault_size": self._vault.size(),
            "surrogate_cache_size": cache_size,
            "surrogate_cache_max": cache_max,
            "llm_in_flight": self._llm_in_flight,
            "llm_max_concurrency": config.llm_max_concurrency,
            "pf_in_flight": self._pf_in_flight,
            "pf_max_concurrency": config.privacy_filter_max_concurrency,
        }

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

        The LLM concurrency semaphore is acquired ONLY around LLM detector
        calls — throttling regex would just serialize work that wasn't a
        problem. Process-wide cap protects the upstream LLM service from
        thundering-herd.

        Per-detector exceptions are handled inside the inner runner so one
        detector crashing doesn't poison the gather. LLMUnavailableError
        still propagates under LLM_FAIL_CLOSED, in which case asyncio
        cancels the other in-flight detectors — the right behaviour
        because we're about to BLOCK the request anyway.

        `overrides` is plumbed through to the detectors that care:
        regex picks up the overlap-strategy override, LLM picks up the
        model override.
        """
        active = self._resolve_active_detectors(overrides)

        async def _run(det: Detector) -> list[Match]:
            try:
                if isinstance(det, LLMDetector):
                    # Only the LLM layer takes an api_key (for forwarded
                    # virtual-key auth). Regex doesn't talk to any backend.
                    async with self._llm_semaphore:
                        self._llm_in_flight += 1
                        try:
                            return await det.detect(
                                text,
                                api_key=api_key,
                                model=overrides.llm_model,
                                prompt_name=overrides.llm_prompt,
                            )
                        finally:
                            self._llm_in_flight -= 1
                if isinstance(det, RegexDetector):
                    return await det.detect(
                        text,
                        overlap_strategy=overrides.regex_overlap_strategy,
                        patterns_name=overrides.regex_patterns,
                    )
                if isinstance(det, DenylistDetector):
                    return await det.detect(
                        text, denylist_name=overrides.denylist,
                    )
                # Privacy-filter (in-process or remote) goes through its
                # own concurrency cap. In-process inference is CPU/GPU-
                # bound and benefits from serialization the same way LLM
                # does; remote PF is one HTTP call per text and the
                # service has finite worker capacity, so unbounded fan-
                # out from a chatty request is the same thundering-herd
                # the LLM cap was added to prevent.
                if isinstance(det, (PrivacyFilterDetector, RemotePrivacyFilterDetector)):
                    async with self._pf_semaphore:
                        self._pf_in_flight += 1
                        try:
                            return await det.detect(text)
                        finally:
                            self._pf_in_flight -= 1
                return await det.detect(text)
            except LLMUnavailableError as exc:
                if config.llm_fail_closed:
                    raise
                log.warning("LLM detector unavailable, proceeding fail-open: %s", exc)
                return []
            except PrivacyFilterUnavailableError as exc:
                if config.privacy_filter_fail_closed:
                    raise
                log.warning(
                    "Privacy-filter detector unavailable, proceeding fail-open: %s", exc,
                )
                return []
            except Exception as exc:  # defensive: a buggy detector shouldn't kill us
                log.exception("Detector %s crashed: %s", det.name, exc)
                # Asymmetry on purpose: a buggy regex pattern can degrade to
                # empty matches without taking the request down — the LLM
                # layer still runs. But an unexpected LLM failure under
                # LLM_FAIL_CLOSED is not safely degradable; if we silently
                # returned [] here, an operator who set LLM_FAIL_CLOSED=true
                # specifically to BLOCK on any LLM problem would instead
                # see the request quietly proceed with regex-only redaction.
                # Re-raise as LLMUnavailableError so the BLOCKED path fires.
                if isinstance(det, LLMDetector) and config.llm_fail_closed:
                    raise LLMUnavailableError(
                        f"Unexpected failure in LLM detector "
                        f"({type(exc).__name__}): {exc}"
                    ) from exc
                # Same asymmetry for the privacy-filter detector — both
                # in-process (PrivacyFilterDetector) and remote
                # (RemotePrivacyFilterDetector). An operator who set
                # PRIVACY_FILTER_FAIL_CLOSED=true wants to BLOCK on any
                # PF problem, including unexpected crashes from the
                # in-process pipeline (torch OOM, tokenizer drift, etc.)
                # that wouldn't otherwise raise our typed error.
                if (
                    isinstance(det, (PrivacyFilterDetector, RemotePrivacyFilterDetector))
                    and config.privacy_filter_fail_closed
                ):
                    raise PrivacyFilterUnavailableError(
                        f"Unexpected failure in privacy-filter detector "
                        f"({type(exc).__name__}): {exc}"
                    ) from exc
                return []

        # TaskGroup (vs asyncio.gather) cancels in-flight sibling tasks
        # the moment one detector raises. Matters under fail-closed: when
        # the LLM detector raises LLMUnavailableError we're going to BLOCK
        # the whole request, and a still-running privacy_filter call (which
        # can take hundreds of ms locally or seconds remotely) is wasted
        # work. gather() left the siblings running until completion and
        # discarded their results; TaskGroup short-circuits.
        #
        # The except* unwrap re-raises the first matching typed exception
        # so main.py's `except LLMUnavailableError / PrivacyFilterUnavailableError`
        # clauses still match — they wouldn't catch the BaseExceptionGroup
        # TaskGroup raises by default. `_run` is the only place in this
        # function that lets typed exceptions escape, so the group will
        # contain at most these two types.
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(_run(d)) for d in active]
        except* LLMUnavailableError as eg:
            raise eg.exceptions[0] from None
        except* PrivacyFilterUnavailableError as eg:
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

        per_text_matches = await asyncio.gather(
            *[
                self._detect_one(t, api_key=api_key, overrides=overrides)
                for t in texts
            ]
        )

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
