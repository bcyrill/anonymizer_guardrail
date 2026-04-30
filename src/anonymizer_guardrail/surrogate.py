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


# Generators run against a Faker instance that has already been seeded with the
# entity's stable seed, so calling e.g. fake.company() is deterministic.
_REALISTIC_GENERATORS: dict[str, Callable[[Faker, str], str]] = {
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

# All-opaque mode (USE_FAKER=false). Each type still gets a distinct prefix so
# the upstream model can tell categories apart, but no realistic substitutes
# are emitted — useful when realism would mislead a downstream tool, or when
# you want a hard guarantee that the model never sees Faker-generated names.
_OPAQUE_GENERATORS: dict[str, Callable[[Faker, str], str]] = {
    "PERSON":         _opaque("PERSON"),
    "ORGANIZATION":   _opaque("ORG"),
    "EMAIL_ADDRESS":  _opaque("EMAIL"),
    "IP_ADDRESS":     _opaque("IP"),
    "CIDR":           _opaque("CIDR"),
    "HOSTNAME":       _opaque("HOST"),
    "DOMAIN":         _opaque("DOMAIN"),
    "USERNAME":       _opaque("USER"),
    "PHONE":          _opaque("PHONE"),
    "UUID":           _opaque("UUID"),
    "MAC_ADDRESS":    _opaque("MAC"),
    "URL":            _opaque("URL"),
    "CREDENTIAL":     _opaque("CRED"),
    "TOKEN":          _opaque("TOKEN"),
    "HASH":           _opaque("HASH"),
    "JWT":            _opaque("JWT"),
    "AWS_ACCESS_KEY": _opaque("AWS"),
    "PATH":           _opaque("PATH"),
    "IDENTIFIER":     _opaque("ID"),
    "OTHER":          _opaque("REDACTED"),
}


class SurrogateGenerator:
    """Process-wide cache of original → surrogate mappings."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._use_faker = bool(config.use_faker)
        self._locales = _parse_locales(config.faker_locale)
        if self._use_faker:
            self._generators = _REALISTIC_GENERATORS
            # Instantiate once eagerly so a typo'd locale (e.g. "pr_BR" instead
            # of "pt_BR") fails at boot with a clear message, not at first
            # request with a confusing AttributeError from inside Faker.
            try:
                Faker(self._locales)
            except (AttributeError, ValueError, ModuleNotFoundError) as exc:
                raise RuntimeError(
                    f"FAKER_LOCALE={config.faker_locale!r} is not a valid Faker "
                    f"locale: {exc}. See https://faker.readthedocs.io/ for the list."
                ) from exc
        else:
            # USE_FAKER=false → no Faker instance is ever created.
            self._generators = _OPAQUE_GENERATORS

    def for_match(self, match: Match) -> str:
        """Return a stable surrogate for this match."""
        key = (match.entity_type, match.text)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        seed = _seed_for(match.text, match.entity_type)
        gen = self._generators.get(match.entity_type, self._generators["OTHER"])
        if self._use_faker:
            fake = Faker(self._locales)
            fake.seed_instance(seed)
            surrogate = gen(fake, match.text)
        else:
            # Opaque generators ignore the Faker arg, but the signature is
            # shared — pass None to avoid the per-call Faker construction.
            surrogate = gen(None, match.text)  # type: ignore[arg-type]

        # If by astronomical luck the surrogate equals the original, salt and
        # retry with a different opaque prefix derived from the entity type.
        if surrogate == match.text:
            surrogate = _opaque(match.entity_type)(None, match.text)  # type: ignore[arg-type]

        with self._lock:
            # Re-check under lock; another coroutine may have populated it.
            self._cache.setdefault(key, surrogate)
            return self._cache[key]


__all__ = ["SurrogateGenerator"]
