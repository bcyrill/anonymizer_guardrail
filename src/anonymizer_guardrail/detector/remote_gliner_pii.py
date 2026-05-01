"""
Remote GLiNER-PII detector.

HTTP client for the standalone gliner-pii-service (see
services/gliner_pii/). NVIDIA's nvidia/gliner-pii is a fine-tune of
GLiNER large-v2.1 whose differentiator over a fixed token-classification
model is *zero-shot labels* — the entity-type list is an input to the
model rather than baked into the architecture. Operators can therefore
tune the vocabulary per deployment (or eventually per request) without
retraining.

There is intentionally no in-process variant today. Pulling the gliner
library + ~570M weights into the guardrail image would defeat the
point of having a slim guardrail; deploy the inference container
separately and point this detector at it via GLINER_PII_URL.

Behaviour mirrors RemotePrivacyFilterDetector:

  * Empty / whitespace-only input short-circuits to no matches.
  * Availability errors (connect / timeout / non-200 / transport HTTP
    errors) raise GlinerPIIUnavailableError so the pipeline's
    GLINER_PII_FAIL_CLOSED policy decides — block the request, or
    fall back to coverage from the other detectors.
  * Content errors (non-JSON body, malformed `matches` field,
    hallucinated text) are non-fatal and log+return-[] without raising.

The label-to-ENTITY_TYPES mapping below normalizes gliner's lowercase
snake_case labels (e.g. "ssn", "email") to the guardrail's canonical
UPPER_SNAKE entity types (e.g. "NATIONAL_ID", "EMAIL_ADDRESS"). Anything
unmapped falls through to OTHER via Match.__post_init__ — same
default-fallback the LLM and remote PF detectors use.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import _env_bool, _env_int
from .base import Detector, Match
from .spec import DetectorSpec

log = logging.getLogger("anonymizer.gliner_pii.remote")


# ── Per-detector config ───────────────────────────────────────────────────
@dataclass(frozen=True)
class GlinerPIIConfig:
    # Empty (default) → DETECTOR_MODE=gliner_pii is rejected at boot
    # with a clear error: there's no in-process fallback, so an empty
    # URL means "this detector isn't deployable in this process."
    # Set to an HTTP URL → the detector posts to {URL}/detect on every
    # request. The standalone gliner-pii-service container (see
    # services/gliner_pii/) is the canonical other end.
    url: str = os.getenv("GLINER_PII_URL", "").strip()
    # Per-call timeout on the remote detector's HTTP requests.
    timeout_s: int = _env_int("GLINER_PII_TIMEOUT_S", 30)
    # Failure mode. Independent of llm.CONFIG.fail_closed and
    # privacy_filter.CONFIG.fail_closed — operators can fail closed on
    # one detector and open on another without coupling.
    fail_closed: bool = _env_bool("GLINER_PII_FAIL_CLOSED", True)
    # Default zero-shot label list to send with every request. Empty
    # falls back to whatever the gliner-pii-service has configured as
    # *its* DEFAULT_LABELS.
    labels: str = os.getenv("GLINER_PII_LABELS", "")
    # Confidence cutoff sent with every request. Empty → use whatever
    # the gliner-pii-service has configured as its DEFAULT_THRESHOLD.
    threshold: str = os.getenv("GLINER_PII_THRESHOLD", "")
    # Max number of concurrent gliner-pii calls. Same rationale as
    # privacy_filter.CONFIG.max_concurrency.
    max_concurrency: int = _env_int("GLINER_PII_MAX_CONCURRENCY", 10)


CONFIG = GlinerPIIConfig()


class GlinerPIIUnavailableError(RuntimeError):
    """Raised when the gliner-pii inference service cannot complete its
    work for an availability reason — service unreachable, timeout,
    transport error, non-200 status. Caught by the pipeline; whether
    it propagates (BLOCKED) or degrades to empty matches is decided by
    CONFIG.fail_closed.

    Mirrors LLMUnavailableError / PrivacyFilterUnavailableError. We
    deliberately keep them distinct so an operator can fail closed on
    one detector but open on another; a single shared exception would
    force them to move in lockstep.
    """


# Lowercase snake_case → canonical guardrail ENTITY_TYPES. Built from
# the gliner-pii-service's DEFAULT_LABELS list plus the most common
# additional labels operators might configure. Anything not in this
# map keeps its raw label and falls through to OTHER via
# Match.__post_init__ — the surrogate generator then emits an opaque
# token, which is the right default for an entity-type the rest of
# the system doesn't recognize yet.
#
# Keep this map narrow and obvious. Adding speculative entries (e.g.
# "vehicle_plate" → some guardrail type) ahead of an actual deployment
# need just creates surface area to maintain.
_LABEL_TO_ENTITY_TYPE: dict[str, str] = {
    "person":                 "PERSON",
    "organization":           "ORGANIZATION",
    "email":                  "EMAIL_ADDRESS",
    "email_address":          "EMAIL_ADDRESS",
    "phone":                  "PHONE",
    "phone_number":           "PHONE",
    "address":                "ADDRESS",
    "date_of_birth":          "DATE_OF_BIRTH",
    "credit_card":            "CREDIT_CARD",
    "credit_card_number":     "CREDIT_CARD",
    "iban":                   "IBAN",
    "ssn":                    "NATIONAL_ID",
    "national_id":            "NATIONAL_ID",
    # Catch-all numeric identifiers — gliner's `account_number` is
    # broad enough (IBAN-shaped, CC-shaped, internal customer IDs)
    # that the safest mapping is the generic IDENTIFIER bucket. The
    # regex layer claims more specific shapes first when run in
    # combination, so the generic bucket only catches what regex
    # didn't recognize.
    "account_number":         "IDENTIFIER",
    "passport_number":        "IDENTIFIER",
    "medical_record_number":  "IDENTIFIER",
    # gliner doesn't distinguish v4 vs v6 in its `ip_address` label;
    # default to v4 because it's overwhelmingly more common in prose.
    # When v6 matters specifically, use the regex detector — it has
    # both IPV4_ADDRESS and IPV6_ADDRESS shapes.
    "ip_address":             "IPV4_ADDRESS",
    "url":                    "URL",
    "username":               "USERNAME",
}


def _parse_labels(raw: str) -> list[str] | None:
    """Comma-separated → list, or None when unset (server-side default applies)."""
    parts = [s.strip() for s in raw.split(",") if s.strip()]
    return parts or None


def _parse_threshold(raw: str) -> float | None:
    """Empty string → None (server-side default applies). Bad values warn-and-skip."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "GLINER_PII_THRESHOLD=%r is not a float; falling back to the "
            "server-side default.",
            raw,
        )
        return None


