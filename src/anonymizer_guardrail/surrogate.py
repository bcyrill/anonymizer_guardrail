"""
Surrogate generator.

Each detected entity is replaced with a *realistic* substitute of the same
type, so the upstream LLM's reasoning quality survives anonymization
("acmecorp.local" → "quasarware.local", not "[ORGANIZATION_7F3A2B]").

Determinism guarantees, in order of strictness:
  * **Within a single request**: the same `(original, entity_type)` always
    maps to the same surrogate. Hard-guaranteed regardless of cache state —
    the pipeline keeps its own per-call mapping.
  * **Across requests, while the entry stays in the LRU cache**: the same
    pair maps to the same surrogate. Backs cross-request consistency for
    multi-turn conversations.
  * **After the entry has been evicted from the LRU**: a fresh request may
    produce a *different* surrogate for the same input. Bounded by
    SURROGATE_CACHE_MAX_SIZE — raise the cap if your conversations are
    long enough to outlive default eviction.
  * **Across process restarts**: by default surrogates change after a
    restart because we mix a process-random salt into the blake2b keys.
    Set SURROGATE_SALT to a fixed value to keep surrogates stable across
    restarts; see _resolve_salt below for the privacy trade-off.

Privacy rationale for the salt: without it, the opaque-token surrogate
literally IS a hash of the original (e.g. `[IP_ADDRESS_…]`). For low-
entropy entity types — IPs, phones, MACs, names from a known list — an
attacker with access to surrogates (model-provider logs, LiteLLM logs,
etc.) can brute-force candidate inputs offline and match the hash.
Mixing in a per-process secret defeats that attack: the attacker would
need the salt to compute candidate hashes, and the salt never leaves
process memory unless the operator opts into a stable SURROGATE_SALT.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
from collections import OrderedDict
from datetime import date
from typing import Callable

from babel.core import UnknownLocaleError
from babel.dates import format_date
from faker import Faker

from .config import config
from .detector.base import Match

log = logging.getLogger("anonymizer.surrogate")


def _format_dob(faker: Faker, dob: date) -> str:
    """Render a date_of_birth in the locale's medium-form date convention.

    Faker returns a `datetime.date`; using `.isoformat()` would always
    give ISO 8601 (`1985-04-15`) regardless of locale, which looks out
    of place when the rest of the surrogate (name, address, phone) is
    locale-shaped. Babel's CLDR data knows that de_CH wants `15.02.1985`,
    en_US wants `Feb 15, 1985`, ja_JP wants `1985/02/15`, etc.

    Falls back to ISO if Babel doesn't recognise the configured locale —
    we'd rather emit a reasonable date than crash the surrogate path.
    """
    locales = getattr(faker, "locales", None) or ["en_US"]
    primary = locales[0]
    try:
        return format_date(dob, format="medium", locale=primary)
    except UnknownLocaleError:
        return dob.isoformat()

# blake2b accepts a key up to 64 bytes — anything longer is rejected at
# hash construction. We truncate to be safe.
_MAX_BLAKE2B_KEY_LEN = 64


def _resolve_salt(raw: str) -> bytes:
    """Return blake2b key bytes for the surrogate hashes.

    Empty `raw` → fresh 16 bytes from `secrets.token_bytes`. After process
    restart, the salt is different and surrogates change accordingly,
    breaking offline brute-force of low-entropy entity types (IPs, phones,
    MACs, names from a known list).

    Non-empty `raw` → the literal string, UTF-8-encoded and truncated to
    64 bytes. Stable across restarts; useful when an operator wants to
    correlate surrogates over time, at the cost of letting an attacker
    who learns the salt brute-force the same low-entropy entities.
    """
    if raw:
        return raw.encode("utf-8")[:_MAX_BLAKE2B_KEY_LEN]
    return secrets.token_bytes(16)


def _parse_locales(raw: str) -> list[str] | None:
    """Convert FAKER_LOCALE into the form Faker expects (or None for default)."""
    cleaned = [s.strip() for s in raw.split(",") if s.strip()]
    return cleaned or None


def _seed_for(text: str, entity_type: str, salt: bytes) -> int:
    """Stable 64-bit seed derived from the original string + type, keyed by
    the per-process salt. Same input → same seed within a process; different
    input or different process (with default random salt) → different seed.
    """
    h = hashlib.blake2b(
        f"{entity_type}:{text}".encode(),
        key=salt,
        digest_size=8,
    ).digest()
    return int.from_bytes(h, "big")


def _opaque(prefix: str, salt: bytes) -> Callable[[Faker, str], str]:
    """Generator that emits a non-realistic but deterministic token.

    Used for things where realistic surrogates would be misleading (hashes,
    JWTs, raw credentials) — the upstream model doesn't need them to look
    real, only to be consistent. The blake2b key prevents an attacker with
    only the surrogate from inverting it via brute-force of plausible
    inputs (which is otherwise feasible for low-entropy entities).
    """

    def gen(_fake: Faker, original: str) -> str:
        # 8 hex chars from a fresh keyed hash (independent of the seeded
        # Faker so collisions across types stay unlikely).
        digest = hashlib.blake2b(
            original.encode(),
            key=salt,
            digest_size=4,
        ).hexdigest().upper()
        return f"[{prefix}_{digest}]"

    return gen


# Single source of truth for both Faker-mode and opaque-mode generators.
# A value of None means this type is always opaque (e.g. credentials,
# hashes, paths — where a realistic substitute would mislead). The opaque
# prefix is just the entity type name, so adding a new type only requires
# editing this one table.
_FakerGen = Callable[[Faker, str], str]
_GENERATOR_SPEC: dict[str, _FakerGen | None] = {
    "PERSON":         lambda f, _o: f.name(),
    "ORGANIZATION":   lambda f, _o: f.company(),
    "EMAIL_ADDRESS":  lambda f, _o: f.email(),
    "IPV4_ADDRESS":   lambda f, _o: f.ipv4_public(),
    "IPV6_ADDRESS":   lambda f, _o: f.ipv6(),
    "IPV4_CIDR":      lambda f, _o: f"{f.ipv4_public()}/24",
    # /64 is the standard end-site allocation in IPv6 land — picking one
    # consistent length keeps surrogates readable and matches what most
    # real-world configs ship with.
    "IPV6_CIDR":      lambda f, _o: f"{f.ipv6()}/64",
    "HOSTNAME":       lambda f, _o: f.hostname(),
    "DOMAIN":         lambda f, _o: f.domain_name(),
    "USERNAME":       lambda f, _o: f.user_name(),
    "PHONE":          lambda f, _o: f.phone_number(),
    "UUID":           lambda f, _o: f.uuid4(),
    "MAC_ADDRESS":    lambda f, _o: f.mac_address(),
    "URL":            lambda f, _o: f.url(),
    # Faker's address() returns multi-line ("street\ncity, state zip");
    # collapse to single line so the surrogate doesn't introduce stray
    # newlines into whatever text we're anonymizing.
    "ADDRESS":        lambda f, _o: f.address().replace("\n", ", "),
    "CREDIT_CARD":    lambda f, _o: f.credit_card_number(),
    # ISO-format date string keeps the substitution unambiguous; Faker's
    # date_of_birth() returns a datetime.date object that we'd otherwise
    # have to coerce.
    "DATE_OF_BIRTH":  lambda f, _o: _format_dob(f, f.date_of_birth()),
    "IBAN":           lambda f, _o: f.iban(),
    # Locale-aware: en_US → SSN, pt_BR → CPF, etc. Operators set
    # FAKER_LOCALE to match their data.
    "NATIONAL_ID":    lambda f, _o: f.ssn(),
    # Always-opaque types: realism would mislead. Token/path varieties are
    # too broad for a single Faker provider to substitute meaningfully.
    "CREDENTIAL":     None,
    "TOKEN":          None,
    "HASH":           None,
    "JWT":            None,
    "AWS_ACCESS_KEY": None,
    "PATH":           None,
    "IDENTIFIER":     None,
    "OTHER":          None,
}


def _build_generators(use_faker: bool, salt: bytes) -> dict[str, _FakerGen]:
    """Materialize either the Faker-backed table (USE_FAKER=true) or the
    all-opaque one (USE_FAKER=false) from the single _GENERATOR_SPEC.
    The salt is mixed into every opaque generator's blake2b key."""
    out: dict[str, _FakerGen] = {}
    for etype, faker_fn in _GENERATOR_SPEC.items():
        if use_faker and faker_fn is not None:
            out[etype] = faker_fn
        else:
            out[etype] = _opaque(etype, salt)
    return out


