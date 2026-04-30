"""
Surrogate generator.

Each detected entity is replaced with a *realistic* substitute of the same
type, so the upstream LLM's reasoning quality survives anonymization
("acmecorp.local" → "quasarware.local", not "[ORGANIZATION_7F3A2B]").

Determinism: the same `(original, entity_type)` pair always maps to the same
surrogate within a process. This matters for multi-turn conversations and for
the same entity appearing in multiple texts of one request.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Callable

from faker import Faker

from .config import config
from .detector.base import Match


def _parse_locales(raw: str) -> list[str] | None:
    """Convert FAKER_LOCALE into the form Faker expects (or None for default)."""
    cleaned = [s.strip() for s in raw.split(",") if s.strip()]
    return cleaned or None


def _seed_for(text: str, entity_type: str) -> int:
    """Stable 64-bit seed derived from the original string + type."""
    h = hashlib.blake2b(f"{entity_type}:{text}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big")


def _opaque(prefix: str) -> Callable[[Faker, str], str]:
    """Generator that emits a non-realistic but deterministic token.

    Used for things where realistic surrogates would be misleading (hashes,
    JWTs, raw credentials) — the upstream model doesn't need them to look
    real, only to be consistent.
    """

    def gen(_fake: Faker, original: str) -> str:
        # 8 hex chars from a fresh hash (independent of the seeded Faker so
        # collisions across types stay unlikely).
        digest = hashlib.blake2b(original.encode(), digest_size=4).hexdigest().upper()
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


def _build_generators(use_faker: bool) -> dict[str, _FakerGen]:
    """Materialize either the Faker-backed table (USE_FAKER=true) or the
    all-opaque one (USE_FAKER=false) from the single _GENERATOR_SPEC."""
    out: dict[str, _FakerGen] = {}
    for etype, faker_fn in _GENERATOR_SPEC.items():
        if use_faker and faker_fn is not None:
            out[etype] = faker_fn
        else:
            out[etype] = _opaque(etype)
    return out


# Cap on how many salted retries we attempt to find a non-colliding
# surrogate. Three retries is plenty: collisions are rare to begin with,
# and the salted-seed/salted-text path is independent of the original.
_MAX_COLLISION_RETRIES = 4
# Golden-ratio-derived constant. Used to perturb the seed across retries
# so each attempt explores a different Faker output.
_SALT_MULTIPLIER = 0x9E3779B97F4A7C15


class SurrogateGenerator:
    """Process-wide cache of original → surrogate mappings."""

    def __init__(self) -> None:
        # OrderedDict so we can implement LRU eviction in O(1):
        #   - on hit: move_to_end(key) marks it most-recently-used
        #   - on overflow: popitem(last=False) drops least-recently-used
        # Cap is configurable via SURROGATE_CACHE_MAX_SIZE.
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
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
        self._generators = _build_generators(self._use_faker)
        self._fake: Faker | None = None
        if self._use_faker:
            # Instantiate once and reuse — Faker construction is ~1–2 ms;
            # `seed_instance` on an existing instance is ~0.15 ms. With a
            # cache that dedupes by (text, type), this only matters when a
            # request brings many unique entities, but it's a free win.
            # An invalid locale also fails here at boot with a clear message
            # rather than at first request with a confusing AttributeError.
            try:
                self._fake = Faker(self._locales)
            except (AttributeError, ValueError, ModuleNotFoundError) as exc:
                raise RuntimeError(
                    f"FAKER_LOCALE={config.faker_locale!r} is not a valid Faker "
                    f"locale: {exc}. See https://faker.readthedocs.io/ for the list."
                ) from exc

    def for_match(self, match: Match) -> str:
        """Return a stable surrogate for this match."""
        key = (match.entity_type, match.text)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                # Mark this entry as most-recently-used so a busy entity
                # survives eviction churn from one-shot lookups.
                self._cache.move_to_end(key)
                return cached

            seed = _seed_for(match.text, match.entity_type)
            gen = self._generators.get(match.entity_type, self._generators["OTHER"])

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
            for attempt in range(_MAX_COLLISION_RETRIES):
                attempt_seed = seed if attempt == 0 else seed ^ (attempt * _SALT_MULTIPLIER)
                attempt_text = match.text if attempt == 0 else f"{match.text}#{attempt}"
                if self._fake is not None:
                    self._fake.seed_instance(attempt_seed)
                    candidate = gen(self._fake, attempt_text)
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
