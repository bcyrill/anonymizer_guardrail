"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # ── HTTP server ────────────────────────────────────────────────────────────
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8000)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ── Detector pipeline ──────────────────────────────────────────────────────
    # "regex"  → regex layer only (deterministic, no external deps)
    # "llm"    → LLM layer only
    # "both"   → regex first, then LLM on the same input (matches deduped)
    detector_mode: str = os.getenv("DETECTOR_MODE", "both").lower()

    # ── LLM detector ───────────────────────────────────────────────────────────
    # OpenAI-compatible endpoint. When pointed at LiteLLM, ensure the model used
    # here does NOT have this guardrail attached (otherwise → infinite recursion).
    llm_api_base: str = os.getenv("LLM_API_BASE", "http://litellm:4000/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "anonymize")
    llm_timeout_s: int = _env_int("LLM_TIMEOUT_S", 30)
    # When true, prefer the Authorization bearer token forwarded by LiteLLM
    # (via `extra_headers: [authorization]` on the guardrail) over LLM_API_KEY.
    # Lets the detection LLM be called with the same virtual key the user
    # authenticated to LiteLLM with, so detection cost/quota land on that key.
    # Falls back to LLM_API_KEY if the header is absent.
    llm_use_forwarded_key: bool = _env_bool("LLM_USE_FORWARDED_KEY", False)
    # Path to the system prompt for the LLM detector. If unset, the prompt
    # bundled inside the package (prompts/llm_default.md) is used. Operators
    # can override to tweak detection behaviour for their domain without
    # rebuilding the image.
    llm_system_prompt_path: str = os.getenv("LLM_SYSTEM_PROMPT_PATH", "")
    # Path to the YAML file defining the regex detector's patterns. If unset,
    # the bundled default (patterns/regex_default.yaml) is used. Pre-built
    # alternatives also ship with the package (e.g. patterns/regex_pentest.yaml)
    # — point this at any of them or at your own file.
    regex_patterns_path: str = os.getenv("REGEX_PATTERNS_PATH", "")
    # Hard cap on input size sent to the LLM in one call. Inputs above this
    # are REFUSED (LLMUnavailableError → FAIL_CLOSED policy applies), never
    # silently truncated — truncating in a guardrail would let everything past
    # this length through unscanned. Default is generous (~50K tokens), well
    # within typical small-model context windows; tune to your model.
    llm_max_chars: int = _env_int("LLM_MAX_CHARS", 200_000)

    # ── Vault (call_id → mapping) ──────────────────────────────────────────────
    vault_ttl_s: int = _env_int("VAULT_TTL_S", 600)

    # ── Failure mode ───────────────────────────────────────────────────────────
    # If the LLM detector errors out, do we proceed with regex-only results
    # (fail_open) or block the request (fail_closed)? LiteLLM has its own
    # `unreachable_fallback` for when WE are down; this controls our internal
    # behaviour when our LLM dependency is down.
    fail_closed: bool = _env_bool("FAIL_CLOSED", True)


config = Config()
