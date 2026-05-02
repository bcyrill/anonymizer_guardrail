"""
Privacy-filter detector — HTTP client + config + SPEC.

The privacy-filter detector runs as a standalone sidecar (see
`services/privacy_filter/`) and the guardrail talks to it over HTTP.
The service emits raw opf DetectedSpans (`label / start / end /
text`); this module's `_to_matches` import handles label
canonicalisation, span merge / split, and per-label gap caps to
produce `Match` objects.

Single source of truth for everything privacy-filter on the
guardrail side: `PrivacyFilterConfig`, `PrivacyFilterUnavailableError`,
`RemotePrivacyFilterDetector`, the factory, and the registered SPEC.

Failure semantics:

  * Availability errors (connect / timeout / non-200 / transport)
    raise `PrivacyFilterUnavailableError` so
    `PRIVACY_FILTER_FAIL_CLOSED` decides — block the request, or
    fall back to coverage from the other detectors.
  * Whole-response content errors on a 200 (non-JSON body, top-level
    type mismatch, `spans` field present but not a list) raise the
    same typed error: the service replied but didn't say anything
    useful, which the guardrail must not treat as "no PII found".
  * Per-entry malformed entries (entry not a dict, hallucinated text
    not in source, etc.) drop silently — local to one entity, the
    rest of the response is still usable.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Literal

import httpx
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .base import Detector, Match
from .remote_base import BaseRemoteDetector
from .spec import DetectorSpec

log = logging.getLogger("anonymizer.privacy_filter")


# ── Config ────────────────────────────────────────────────────────────────
class PrivacyFilterConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRIVACY_FILTER_",
        extra="ignore",
        frozen=True,
    )

    # HTTP URL of the privacy-filter-service container. The launcher's
    # `--privacy-filter-backend service` auto-start sets this to
    # `http://privacy-filter-service:8001`; operators running their
    # own service set it manually. Empty → the factory below errors
    # at instantiation with a remediation message.
    url: str = ""
    # Per-call timeout on the remote detector's HTTP requests.
    timeout_s: int = 30
    # Failure mode for the privacy_filter detector. true (default) →
    # block the request on PF outage; false → degrade to no PF matches
    # and proceed with the other detectors. Independent from
    # llm.CONFIG.fail_closed and gliner_pii.CONFIG.fail_closed.
    fail_closed: bool = True
    # Max concurrent PF detector calls. Independent from
    # LLM_MAX_CONCURRENCY: a saturated PF queue shouldn't reduce LLM
    # headroom or vice versa.
    max_concurrency: int = 10
    # LRU cap for the per-detector result cache. 0 disables caching
    # (default). When enabled, repeat calls with the same input text
    # skip the remote PF round-trip. The PF detector has no per-call
    # overrides today, so the cache key is just (text,). See
    # detector/cache.py for the trade-offs.
    cache_max_size: int = 0
    # How the pipeline dispatches `req.texts` to this detector:
    #   "per_text" (default) — one detect() call per text, in
    #     parallel under the PF concurrency cap. Compatible with the
    #     result cache; preserves per-text failure isolation.
    #   "merged"   — concatenate all texts with a sentinel separator
    #     and make one detect() call against the blob. Useful when
    #     PF's NER classification benefits from cross-segment context
    #     (e.g. an ambiguous span in one text reads more clearly given
    #     a related neighbouring text). Mutually exclusive with the
    #     result cache: every blob is unique by construction, so a
    #     non-zero `cache_max_size` paired with merged mode logs a
    #     warning and the cache is silently bypassed.
    # PF has no `max_chars` cap, so the merged blob is sent as-is —
    # the privacy-filter-service decides its own size policy.
    # See `pipeline.py` for the dispatch logic.
    input_mode: Literal["per_text", "merged"] = "per_text"

    @field_validator("url", mode="after")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        """Strip whitespace and require an http(s):// scheme on non-empty
        values. Empty means "detector not deployable in this process";
        the factory raises a clearer error there. Non-empty without a
        scheme would otherwise surface at first request as a confusing
        httpx error — fail loud at boot instead, matching the project's
        wider "fail-loud-on-misconfiguration" stance."""
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(
                f"PRIVACY_FILTER_URL={v!r} must start with http:// or https:// "
                f"(got no scheme). Set the full URL of the privacy-filter "
                f"service, e.g. http://privacy-filter-service:8001."
            )
        return v


CONFIG = PrivacyFilterConfig()


# ── Typed availability error ──────────────────────────────────────────────
class PrivacyFilterUnavailableError(RuntimeError):
    """Raised when the privacy-filter detector cannot complete its work
    for an availability reason — service unreachable, timeout, transport
    error, non-200 status. Caught by the pipeline; whether it propagates
    (BLOCKED) or degrades to empty matches is decided by
    `CONFIG.fail_closed`.

    Mirrors LLMUnavailableError. We deliberately keep the two distinct
    so an operator can fail closed on the LLM but open on PF (or vice
    versa); a single shared exception would force them to move in
    lockstep.
    """


# ── Post-processing: opf raw spans → canonical Match objects ──────────────
# The standalone privacy-filter-service emits raw opf DetectedSpans
# (`label / start / end / text`); label translation, span merge,
# split, and per-label gap caps run here on the guardrail side.
# Mirrors gliner-pii's split: thin model wrapper on the service,
# canonicalisation in the detector.

# Map opf's BIO-decoded labels to our canonical ENTITY_TYPES. Source:
# https://huggingface.co/openai/privacy-filter — labels described in
# the model card. Anything not in this map falls through to OTHER.
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
    "secret":          "CREDENTIAL",
}


# Per-label rules for merging consecutive same-type spans the model split.
# NER models routinely emit two PERSON spans for "Alice Smith" instead of
# one — without merging, each half gets its own surrogate and the output
# ends up as two concatenated fake names.
#
# The rule has three knobs per label: a max-gap length (how far apart can
# the spans sit), the allowed connector character set (which characters
# can appear in the gap), and an unconditional `\n\n`-block (paragraph
# breaks are always a stronger separator than a same-label call).
_MAX_GAP_BY_LABEL: dict[str, int] = {
    "PERSON": 3,
    "ADDRESS": 3,
    "DATE_OF_BIRTH": 3,
    "URL": 2,
    "PHONE": 2,
    "CREDENTIAL": 2,
    "IDENTIFIER": 1,
    "EMAIL_ADDRESS": 0,   # never merge — emails don't fragment with whitespace
}
# Conservative default for any label not in the table above (e.g. OTHER):
# allow at most one connector char so a model that emits ad-hoc labels
# doesn't get permissive merging by default.
_DEFAULT_MAX_GAP = 1

# Allowed connector characters in a merge gap. Default is whitespace
# (space, tab, single newline, carriage return) — `\n\n` itself is
# blocked unconditionally regardless of label, so listing `\n` here only
# enables intra-paragraph wrapping merges. ADDRESS adds commas because
# they're structurally part of the value ("123 Main St, Springfield" is
# one place). Slashes / semicolons / "and" stay out of every set —
# those are list separators, not intra-entity glue.
_DEFAULT_CONNECTORS: frozenset[str] = frozenset(" \t\n\r")
_CONNECTORS_BY_LABEL: dict[str, frozenset[str]] = {
    "ADDRESS": _DEFAULT_CONNECTORS | frozenset(","),
}


def _gap_is_mergeable(
    source_text: str,
    left_end: int,
    right_start: int,
    label: str,
) -> bool:
    """Decide whether two adjacent same-label spans should merge,
    given the gap of source text between them.

    Rules in priority order:
      1. Negative gap (overlap)              → never merge.
      2. Empty gap (truly adjacent)          → always merge.
      3. Gap contains `\\n\\n`               → never merge (a paragraph
         break is a stronger separator than the model's same-label call).
      4. Gap longer than the label's max     → never merge.
      5. Otherwise merge iff every gap char is in the label's allowed
         connector set.
    """
    if right_start < left_end:
        return False
    gap = source_text[left_end:right_start]
    if not gap:
        return True
    if "\n\n" in gap:
        return False
    max_gap = _MAX_GAP_BY_LABEL.get(label, _DEFAULT_MAX_GAP)
    if len(gap) > max_gap:
        return False
    connectors = _CONNECTORS_BY_LABEL.get(label, _DEFAULT_CONNECTORS)
    return all(ch in connectors for ch in gap)


# Paragraph break — used by the split pass to break apart any single
# span the decoder collapsed across a `\n\n`. Defense in depth: the
# current decoder rarely produces such spans, but production traffic
# is broader than test fixtures and the runtime cost is negligible.
_PARAGRAPH_BREAK = re.compile(r"\n{2,}")


def _to_matches(spans: Any, source_text: str) -> list[Match]:
    """Four passes — extract, merge, split, emit — converting opf
    DetectedSpan-shaped objects into canonical Match instances.

    `spans` is iterable of objects with `.label / .start / .end / .text`
    attributes (real opf DetectedSpans, the deserialised wire-format
    `_RawSpan` instances built by `_parse_matches`, or test fakes via
    duck-typing). Output is a list of `Match` objects with canonical
    `entity_type` (PERSON, EMAIL_ADDRESS, …), ready to flow through the
    guardrail's pipeline alongside matches from the other detectors.
    """
    # 1) extract: turn opf DetectedSpans into (start, end, type) tuples.
    extracted: list[tuple[int, int, str]] = []
    for span in spans:
        # opf's DetectedSpan has `.label / .start / .end / .text`.
        # Use getattr so test fakes that mimic the dataclass don't have
        # to instantiate every field they don't care about.
        label = getattr(span, "label", "") or ""
        if not label:
            continue
        entity_type = _LABEL_MAP.get(str(label), "OTHER")
        start = getattr(span, "start", None)
        end = getattr(span, "end", None)
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end > len(source_text)
            or start >= end
        ):
            # No usable offsets → fall back to the span's own .text with
            # no merging (we can't compute gaps without positions).
            fallback = str(getattr(span, "text", "") or "").strip()
            if fallback and fallback in source_text:
                pos = source_text.find(fallback)
                extracted.append((pos, pos + len(fallback), entity_type))
            continue
        extracted.append((start, end, entity_type))

    # opf normally returns spans in left-to-right order, but be
    # defensive against re-orders — the merge pass below assumes
    # increasing start.
    extracted.sort(key=lambda s: (s[0], s[1]))

    # 2) merge: same type, gap mergeable by the label's rule.
    merged: list[tuple[int, int, str]] = []
    for start, end, etype in extracted:
        if merged:
            prev_start, prev_end, prev_etype = merged[-1]
            if prev_etype == etype and _gap_is_mergeable(
                source_text, prev_end, start, etype,
            ):
                merged[-1] = (prev_start, end, etype)
                continue
        merged.append((start, end, etype))

    # 3) split — break any span that crosses a paragraph break. Mirror
    # of the merge pass's `\n\n` carve-out, applied from the opposite
    # direction (when the decoder returns one span that already crossed
    # the break, the merge regex never gets to vote).
    split: list[tuple[int, int, str]] = []
    for start, end, etype in merged:
        sub = source_text[start:end]
        if "\n\n" not in sub:
            split.append((start, end, etype))
            continue
        cursor = 0
        for m in _PARAGRAPH_BREAK.finditer(sub):
            if m.start() > cursor:
                split.append((start + cursor, start + m.start(), etype))
            cursor = m.end()
        if cursor < len(sub):
            split.append((start + cursor, end, etype))

    # 4) emit
    out: list[Match] = []
    for start, end, etype in split:
        value = source_text[start:end].strip()
        # Hallucination / drift guard: the substring must actually be
        # in the source text. Slicing can fall outside it if the
        # tokenizer's offsets ever drift.
        if not value or value not in source_text:
            continue
        out.append(Match(text=value, entity_type=etype))
    return out


# ── Detector ──────────────────────────────────────────────────────────────
class RemotePrivacyFilterDetector(BaseRemoteDetector):
    """Talks HTTP to a privacy-filter inference service."""

    name = "privacy_filter"

    def __init__(
        self,
        url: str | None = None,
        timeout_s: int | None = None,
    ) -> None:
        resolved_url = (url or CONFIG.url).rstrip("/")
        if not resolved_url:
            # The factory below only constructs this class when the URL
            # is set, so reaching here means a caller bypassed the
            # factory. Fail loud rather than send requests to "/detect".
            raise RuntimeError(
                "RemotePrivacyFilterDetector requires PRIVACY_FILTER_URL "
                "to be set."
            )
        super().__init__(
            timeout_s=timeout_s or CONFIG.timeout_s,
            cache_max_size=CONFIG.cache_max_size,
        )
        self.url = resolved_url
        log.info(
            "Privacy-filter detector wired to remote service at %s "
            "(timeout=%ds).", self.url, self.timeout_s,
        )

    async def detect(self, text: str) -> list[Match]:
        """Public entry point. The PF detector has no per-call
        overrides today, so the cache key is just `(text,)`. Single-
        element tuple keeps the shape consistent with the other
        cache-using detectors and makes future overrides easy to add."""
        cache_key = (text,)
        return await self._detect_via_cache(
            text, cache_key, lambda: self._do_detect(text),
        )

    async def _do_detect(self, text: str) -> list[Match]:
        """The actual HTTP call. Split out from `detect` so the cache
        wrapper is the only thing the public entry point does."""
        endpoint = f"{self.url}/detect"
        # Availability errors (transport-layer + non-200) raise so the
        # pipeline's PRIVACY_FILTER_FAIL_CLOSED policy decides whether
        # to BLOCK the request or fall back to coverage from the other
        # detectors. Mirrors LLMUnavailableError's contract.
        try:
            resp = await self._client.post(endpoint, json={"text": text})
        except httpx.ConnectError as exc:
            raise PrivacyFilterUnavailableError(
                f"Cannot reach privacy-filter service at {endpoint}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise PrivacyFilterUnavailableError(
                f"Privacy-filter service at {endpoint} timed out after "
                f"{self.timeout_s}s: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # Catches ReadError, WriteError, RemoteProtocolError,
            # NetworkError, ProxyError, etc. — any transport-layer
            # error. Routing through PrivacyFilterUnavailableError so
            # PRIVACY_FILTER_FAIL_CLOSED applies.
            raise PrivacyFilterUnavailableError(
                f"HTTP error talking to privacy-filter at {endpoint}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise PrivacyFilterUnavailableError(
                f"Privacy-filter at {endpoint} returned HTTP "
                f"{resp.status_code}: {resp.text[:300]}"
            )

        # Whole-response failures on a 200 still raise so
        # PRIVACY_FILTER_FAIL_CLOSED applies. The service is reachable
        # but its reply is unusable — soft-failing to [] would let
        # unredacted text through under fail_closed=True.
        try:
            body = resp.json()
        except ValueError as exc:
            raise PrivacyFilterUnavailableError(
                f"Privacy-filter at {endpoint} returned non-JSON body on "
                f"HTTP 200: {exc} | body={resp.text[:300]!r}"
            ) from exc

        return _parse_matches(body, text, endpoint)

    # cache_stats() and aclose() come from BaseRemoteDetector.


# ── Wire-format parsing ───────────────────────────────────────────────────
class _RawSpan:
    """Duck-typed stand-in for opf's DetectedSpan, built from the JSON
    wire shape so `_to_matches` can read `.label / .start / .end /
    .text` against either the privacy-filter-service (opf-only) or the
    privacy-filter-hf-service (HF forward + opf decode) — both emit
    the same wire shape."""

    __slots__ = ("label", "start", "end", "text")

    def __init__(self, *, label: str, start: int, end: int, text: str) -> None:
        self.label = label
        self.start = start
        self.end = end
        self.text = text


def _parse_matches(body: Any, source_text: str, endpoint: str) -> list[Match]:
    """Translate `{"spans": [{label, start, end, text}, ...]}` from the
    service into Match objects.

    Whole-response shape failures raise `PrivacyFilterUnavailableError`
    so `PRIVACY_FILTER_FAIL_CLOSED` decides — these mean the service
    can't be acted on for this request. Per-entry malformed entries
    (entry not a dict, hallucinated text not in source) are dropped
    inside `_to_matches` itself, by virtue of its existing offset
    + hallucination guards.
    """
    if not isinstance(body, dict):
        raise PrivacyFilterUnavailableError(
            f"Privacy-filter at {endpoint} returned a JSON value that "
            f"isn't an object (got {type(body).__name__}): {body!r}"
        )
    spans_field = body.get("spans", [])
    if not isinstance(spans_field, list):
        raise PrivacyFilterUnavailableError(
            f"Privacy-filter at {endpoint} returned `spans` of type "
            f"{type(spans_field).__name__}, expected list: "
            f"{spans_field!r}"
        )

    raw_spans: list[_RawSpan] = []
    for entry in spans_field:
        if not isinstance(entry, dict):
            continue
        # Best-effort extraction. `_to_matches` validates start/end via
        # isinstance(int) and falls back to .text-substring lookup if
        # offsets are missing, so we don't need to pre-validate here.
        raw_spans.append(_RawSpan(
            label=str(entry.get("label", "")),
            start=entry.get("start", -1) if isinstance(entry.get("start"), int) else -1,
            end=entry.get("end", -1) if isinstance(entry.get("end"), int) else -1,
            text=str(entry.get("text", "")),
        ))

    matches = _to_matches(raw_spans, source_text)
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "Remote privacy-filter parsed %d raw spans → %d matches",
            len(raw_spans), len(matches),
        )
    return matches


# ── Factory + SPEC ────────────────────────────────────────────────────────
def _privacy_filter_factory() -> Detector:
    """Construct the remote privacy-filter detector. `PRIVACY_FILTER_URL`
    must point at a running privacy-filter-service. The launcher's
    `--privacy-filter-backend service` flag auto-sets it when the
    sidecar is auto-started.

    Evaluated at instantiation (not module import) so a test that
    monkeypatches `CONFIG` (e.g. via `model_copy(update=...)`) is
    reflected here at call time.
    """
    if not CONFIG.url:
        raise RuntimeError(
            "PRIVACY_FILTER_URL must be set when DETECTOR_MODE includes "
            "'privacy_filter'. Either:\n"
            "  • Run the launcher with `--privacy-filter-backend service` "
            "to auto-start the sidecar (the launcher sets the URL for "
            "you), OR\n"
            "  • Set PRIVACY_FILTER_URL=<service-url> manually and ensure "
            "the privacy-filter-service container is reachable."
        )
    return RemotePrivacyFilterDetector()


SPEC = DetectorSpec(
    name="privacy_filter",
    factory=_privacy_filter_factory,
    module=sys.modules[__name__],
    has_semaphore=True,
    # Stats prefix shortens to "pf" because operators have Grafana
    # queries pinned to that name from before the registry existed.
    # Changing it would silently break those queries.
    stats_prefix="pf",
    unavailable_error=PrivacyFilterUnavailableError,
    blocked_reason=(
        "Anonymization privacy-filter is unreachable; request "
        "blocked to prevent unredacted data from reaching the "
        "upstream model."
    ),
    # Result caching — operator-controlled via
    # PRIVACY_FILTER_CACHE_MAX_SIZE (0 = disabled, the default).
    # Surfaces pf_cache_size/max/hits/misses on /health.
    has_cache=True,
)


__all__ = [
    "PrivacyFilterConfig",
    "PrivacyFilterUnavailableError",
    "RemotePrivacyFilterDetector",
    "CONFIG",
    "SPEC",
]