class RemoteGlinerPIIDetector:
    """Talks HTTP to a gliner-pii inference service."""

    name = "gliner_pii"

    def __init__(
        self,
        url: str | None = None,
        timeout_s: int | None = None,
        labels: list[str] | None = None,
        threshold: float | None = None,
    ) -> None:
        self.url = (url or CONFIG.url).rstrip("/")
        if not self.url:
            # The factory in pipeline.py only constructs this class when
            # the URL is set, so reaching here means a caller bypassed
            # the factory. Fail loud rather than send requests to "/detect".
            raise RuntimeError(
                "RemoteGlinerPIIDetector requires GLINER_PII_URL to be set. "
                "Unlike privacy_filter, there is no in-process fallback — "
                "deploy the gliner-pii-service container and set "
                "GLINER_PII_URL=http://<host>:<port>."
            )
        self.timeout_s = timeout_s or CONFIG.timeout_s
        self.labels = labels if labels is not None else _parse_labels(CONFIG.labels)
        self.threshold = (
            threshold if threshold is not None else _parse_threshold(CONFIG.threshold)
        )
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        log.info(
            "GLiNER-PII detector wired to remote service at %s "
            "(timeout=%ds, labels=%s, threshold=%s).",
            self.url, self.timeout_s,
            self.labels if self.labels is not None else "<server default>",
            self.threshold if self.threshold is not None else "<server default>",
        )

    async def detect(
        self,
        text: str,
        *,
        labels: list[str] | None = None,
        threshold: float | None = None,
    ) -> list[Match]:
        """Per-call `labels` / `threshold` override the detector's
        configured defaults for this request only — same shape as
        `LLMDetector.detect(model=…, prompt_name=…)`. Lets the
        per-request override path (`additional_provider_specific_params`)
        pin a different label vocabulary per route without redeploying.
        """
        if not text or not text.strip():
            return []

        # Per-call overrides win; otherwise use what was configured at
        # construction time (which itself fell back to the env defaults).
        # Local variables — never mutate self.labels / self.threshold,
        # the detector instance is shared across requests.
        effective_labels = labels if labels is not None else self.labels
        effective_threshold = (
            threshold if threshold is not None else self.threshold
        )

        endpoint = f"{self.url}/detect"
        # Build the request body with only the fields the operator
        # actually configured. Omitting `labels` / `threshold` lets the
        # server's DEFAULT_LABELS / DEFAULT_THRESHOLD apply, which
        # keeps both ends decoupled — operators can tune the vocabulary
        # on either side without coordinating.
        body: dict[str, Any] = {"text": text}
        if effective_labels is not None:
            body["labels"] = effective_labels
        if effective_threshold is not None:
            body["threshold"] = effective_threshold

        # Availability errors (transport-layer + non-200) raise so the
        # pipeline's GLINER_PII_FAIL_CLOSED policy decides whether
        # to BLOCK the request or fall back to coverage from the other
        # detectors. Mirrors LLMUnavailableError / PrivacyFilterUnavailableError.
        try:
            resp = await self._client.post(endpoint, json=body)
        except httpx.ConnectError as exc:
            raise GlinerPIIUnavailableError(
                f"Cannot reach gliner-pii service at {endpoint}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise GlinerPIIUnavailableError(
                f"GLiNER-PII service at {endpoint} timed out after "
                f"{self.timeout_s}s: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # Catches ReadError, WriteError, RemoteProtocolError,
            # NetworkError, ProxyError, etc. — any transport-layer
            # error. Routing through GlinerPIIUnavailableError so
            # GLINER_PII_FAIL_CLOSED applies.
            raise GlinerPIIUnavailableError(
                f"HTTP error talking to gliner-pii at {endpoint}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise GlinerPIIUnavailableError(
                f"GLiNER-PII at {endpoint} returned HTTP "
                f"{resp.status_code}: {resp.text[:300]}"
            )

        # Content errors below are non-fatal: log and return []. The
        # service is reachable and replied 200, so this is a malformed
        # response, not an outage. Same split LLMDetector / remote PF
        # use for unexpected JSON shapes inside a 200 response.
        try:
            payload = resp.json()
        except ValueError as exc:
            log.warning(
                "GLiNER-PII at %s returned non-JSON: %s | body=%r",
                endpoint, exc, resp.text[:300],
            )
            return []

        return _parse_matches(payload, text)

    async def aclose(self) -> None:
        """Drain the httpx connection pool. Wired to Pipeline.aclose
        which fires on FastAPI shutdown."""
        await self._client.aclose()


def _parse_matches(body: Any, source_text: str) -> list[Match]:
    """Translate `{"matches": [{...}, ...]}` from the service into Match
    objects. Drops malformed entries and any whose `text` field doesn't
    actually appear in the source — defensive against a future service
    version returning bad offsets, mirroring the LLM / remote PF
    detectors' hallucination guard."""
    if not isinstance(body, dict):
        log.warning(
            "GLiNER-PII response wasn't a JSON object: %r", body,
        )
        return []
    matches_field = body.get("matches", [])
    if not isinstance(matches_field, list):
        log.warning(
            "GLiNER-PII response `matches` wasn't a list: %r",
            matches_field,
        )
        return []

    out: list[Match] = []
    dropped: list[tuple[str, str]] = []
    for entry in matches_field:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        raw_label = str(entry.get("entity_type", "OTHER"))
        # Map gliner's snake_case labels to canonical ENTITY_TYPES.
        # Anything unmapped passes through as-is and Match.__post_init__
        # normalizes it to OTHER, which is the right default for a
        # label the rest of the pipeline doesn't know.
        entity_type = _LABEL_TO_ENTITY_TYPE.get(raw_label.lower(), raw_label.upper())
        # Hallucination guard: the service should always return
        # substrings of the input. A buggy or maliciously crafted
        # service shouldn't be able to inject arbitrary surrogates
        # into our flow.
        if not text or text not in source_text:
            dropped.append((text, raw_label))
            continue
        out.append(Match(text=text, entity_type=entity_type))
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "Remote GLiNER-PII parsed %d matches, dropped %d "
            "hallucinations: kept=%s dropped=%s",
            len(out), len(dropped),
            [(m.text, m.entity_type) for m in out], dropped,
        )
    return out


