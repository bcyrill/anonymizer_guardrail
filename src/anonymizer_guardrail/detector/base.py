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
        "IPV4_ADDRESS",
        "IPV6_ADDRESS",
        "IPV4_CIDR",
        "IPV6_CIDR",
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
        "MAC_ADDRESS",
        "URL",
        "PATH",
        "IDENTIFIER",
        "ADDRESS",
        "CREDIT_CARD",
        "DATE_OF_BIRTH",
        "IBAN",
        "NATIONAL_ID",
        "OTHER",
    }
)


# Entity types where trailing `.,;!?` can be STRUCTURALLY part of the
# value, so the trim below must NOT touch them. Examples:
#   - CREDENTIAL: passwords like "hunter2!" or "P@ssw0rd?"
#   - TOKEN:      opaque, no guarantees about character set
#   - PATH:       trailing `.` may signal a directory or hidden file
#   - IDENTIFIER: opaque catch-all bucket
#   - OTHER:      unknown semantics — safer to leave verbatim
# Every other type is "natural-text" enough that a trailing period /
# comma / etc. is almost always sentence punctuation pulled in by the
# detector, never part of the entity itself.
_DO_NOT_TRIM_TYPES: frozenset[str] = frozenset(
    {"CREDENTIAL", "TOKEN", "PATH", "IDENTIFIER", "OTHER"}
)


@dataclass(frozen=True)
class Match:
    """A single detected sensitive substring."""

    text: str
    entity_type: str

    def __post_init__(self) -> None:
        # Normalize unknown types to OTHER first — the trim decision
        # below depends on the canonical type.
        if self.entity_type not in ENTITY_TYPES:
            object.__setattr__(self, "entity_type", "OTHER")
        # Trim trailing sentence punctuation that detectors sometimes pull
        # into spans (the LLM and NER paths especially — regex patterns
        # mostly anchor with \b already, so this is a no-op for them).
        # Skipped for types listed in _DO_NOT_TRIM_TYPES, where the
        # punctuation can legitimately be part of the value.
        if self.entity_type not in _DO_NOT_TRIM_TYPES:
            trimmed = self.text.rstrip(".,;!?")
            if trimmed != self.text:
                object.__setattr__(self, "text", trimmed)


class Detector(Protocol):
    """A detection layer that returns sensitive substrings found in a text.

    The base contract is intentionally narrow: pure (text → matches). Detectors
    that need extra inputs (e.g. LLMDetector accepts a per-call `api_key` for
    forwarded credentials) declare those on their own concrete signature, and
    the Pipeline type-narrows with isinstance checks before passing them.
    """

    name: str

    async def detect(self, text: str) -> list[Match]: ...
