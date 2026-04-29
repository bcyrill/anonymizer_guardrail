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
    # Texts longer than this get truncated before going to the LLM (not the
    # upstream user model — only the detection call). Keeps costs predictable.
    llm_max_chars: int = _env_int("LLM_MAX_CHARS", 8000)

    # ── Vault (call_id → mapping) ──────────────────────────────────────────────
    vault_ttl_s: int = _env_int("VAULT_TTL_S", 600)

    # ── Failure mode ───────────────────────────────────────────────────────────
    # If the LLM detector errors out, do we proceed with regex-only results
    # (fail_open) or block the request (fail_closed)? LiteLLM has its own
    # `unreachable_fallback` for when WE are down; this controls our internal
    # behaviour when our LLM dependency is down.
    fail_closed: bool = _env_bool("FAIL_CLOSED", True)


config = Config()
