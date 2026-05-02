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

Backed by `pydantic-settings`. Each field's env-var name is the
upper-snake-case form of the field name; pydantic handles type coercion
and validation, so a malformed value (`PORT=abc`) crashes at boot
rather than silently falling back to the default. Tests substitute
config instances via `model_copy(update={...})`.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        # Tolerate stray env vars that aren't fields on this model —
        # the per-detector configs read their own prefixes from the
        # same environment, so a strict policy would reject them all.
        extra="ignore",
        frozen=True,
    )

    # ── HTTP server ────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    # Hard cap on POST body bytes — applied by the body-size middleware
    # in main.py BEFORE Pydantic deserializes the request. Distinct from
    # `LLM_MAX_CHARS`: that's an LLM-context-window concern that fires
    # AFTER parsing; this is a DoS-protection concern that prevents the
    # process from allocating memory for a runaway payload in the first
    # place. Floor of 1 via Field(ge=1) — a 0/negative cap would either
    # reject every request or be silently ignored, both worse than a
    # configuration error at boot. Default 10 MiB: large enough for very
    # chatty conversation histories with many `texts` entries, small
    # enough that a runaway client can't exhaust process memory.
    max_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1)

    # ── Detector pipeline ──────────────────────────────────────────────────────
    # Comma-separated list of detector names. Order matters: `_dedup` keeps
    # the first-seen entity_type for any duplicate text, so the detector
    # listed first wins type-resolution conflicts. Examples:
    #   "regex"      → regex layer only (deterministic, no external deps)
    #   "llm"        → LLM layer only
    #   "regex,llm"  → both detectors run in parallel; regex types win
    # Whitespace tolerated; duplicates collapsed; unknown names warn-and-skip.
    detector_mode: str = "regex,llm"

    # ── Surrogate generator ────────────────────────────────────────────────────
    # Faker locale(s) used by the surrogate generator. Empty → Faker's own
    # default (en_US). Single locale ("pt_BR") or comma-separated list
    # ("pt_BR,en_US") with the first one preferred and others used as
    # fallback for providers the primary doesn't implement. Invalid
    # locales fail at startup, not at first request.
    faker_locale: str = ""
    # When false, every surrogate is an opaque deterministic placeholder
    # (e.g. `[PERSON_7F3A2B]`) instead of a realistic Faker substitute.
    # Useful when the upstream model misinterprets realistic surrogates as
    # real data, or when realism would mislead a downstream tool. Faker is
    # not even instantiated when disabled.
    use_faker: bool = True
    # Cap on the surrogate generator's process-wide LRU cache. Each entry is
    # ~150–300 bytes (entity type + original text + surrogate + bookkeeping),
    # so 100k entries ≈ 30 MB worst case. The cache backs cross-request
    # surrogate consistency for multi-turn conversations — set higher if
    # you have very chatty users; lower (or zero, effectively disabling)
    # if memory is tight and per-request consistency is enough.
    surrogate_cache_max_size: int = 100_000
    # Cap on the per-locale Faker instance cache (separate from the
    # surrogate cache above). Each Faker is a few MB resident (provider
    # dictionaries); without a bound, callers cycling distinct
    # `faker_locale` overrides could grow memory unboundedly. Floor of 1
    # via Field(ge=1) so a typoed 0/negative value can't disable the
    # cache entirely (would force a fresh Faker per call → ~1–2 ms each).
    surrogate_faker_lru_max: int = Field(default=32, ge=1)
    # Secret keying material mixed into the blake2b hashes the surrogate
    # generator uses (both the Faker seed and the opaque-token digest).
    # Empty (the default) → a fresh 16-byte random salt is generated each
    # time the process starts. Set to a fixed string to keep surrogates
    # stable across restarts (useful for log-correlation analysis, but
    # weakens privacy: the salt becomes ambient and attackers who learn
    # it can brute-force low-entropy entities like IPs and phone numbers).
    surrogate_salt: str = ""

    # ── Vault (call_id → mapping) ──────────────────────────────────────────────
    vault_ttl_s: int = 600
    # Hard cap on vault entries. TTL-only eviction fires lazily on put(), so a
    # burst of unique call_ids without matching post_calls (client crashes,
    # timeouts, malicious flood) can grow the vault to millions of entries
    # before TTL clears them. The cap forces LRU eviction on overflow as a
    # backstop. Each entry is a small dict of surrogate→original strings
    # (~hundreds of bytes typical), so 10k entries ≈ a few MB worst case.
    # Raise if your traffic legitimately holds many in-flight call_ids
    # simultaneously (very chatty conversations, long-running streams).
    vault_max_entries: int = 10_000

    @field_validator("detector_mode", mode="after")
    @classmethod
    def _lower_detector_mode(cls, v: str) -> str:
        # The pipeline parses this into individual names downstream and
        # case-folds there too, but normalizing once here keeps the
        # `detector_mode` field readable in /health output regardless of
        # how the operator capitalised the env var.
        return v.lower()


config = Config()
