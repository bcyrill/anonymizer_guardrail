"""
Remote privacy-filter detector.

HTTP client variant of `PrivacyFilterDetector`. Posts the input text to a
standalone privacy-filter inference service (see services/privacy_filter/)
and parses the returned span list back into Match objects.

When `PRIVACY_FILTER_URL` is set, the pipeline factory uses this
implementation instead of the in-process one — the heavy ML deps then
live in their own container, the guardrail image stays slim, and
multiple guardrail replicas can share one inference service.

Behaviour mirrors the in-process detector:

  * Empty / whitespace-only input short-circuits to no matches.
  * Same label set, same span semantics — the *service* applies the
    label-map and span-merge logic on its end, so the wire format is
    already final post-processing output. We only have to wrap each
    entry in a Match (and apply the source-text hallucination guard,
    in case a future service version drifts).

Availability errors (connect / timeout / non-200 / transport HTTP
errors) raise PrivacyFilterUnavailableError so the pipeline's
PRIVACY_FILTER_FAIL_CLOSED policy decides — block the request, or
fall back to coverage from the other detectors. Whole-response
content errors on a 200 (non-JSON body, top-level type mismatch,
`matches` field present but not a list) raise the same typed error:
the service replied but didn't say anything useful, which the
guardrail must not treat as "no PII found" — fail_closed=True is
explicitly the "block rather than risk leakage" posture. Per-entry
malformed entries (entry not a dict, missing text, hallucinated
text not in source) are still dropped silently — those are local
to one entity, the rest of the response is still good. Same split
as the LLM detector.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import privacy_filter as _pf_mod
from .base import Match

log = logging.getLogger("anonymizer.privacy_filter.remote")


class PrivacyFilterUnavailableError(RuntimeError):
    """Raised when the privacy-filter detector cannot complete its work
    for an availability reason — service unreachable, timeout, transport
    error, non-200 status. Caught by the pipeline; whether it propagates
    (BLOCKED) or degrades to empty matches is decided by
    privacy_filter.CONFIG.fail_closed.

    Mirrors LLMUnavailableError. We deliberately keep the two distinct
    so an operator can fail closed on the LLM but open on PF (or vice
    versa); a single shared exception would force them to move in
    lockstep.
    """


# Reuse the same canonical entity types the service emits. Anything else
# falls through to OTHER via Match.__post_init__ — matches what happens
# if someone deploys a custom build of the service that adds new labels
# without updating the guardrail.
class RemotePrivacyFilterDetector:
    """Talks HTTP to a privacy-filter inference service."""

    name = "privacy_filter"

    def __init__(
        self,
        url: str | None = None,
        timeout_s: int | None = None,
    ) -> None:
        self.url = (url or _pf_mod.CONFIG.url).rstrip("/")
        if not self.url:
            # The factory in pipeline.py only constructs this class when
            # the URL is set, so reaching here means a caller bypassed
            # the factory. Fail loud rather than send requests to "/detect".
            raise RuntimeError(
                "RemotePrivacyFilterDetector requires PRIVACY_FILTER_URL "
                "to be set. Leave it unset to use the in-process detector."
            )
        self.timeout_s = timeout_s or _pf_mod.CONFIG.timeout_s
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        log.info(
            "Privacy-filter detector wired to remote service at %s "
            "(timeout=%ds).", self.url, self.timeout_s,
        )

    async def detect(self, text: str) -> list[Match]:
        if not text or not text.strip():
            return []

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

    async def aclose(self) -> None:
        """Drain the httpx connection pool. Wired to Pipeline.aclose
        which fires on FastAPI shutdown."""
        await self._client.aclose()


def _parse_matches(body: Any, source_text: str, endpoint: str) -> list[Match]:
    """Translate `{"matches": [{...}, ...]}` from the service into Match
    objects.

    Whole-response shape failures (body not a JSON object, `matches`
    field present but not a list) raise PrivacyFilterUnavailableError
    so PRIVACY_FILTER_FAIL_CLOSED decides — these mean the service
    can't be acted on for this request. Per-entry malformed entries
    (entry not a dict, missing/empty text, text not in source) are
    dropped silently because they only invalidate one entity; the
    rest of the response is still usable.
    """
    if not isinstance(body, dict):
        raise PrivacyFilterUnavailableError(
            f"Privacy-filter at {endpoint} returned a JSON value that "
            f"isn't an object (got {type(body).__name__}): {body!r}"
        )
    matches_field = body.get("matches", [])
    if not isinstance(matches_field, list):
        raise PrivacyFilterUnavailableError(
            f"Privacy-filter at {endpoint} returned `matches` of type "
            f"{type(matches_field).__name__}, expected list: "
            f"{matches_field!r}"
        )

    out: list[Match] = []
    dropped: list[tuple[str, str]] = []
    for entry in matches_field:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        etype = str(entry.get("entity_type", "OTHER"))
        # Hallucination guard: the service should always return
        # substrings of the input, but a buggy or maliciously crafted
        # service shouldn't be able to inject arbitrary surrogates into
        # our flow. Drop anything that isn't actually in the input.
        if not text or text not in source_text:
            dropped.append((text, etype))
            continue
        out.append(Match(text=text, entity_type=etype))
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "Remote privacy-filter parsed %d matches, dropped %d "
            "hallucinations: kept=%s dropped=%s",
            len(out), len(dropped),
            [(m.text, m.entity_type) for m in out], dropped,
        )
    return out


__all__ = ["RemotePrivacyFilterDetector"]
