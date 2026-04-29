"""
LLM detection layer.

Sends the input text to an OpenAI-compatible Chat Completions endpoint with a
JSON-mode system prompt that asks the model to enumerate sensitive entities.

When pointed at LiteLLM, the model alias used here MUST NOT have this guardrail
attached — otherwise every detection call would re-enter the guardrail and
recurse forever. See README for the recommended LiteLLM config pattern.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import config
from .base import Match

log = logging.getLogger("anonymizer.llm")


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM detection backend is unreachable or unresponsive."""


_SYSTEM_PROMPT = """\
You are a privacy guardian. Given a piece of text, identify every substring \
that could plausibly identify a real person, organization, or piece of \
infrastructure, OR that constitutes a secret.

When in doubt, flag it. False positives are safer than false negatives.

Flag, at minimum:
- People: full names, usernames embedded in `first.last` form, national IDs.
- Organizations: company names, project codenames, internal product names, \
WiFi SSIDs, NetBIOS / AD domain names, custom CA / certificate template names.
- Network identifiers: IPs (incl. private RFC1918), CIDRs, FQDNs, internal \
hostnames, subdomains.
- Credentials & secrets: passwords, API keys, tokens, hashes, JWTs, private \
keys, connection strings.
- Other identifiers: email addresses, phone numbers, mailing addresses.

Do NOT flag generic technical vocabulary (function names, well-known protocols, \
public software product names, OS versions, generic role titles like "admin" \
or "CEO" with no name attached).

Return ONLY valid JSON, no prose, no markdown:
{"entities": [{"text": "<exact substring from input>", \
"type": "PERSON|ORGANIZATION|EMAIL_ADDRESS|IP_ADDRESS|CIDR|HOSTNAME|DOMAIN|\
USERNAME|CREDENTIAL|TOKEN|HASH|UUID|AWS_ACCESS_KEY|JWT|PHONE|OTHER"}]}

Nothing found: {"entities": []}
"""


def _strip_thinking(raw: str) -> str:
    """Remove <think>…</think> blocks emitted by reasoning models."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


def _extract_json_object(raw: str) -> str:
    """Pull a JSON object out of a response that may contain code fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        return fenced.group(1)
    bare = re.search(r"\{.*\}", raw, re.DOTALL)
    return bare.group(0) if bare else raw


def _parse_entities(raw: str, source_text: str) -> list[Match]:
    try:
        cleaned = _extract_json_object(_strip_thinking(raw))
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("LLM returned non-JSON response: %s | raw=%r", exc, raw[:300])
        return []

    out: list[Match] = []
    for entry in data.get("entities", []):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        # Hallucination guard: if the model invents a substring that isn't
        # actually in the input, drop it. We can't anonymize what isn't there.
        if not text or text not in source_text:
            continue
        out.append(Match(text=text, entity_type=str(entry.get("type", "OTHER"))))
    return out


class LLMDetector:
    """OpenAI Chat Completions detector, JSON mode."""

    name = "llm"

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: int | None = None,
    ) -> None:
        self.api_base = (api_base or config.llm_api_base).rstrip("/")
        self.api_key = api_key if api_key is not None else config.llm_api_key
        self.model = model or config.llm_model
        self.timeout_s = timeout_s or config.llm_timeout_s

    async def detect(self, text: str) -> list[Match]:
        if not text or not text.strip():
            return []

        # Refuse oversized inputs rather than truncating. Truncation would
        # silently let everything past the cap through unscanned — the
        # opposite of what a guardrail should do. Raised as
        # LLMUnavailableError so the FAIL_CLOSED policy in the pipeline
        # decides: block the request, or fall back to regex-only.
        if len(text) > config.llm_max_chars:
            raise LLMUnavailableError(
                f"Input too large for LLM detection: {len(text)} chars > "
                f"LLM_MAX_CHARS ({config.llm_max_chars}). Raise the cap or "
                f"point at a model with a larger context window."
            )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "stream": False,
            # Many OpenAI-compatible backends honour this; ones that don't will
            # ignore it harmlessly and we'll still extract JSON from prose.
            "response_format": {"type": "json_object"},
        }

        url = f"{self.api_base}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(f"Cannot reach LLM at {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"LLM timed out at {url}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMUnavailableError(
                f"LLM at {url} returned {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("Unexpected LLM response shape: %s | body=%r", exc, resp.text[:300])
            return []

        return _parse_entities(content, text)


__all__ = ["LLMDetector", "LLMUnavailableError"]
