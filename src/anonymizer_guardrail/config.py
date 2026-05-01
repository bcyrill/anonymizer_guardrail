"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _llm_fail_closed_default() -> bool:
    """Read LLM_FAIL_CLOSED, fall back to the deprecated FAIL_CLOSED with
    a loud warning, default true.

    `FAIL_CLOSED` was renamed to `LLM_FAIL_CLOSED` once the privacy-filter
    detector got its own `PRIVACY_FILTER_FAIL_CLOSED` knob — the old name
    misled operators into thinking it governed both. Existing deployments
    setting `FAIL_CLOSED=…` keep working for one release cycle, but get a
    rename hint at boot so the migration is impossible to miss.
    """
    raw = os.getenv("LLM_FAIL_CLOSED")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    legacy = os.getenv("FAIL_CLOSED")
    if legacy is not None:
        logging.getLogger("anonymizer.config").warning(
            "FAIL_CLOSED is deprecated; rename to LLM_FAIL_CLOSED. "
            "It governs the LLM detector only — the privacy-filter "
            "detector has its own PRIVACY_FILTER_FAIL_CLOSED. The old "
            "name will be removed in a future release."
        )
        return legacy.strip().lower() in ("1", "true", "yes", "on")
    return True


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
    # Optional registry of NAMED alternative LLM prompts that callers can
    # opt into per-request via additional_provider_specific_params.llm_prompt.
    # Format: comma-separated `name=path` pairs, same syntax style as
    # DETECTOR_MODE / FAKER_LOCALE. Each path uses the same `bundled:NAME` /
    # filesystem-path conventions as LLM_SYSTEM_PROMPT_PATH. Entries are
    # validated at startup. Names referenced from a request that aren't in
    # this registry log a warning and fall back to the default prompt
    # (LLM_SYSTEM_PROMPT_PATH). The registry deliberately doesn't contain
    # a "default" entry — that's reserved for LLM_SYSTEM_PROMPT_PATH so
    # configuring a registry doesn't change the no-override behaviour.
    # Example:
    #   LLM_SYSTEM_PROMPT_REGISTRY="pentest=bundled:llm_pentest.md,legal=/etc/anon/legal.md"
    llm_system_prompt_registry: str = os.getenv("LLM_SYSTEM_PROMPT_REGISTRY", "")
    # Path to the YAML file defining the regex detector's patterns. If unset,
    # the bundled default (patterns/regex_default.yaml) is used. Pre-built
    # alternatives also ship with the package (e.g. patterns/regex_pentest.yaml)
    # — point this at any of them or at your own file.
    regex_patterns_path: str = os.getenv("REGEX_PATTERNS_PATH", "")
    # Same registry mechanism for regex pattern files. See
    # llm_system_prompt_registry above for the format and semantics.
    # Example:
    #   REGEX_PATTERNS_REGISTRY="pentest=bundled:regex_pentest.yaml,internal=/etc/anon/internal.yaml"
    regex_patterns_registry: str = os.getenv("REGEX_PATTERNS_REGISTRY", "")
    # Path to the YAML file defining the denylist detector's literal-string
    # entries. If unset, the detector still loads under DETECTOR_MODE=denylist
    # but matches nothing — useful for boot order independence (operator
    # can set DETECTOR_MODE first and DENYLIST_PATH later). Accepts the
    # same `bundled:NAME` / filesystem-path conventions as REGEX_PATTERNS_PATH.
    denylist_path: str = os.getenv("DENYLIST_PATH", "")
    # Optional registry of NAMED alternative denylists that callers can
    # opt into per-request via additional_provider_specific_params.denylist.
    # Same format as REGEX_PATTERNS_REGISTRY / LLM_SYSTEM_PROMPT_REGISTRY:
    # comma-separated `name=path` pairs. Each path uses the same
    # `bundled:NAME` / filesystem-path conventions as DENYLIST_PATH.
    # The default lives in DENYLIST_PATH; the registry never contains
    # a "default" entry.
    # Example:
    #   DENYLIST_REGISTRY="legal=/etc/anon/legal-deny.yaml,marketing=/etc/anon/marketing-deny.yaml"
    denylist_registry: str = os.getenv("DENYLIST_REGISTRY", "")
    # Matching backend for the denylist detector:
    #   - "regex" (default) — Python `re` alternation. Pure stdlib, fast
    #                          for low-thousands of entries.
    #   - "aho"             — Aho-Corasick via pyahocorasick. Sub-linear
    #                          in pattern count; pays off above ~5k entries
    #                          or when match latency matters more than
    #                          install size. Requires the
    #                          `denylist-aho` optional extra.
    # Validated at startup; an unknown value crashes loud.
    denylist_backend: str = os.getenv("DENYLIST_BACKEND", "regex").strip().lower()
    # How the regex detector resolves overlapping matches between patterns:
    #   - "longest"  pick the longest match span (default). Robust against
    #                a child YAML's narrow pattern shadowing a parent's
    #                wider pattern — e.g. the pentest set's
    #                `\b\d{12}\b` (AWS Account ID) matching the trailing
    #                group of a UUID and squeezing out the UUID pattern.
    #   - "priority" first pattern in YAML order wins. Useful when an
    #                operator deliberately orders patterns most-specific-
    #                first and wants that ordering to be load-bearing.
    # Validated at startup; a typo crashes loudly rather than silently
    # falling through to a default.
    regex_overlap_strategy: str = os.getenv("REGEX_OVERLAP_STRATEGY", "longest").lower()
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
    # Hard cap on input size sent to the LLM in one call. Inputs above this
    # are REFUSED (LLMUnavailableError → FAIL_CLOSED policy applies), never
    # silently truncated — truncating in a guardrail would let everything past
    # this length through unscanned. Default is generous (~50K tokens), well
    # within typical small-model context windows; tune to your model.
    llm_max_chars: int = _env_int("LLM_MAX_CHARS", 200_000)

    # Max number of concurrent LLM calls.
    llm_max_concurrency: int = _env_int("LLM_MAX_CONCURRENCY", 10)

    # ── Privacy-filter detector (in-process vs remote) ─────────────────────────
    # Empty (default) → DETECTOR_MODE=privacy_filter loads the model
    # in-process (PrivacyFilterDetector). Pulls in torch + transformers
    # + ~6 GB of weights, so the image flavour matters (slim doesn't
    # ship them; pf / pf-baked do).
    #
    # Set to an HTTP URL → the detector becomes RemotePrivacyFilterDetector
    # and posts to {URL}/detect on every request. The standalone
    # privacy-filter-service container (see services/privacy_filter/)
    # is the canonical other end. Lets the slim guardrail image cover
    # the privacy_filter detector and lets multiple guardrail replicas
    # share one inference service.
    privacy_filter_url: str = os.getenv("PRIVACY_FILTER_URL", "").strip()
    # Per-call timeout on the remote detector's HTTP requests. Inference
    # for openai/privacy-filter is ~hundreds of ms on CPU; the default
    # leaves plenty of headroom for first-request load and queueing.
    privacy_filter_timeout_s: int = _env_int("PRIVACY_FILTER_TIMEOUT_S", 30)
    # Failure mode for the privacy_filter detector — mirrors FAIL_CLOSED
    # (which governs the LLM detector). When the privacy_filter service
    # is unreachable, times out, or returns non-200:
    #   true (default)  → block the request. The operator chose to
    #                     include privacy_filter for a reason; silently
    #                     dropping its coverage on a transient HTTP
    #                     hiccup is the same kind of footgun the LLM's
    #                     fail-closed default guards against.
    #   false           → log the error and proceed with coverage from
    #                     the other detectors (regex, denylist, llm).
    # The two flags are independent — operators can fail closed on
    # the LLM but open on PF (or vice versa) without coupling.
    # Affects both the in-process and remote detectors: the pipeline's
    # catch-all re-raises a generic PF crash as
    # PrivacyFilterUnavailableError when this is true.
    privacy_filter_fail_closed: bool = _env_bool("PRIVACY_FILTER_FAIL_CLOSED", True)

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

    # ── Failure mode ───────────────────────────────────────────────────────────
    # If the LLM detector errors out, do we proceed with regex-only results
    # (fail_open) or block the request (fail_closed)? LiteLLM has its own
    # `unreachable_fallback` for when WE are down; this controls our internal
    # behaviour when our LLM dependency is down. Independent from
    # `privacy_filter_fail_closed` (above) — operators can fail closed on
    # one detector and open on the other.
    # Backward compat: `FAIL_CLOSED` was the old name for this env var,
    # before the privacy-filter detector got its own fail-mode flag. The
    # default-resolver below honours `FAIL_CLOSED` with a deprecation
    # warning if the new name is unset.
    llm_fail_closed: bool = _llm_fail_closed_default()


config = Config()