# Total attempts (1 natural pass at attempt=0, plus 3 salted retries at
# attempt=1..3). Three salted retries is plenty: collisions are rare to
# begin with, and the salted-seed/salted-text path is independent of the
# original.
_MAX_COLLISION_ATTEMPTS = 4
# Golden-ratio-derived constant. Used to perturb the seed across retries
# so each attempt explores a different Faker output.
_SALT_MULTIPLIER = 0x9E3779B97F4A7C15


_CacheKey = tuple[str, str, bool, tuple[str, ...] | None]
"""Surrogate cache key: (entity_type, text, use_faker, locale_tuple).
The trailing two slots encode the per-request overrides — None means
"use the configured default", which keeps default-traffic entries in
their own bucket and avoids cache-key churn on existing deployments."""


class SurrogateGenerator:
    """Process-wide cache of original → surrogate mappings."""

    def __init__(self) -> None:
        # OrderedDict so we can implement LRU eviction in O(1):
        #   - on hit: move_to_end(key) marks it most-recently-used
        #   - on overflow: popitem(last=False) drops least-recently-used
        # Cap is configurable via SURROGATE_CACHE_MAX_SIZE.
        self._cache: OrderedDict[_CacheKey, str] = OrderedDict()
        # Sane minimum: a request can produce hundreds of unique entities,
        # so we don't want zero-or-tiny caps to silently cripple within-
        # request consistency. The pipeline still de-dups within a single
        # request via its own dict, so this is purely about cross-request
        # consistency — but we keep the floor as a guard against typos.
        self._max_cache_size = max(1, int(config.surrogate_cache_max_size))
        # Surrogate values already issued, for collision detection.
        # Kept in sync with _cache.values() under the same lock — eviction
        # removes from BOTH so a surrogate "freed" by LRU can be re-issued.
        self._used_surrogates: set[str] = set()
        # The lock guards the cache, the used-surrogates set, AND the
        # shared Faker instance: `seed_instance + gen()` is a critical
        # section that must not be interleaved by another caller (or one
        # match's seed would bleed into another's output). Sub-millisecond
        # hold time, so contention under realistic concurrency is invisible.
        self._lock = threading.Lock()
        self._use_faker = bool(config.use_faker)
        self._locales = _parse_locales(config.faker_locale)
        # Random per-process key by default; the operator can pin a stable
        # value via SURROGATE_SALT for log-correlation use cases.
        self._salt = _resolve_salt(config.surrogate_salt)
        self._generators = _build_generators(self._use_faker, self._salt)
        # Always instantiate a default Faker — even when config.use_faker
        # is False — so a per-request `use_faker=true` override has a
        # working instance to fall back on without needing a different
        # locale. Construction is ~1–2 ms (one-time at startup) and
        # negligible memory; the saved code path is "the override always
        # works, regardless of config default."
        # An invalid FAKER_LOCALE fails loud at boot only when the
        # operator actually asked for Faker mode. Under USE_FAKER=false
        # the locale was never the operator's stated intent, so we log
        # and leave self._fake unset; per-request use_faker=true
        # overrides will fall back to opaque output for that call.
        self._fake: Faker | None = None
        try:
            self._fake = Faker(self._locales)
        except (AttributeError, ValueError, ModuleNotFoundError) as exc:
            if self._use_faker:
                raise RuntimeError(
                    f"FAKER_LOCALE={config.faker_locale!r} is not a valid Faker "
                    f"locale: {exc}. See https://faker.readthedocs.io/ for the list."
                ) from exc
            log.warning(
                "FAKER_LOCALE=%r is not a valid Faker locale (%s). USE_FAKER "
                "is false so this is non-fatal at startup, but per-request "
                "use_faker=true overrides won't be able to fall back to it.",
                config.faker_locale, exc,
            )
        # Cache of Faker instances keyed by locale tuple, populated on
        # demand by per-request overrides. The default instance lives
        # in self._fake (above); this OrderedDict only holds the
        # alternates so we don't rebuild a Faker on every request.
        #
        # LRU-bounded: Faker instances are a few MB each (loaded
        # provider dictionaries per locale) — without a bound a caller
        # cycling through many distinct locale tuples would grow this
        # without limit, an obvious OOM vector. The cap is small (a
        # handful of distinct locale chains is realistic; on overflow
        # the oldest is dropped and reconstructed on next use, ~1–2 ms).
        self._faker_by_locale: OrderedDict[tuple[str, ...], Faker] = OrderedDict()

    def cache_stats(self) -> tuple[int, int]:
        """Return (current_size, max_size). Read without the lock —
        len() on a CPython dict is atomic, and max_size is immutable
        after __init__. Used by Pipeline.stats() for the /health probe."""
        return len(self._cache), self._max_cache_size

    def faker_cache_keys(self) -> tuple[tuple[str, ...], ...]:
        """Snapshot of the locale tuples currently cached in the
        per-locale Faker LRU. Read-only inspection accessor — callers
        get a tuple-of-tuples copy so they can't mutate the underlying
        OrderedDict.

        Existed so tests can verify LRU-eviction behaviour without
        reaching into `_faker_by_locale` directly. Operators rarely
        need this; the cap (`SURROGATE_FAKER_LRU_MAX`) bounds memory,
        and an evicted Faker is reconstructible on next use at
        ~1–2 ms cost. No `/health` integration today."""
        return tuple(self._faker_by_locale.keys())

    def _resolve_faker_locked(
        self, locale_tuple: tuple[str, ...] | None, use_faker: bool
    ) -> Faker | None:
        """Return the Faker instance to use for one for_match call.

        Caller MUST hold `self._lock`. The lock is shared with the
        surrogate cache because (a) `move_to_end` + `popitem(last=False)`
        on the per-locale OrderedDict aren't thread-atomic on their own,
        and (b) the resolved Faker is then immediately used inside the
        same critical section for `seed_instance + gen()`, which has its
        own non-interleaving requirement. Sharing one lock keeps both
        invariants under the same hold.

        - use_faker=False → no Faker (opaque-token gens); return None.
        - locale_tuple is None → the configured default (self._fake), if any.
        - Otherwise → look up / build a Faker for that exact locale tuple,
          cached LRU so repeated overrides reuse the instance instead of
          paying ~1–2 ms construction cost per call. Cap is
          `config.surrogate_faker_lru_max`; on overflow the least-recently-
          used Faker is dropped (it gets rebuilt on demand if seen again).

        An invalid locale logs a warning and falls back to the default —
        same warn-and-ignore policy as the other per-request overrides.
        """
        if not use_faker:
            return None
        if locale_tuple is None:
            return self._fake
        cached = self._faker_by_locale.get(locale_tuple)
        if cached is not None:
            self._faker_by_locale.move_to_end(locale_tuple)
            return cached
        try:
            f = Faker(list(locale_tuple))
        except (AttributeError, ValueError, ModuleNotFoundError) as exc:
            log.warning(
                "Override faker_locale=%s is not a valid Faker locale (%s); "
                "falling back to the configured default.",
                locale_tuple, exc,
            )
            return self._fake
        self._faker_by_locale[locale_tuple] = f
        # LRU eviction. Faker instances aren't tiny — bounding the cache
        # is the OOM-safety net against callers cycling distinct locale
        # tuples. Eviction is essentially free here (Python just drops
        # the reference; no I/O, no provider unloading).
        while len(self._faker_by_locale) > config.surrogate_faker_lru_max:
            self._faker_by_locale.popitem(last=False)
        return f

    def for_match(
        self,
        match: Match,
        *,
        use_faker: bool | None = None,
        locale: tuple[str, ...] | None = None,
    ) -> str:
        """Return a stable surrogate for this match.

        `use_faker` and `locale` are per-call overrides; None means
        "use the configured default" and bucket into the default cache
        slot. Overridden values get their own slot in the cache, keyed
        by (entity_type, text, use_faker, locale) — each unique combo
        is consistent both within and across requests.
        """
        # Resolve to concrete values so the cache key is always
        # canonical (avoids collisions where one caller passes None
        # and another passes the matching default).
        effective_use_faker = self._use_faker if use_faker is None else bool(use_faker)
        # locale is meaningful only when use_faker is True; with opaque
        # gens the locale doesn't change the output, so we collapse to
        # None so all opaque-mode calls share a cache slot.
        effective_locale: tuple[str, ...] | None
        if effective_use_faker:
            effective_locale = locale if locale is not None else None
        else:
            effective_locale = None

        # Override resolution path: when we got a use_faker override,
        # fall back from default _generators to a per-call generator
        # set. Cheap to build (just dict comprehension over _GENERATOR_SPEC).
        if use_faker is None:
            generators = self._generators
        else:
            generators = _build_generators(effective_use_faker, self._salt)

        key: _CacheKey = (
            match.entity_type, match.text, effective_use_faker, effective_locale,
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                # Mark this entry as most-recently-used so a busy entity
                # survives eviction churn from one-shot lookups. Cache hits
                # skip Faker resolution entirely — saves a dict lookup
                # plus the locale-tuple LRU bookkeeping on the hot path.
                self._cache.move_to_end(key)
                return cached

            # Faker resolution must run under the lock: the per-locale
            # LRU's move_to_end + popitem aren't thread-atomic on their
            # own, and the resolved Faker is then used immediately
            # inside this critical section for seed_instance + gen().
            fake = self._resolve_faker_locked(effective_locale, effective_use_faker)

            seed = _seed_for(match.text, match.entity_type, self._salt)
            gen = generators.get(match.entity_type, generators["OTHER"])

            # Try the natural surrogate first. Salt-retry on collision —
            # either with the original itself, or with a surrogate already
            # issued for some other entity in this process. Without this,
            # two distinct originals could share a surrogate, and the vault
            # (keyed by surrogate→original) would lose one of them.
            #
            # Salting strategy: change BOTH the seed (for Faker-backed gens)
            # AND the input text (for opaque gens). Faker types respond to
            # the reseed; opaque types respond to the text-salt. Doing both
            # is harmless overlap and lets us share one retry loop.
            surrogate: str | None = None
            for attempt in range(_MAX_COLLISION_ATTEMPTS):
                attempt_seed = seed if attempt == 0 else seed ^ (attempt * _SALT_MULTIPLIER)
                attempt_text = match.text if attempt == 0 else f"{match.text}#{attempt}"
                if fake is not None:
                    fake.seed_instance(attempt_seed)
                    candidate = gen(fake, attempt_text)
                else:
                    candidate = gen(None, attempt_text)  # type: ignore[arg-type]
                if candidate != match.text and candidate not in self._used_surrogates:
                    surrogate = candidate
                    break

            if surrogate is None:
                # All retries collided — extraordinarily unlikely, but bound
                # the worst case with a guaranteed-unique opaque token. The
                # 64-bit seed is derived from (entity_type, text), so two
                # distinct entities would need a blake2b collision to land
                # here on the same value (~2⁻⁶⁴).
                surrogate = f"[{match.entity_type}_{seed:016x}]"

            self._cache[key] = surrogate
            self._used_surrogates.add(surrogate)
            # LRU eviction: if we're over capacity, drop the oldest entry
            # and free its surrogate value for future re-use. Note this
            # weakens the cross-request consistency invariant for evicted
            # entries — same input may hash to a different surrogate after
            # eviction. Within-request consistency is unaffected because
            # the pipeline de-dups via its own per-request mapping.
            while len(self._cache) > self._max_cache_size:
                _, evicted_surrogate = self._cache.popitem(last=False)
                self._used_surrogates.discard(evicted_surrogate)
            return surrogate


__all__ = ["SurrogateGenerator"]
