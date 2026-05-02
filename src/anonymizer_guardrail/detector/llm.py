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
import sys
from typing import Any, Literal

import httpx
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..bundled_resource import read_bundled_default, resolve_spec
from ..registry import parse_named_path_registry
from .base import Match
from .launcher import LauncherSpec, ServiceSpec
from .remote_base import BaseRemoteDetector
from .spec import DetectorSpec

log = logging.getLogger("anonymizer.llm")


# ── Per-detector config ───────────────────────────────────────────────────
class LLMConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        extra="ignore",
        frozen=True,
    )

    # OpenAI-compatible endpoint. When pointed at LiteLLM, ensure the
    # model used here does NOT have this guardrail attached (otherwise
    # → infinite recursion).
    api_base: str = "http://litellm:4000/v1"
    api_key: str = ""
    model: str = "anonymize"
    timeout_s: int = 30
    # When true, prefer the Authorization bearer token forwarded by
    # LiteLLM (via `extra_headers: [authorization]` on the guardrail)
    # over LLM_API_KEY. Falls back to LLM_API_KEY if the header is absent.
    use_forwarded_key: bool = False
    # Path to the system prompt for the LLM detector. Empty → bundled
    # default (prompts/llm_default.md).
    system_prompt_path: str = ""
    # Optional registry of NAMED alternative LLM prompts that callers
    # can opt into per-request via additional_provider_specific_params.llm_prompt.
    system_prompt_registry: str = ""
    # Hard cap on input size sent to the LLM in one call. Inputs above
    # this are REFUSED (LLMUnavailableError → fail-closed policy
    # applies), never silently truncated.
    max_chars: int = 200_000
    # Max number of concurrent LLM calls.
    max_concurrency: int = 10
    # Failure mode when the LLM detector errors out. true (default) →
    # block; false → fall back to coverage from the other detectors.
    fail_closed: bool = True
    # LRU cap for the per-detector result cache. 0 disables caching
    # (default). When enabled, caching skips the LLM call for inputs
    # we've seen before within the cache lifetime — useful for
    # multi-turn conversations where each turn replays the full history
    # in `texts`. See detector/cache.py for the trade-offs (notably
    # that it freezes the first-seen result for a given input).
    cache_max_size: int = 0
    # How the pipeline dispatches `req.texts` to the LLM detector:
    #   "per_text" (default) — one detect() call per text, in
    #     parallel under the LLM concurrency cap. Compatible with the
    #     result cache; preserves per-text failure isolation.
    #   "merged"   — concatenate all texts with a sentinel separator
    #     and make one detect() call against the blob. Amortises the
    #     system prompt across short fragments and gives the model
    #     cross-segment context (a username from text 1 next to a
    #     "password reset for ..." in text 2 reads as CREDENTIAL more
    #     reliably than each in isolation). Mutually exclusive with the
    #     result cache: every blob is unique by construction, so a
    #     non-zero `cache_max_size` paired with merged mode logs a
    #     warning and the cache is silently bypassed.
    # See `pipeline.py` for the dispatch logic and the sentinel choice.
    input_mode: Literal["per_text", "merged"] = "per_text"

    @field_validator("api_base", mode="after")
    @classmethod
    def _validate_api_base(cls, v: str) -> str:
        """Strip whitespace and require an http(s):// scheme. Without
        this, an operator who sets `LLM_API_BASE=litellm:4000/v1` (no
        scheme) gets a confusing httpx error at first request rather
        than a fail-loud at boot."""
        v = v.strip()
        if not v:
            raise ValueError(
                "LLM_API_BASE is required and must be set to a full URL "
                "(e.g. http://litellm:4000/v1)."
            )
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"LLM_API_BASE={v!r} must start with http:// or https:// "
                f"(got no scheme). Set the full URL, e.g. "
                f"http://litellm:4000/v1."
            )
        return v


