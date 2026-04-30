"""
Surrogate generator.

Each detected entity is replaced with a *realistic* substitute of the same
type, so the upstream LLM's reasoning quality survives anonymization
("acmecorp.local" → "quasarware.local", not "[ORG_7F3A2B]").

Determinism: the same `(original, entity_type)` pair always maps to the same
surrogate within a process. This matters for multi-turn conversations and for
the same entity appearing in multiple texts of one request.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Callable

from faker import Faker

from .detector.base import Match


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


# Generators run against a Faker instance that has already been seeded with the
# entity's stable seed, so calling e.g. fake.company() is deterministic.
_GENERATORS: dict[str, Callable[[Faker, str], str]] = {
    "PERSON":         lambda f, _o: f.name(),
    "ORGANIZATION":   lambda f, _o: f.company(),
    "EMAIL_ADDRESS":  lambda f, _o: f.email(),
    "IP_ADDRESS":     lambda f, _o: f.ipv4_public(),
    "CIDR":           lambda f, _o: f"{f.ipv4_public()}/24",
    "HOSTNAME":       lambda f, _o: f.hostname(),
    "DOMAIN":         lambda f, _o: f.domain_name(),
    "USERNAME":       lambda f, _o: f.user_name(),
    "PHONE":          lambda f, _o: f.phone_number(),
    "UUID":           lambda f, _o: f.uuid4(),
    "MAC_ADDRESS":    lambda f, _o: f.mac_address(),
    "URL":            lambda f, _o: f.url(),
    "CREDENTIAL":     _opaque("CRED"),
    "TOKEN":          _opaque("TOKEN"),
    "HASH":           _opaque("HASH"),
    "JWT":            _opaque("JWT"),
    "AWS_ACCESS_KEY": _opaque("AWS"),
    # PATH and IDENTIFIER are too varied for a "realistic" surrogate
    # (S3 buckets, ARNs, /opt paths, ADB serials all share the type)
    # so we use opaque placeholders rather than risk misleading the LLM.
    "PATH":           _opaque("PATH"),
    "IDENTIFIER":     _opaque("ID"),
    "OTHER":          _opaque("REDACTED"),
}


class SurrogateGenerator:
    """Process-wide cache of original → surrogate mappings."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def for_match(self, match: Match) -> str:
        """Return a stable surrogate for this match."""
        key = (match.entity_type, match.text)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        seed = _seed_for(match.text, match.entity_type)
        fake = Faker()
        fake.seed_instance(seed)
        gen = _GENERATORS.get(match.entity_type, _GENERATORS["OTHER"])
        surrogate = gen(fake, match.text)

        # If by astronomical luck the surrogate equals the original, salt and retry.
        if surrogate == match.text:
            surrogate = _opaque(match.entity_type)(fake, match.text)

        with self._lock:
            # Re-check under lock; another coroutine may have populated it.
            self._cache.setdefault(key, surrogate)
            return self._cache[key]


__all__ = ["SurrogateGenerator"]
