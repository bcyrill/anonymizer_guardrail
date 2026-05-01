"""Cross-cutting runtime configuration loaded from environment variables.

Per-detector config (LLM, regex, denylist, privacy_filter, gliner_pii)
lives in each detector's own module as a `CONFIG` constant — see
`detector/llm.py` etc. The `DetectorSpec` registry resolves these via
`spec.config.<field>` so the central `Config` here only carries the
truly cross-cutting fields:

  * HTTP server (host, port, log_level)
  * Pipeline-level (DETECTOR_MODE)
  * Surrogate generator (locale, salt, cache cap, USE_FAKER)
  * Vault (TTL, max entries)

The `_env_bool` / `_env_int` helpers stay public-ish (single underscore)
so detector modules can import them — keeps the env-parsing convention
identical across all configs.
"""

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
    # Comma-separated list of detector names. Order matters: `_dedup` keeps
    # the first-seen entity_type for any duplicate text, so the detector
    # listed first wins type-resolution conflicts. Examples:
    #   "regex"      → regex layer only (deterministic, no external deps)
    #   "llm"        → LLM layer only
    #   "regex,llm"  → both detectors run in parallel; regex types win
    # Whitespace tolerated; duplicates collapsed; unknown names warn-and-skip.
    detector_mode: str = os.getenv("DETECTOR_MODE", "regex,llm").lower()

    # ── Surrogate generator ────────────────────────────────────────────────────
    # Faker locale(s) used by the surrogate generator. Empty → Faker's own
    # default (en_US). Single locale ("pt_BR") or comma-separated list
    # ("pt_BR,en_US") with the first one preferred and others used as
    # fallback for providers the primary doesn't implement. Invalid
    # locales fail at startup, not at first request.
    faker_locale: str = os.getenv("FAKER_LOCALE", "")
    # When false, every surrogate is an opaque deterministic placeholder
    # (e.g. `[PERSON_7F3A2B]`) instead of a realistic Faker substitute.
    # Useful when the upstream model misinterprets realistic surrogates as
    # real data, or when realism would mislead a downstream tool. Faker is
    # not even instantiated when disabled.
    use_faker: bool = _env_bool("USE_FAKER", True)
    # Cap on the surrogate generator's process-wide LRU cache. Each entry is
    # ~150–300 bytes (entity type + original text + surrogate + bookkeeping),
    # so 100k entries ≈ 30 MB worst case. The cache backs cross-request
    # surrogate consistency for multi-turn conversations — set higher if
    # you have very chatty users; lower (or zero, effectively disabling)
    # if memory is tight and per-request consistency is enough.
    surrogate_cache_max_size: int = _env_int("SURROGATE_CACHE_MAX_SIZE", 100_000)
    # Secret keying material mixed into the blake2b hashes the surrogate
    # generator uses (both the Faker seed and the opaque-token digest).
    # Empty (the default) → a fresh 16-byte random salt is generated each
    # time the process starts. Set to a fixed string to keep surrogates
    # stable across restarts (useful for log-correlation analysis, but
    # weakens privacy: the salt becomes ambient and attackers who learn
    # it can brute-force low-entropy entities like IPs and phone numbers).
    surrogate_salt: str = os.getenv("SURROGATE_SALT", "")

    # ── Vault (call_id → mapping) ──────────────────────────────────────────────
    vault_ttl_s: int = _env_int("VAULT_TTL_S", 600)
    # Hard cap on vault entries. TTL-only eviction fires lazily on put(), so a
    # burst of unique call_ids without matching post_calls (client crashes,
    # timeouts, malicious flood) can grow the vault to millions of entries
    # before TTL clears them. The cap forces LRU eviction on overflow as a
    # backstop. Each entry is a small dict of surrogate→original strings
    # (~hundreds of bytes typical), so 10k entries ≈ a few MB worst case.
    # Raise if your traffic legitimately holds many in-flight call_ids
    # simultaneously (very chatty conversations, long-running streams).
    vault_max_entries: int = _env_int("VAULT_MAX_ENTRIES", 10_000)


config = Config()