CONFIG = LLMConfig()


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM detection backend is unreachable or unresponsive."""


_DEFAULT_PROMPT_RELPATH = "prompts/llm_default.md"
_BUNDLED_PROMPTS_DIR = "prompts"


def _load_system_prompt(
    override: str | None = None, label: str = "LLM_SYSTEM_PROMPT_PATH"
) -> str:
    """Load one system prompt.

    `override` is the path/bundled-name string. None (the default)
    means "read CONFIG.system_prompt_path" so callers that haven't
    been updated for the registry refactor (e.g. tests that monkey-
    patch config) keep working. Empty string → bundled default.
    `label` names the source for error messages — defaults to the env
    var; the registry loader passes a per-entry label so a typo in
    `LLM_SYSTEM_PROMPT_REGISTRY="legal=…"` reports the entry name, not
    the global env var.

    Spec syntax (filesystem path, `bundled:NAME`, fail-loud-on-typo) is
    shared with the regex / denylist loaders via bundled_resource.

    Loaded once at import-time — restart to pick up edits, same as every
    other config knob. An empty/whitespace-only file is rejected: a
    no-instructions LLM would happily return `{"entities": []}` for every
    input, which is a silent regex-only fallback masquerading as success.
    """
    if override is None:
        override = CONFIG.system_prompt_path
    override = override.strip()
    if override:
        text, _source, _file_dir = resolve_spec(
            override, bundled_dir=_BUNDLED_PROMPTS_DIR, label=label,
        )
        # Keep the env-var-style source label for the empty-prompt
        # error so an operator's eyes go straight to the env var name.
        source = f"{label}={override!r}"
    else:
        text = read_bundled_default(_DEFAULT_PROMPT_RELPATH)
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
    raw = CONFIG.system_prompt_registry
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
    """Parse the model's content payload into Match objects.

    Whole-response failures (non-JSON content, top-level type mismatch,
    `entities` field present but not a list) raise LLMUnavailableError
    so the LLM_FAIL_CLOSED policy decides — a 200 carrying garbage means
    the detector can't do its job, which is the case fail-closed exists
    to catch. Per-entry malformed entries (entry not a dict, missing
    text) are still dropped silently — those are local to one entity;
    the rest of the response is still good. The hallucination guard
    (text not in source) is the same kind of per-entry drop.
    """
    try:
        cleaned = _extract_json_object(_strip_thinking(raw))
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMUnavailableError(
            f"LLM returned non-JSON content: {exc} | raw={raw[:300]!r}"
        ) from exc

    if not isinstance(data, dict):
        raise LLMUnavailableError(
            f"LLM content was JSON but not an object (got "
            f"{type(data).__name__}): raw={raw[:300]!r}"
        )

    entities = data.get("entities", [])
    if not isinstance(entities, list):
        raise LLMUnavailableError(
            f"LLM content `entities` field is not a list (got "
            f"{type(entities).__name__}): raw={raw[:300]!r}"
        )

    out: list[Match] = []
    dropped: list[tuple[str, str]] = []
    for entry in entities:
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


class LLMDetector(BaseRemoteDetector):
    """OpenAI Chat Completions detector, JSON mode."""

    name = "llm"

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: int | None = None,
    ) -> None:
        super().__init__(
            timeout_s=timeout_s or CONFIG.timeout_s,
            cache_max_size=CONFIG.cache_max_size,
        )
        self.api_base = (api_base or CONFIG.api_base).rstrip("/")
        self.api_key = api_key if api_key is not None else CONFIG.api_key
        self.model = model or CONFIG.model

    async def detect(
        self,
        text: str,
        *,
        api_key: str | None = None,
        model: str | None = None,
        prompt_name: str | None = None,
    ) -> list[Match]:
        """Public entry point. Resolves the cache key from the text plus
        the overrides that affect output, then delegates to `_do_detect`
        through the base's cache wrapper.

        `api_key` is intentionally NOT in the cache key: same model +
        prompt + text → same detection result regardless of which key
        authenticated the call. Including it would fragment the cache
        across users (e.g. forwarded keys) for no correctness benefit.
        """
        # Resolve overrides to their effective concrete values so the
        # cache key is canonical: a None-override and a no-override
        # carrying the matching default share one cache slot. Same
        # pattern as SurrogateGenerator.for_match.
        effective_model = (model or self.model).strip() or self.model
        effective_prompt_name = prompt_name if prompt_name else "default"

        cache_key = (text, effective_model, effective_prompt_name)
        return await self._detect_via_cache(
            text, cache_key,
            lambda: self._do_detect(
                text,
                api_key=api_key,
                effective_model=effective_model,
                effective_prompt_name=effective_prompt_name,
            ),
        )

    async def _do_detect(
        self,
        text: str,
        *,
        api_key: str | None,
        effective_model: str,
        effective_prompt_name: str,
    ) -> list[Match]:
        """The actual HTTP call. Split out from `detect` so the cache
        wrapper is the only thing the public entry point does. Receives
        already-resolved override values to keep cache-key construction
        and HTTP-call construction reading from the same source of truth.
        """
        # Per-call override (e.g. forwarded user key from LiteLLM) takes
        # precedence; otherwise use the configured key. Empty string after
        # strip is treated as "no key" so we don't send an empty Bearer.
        effective_key = (api_key if api_key is not None else self.api_key) or ""
        effective_key = effective_key.strip()

        # `prompt_name` selects a NAMED alternative system prompt from
        # LLM_SYSTEM_PROMPT_REGISTRY. "default" → the configured default
        # prompt. Unknown name → log a warning and fall back to the
        # default (don't block the request over a typo in the override).
        effective_prompt = _SYSTEM_PROMPT
        if effective_prompt_name != "default":
            named = _SYSTEM_PROMPT_REGISTRY.get(effective_prompt_name)
            if named is None:
                log.warning(
                    "Override llm_prompt=%r isn't in LLM_SYSTEM_PROMPT_REGISTRY "
                    "(known: %s); falling back to the default prompt.",
                    effective_prompt_name,
                    sorted(_SYSTEM_PROMPT_REGISTRY) or "<empty>",
                )
            else:
                effective_prompt = named

        # Refuse oversized inputs rather than truncating. Truncation would
        # silently let everything past the cap through unscanned — the
        # opposite of what a guardrail should do. Raised as
        # LLMUnavailableError so the LLM_FAIL_CLOSED policy in the pipeline
        # decides: block the request, or fall back to regex-only.
        if len(text) > CONFIG.max_chars:
            raise LLMUnavailableError(
                f"Input too large for LLM detection: {len(text)} chars > "
                f"LLM_MAX_CHARS ({CONFIG.max_chars}). Raise the cap or "
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
            # LLM_FAIL_CLOSED applies; bare exceptions used to be swallowed
            # by the pipeline's defensive handler, which would silently ship
            # unredacted text upstream.
            raise LLMUnavailableError(
                f"HTTP error talking to LLM at {url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise LLMUnavailableError(
                f"LLM at {url} returned {resp.status_code}: {resp.text[:300]}"
            )

        # 200 OK with an unparseable body or a missing
        # choices/message/content path means the backend replied but the
        # detector can't act on what came back. Treat it as an
        # availability failure so LLM_FAIL_CLOSED decides — soft-failing
        # to [] would silently let unredacted text through under
        # fail_closed=True, defeating the policy.
        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMUnavailableError(
                f"LLM at {url} returned non-JSON body on HTTP 200: "
                f"{exc} | body={resp.text[:300]!r}"
            ) from exc
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError(
                f"LLM at {url} returned unexpected response shape: {exc} | "
                f"body={resp.text[:300]!r}"
            ) from exc

        return _parse_entities(content, text)

    # cache_stats() and aclose() come from BaseRemoteDetector.


def _llm_call_kwargs(overrides: Any, api_key: str | None) -> dict[str, Any]:
    """Per-call kwargs. The forwarded api_key from the request flows
    through to the detector's per-request override so detection cost
    can attribute back to the user's virtual key."""
    return {
        "api_key": api_key,
        "model": overrides.llm_model,
        "prompt_name": overrides.llm_prompt,
    }


