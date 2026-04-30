"""
PrivacyFilterDetector — local NER backed by openai/privacy-filter.

A purpose-built encoder-only token classifier (1.5B params, 50M active via
MoE; Apache 2.0). Catches 8 PII categories: people, emails, phones, URLs,
addresses, dates, account numbers, and secrets. Runs entirely in-process
— no external service, no API key, no data leaves the box for detection.

Coverage is a strict subset of what the LLM detector picks up via prompt
(it doesn't see ORGANIZATION, HOSTNAME, USERNAME, IP/MAC addresses, JWTs,
etc.), so this is a complement to — not a replacement for — the existing
detectors. Combine via `DETECTOR_MODE=regex,privacy_filter,llm` (or any
ordering that fits your priority).

Optional dependency: install the `privacy-filter` extra
(`pip install anonymizer-guardrail[privacy-filter]`) to pull in torch +
transformers. Without those, this module imports fine but instantiating
the detector raises a clear ImportError pointing at the extras.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .base import Match

log = logging.getLogger("anonymizer.privacy_filter")

# Map the model's BIO-decoded labels to our ENTITY_TYPES. Source:
# https://huggingface.co/openai/privacy-filter — labels described in the
# model card. Anything not in this map falls through to OTHER via the
# Match.__post_init__ normalization.
_LABEL_MAP: dict[str, str] = {
    "private_person":  "PERSON",
    "private_email":   "EMAIL_ADDRESS",
    "private_phone":   "PHONE",
    "private_url":     "URL",
    "private_address": "ADDRESS",
    # The model treats any date as PII; we map to DATE_OF_BIRTH because
    # that's the only date-shaped entity type we have. Operators who care
    # about distinguishing arbitrary dates should post-filter.
    "private_date":    "DATE_OF_BIRTH",
    # `account_number` is a catch-all for IBAN / CC / SSN-shaped strings.
    # The model doesn't tell us which, so we use the generic IDENTIFIER
    # bucket and let the regex layer claim the more specific shapes first.
    "account_number":  "IDENTIFIER",
    "secret":           "CREDENTIAL",
}

_DEFAULT_MODEL = "openai/privacy-filter"

# Per-type rules for merging consecutive same-type spans the model split.
# NER models routinely emit two PERSON spans for "Alice Smith" instead of
# one — without this, each half gets its own surrogate and the output ends
# up as two concatenated fake names.
#
# Conservative on purpose: a span gap with ANY non-whitespace character
# (slash, pipe, semicolon, "and", etc.) is treated as a list separator
# and the spans stay distinct. We only merge across pure whitespace, so
# "Alice / Bob" keeps its two-person structure ("Fake1 / Fake2"), while
# "Alice Smith" collapses into one entity.
#
# Addresses are the one exception: the comma is part of the address
# itself ("123 Main St, Springfield" is one place), so the ADDRESS rule
# also accepts whitespace-and-comma gaps.
_DEFAULT_GAP_PATTERN = re.compile(r"\s+")
_GAP_PATTERNS: dict[str, re.Pattern[str]] = {
    "ADDRESS": re.compile(r"[\s,]+"),
}


def _gap_pattern_for(entity_type: str) -> re.Pattern[str]:
    return _GAP_PATTERNS.get(entity_type, _DEFAULT_GAP_PATTERN)


def _load_pipeline(model_name: str) -> Any:
    """Construct the HuggingFace token-classification pipeline.

    Imports happen lazily so this module is harmless to import on
    deployments without the privacy-filter extras installed. The actual
    failure (with a clear remediation message) is deferred until someone
    tries to instantiate PrivacyFilterDetector.
    """
    try:
        from transformers import pipeline  # noqa: WPS433 — intentional lazy import
    except ImportError as exc:
        raise ImportError(
            "PrivacyFilterDetector requires the privacy-filter optional "
            "dependency. Install with `pip install \"anonymizer-guardrail"
            "[privacy-filter]\"`, or build the container with "
            "`--build-arg WITH_PRIVACY_FILTER=true`."
        ) from exc
    # aggregation_strategy="first" collapses BIOES per-token output into
    # contiguous spans, with the entity_group derived from the first
    # token's label — matches what the model card example expects.
    return pipeline(
        "token-classification",
        model=model_name,
        aggregation_strategy="first",
    )


class PrivacyFilterDetector:
    """Token-classification detector backed by openai/privacy-filter."""

    name = "privacy_filter"

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        log.info("Loading privacy-filter model (%s) — first request will be slow…", model_name)
        self._pipeline = _load_pipeline(model_name)
        log.info("privacy-filter ready")

    async def detect(self, text: str) -> list[Match]:
        if not text or not text.strip():
            return []
        # Inference is CPU/GPU-bound and synchronous. asyncio.to_thread
        # offloads it so the event loop stays responsive — unlike regex
        # (microseconds, fine on the loop), this is hundreds of ms.
        spans: list[dict[str, Any]] = await asyncio.to_thread(self._pipeline, text)
        return self._to_matches(spans, text)

    @staticmethod
    def _to_matches(spans: list[dict[str, Any]], source_text: str) -> list[Match]:
        # Three passes:
        #   1. extract  — turn raw pipeline output into (start, end, type)
        #                 tuples, dropping anything malformed
        #   2. merge    — collapse same-type adjacent spans the model split
        #                 across pure whitespace (or comma for ADDRESS)
        #   3. emit     — slice the source text and build Match objects
        # The extract/merge separation is the bit that distinguishes
        # "Alice / Bob" (two people, slash gap → keep separate) from
        # "Alice Smith" (one person, space gap → merge).

        # 1) extract
        extracted: list[tuple[int, int, str]] = []
        for span in spans:
            label = span.get("entity_group") or span.get("entity")
            if not label:
                continue
            entity_type = _LABEL_MAP.get(str(label), "OTHER")
            start, end = span.get("start"), span.get("end")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end > len(source_text)
                or start >= end
            ):
                # No usable offsets → fall back to the `word` field with
                # no merging (we can't compute gaps without positions).
                fallback = str(span.get("word", "")).strip()
                if fallback and fallback in source_text:
                    pos = source_text.find(fallback)
                    extracted.append((pos, pos + len(fallback), entity_type))
                continue
            extracted.append((start, end, entity_type))

        # Order matters for the merge pass; the pipeline normally returns
        # spans in left-to-right order, but be defensive against re-orders
        # introduced by aggregation strategies in future transformers.
        extracted.sort(key=lambda s: (s[0], s[1]))

        # 2) merge — same type, non-overlapping, gap matches the type's
        # rule. Anything else stays as a separate entity.
        merged: list[tuple[int, int, str]] = []
        for start, end, etype in extracted:
            if merged:
                prev_start, prev_end, prev_etype = merged[-1]
                if prev_etype == etype and prev_end <= start:
                    gap = source_text[prev_end:start]
                    if not gap or _gap_pattern_for(etype).fullmatch(gap):
                        merged[-1] = (prev_start, end, etype)
                        continue
            merged.append((start, end, etype))

        # 3) emit
        out: list[Match] = []
        for start, end, etype in merged:
            value = source_text[start:end].strip()
            # Hallucination / drift guard: the substring must actually
            # be in the source text. Slicing can fall outside it if the
            # tokenizer's offsets ever drift.
            if not value or value not in source_text:
                continue
            out.append(Match(text=value, entity_type=etype))
        return out


__all__ = ["PrivacyFilterDetector"]