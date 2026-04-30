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
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from ..config import config
from .base import Match

log = logging.getLogger("anonymizer.llm")


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM detection backend is unreachable or unresponsive."""


_DEFAULT_PROMPT_RELPATH = "prompts/llm_default.md"


def _load_system_prompt() -> str:
    """Load the detector's system prompt.

    LLM_SYSTEM_PROMPT_PATH wins if set; otherwise we fall back to the
    bundled default. Loaded once at import-time — restart to pick up edits,
    same as every other config knob.
    """
    override = config.llm_system_prompt_path.strip()
    if override:
        path = Path(override)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            # Fail loud rather than silently fall back: an operator who set
            # this path expects their prompt to be used. Crashing at import
            # surfaces the typo immediately instead of after the first call.
            raise RuntimeError(
                f"LLM_SYSTEM_PROMPT_PATH={override!r} could not be read: {exc}"
            ) from exc
    # Anchor at the parent package so `prompts/` doesn't need __init__.py.
    return (
        resources.files("anonymizer_guardrail")
        .joinpath(_DEFAULT_PROMPT_RELPATH)
        .read_text(encoding="utf-8")
    )


_SYSTEM_PROMPT = _load_system_prompt()


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

    async def detect(self, text: str, *, api_key: str | None = None) -> list[Match]:
        if not text or not text.strip():
            return []

        # Per-call override (e.g. forwarded user key from LiteLLM) takes
        # precedence; otherwise use the configured key. Empty string after
        # strip is treated as "no key" so we don't send an empty Bearer.
        effective_key = (api_key if api_key is not None else self.api_key) or ""
        effective_key = effective_key.strip()

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
        if effective_key:
            headers["Authorization"] = f"Bearer {effective_key}"

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
