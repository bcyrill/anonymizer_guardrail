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
fall back to coverage from the other detectors. Content errors
(non-JSON body, malformed `matches` field, hallucinated text) are
non-fatal and log+return-[] without raising. Same split as the LLM
detector.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import config
from .base import Match

log = logging.getLogger("anonymizer.privacy_filter.remote")


class PrivacyFilterUnavailableError(RuntimeError):
    """Raised when the privacy-filter detector cannot complete its work
    for an availability reason — service unreachable, timeout, transport
    error, non-200 status. Caught by the pipeline; whether it propagates
    (BLOCKED) or degrades to empty matches is decided by
    config.privacy_filter_fail_closed.

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
        self.url = (url or config.privacy_filter_url).rstrip("/")
        if not self.url:
            # The factory in pipeline.py only constructs this class when
            # the URL is set, so reaching here means a caller bypassed
            # the factory. Fail loud rather than send requests to "/detect".
            raise RuntimeError(
                "RemotePrivacyFilterDetector requires PRIVACY_FILTER_URL "
                "to be set. Leave it unset to use the in-process detector."
            )
        self.timeout_s = timeout_s or config.privacy_filter_timeout_s
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

        # Content errors below are non-fatal: log and return []. The
        # service is reachable and replied 200, so this is a malformed
        # response, not an outage. Same split LLMDetector uses for
        # unexpected JSON shapes inside a 200 response.
        try:
            body = resp.json()
        except ValueError as exc:
            log.warning(
                "Privacy-filter at %s returned non-JSON: %s | body=%r",
                endpoint, exc, resp.text[:300],
            )
            return []

        return _parse_matches(body, text)

    async def aclose(self) -> None:
        """Drain the httpx connection pool. Wired to Pipeline.aclose
        which fires on FastAPI shutdown."""
        await self._client.aclose()


def _parse_matches(body: Any, source_text: str) -> list[Match]:
    """Translate `{"matches": [{...}, ...]}` from the service into Match
    objects. Drops malformed entries and any whose `text` field doesn't
    actually appear in the source — a defensive guard against a future
    service version returning bad offsets, mirroring the LLM detector's
    hallucination guard."""
    if not isinstance(body, dict):
        log.warning(
            "Privacy-filter response wasn't a JSON object: %r", body,
        )
        return []
    matches_field = body.get("matches", [])
    if not isinstance(matches_field, list):
        log.warning(
            "Privacy-filter response `matches` wasn't a list: %r",
            matches_field,
        )
        return []

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
