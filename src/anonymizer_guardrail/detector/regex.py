"""
Regex detection layer.

Catches things with recognizable shapes: IPs, emails, hashes, tokens, well-known
secret prefixes. Patterns are intentionally conservative — high precision over
high recall, since the LLM layer covers contextual cases regex cannot.

Patterns are loaded from a YAML file at startup. The bundled default lives
at `patterns/regex_default.yaml`; override with REGEX_PATTERNS_PATH. See the
schema documentation in the YAML files themselves.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..bundled_resource import (
    read_bundled,
    read_bundled_default,
    resolve_spec,
)
from ..registry import parse_named_path_registry
from .base import Match
from .spec import DetectorSpec

log = logging.getLogger("anonymizer.regex")


# ── Per-detector config ───────────────────────────────────────────────────
# Lives here (not in central config.py) so the env-var fields are
# colocated with the code that reads them. Tests can swap the module
# CONFIG via `monkeypatch.setattr(regex, "CONFIG", regex.CONFIG.model_copy(
# update={...}))`; the SPEC's `config` property reads the module
# attribute live.
class RegexConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REGEX_",
        extra="ignore",
        frozen=True,
    )

    # Path to the YAML file defining the regex detector's patterns. If
    # unset, the bundled default (patterns/regex_default.yaml) is used.
    # Pre-built alternatives also ship with the package
    # (e.g. patterns/regex_pentest.yaml) — point this at any of them
    # or at your own file.
    patterns_path: str = ""
    # Optional registry of NAMED alternative regex pattern files that
    # callers can opt into per-request via
    # additional_provider_specific_params.regex_patterns. Comma-separated
    # `name=path` pairs.
    patterns_registry: str = ""
    # How the regex detector resolves overlapping matches between patterns:
    #   - "longest"  pick the longest match span (default).
    #   - "priority" first pattern in YAML order wins.
    # Validated at startup; a typo crashes loudly rather than silently
    # falling through to a default.
    overlap_strategy: str = "longest"

    @field_validator("overlap_strategy", mode="after")
    @classmethod
    def _lower_overlap(cls, v: str) -> str:
        return v.lower()


CONFIG = RegexConfig()

# Map YAML flag names → re module flags. Restricted to the subset that's
# meaningful for detection (Python's UNICODE/LOCALE etc. are intentionally
# excluded; an operator who needs them can rewrite the patterns inline).
_FLAG_NAMES: dict[str, int] = {
    "IGNORECASE": re.IGNORECASE,
    "I": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "M": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "S": re.DOTALL,
    "VERBOSE": re.VERBOSE,
    "X": re.VERBOSE,
    "ASCII": re.ASCII,
    "A": re.ASCII,
}

_DEFAULT_PATTERNS_RELPATH = "patterns/regex_default.yaml"
_BUNDLED_PATTERNS_DIR = "patterns"
# Hard cap on how deep an `extends:` chain can go. Anything past this is
# almost certainly a config bug (or a deliberate cycle); fail loud rather
# than recurse forever.
_MAX_EXTENDS_DEPTH = 8


def _resolve_flags(raw: Any, source: str) -> int:
    """Convert a YAML flags entry (list[str]) into a single re-flag bitmask."""
    if raw is None:
        return 0
    if not isinstance(raw, list):
        raise RuntimeError(
            f"{source}: `flags` must be a list of flag names, got {type(raw).__name__}"
        )
    bits = 0
    for name in raw:
        if not isinstance(name, str):
            raise RuntimeError(f"{source}: flag entries must be strings, got {name!r}")
        key = name.strip().upper()
        if key not in _FLAG_NAMES:
            raise RuntimeError(
                f"{source}: unknown regex flag {name!r}. "
                f"Allowed: {', '.join(sorted(set(_FLAG_NAMES) - set('IMSXA')))}."
            )
        bits |= _FLAG_NAMES[key]
    return bits


def _resolve_extends(
    extends_raw: Any, current_source: str, current_dir: Path | None
) -> list[tuple[str, str]]:
    """Resolve an `extends:` field to a list of (yaml_text, source_label) pairs.

    Resolution rules:
      - bare filename (no slash)  → look up in the bundled patterns/ dir
      - relative path              → relative to the file containing `extends:`
                                     (only meaningful when extending a
                                     filesystem file, not a bundled one)
      - absolute path              → used as-is

    Bare filenames are the common case (`extends: regex_default.yaml` from
    a custom file that wants the bundled defaults), and they work uniformly
    whether we're loading from a filesystem path or a wheel resource.
    """
    if extends_raw is None:
        return []
    if isinstance(extends_raw, str):
        names = [extends_raw]
    elif isinstance(extends_raw, list) and all(isinstance(x, str) for x in extends_raw):
        names = list(extends_raw)
    else:
        raise RuntimeError(
            f"{current_source}: `extends` must be a string or list of strings."
        )

    out: list[tuple[str, str]] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if "/" not in name and "\\" not in name:
            # Bundled lookup. Use read_bundled for the canonical
            # error wording, but re-wrap the not-found case so the
            # message points back at the YAML directive that caused
            # the lookup ("did you mean a path with `/`?").
            try:
                text = read_bundled(
                    name,
                    bundled_dir=_BUNDLED_PATTERNS_DIR,
                    label=current_source,
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    f"{current_source}: extends={name!r} not found in bundled "
                    f"patterns/ — use a path with `/` to reference an "
                    f"on-disk file."
                ) from exc
            out.append((text, f"bundled patterns/{name}"))
            continue

        path = Path(name)
        if not path.is_absolute() and current_dir is not None:
            path = current_dir / path
        try:
            out.append((path.read_text(encoding="utf-8"), str(path)))
        except OSError as exc:
            raise RuntimeError(
                f"{current_source}: extends={name!r} could not be read: {exc}"
            ) from exc
    return out


def _read_root_patterns_yaml(
    override: str | None = None,
    label: str = "REGEX_PATTERNS_PATH",
) -> tuple[str, str, Path | None]:
    """Return (yaml_text, source_label, file_dir) for one root patterns file.

    `override` is the path/bundled-name string. None (the default) means
    "read from CONFIG.patterns_path" so callers that haven't been
    updated for the registry refactor keep working. Empty string is
    treated the same as None → bundled default. `label` is used in
    error messages.

    file_dir is the parent directory if the source is on-disk, else None
    (used to resolve relative `extends:` paths).
    """
    if override is None:
        override = CONFIG.patterns_path
    override = override.strip()
    if override:
        return resolve_spec(
            override, bundled_dir=_BUNDLED_PATTERNS_DIR, label=label,
        )
    return (
        read_bundled_default(_DEFAULT_PATTERNS_RELPATH),
        f"bundled {_DEFAULT_PATTERNS_RELPATH}",
        None,
    )


def _parse_yaml(text: str, source: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{source}: invalid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{source}: top-level YAML must be a mapping with a `patterns:` list."
        )
    return data


def _compile_entries(
    entries: Any, source: str
) -> list[tuple[str, re.Pattern[str]]]:
    if not isinstance(entries, list):
        raise RuntimeError(f"{source}: `patterns` must be a list.")
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for idx, entry in enumerate(entries):
        loc = f"{source} entry {idx}"
        if not isinstance(entry, dict):
            raise RuntimeError(f"{loc}: each entry must be a mapping.")
        etype = entry.get("type")
        pattern = entry.get("pattern")
        if not isinstance(etype, str) or not etype:
            raise RuntimeError(f"{loc}: `type` is required and must be a string.")
        if not isinstance(pattern, str) or not pattern:
            raise RuntimeError(f"{loc}: `pattern` is required and must be a string.")
        flags = _resolve_flags(entry.get("flags"), loc)
        try:
            compiled.append((etype, re.compile(pattern, flags)))
        except re.error as exc:
            raise RuntimeError(
                f"{loc}: pattern {pattern!r} did not compile — {exc}"
            ) from exc
    return compiled


def _load_recursive(
    text: str,
    source: str,
    current_dir: Path | None,
    seen: set[str],
    depth: int,
) -> list[tuple[str, re.Pattern[str]]]:
    if depth > _MAX_EXTENDS_DEPTH:
        raise RuntimeError(
            f"{source}: extends chain exceeded depth {_MAX_EXTENDS_DEPTH} — "
            f"likely a cycle or accidental recursion."
        )
    if source in seen:
        raise RuntimeError(f"{source}: cycle detected in extends chain.")
    seen = seen | {source}

    data = _parse_yaml(text, source)
    # Order matters because the detector is first-match-wins on overlapping
    # spans. Conventional inheritance semantics: a file's OWN patterns take
    # precedence over what it extends (child overrides parent). So we put
    # local patterns first, then the extended chain. Without this, a loose
    # default pattern (e.g. PHONE) could shadow a more specific pattern
    # declared in the extending file (e.g. CPF in the pentest YAML).
    out: list[tuple[str, re.Pattern[str]]] = []
    out.extend(_compile_entries(data.get("patterns", []), source))
    for ext_text, ext_source in _resolve_extends(
        data.get("extends"), source, current_dir
    ):
        # `extends:` always resolves either to a bundled file (no on-disk
        # parent dir) or to a path we already absolutised, so children can
        # only use bundled extends themselves.
        out.extend(
            _load_recursive(ext_text, ext_source, None, seen, depth + 1)
        )
    return out


def _load_patterns(
    path: str | None = None, label: str = "REGEX_PATTERNS_PATH"
) -> list[tuple[str, re.Pattern[str]]]:
    """Load + compile one pattern file. None → read CONFIG.patterns_path."""
    text, source, current_dir = _read_root_patterns_yaml(path, label)
    compiled = _load_recursive(text, source, current_dir, set(), depth=0)
    if not compiled:
        # An empty pattern set is almost certainly a mistake (typo'd top-level
        # key, accidentally-empty file). The LLM layer keeps working even if
        # regex is degraded, but the operator deserves to know.
        raise RuntimeError(f"{source}: no patterns were loaded — is the file empty?")
    return compiled


def _load_patterns_registry() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """Compile every entry in REGEX_PATTERNS_REGISTRY at startup.

    Returns a dict mapping registry name → compiled pattern list.
    Validation is the same as the default path: typos / unreadable
    files / missing patterns crash boot loudly.
    """
    raw = CONFIG.patterns_registry
    pairs = parse_named_path_registry(raw, "REGEX_PATTERNS_REGISTRY")
    out: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for name, path in pairs.items():
        out[name] = _load_patterns(path, f"REGEX_PATTERNS_REGISTRY[{name}]")
    return out


_COMPILED_PATTERNS = _load_patterns()
_COMPILED_PATTERNS_REGISTRY = _load_patterns_registry()


# ── Per-type structural validators ──────────────────────────────────────────
# Run after a pattern has matched, before the candidate is added to the
# results. Lets us reject structurally-implausible matches that the regex
# layer can't catch on its own (Python `re` can't do arithmetic, so it can't
# Luhn-check a credit card or MOD-97-check an IBAN). Each validator returns
# True to keep the match, False to drop it.
#
# Default: no validator = accept all (keeps current behaviour for any type
# without an entry).


def _luhn(s: str) -> bool:
    """Mod-10 (Luhn) checksum used by all major credit-card brands.

    Strips spaces and hyphens — the regex pattern accepts both as group
    separators, so the raw matched text may contain them. Empty / non-
    numeric inputs return False (caller should never see those, but a
    defensive False is cheaper than a TypeError).
    """
    digits = [int(c) for c in s if c.isdigit()]
    if len(digits) < 2:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _iban_mod97(s: str) -> bool:
    """ISO 13616 IBAN integrity check.

    Algorithm: strip spaces, move the first four chars (country code +
    check digits) to the end, replace letters with two-digit numbers
    (A=10..Z=35), and confirm the result mod 97 == 1.
    """
    iban = s.replace(" ", "").upper()
    if len(iban) < 5:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric_chars: list[str] = []
    for c in rearranged:
        if c.isdigit():
            numeric_chars.append(c)
        elif "A" <= c <= "Z":
            numeric_chars.append(str(ord(c) - ord("A") + 10))
        else:
            return False
    try:
        return int("".join(numeric_chars)) % 97 == 1
    except ValueError:
        return False


def _ipv4_address(s: str) -> bool:
    """Reject 999.999.999.999 and other octet-range nonsense the regex
    can't see. Hand-rolled (rather than `ipaddress.IPv4Address`) because
    Python 3.9+ rejects leading zeros — fine for IETF parsers, but a
    common shape in prose / logs that we still want to anonymize."""
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p or not p.isdigit() or len(p) > 3 or int(p) > 255:
            return False
    return True


def _ipv4_network(s: str) -> bool:
    """IPv4 CIDR: octet validation plus a 0..32 prefix check."""
    if "/" not in s:
        return False
    ip, _, prefix = s.rpartition("/")
    if not _ipv4_address(ip) or not prefix.isdigit():
        return False
    return 0 <= int(prefix) <= 32


def _ipv6_address(s: str) -> bool:
    """Validate IPv6 — covers the at-most-one-`::` rule, group count,
    hex-digit ranges, and zone-ID syntax. Strips a `%zone` suffix before
    handing to ipaddress so older Python versions still validate the
    address half (newer 3.9+ stdlibs accept zones natively but we don't
    rely on it)."""
    candidate = s.split("%", 1)[0]
    try:
        ipaddress.IPv6Address(candidate)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def _ipv6_network(s: str) -> bool:
    try:
        ipaddress.IPv6Network(s, strict=False)
        return True
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        return False


_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "CREDIT_CARD":  _luhn,
    "IBAN":         _iban_mod97,
    "IPV4_ADDRESS": _ipv4_address,
    "IPV4_CIDR":    _ipv4_network,
    "IPV6_ADDRESS": _ipv6_address,
    "IPV6_CIDR":    _ipv6_network,
}


_VALID_OVERLAP_STRATEGIES = frozenset({"longest", "priority"})
if CONFIG.overlap_strategy not in _VALID_OVERLAP_STRATEGIES:
    raise RuntimeError(
        f"Invalid REGEX_OVERLAP_STRATEGY={CONFIG.overlap_strategy!r}. "
        f"Allowed: {', '.join(sorted(_VALID_OVERLAP_STRATEGIES))}."
    )


class RegexDetector:
    """Compiled-regex detector. Stateless and synchronous; async only by interface."""

    name = "regex"

    def __init__(self) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = _COMPILED_PATTERNS
        # Named alternatives loaded from REGEX_PATTERNS_REGISTRY at
        # startup. Empty when the registry env var isn't set; per-call
        # patterns_name overrides resolve against this dict.
        self._registry: dict[str, list[tuple[str, re.Pattern[str]]]] = (
            _COMPILED_PATTERNS_REGISTRY
        )

    async def detect(
        self,
        text: str,
        *,
        overlap_strategy: str | None = None,
        patterns_name: str | None = None,
    ) -> list[Match]:
        """Run every compiled pattern and return the resolved match list.

        `overlap_strategy` is a per-call override; None means "use
        CONFIG.overlap_strategy". Validated up front so a typo
        from a per-request override surfaces as a clear error rather
        than silently behaving like the default.

        `patterns_name` selects a NAMED alternative pattern set from
        REGEX_PATTERNS_REGISTRY. None / "default" → the global default
        (REGEX_PATTERNS_PATH or bundled). Unknown name → log a warning
        and fall back to the default — keeps the request from being
        blocked by a typo in the override.
        """
        if not text:
            return []
        strategy = overlap_strategy or CONFIG.overlap_strategy
        if strategy not in _VALID_OVERLAP_STRATEGIES:
            raise ValueError(
                f"Invalid overlap_strategy={strategy!r}. "
                f"Allowed: {', '.join(sorted(_VALID_OVERLAP_STRATEGIES))}."
            )

        compiled = self._compiled
        if patterns_name and patterns_name != "default":
            named = self._registry.get(patterns_name)
            if named is None:
                log.warning(
                    "Override regex_patterns=%r isn't in REGEX_PATTERNS_REGISTRY "
                    "(known: %s); falling back to the default pattern set.",
                    patterns_name, sorted(self._registry) or "<empty>",
                )
            else:
                compiled = named

        # Pass 1: walk every pattern and collect every non-empty candidate.
        # Both strategies pay the same regex cost — Python's re engine
        # has to scan the text for each pattern regardless of strategy
        # (there's no API to mask out already-claimed regions). The
        # strategy only changes the iteration order in pass 2.
        #
        # Capture-group convention: when a pattern declares one or more
        # groups, the first non-None group's span IS the entity (the
        # surrounding match is just label/anchor context). Patterns
        # without groups → full match. This lets two patterns hit the
        # same line as long as their *value* spans don't overlap.
        candidates: list[tuple[int, int, str, str]] = []  # (start, end, value, type)
        for entity_type, pattern in compiled:
            for m in pattern.finditer(text):
                # Capture-group priority:
                # 1. Named group "entity" (explicit designation)
                # 2. First non-None positional group (backward compatibility)
                # 3. Full match (no groups defined)
                try:
                    start, end = m.span("entity")
                    value = m.group("entity")
                except (IndexError, KeyError):
                    idx = next(
                        (i + 1 for i, g in enumerate(m.groups()) if g is not None),
                        None,
                    )
                    if idx is None:
                        start, end = m.span()
                        value = m.group(0)
                    else:
                        start, end = m.start(idx), m.end(idx)
                        value = m.group(idx)
                stripped = value.strip() if value else ""
                if not stripped:
                    continue
                # Per-type structural validation — drop matches that the
                # regex shape allowed but the canonical algorithm rejects
                # (Luhn for cards, MOD-97 for IBANs, octet/group integrity
                # for IPs). Skipping the candidate frees its span so a
                # later pattern can claim it.
                validator = _VALIDATORS.get(entity_type)
                if validator is not None and not validator(stripped):
                    continue
                candidates.append((start, end, stripped, entity_type))

        # Pass 2: greedy span allocation. Iteration order is the strategy.
        #   - "longest"  sort by descending span length, ties broken by
        #                earliest start, then by YAML order (Python's
        #                stable sort preserves insertion order for equal
        #                keys). A longer match wins over a shorter one
        #                that overlaps it.
        #   - "priority" keep insertion order (YAML order, then in-pattern
        #                position). The first pattern that matches a span
        #                wins — same as the pre-v0.2 behaviour.
        if strategy == "longest":
            candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))

        claimed: list[tuple[int, int]] = []
        results: list[Match] = []
        for start, end, value, entity_type in candidates:
            if any(s < end and start < e for s, e in claimed):
                continue
            claimed.append((start, end))
            results.append(Match(text=value, entity_type=entity_type))
        return results


def _regex_call_kwargs(overrides: Any, _api_key: str | None) -> dict[str, Any]:
    """Per-call kwargs for RegexDetector.detect(). Pulled out so the
    SPEC declaration stays declarative — the closure over `overrides`
    is the only thing that varies between calls."""
    return {
        "overlap_strategy": overrides.regex_overlap_strategy,
        "patterns_name": overrides.regex_patterns,
    }


SPEC = DetectorSpec(
    name="regex",
    factory=RegexDetector,
    module=sys.modules[__name__],
    prepare_call_kwargs=_regex_call_kwargs,
    # Regex is microseconds per call — gating it would only serialize
    # work that wasn't a problem. No semaphore.
    has_semaphore=False,
    # Regex doesn't have an availability concept: a bad pattern is a
    # programmer error, not an outage. The pipeline catches generic
    # exceptions and degrades to []; no typed error is raised.
    unavailable_error=None,
)


__all__ = ["RegexDetector", "SPEC"]