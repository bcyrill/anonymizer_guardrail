"""
Regex detection layer.

Catches things with recognizable shapes: IPs, emails, hashes, tokens, well-known
secret prefixes. Patterns are intentionally conservative — high precision over
high recall, since the LLM layer covers contextual cases regex cannot.

Add patterns by editing _PATTERNS below. Each entry is (entity_type, regex).
"""

from __future__ import annotations

import re

from .base import Detector, Match

# Order matters where patterns might overlap on the same span: the *first*
# matching pattern wins for a given (start, end). Put more specific patterns
# above more general ones (e.g. CIDR before bare IP, AWS key before generic
# token shapes).
_PATTERNS: list[tuple[str, str]] = [
    # ── Cloud / SaaS secrets (very high precision) ─────────────────────────────
    ("AWS_ACCESS_KEY", r"\bAKIA[0-9A-Z]{16}\b"),
    ("TOKEN", r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"),                       # GitHub PAT
    ("TOKEN", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),                      # Slack
    ("TOKEN", r"\bsk-[A-Za-z0-9_-]{20,}\b"),                             # OpenAI-style
    ("JWT", r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),

    # ── Identifiers ────────────────────────────────────────────────────────────
    ("EMAIL_ADDRESS", r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    (
        "UUID",
        r"\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-"
        r"[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b",
    ),

    # ── Network ────────────────────────────────────────────────────────────────
    ("CIDR", r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b"),
    ("IP_ADDRESS", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    (
        "HOSTNAME",
        r"\b[a-zA-Z0-9][a-zA-Z0-9-]{0,62}"
        r"\.(?:local|internal|corp|lan|home|intranet)\b",
    ),

    # ── Hashes (anchor by length + word boundaries) ────────────────────────────
    # Order: longest first, so a SHA256 isn't truncated to a SHA1 prefix.
    ("HASH", r"\b[a-fA-F0-9]{64}\b"),  # SHA-256
    ("HASH", r"\b[a-fA-F0-9]{40}\b"),  # SHA-1
    ("HASH", r"\b[a-fA-F0-9]{32}\b"),  # MD5

    # ── Phone (loose; international or US) ─────────────────────────────────────
    ("PHONE", r"\b\+?\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b"),
]


class RegexDetector:
    """Compiled-regex detector. Stateless and synchronous; async only by interface."""

    name = "regex"

    def __init__(self) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = [
            (etype, re.compile(pat)) for etype, pat in _PATTERNS
        ]

    async def detect(self, text: str, *, api_key: str | None = None) -> list[Match]:
        if not text:
            return []
        del api_key  # Regex detection doesn't talk to any backend.

        # Span-based dedup: if two patterns hit overlapping ranges, keep the one
        # whose pattern appears earlier in _PATTERNS (i.e. the more specific).
        claimed: list[tuple[int, int]] = []
        results: list[Match] = []

        for entity_type, pattern in self._compiled:
            for m in pattern.finditer(text):
                start, end = m.span()
                if any(s < end and start < e for s, e in claimed):
                    continue
                claimed.append((start, end))
                results.append(Match(text=m.group(0), entity_type=entity_type))

        return results


__all__ = ["RegexDetector"]