SPEC = DetectorSpec(
    name="llm",
    factory=LLMDetector,
    module=sys.modules[__name__],
    prepare_call_kwargs=_llm_call_kwargs,
    has_semaphore=True,
    stats_prefix="llm",
    unavailable_error=LLMUnavailableError,
    blocked_reason=(
        "Anonymization LLM is unreachable; request blocked to prevent "
        "unredacted data from reaching the upstream model."
    ),
    # Result caching — operator-controlled via LLM_CACHE_MAX_SIZE
    # (0 = disabled, the default). Surfaces cache_size/cache_max/
    # cache_hits/cache_misses on /health under the `llm_` prefix.
    has_cache=True,
)


# Auto-startable backing service: `fake-llm` (rule-driven OpenAI-compatible
# stub). Selected via `--llm-backend service` on the launcher.
LAUNCHER_SPEC = LauncherSpec(
    guardrail_env_passthroughs=[
        "LLM_API_BASE",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_TIMEOUT_S",
        "LLM_USE_FORWARDED_KEY",
        "LLM_SYSTEM_PROMPT_PATH",
        "LLM_SYSTEM_PROMPT_REGISTRY",
        "LLM_MAX_CHARS",
        "LLM_MAX_CONCURRENCY",
        "LLM_FAIL_CLOSED",
    ],
    service=ServiceSpec(
        container_name="fake-llm",
        image_tag_envs=("TAG_FAKE_LLM",),
        image_tag_defaults=("fake-llm:latest",),
        port=4000,
        readiness_timeout_s=30,
        # No HF cache — fake-llm is rule-driven, no model.
        hf_cache_volume=None,
        guardrail_env_when_started={
            "LLM_API_BASE": "http://fake-llm:4000/v1",
            # fake-llm doesn't actually validate the key but the
            # detector refuses to send the Authorization header
            # when the key is empty — set a non-empty placeholder.
            "LLM_API_KEY": "any-non-empty",
            "LLM_MODEL": "fake",
        },
    ),
)


__all__ = ["LLMDetector", "LLMUnavailableError", "SPEC", "LAUNCHER_SPEC"]
