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
from typing import Iterable

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


def _replace_all(text: str, mapping: dict[str, str]) -> str:
    """Replace every key in `mapping` with its value in a single pass.

    Uses a regular expression to find all keys simultaneously, avoiding
    transitive replacements (where a surrogate generated for Entity A
    matches the original text of Entity B and gets replaced again).
    """
    if not mapping:
        return text

    # Sort keys by length descending to ensure that if one key is a prefix
    # of another, the longer one matches first in the regex.
    pattern = re.compile(
        "|".join(re.escape(k) for k in sorted(mapping.keys(), key=len, reverse=True))
    )
    return pattern.sub(lambda m: mapping[m.group(0)], text)


class Pipeline:
    def __init__(self) -> None:
        self._detectors: list[Detector] = _build_detectors()
        self._surrogates = SurrogateGenerator()
        self._vault = Vault()
        self._llm_semaphore = asyncio.Semaphore(config.llm_max_concurrency)
        log.info(
            "Pipeline ready — detectors=[%s], vault_ttl=%ds, fail_closed=%s, max_concurrency=%d",
            ", ".join(d.name for d in self._detectors),
            config.vault_ttl_s,
            config.fail_closed,
            config.llm_max_concurrency,
        )

    @property
    def vault(self) -> Vault:
        return self._vault

    async def _detect_one(
        self, text: str, *, api_key: str | None = None
    ) -> list[Match]:
        """Run all configured detectors against one text and dedup.

        The LLM concurrency semaphore is acquired ONLY around LLM detector
        calls — regex is CPU-only and instant, so throttling it would just
        serialize work that wasn't a problem. Process-wide cap protects the
        upstream LLM service from thundering-herd.
        """
        results: list[list[Match]] = []
        for det in self._detectors:
            try:
                if isinstance(det, LLMDetector):
                    async with self._llm_semaphore:
                        matches = await det.detect(text, api_key=api_key)
                else:
                    matches = await det.detect(text, api_key=api_key)
                results.append(matches)
            except LLMUnavailableError as exc:
                if config.fail_closed:
                    raise
                log.warning("LLM detector unavailable, proceeding fail-open: %s", exc)
                results.append([])
            except Exception as exc:  # defensive: a buggy detector shouldn't kill us
                log.exception("Detector %s crashed: %s", det.name, exc)
                results.append([])
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

        modified = [_replace_all(t, original_to_surrogate) for t in texts]

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
        restored = [_replace_all(t, mapping) for t in texts]
        log.info(
            "deanonymize call_id=%s entities=%d texts=%d",
            call_id, len(mapping), len(texts),
        )
        return restored


__all__ = ["Pipeline"]
