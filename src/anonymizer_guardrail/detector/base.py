"""Shared types for the detector layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


# Canonical entity types. Detectors should use these where possible so the
# surrogate generator can pick a sensible substitute. Unknown types fall back
# to opaque tokens.
ENTITY_TYPES = frozenset(
    {
        "PERSON",
        "ORGANIZATION",
        "EMAIL_ADDRESS",
        "IP_ADDRESS",
        "CIDR",
        "HOSTNAME",
        "DOMAIN",
        "USERNAME",
        "CREDENTIAL",
        "TOKEN",
        "HASH",
        "UUID",
        "AWS_ACCESS_KEY",
        "JWT",
        "PHONE",
        "OTHER",
    }
)


@dataclass(frozen=True)
class Match:
    """A single detected sensitive substring."""

    text: str
    entity_type: str

    def __post_init__(self) -> None:
        # Normalize unknown types to OTHER so downstream code never has to guard.
        if self.entity_type not in ENTITY_TYPES:
            object.__setattr__(self, "entity_type", "OTHER")


class Detector(Protocol):
    """A detection layer that returns sensitive substrings found in a text."""

    name: str

    async def detect(self, text: str, *, api_key: str | None = None) -> list[Match]: ...
