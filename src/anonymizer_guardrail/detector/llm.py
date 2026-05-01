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

from ..registry import parse_named_path_registry
from ..config import config
from .base import Match

log = logging.getLogger("anonymizer.llm")


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM detection backend is unreachable or unresponsive."""


_DEFAULT_PROMPT_RELPATH = "prompts/llm_default.md"
_BUNDLED_PROMPTS_DIR = "prompts"
_BUNDLED_PREFIX = "bundled:"


def _read_bundled_prompt(name: str, label: str) -> str:
    """Read a file from the package's bundled prompts/ directory."""
    if not name or "/" in name or "\\" in name:
        raise RuntimeError(
            f"{label}=bundled:{name!r}: name must be a bare "
            f"filename (no path separators). Use a filesystem path if you "
            f"want to reference a file outside the bundled prompts/."
        )
    try:
        return (
            resources.files("anonymizer_guardrail")
            .joinpath(f"{_BUNDLED_PROMPTS_DIR}/{name}")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            f"{label}=bundled:{name!r} not found in bundled prompts/: {exc}"
        ) from exc


def _load_system_prompt(
    override: str | None = None, label: str = "LLM_SYSTEM_PROMPT_PATH"
) -> str:
    """Load one system prompt.

    `override` is the path/bundled-name string. None (the default)
    means "read config.llm_system_prompt_path" so callers that haven't
    been updated for the registry refactor (e.g. tests that monkey-
    patch config) keep working. Empty string → bundled default.
    `label` names the source for error messages — defaults to the env
    var; the registry loader passes a per-entry label so a typo in
    `LLM_SYSTEM_PROMPT_REGISTRY="legal=…"` reports the entry name, not
    the global env var.

    The override accepts either:
      * a filesystem path (absolute or relative)
      * `bundled:<name>` — a bare filename in the package's prompts/ dir,
        which insulates the env var from the Python version embedded in
        the site-packages path.

    Loaded once at import-time — restart to pick up edits, same as every
    other config knob. An empty/whitespace-only file is rejected: a
    no-instructions LLM would happily return `{"entities": []}` for every
    input, which is a silent regex-only fallback masquerading as success.
    """
    if override is None:
        override = config.llm_system_prompt_path
    override = override.strip()
    if override:
        if override.startswith(_BUNDLED_PREFIX):
            text = _read_bundled_prompt(
                override[len(_BUNDLED_PREFIX):].strip(), label,
            )
            source = f"{label}={override!r}"
        else:
            path = Path(override)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                # Fail loud rather than silently fall back: an operator who set
                # this path expects their prompt to be used. Crashing at import
                # surfaces the typo immediately instead of after the first call.
                raise RuntimeError(
                    f"{label}={override!r} could not be read: {exc}"
                ) from exc
            source = f"{label}={override!r}"
    else:
        # Anchor at the parent package so `prompts/` doesn't need __init__.py.
        text = (
            resources.files("anonymizer_guardrail")
            .joinpath(_DEFAULT_PROMPT_RELPATH)
            .read_text(encoding="utf-8")
        )
        source = f"bundled {_DEFAULT_PROMPT_RELPATH}"

    if not text.strip():
        raise RuntimeError(
            f"{source} is empty — refusing to load. An empty prompt makes the "
            f"LLM detector useless: the model would have no instructions and "
            f"would consistently return zero entities."
        )
    return text


def _load_system_prompt_registry() -> dict[str, str]:
    """Compile every entry in LLM_SYSTEM_PROMPT_REGISTRY at startup.

    Returns a dict mapping registry name → prompt text. Same validation
    as the default path: typos / unreadable files / empty prompts crash
    boot loudly.
    """
    raw = config.llm_system_prompt_registry
    pairs = parse_named_path_registry(raw, "LLM_SYSTEM_PROMPT_REGISTRY")
    return {
        name: _load_system_prompt(path, f"LLM_SYSTEM_PROMPT_REGISTRY[{name}]")
        for name, path in pairs.items()
    }


_SYSTEM_PROMPT = _load_system_prompt()
_SYSTEM_PROMPT_REGISTRY = _load_system_prompt_registry()


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
    dropped: list[tuple[str, str]] = []
    for entry in data.get("entities", []):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        etype = str(entry.get("type", "OTHER"))
        # Hallucination guard: if the model invents a substring that isn't
        # actually in the input, drop it. We can't anonymize what isn't there.
        if not text or text not in source_text:
            dropped.append((text, etype))
            continue
        out.append(Match(text=text, entity_type=etype))
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "LLM parsed %d entities, dropped %d hallucinations: kept=%s dropped=%s",
            len(out), len(dropped),
            [(m.text, m.entity_type) for m in out],
            dropped,
        )
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
        self._client = httpx.AsyncClient(timeout=self.timeout_s)

    async def detect(
        self,
        text: str,
        *,
        api_key: str | None = None,
        model: str | None = None,
        prompt_name: str | None = None,
    ) -> list[Match]:
        if not text or not text.strip():
            return []

        # Per-call override (e.g. forwarded user key from LiteLLM) takes
        # precedence; otherwise use the configured key. Empty string after
        # strip is treated as "no key" so we don't send an empty Bearer.
        effective_key = (api_key if api_key is not None else self.api_key) or ""
        effective_key = effective_key.strip()

        # `model` lets callers (per-request override path) swap the
        # detection model without rebuilding the LLMDetector. Falls
        # back to whatever was configured at startup.
        effective_model = (model or self.model).strip() or self.model

        # `prompt_name` selects a NAMED alternative system prompt from
        # LLM_SYSTEM_PROMPT_REGISTRY. None / "default" → the
        # configured default prompt. Unknown name → log a warning and
        # fall back to the default (don't block the request over a
        # typo in the override).
        effective_prompt = _SYSTEM_PROMPT
        if prompt_name and prompt_name != "default":
            named = _SYSTEM_PROMPT_REGISTRY.get(prompt_name)
            if named is None:
                log.warning(
                    "Override llm_prompt=%r isn't in LLM_SYSTEM_PROMPT_REGISTRY "
                    "(known: %s); falling back to the default prompt.",
                    prompt_name, sorted(_SYSTEM_PROMPT_REGISTRY) or "<empty>",
                )
            else:
                effective_prompt = named

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
            "model": effective_model,
            "messages": [
                {"role": "system", "content": effective_prompt},
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
            resp = await self._client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(f"Cannot reach LLM at {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"LLM timed out at {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            # Catches ReadError, WriteError, RemoteProtocolError, NetworkError,
            # ProxyError, etc. — any HTTP-layer failure that isn't a clean
            # connect/timeout. Routing through LLMUnavailableError ensures
            # FAIL_CLOSED applies; bare exceptions used to be swallowed by
            # the pipeline's defensive handler, which would silently ship
            # unredacted text upstream.
            raise LLMUnavailableError(
                f"HTTP error talking to LLM at {url}: {exc}"
            ) from exc

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

    async def aclose(self) -> None:
        """Close the shared httpx client. Called from Pipeline.aclose() at
        FastAPI shutdown so the connection pool drains cleanly."""
        await self._client.aclose()


__all__ = ["LLMDetector", "LLMUnavailableError"]
