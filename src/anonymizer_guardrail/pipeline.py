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

from .config import config
from .detector.base import Detector, Match
from .detector.llm import LLMDetector, LLMUnavailableError
from .detector.regex import RegexDetector
from .surrogate import SurrogateGenerator
from .vault import Vault

log = logging.getLogger("anonymizer.pipeline")


def _build_detectors() -> list[Detector]:
    mode = config.detector_mode
    if mode == "regex":
        return [RegexDetector()]
    if mode == "llm":
        return [LLMDetector()]
    if mode == "both":
        return [RegexDetector(), LLMDetector()]
    log.warning("Unknown DETECTOR_MODE=%r, defaulting to 'regex'", mode)
    return [RegexDetector()]


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
        # In-flight LLM call counter. We increment/decrement around the
        # detect() call rather than reading Semaphore._value (private)
        # because the asyncio.Semaphore API doesn't expose a stable
        # "current waiters" attribute. Single-threaded asyncio means
        # plain int mutation is safe between await points.
        self._llm_in_flight = 0
        log.info(
            "Pipeline ready — detectors=[%s], vault_ttl=%ds, fail_closed=%s, max_concurrency=%d",
            ", ".join(d.name for d in self._detectors),
            config.vault_ttl_s,
            config.fail_closed,
            config.llm_max_concurrency,
        )

    def stats(self) -> dict[str, int | str]:
        """Snapshot of pipeline-internal counters for the /health probe.
        All reads are cheap and lock-free."""
        cache_size, cache_max = self._surrogates.cache_stats()
        return {
            "vault_size": self._vault.size(),
            "surrogate_cache_size": cache_size,
            "surrogate_cache_max": cache_max,
            "llm_in_flight": self._llm_in_flight,
            "llm_max_concurrency": config.llm_max_concurrency,
        }

    @property
    def vault(self) -> Vault:
        return self._vault

    async def _detect_one(
        self, text: str, *, api_key: str | None = None
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
        still propagates under FAIL_CLOSED, in which case asyncio cancels
        the other in-flight detectors — the right behaviour because we're
        about to BLOCK the request anyway.
        """

        async def _run(det: Detector) -> list[Match]:
            try:
                if isinstance(det, LLMDetector):
                    # Only the LLM layer takes an api_key (for forwarded
                    # virtual-key auth). Regex doesn't talk to any backend.
                    async with self._llm_semaphore:
                        self._llm_in_flight += 1
                        try:
                            return await det.detect(text, api_key=api_key)
                        finally:
                            self._llm_in_flight -= 1
                return await det.detect(text)
            except LLMUnavailableError as exc:
                if config.fail_closed:
                    raise
                log.warning("LLM detector unavailable, proceeding fail-open: %s", exc)
                return []
            except Exception as exc:  # defensive: a buggy detector shouldn't kill us
                log.exception("Detector %s crashed: %s", det.name, exc)
                # Asymmetry on purpose: a buggy regex pattern can degrade to
                # empty matches without taking the request down — the LLM
                # layer still runs. But an unexpected LLM failure under
                # FAIL_CLOSED is not safely degradable; if we silently
                # returned [] here, an operator who set FAIL_CLOSED=true
                # specifically to BLOCK on any LLM problem would instead
                # see the request quietly proceed with regex-only redaction.
                # Re-raise as LLMUnavailableError so the BLOCKED path fires.
                if isinstance(det, LLMDetector) and config.fail_closed:
                    raise LLMUnavailableError(
                        f"Unexpected failure in LLM detector "
                        f"({type(exc).__name__}): {exc}"
                    ) from exc
                return []

        results = await asyncio.gather(*[_run(d) for d in self._detectors])
        flat = [m for sub in results for m in sub]
        return _dedup(flat)

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
    ) -> tuple[list[str], dict[str, str]]:
        """Anonymize a batch of texts, persist the reverse mapping, return modified texts.

        Returns (modified_texts, mapping) so the API layer can include the
        mapping size in logs/responses without re-reading the vault.

        `api_key`, if given, overrides config.llm_api_key for the LLM detector
        on this call only — used to forward the caller's own key per-request.
        """
        if not texts:
            return [], {}

        per_text_matches = await asyncio.gather(
            *[self._detect_one(t, api_key=api_key) for t in texts]
        )

        # Build one process-wide mapping (original → surrogate) so the same
        # entity gets the same surrogate across all texts in this request.
        original_to_surrogate: dict[str, str] = {}
        for matches in per_text_matches:
            for m in matches:
                if m.text not in original_to_surrogate:
                    original_to_surrogate[m.text] = self._surrogates.for_match(m)

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
