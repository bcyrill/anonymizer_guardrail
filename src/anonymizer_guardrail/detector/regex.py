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

import re
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..config import config
from .base import Match

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


def _read_bundled(relpath: str) -> str:
    return (
        resources.files("anonymizer_guardrail")
        .joinpath(relpath)
        .read_text(encoding="utf-8")
    )


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
            # Bundled lookup.
            try:
                text = _read_bundled(f"{_BUNDLED_PATTERNS_DIR}/{name}")
            except (FileNotFoundError, OSError) as exc:
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


_BUNDLED_PREFIX = "bundled:"


def _read_root_patterns_yaml() -> tuple[str, str, Path | None]:
    """Return (yaml_text, source_label, file_dir) for the root config.

    file_dir is the parent directory if the source is on-disk, else None
    (used to resolve relative `extends:` paths). The override env var
    accepts either:
      * a filesystem path (absolute or relative)
      * `bundled:<name>` — a bare filename in the package's patterns/ dir,
        which insulates the env var from the Python version embedded in
        the site-packages path.
    """
    override = config.regex_patterns_path.strip()
    if override:
        if override.startswith(_BUNDLED_PREFIX):
            name = override[len(_BUNDLED_PREFIX):].strip()
            if not name or "/" in name or "\\" in name:
                raise RuntimeError(
                    f"REGEX_PATTERNS_PATH=bundled:{name!r}: name must be a "
                    f"bare filename (no path separators). Use a filesystem "
                    f"path if you want a file outside the bundled patterns/."
                )
            try:
                text = _read_bundled(f"{_BUNDLED_PATTERNS_DIR}/{name}")
            except (FileNotFoundError, OSError) as exc:
                raise RuntimeError(
                    f"REGEX_PATTERNS_PATH=bundled:{name!r} not found in "
                    f"bundled patterns/: {exc}"
                ) from exc
            # No on-disk parent → child `extends:` directives must use
            # bundled lookups too (cannot resolve a relative path).
            return text, f"bundled patterns/{name}", None
        path = Path(override)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            # Fail loud rather than fall back: the operator set this path on
            # purpose; silently using a different pattern set would be a
            # nasty source of "why isn't my secret being redacted".
            raise RuntimeError(
                f"REGEX_PATTERNS_PATH={override!r} could not be read: {exc}"
            ) from exc
        return text, str(path), path.parent
    return (
        _read_bundled(_DEFAULT_PATTERNS_RELPATH),
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


def _load_patterns() -> list[tuple[str, re.Pattern[str]]]:
    text, source, current_dir = _read_root_patterns_yaml()
    compiled = _load_recursive(text, source, current_dir, set(), depth=0)
    if not compiled:
        # An empty pattern set is almost certainly a mistake (typo'd top-level
        # key, accidentally-empty file). The LLM layer keeps working even if
        # regex is degraded, but the operator deserves to know.
        raise RuntimeError(f"{source}: no patterns were loaded — is the file empty?")
    return compiled


_COMPILED_PATTERNS = _load_patterns()

_VALID_OVERLAP_STRATEGIES = frozenset({"longest", "priority"})
if config.regex_overlap_strategy not in _VALID_OVERLAP_STRATEGIES:
    raise RuntimeError(
        f"Invalid REGEX_OVERLAP_STRATEGY={config.regex_overlap_strategy!r}. "
        f"Allowed: {', '.join(sorted(_VALID_OVERLAP_STRATEGIES))}."
    )


class RegexDetector:
    """Compiled-regex detector. Stateless and synchronous; async only by interface."""

    name = "regex"

    def __init__(self) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = _COMPILED_PATTERNS

    async def detect(self, text: str) -> list[Match]:
        if not text:
            return []

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
        for entity_type, pattern in self._compiled:
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
        if config.regex_overlap_strategy == "longest":
            candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))

        claimed: list[tuple[int, int]] = []
        results: list[Match] = []
        for start, end, value, entity_type in candidates:
            if any(s < end and start < e for s, e in claimed):
                continue
            claimed.append((start, end))
            results.append(Match(text=value, entity_type=entity_type))
        return results


__all__ = ["RegexDetector"]