def _gliner_call_kwargs(overrides: Any, api_key: str | None) -> dict[str, Any]:  # noqa: ARG001
    """Per-call kwargs from the request's `additional_provider_specific_params`.
    `api_key` is unused (the gliner-pii service is unauthenticated by
    design — it sits inside the trust boundary alongside the
    guardrail) but kept in the signature for the DetectorSpec contract.

    Overrides.gliner_labels is a tuple (frozen for Overrides hashability);
    converted to a list here because the JSON request body uses lists.
    """
    return {
        "labels": list(overrides.gliner_labels) if overrides.gliner_labels is not None else None,
        "threshold": overrides.gliner_threshold,
    }


def _gliner_pii_factory() -> Detector:
    """Construct the gliner-pii detector, requiring GLINER_PII_URL.

    Unlike privacy_filter, there is no in-process fallback — the
    gliner library + ~570M weights would defeat the slim guardrail.
    An empty URL therefore raises at construction time so an operator
    who set DETECTOR_MODE=...,gliner_pii without setting GLINER_PII_URL
    sees a clear error at boot rather than the request-time confusion
    of a silently-skipped detector.

    Lives here (rather than in pipeline.py) so the SPEC declaration
    below can reference it without pipeline.py needing to import
    this module's internals.
    """
    if not CONFIG.url:
        raise RuntimeError(
            "DETECTOR_MODE includes 'gliner_pii' but GLINER_PII_URL is unset. "
            "Deploy the gliner-pii-service container (see services/gliner_pii/) "
            "and set GLINER_PII_URL=http://<host>:<port>."
        )
    return RemoteGlinerPIIDetector()


SPEC = DetectorSpec(
    name="gliner_pii",
    factory=_gliner_pii_factory,
    module=sys.modules[__name__],
    prepare_call_kwargs=_gliner_call_kwargs,
    has_semaphore=True,
    stats_prefix="gliner_pii",
    unavailable_error=GlinerPIIUnavailableError,
    blocked_reason=(
        "Anonymization gliner-pii is unreachable; request "
        "blocked to prevent unredacted data from reaching the "
        "upstream model."
    ),
)


__all__ = ["RemoteGlinerPIIDetector", "GlinerPIIUnavailableError", "SPEC"]
