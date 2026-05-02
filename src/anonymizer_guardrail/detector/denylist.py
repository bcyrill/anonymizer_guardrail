"""
Denylist detection layer.

Matches text against an operator-supplied list of literal strings ("flag
these terms whenever they appear"). Use when an organization has a stable
set of sensitive names — employee names, project codenames, customer
identifiers — that should always be redacted, with no false positives and
no LLM round-trip.

Coverage is complementary to regex (which matches *shapes*) and to the
LLM detector (which matches *semantics*). Combine via
`DETECTOR_MODE=denylist,regex,llm` (or any other ordering — earlier
detectors win type-resolution conflicts in `_dedup`).

Entries are loaded from a YAML file at startup; set `DENYLIST_PATH` to
either a filesystem path or `bundled:<name>` (for parity with the regex
loader, though no denylist files ship with the package — by definition
these lists are org-specific).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Callable

import yaml

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..bundled_resource import resolve_spec
from ..registry import parse_named_path_registry
from .base import Match
from .launcher import LauncherSpec
from .spec import DetectorSpec

log = logging.getLogger("anonymizer.denylist")


# ── Per-detector config ───────────────────────────────────────────────────
class DenylistConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DENYLIST_",
        extra="ignore",
        frozen=True,
    )

    # Path to the YAML file defining the denylist detector's literal-
    # string entries. If unset, the detector still loads under
    # DETECTOR_MODE=denylist but matches nothing — useful for boot-order
    # independence (operator can set DETECTOR_MODE first and DENYLIST_PATH
    # later). Accepts the same `bundled:NAME` / filesystem-path
    # conventions as REGEX_PATTERNS_PATH.
    path: str = ""
    # Optional registry of NAMED alternative denylists that callers can
    # opt into per-request via additional_provider_specific_params.denylist.
    registry: str = ""
    # Matching backend for the denylist detector:
    #   - "regex" (default) — Python `re` alternation. Pure stdlib, fast
    #                          for low-thousands of entries.
    #   - "aho"             — Aho-Corasick via pyahocorasick. Sub-linear
    #                          in pattern count; pays off above ~5k entries
    #                          or when match latency matters more than
    #                          install size. Requires the
    #                          `denylist-aho` optional extra.
    # Validated at startup; an unknown value crashes loud.
    backend: str = "regex"

    @field_validator("backend", mode="after")
    @classmethod
    def _normalize_backend(cls, v: str) -> str:
        return v.strip().lower()


CONFIG = DenylistConfig()


_BUNDLED_DENYLISTS_DIR = "denylists"


def _read_yaml(
    override: str | None = None, label: str = "DENYLIST_PATH"
) -> tuple[str, str] | None:
    """Return (yaml_text, source_label), or None when no path is set.

    None signals an intentional "no denylist" configuration: the
    detector still loads and registers under DETECTOR_MODE=denylist,
    but matches nothing. That keeps boot order independent of whether
    the operator has gotten around to writing a list yet.
    """
    if override is None:
        override = CONFIG.path
    override = override.strip()
    if not override:
        return None
    text, source, _file_dir = resolve_spec(
        override, bundled_dir=_BUNDLED_DENYLISTS_DIR, label=label,
    )
    return text, source


def _bool_field(entry: dict[str, Any], field: str, default: bool, loc: str) -> bool:
    """Parse an optional boolean field with a clear error on bad types.

    YAML parses true/false fine, but operators occasionally write `yes`
    / `no` / `1` / `0` and expect them to work. We reject anything that
    isn't an actual bool — silently coercing a string `"false"` to True
    (truthy in Python) is exactly the kind of subtle bug we'd rather
    fail loudly on at startup than ship.
    """
    raw = entry.get(field, default)
    if isinstance(raw, bool):
        return raw
    raise RuntimeError(
        f"{loc}: `{field}` must be a boolean (true/false), got "
        f"{type(raw).__name__} {raw!r}."
    )


# Compiled-entry record: the literal value, its type, and whether it
# matched case-sensitively. The pattern itself doesn't include the value
# verbatim (it's escaped + boundary-wrapped), so we keep the original
# text here so we can echo it back as the Match.
_Entry = tuple[str, str, bool]  # (value, entity_type, case_sensitive)


def _wrap_with_boundaries(escaped: str, raw_value: str, word_boundary: bool) -> str:
    """Add `\\b` boundaries around an escaped literal when the caller asked
    for word-boundary matching AND the value's edge is itself a word
    character. `\\b` requires a word/non-word transition; sticking it
    against a non-word edge (e.g. `\\b:foo:\\b`) would constrain matches
    to require a word char on the OUTSIDE of the colon, which isn't what
    operators expect. Skipping the boundary on non-word edges keeps the
    intuition "this term, surrounded by word boundaries when that makes
    sense" intact."""
    if not word_boundary or not raw_value:
        return escaped
    left = r"\b" if raw_value[0].isalnum() or raw_value[0] == "_" else ""
    right = r"\b" if raw_value[-1].isalnum() or raw_value[-1] == "_" else ""
    return f"{left}{escaped}{right}"


def _compile_entries(
    entries: list[_Entry], *, case_sensitive: bool
) -> re.Pattern[str] | None:
    """Build one alternation pattern over the entries that share the
    given case-sensitivity. Longest-first ordering matters: the regex
    engine tries alternatives left-to-right and commits to the first
    that matches, so without this an entry like "Acme" would shadow
    "Acme Corp" at the same starting position.

    Each entry's word_boundary preference is encoded into its own
    alternative — that's why we can mix word-bounded and bare entries
    in a single compiled pattern."""
    relevant = [e for e in entries if e[2] is case_sensitive]
    if not relevant:
        return None
    # Sort by descending length on the literal value so longer matches
    # win the alternation race when they share a prefix.
    relevant.sort(key=lambda e: len(e[0]), reverse=True)
    flags = 0 if case_sensitive else re.IGNORECASE
    # e[0] is the already-escaped, boundary-wrapped pattern fragment.
    return re.compile("|".join(f"(?:{e[0]})" for e in relevant), flags)


def _load_entries(
    path: str | None = None, label: str = "DENYLIST_PATH"
) -> list[dict[str, Any]]:
    """Load + parse one denylist YAML file. None → read CONFIG.path.
    Returns the list of validated entry dicts (with normalized defaults).
    Empty file or missing path returns an empty list.
    """
    raw = _read_yaml(path, label)
    if raw is None:
        return []
    text, source = raw
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{source}: invalid YAML — {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{source}: top-level YAML must be a mapping with an `entries:` list."
        )
    entries = data.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise RuntimeError(f"{source}: `entries` must be a list.")
    out: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, bool]] = set()
    for idx, entry in enumerate(entries):
        loc = f"{source} entry {idx}"
        if not isinstance(entry, dict):
            raise RuntimeError(f"{loc}: each entry must be a mapping.")
        etype = entry.get("type")
        value = entry.get("value")
        if not isinstance(etype, str) or not etype.strip():
            raise RuntimeError(f"{loc}: `type` is required and must be a non-empty string.")
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"{loc}: `value` is required and must be a non-empty string.")
        case_sensitive = _bool_field(entry, "case_sensitive", True, loc)
        word_boundary = _bool_field(entry, "word_boundary", True, loc)
        # De-dup at load time: same (value, type, case_sensitive) more
        # than once is almost certainly a copy-paste typo, and the
        # alternation regex doesn't benefit from duplicates anyway. We
        # keep `word_boundary` out of the dedup key on purpose — two
        # entries with the same value/type/case but different boundary
        # prefs is a deliberate operator choice (match strict and
        # loose), so don't collapse it.
        key = (value, etype, case_sensitive)
        if key in seen_keys:
            log.warning(
                "%s: duplicate entry value=%r type=%r case_sensitive=%r — "
                "ignored", loc, value, etype, case_sensitive,
            )
            continue
        seen_keys.add(key)
        out.append(
            dict(type=etype, value=value, case_sensitive=case_sensitive,
                 word_boundary=word_boundary)
        )
    return out


# A compiled denylist is a callable: text → list of (start, end, value, type)
# candidates. Both supported backends (regex and Aho-Corasick) materialize
# into the same shape so `DenylistDetector.detect` runs the same overlap
# resolution regardless of which one built the index. An empty index
# (no entries loaded) is `_EMPTY_CANDIDATES` — a no-op constant — rather
# than None, so callers don't need a "did this load anything?" branch.
_Candidate = tuple[int, int, str, str]
_CandidatesFn = Callable[[str], list[_Candidate]]


def _empty_candidates(_text: str) -> list[_Candidate]:
    return []


_EMPTY_CANDIDATES: _CandidatesFn = _empty_candidates


# ── Regex backend (default) ────────────────────────────────────────────────
def _build_index_regex(entries: list[dict[str, Any]]) -> _CandidatesFn:
    """Compile entries into two re.Patterns (case-sensitive +
    case-insensitive) closed over a candidate-emitter. Word boundaries
    are baked into each pattern fragment by `_wrap_with_boundaries`, so
    nothing else has to know about them at match time."""
    cs_alts: list[_Entry] = []
    ci_alts: list[_Entry] = []
    cs_lookup: dict[str, str] = {}
    ci_lookup: dict[str, str] = {}
    for e in entries:
        escaped = _wrap_with_boundaries(
            re.escape(e["value"]), e["value"], e["word_boundary"]
        )
        if e["case_sensitive"]:
            cs_alts.append((escaped, e["type"], True))
            # Last write wins on a (value, type, cs) collision — but the
            # loader already de-dupes those. A different entry with the
            # SAME value but a different type IS allowed; the lookup
            # records the most recently loaded one.
            cs_lookup[e["value"]] = e["type"]
        else:
            ci_alts.append((escaped, e["type"], False))
            ci_lookup[e["value"].lower()] = e["type"]
    cs_pattern = _compile_entries(cs_alts, case_sensitive=True)
    ci_pattern = _compile_entries(ci_alts, case_sensitive=False)
    if cs_pattern is None and ci_pattern is None:
        return _EMPTY_CANDIDATES

    def collect(text: str) -> list[_Candidate]:
        out: list[_Candidate] = []
        if cs_pattern is not None:
            for m in cs_pattern.finditer(text):
                value = m.group(0)
                etype = cs_lookup.get(value)
                if etype is None:
                    # Defensive: a substring matched our pattern but the
                    # lookup table doesn't know it. Shouldn't happen
                    # given how we build both from the same entries —
                    # skip rather than emit an OTHER-typed mystery.
                    continue
                out.append((m.start(), m.end(), value, etype))
        if ci_pattern is not None:
            for m in ci_pattern.finditer(text):
                value = m.group(0)
                etype = ci_lookup.get(value.lower())
                if etype is None:
                    continue
                out.append((m.start(), m.end(), value, etype))
        return out

    return collect


# ── Aho-Corasick backend ───────────────────────────────────────────────────
def _has_word_boundaries(text: str, start: int, end: int) -> bool:
    """Mimic `\\b` post-hoc on an Aho-Corasick match span. Only enforce
    a boundary on a side where the matched value's edge is itself a
    word character — same rule `_wrap_with_boundaries` uses on the regex
    side, so behaviour stays identical across backends.
    """
    if start < 0 or end > len(text) or start >= end:
        return False
    left_in_word = text[start].isalnum() or text[start] == "_"
    if left_in_word and start > 0:
        prev = text[start - 1]
        if prev.isalnum() or prev == "_":
            return False
    right_in_word = text[end - 1].isalnum() or text[end - 1] == "_"
    if right_in_word and end < len(text):
        nxt = text[end]
        if nxt.isalnum() or nxt == "_":
            return False
    return True


def _build_index_aho(entries: list[dict[str, Any]]) -> _CandidatesFn:
    """Compile entries into two Aho-Corasick automatons (case-sensitive +
    case-insensitive). The case-insensitive one's keys are lowercased
    and the matcher runs against `text.lower()`; the original `text` is
    sliced for the Match so source casing survives.

    Word boundaries can't be expressed in the automaton itself (it's
    pure literal matching), so each entry's `word_boundary` preference
    is attached to its payload and applied as a post-filter on the
    candidate span."""
    try:
        import ahocorasick  # noqa: WPS433 — lazy import keeps the module loadable without the extra
    except ImportError as exc:
        raise RuntimeError(
            "DENYLIST_BACKEND=aho requires the `pyahocorasick` package. "
            "Install with `pip install \"anonymizer-guardrail[denylist-aho]\"` "
            "or set DENYLIST_BACKEND=regex to fall back to the stdlib backend."
        ) from exc

    cs_automaton: Any | None = None
    ci_automaton: Any | None = None
    for e in entries:
        # Payload: (entity_type, word_boundary, len_value). The length
        # is needed because pyahocorasick's iter() returns end-inclusive
        # offsets only, and we have to recover the start position.
        payload = (e["type"], e["word_boundary"], len(e["value"]))
        if e["case_sensitive"]:
            if cs_automaton is None:
                cs_automaton = ahocorasick.Automaton()
            cs_automaton.add_word(e["value"], payload)
        else:
            if ci_automaton is None:
                ci_automaton = ahocorasick.Automaton()
            ci_automaton.add_word(e["value"].lower(), payload)
    if cs_automaton is not None:
        cs_automaton.make_automaton()
    if ci_automaton is not None:
        ci_automaton.make_automaton()
    if cs_automaton is None and ci_automaton is None:
        return _EMPTY_CANDIDATES

    def collect(text: str) -> list[_Candidate]:
        out: list[_Candidate] = []
        if cs_automaton is not None:
            for end_inclusive, (etype, word_boundary, length) in cs_automaton.iter(text):
                start = end_inclusive - length + 1
                end = end_inclusive + 1
                if word_boundary and not _has_word_boundaries(text, start, end):
                    continue
                out.append((start, end, text[start:end], etype))
        if ci_automaton is not None:
            lowered = text.lower()
            for end_inclusive, (etype, word_boundary, length) in ci_automaton.iter(lowered):
                start = end_inclusive - length + 1
                end = end_inclusive + 1
                if word_boundary and not _has_word_boundaries(text, start, end):
                    continue
                # Slice the ORIGINAL text so the Match preserves the
                # caller's casing — the surrogate-replacer needs to find
                # this exact substring in the source on substitution.
                out.append((start, end, text[start:end], etype))
        return out

    return collect


# ── Backend dispatch ───────────────────────────────────────────────────────
_VALID_BACKENDS = frozenset({"regex", "aho"})

if CONFIG.backend not in _VALID_BACKENDS:
    raise RuntimeError(
        f"Invalid DENYLIST_BACKEND={CONFIG.backend!r}. "
        f"Allowed: {', '.join(sorted(_VALID_BACKENDS))}."
    )


def _build_index(entries: list[dict[str, Any]]) -> _CandidatesFn:
    """Compile entries via the configured backend.

    Backend is read from `CONFIG.backend` (validated at module import).
    Each call to `_build_index` honours the *current* CONFIG — tests
    that monkeypatch the backend can re-build a registry from scratch
    and exercise the alternative path without restarting the interpreter.
    """
    if CONFIG.backend == "aho":
        return _build_index_aho(entries)
    return _build_index_regex(entries)


def _load_registry() -> dict[str, _CandidatesFn]:
    """Compile every entry in DENYLIST_REGISTRY at startup.

    Returns a dict mapping registry name → compiled candidates fn.
    Validation is the same as the default path: typos / unreadable
    files / bad schemas crash boot loudly rather than at first request.
    """
    raw = CONFIG.registry
    pairs = parse_named_path_registry(raw, "DENYLIST_REGISTRY")
    out: dict[str, _CandidatesFn] = {}
    for name, path in pairs.items():
        entries = _load_entries(path, f"DENYLIST_REGISTRY[{name}]")
        out[name] = _build_index(entries)
    return out


# Module-level cache of the parsed default denylist. Loaded once at
# import time so a typo or unparseable file fails boot loudly rather
# than at first request — same pattern as regex.py / llm.py.
_LOADED_ENTRIES = _load_entries()
_DEFAULT_INDEX = _build_index(_LOADED_ENTRIES)
_REGISTRY = _load_registry()


class DenylistDetector:
    """Literal-string detector backed by a YAML-defined denylist.

    Stateless and synchronous; async only by interface to satisfy the
    Detector protocol. Construction is cheap (the patterns and lookup
    tables are loaded once at module import); `detect` is just two
    regex passes plus a greedy overlap resolution.

    For the size of denylists this is intended for (a few hundred to
    low-thousands of entries), Python `re` alternation is plenty fast.
    Aho-Corasick (`pyahocorasick`) would only matter at much larger
    scale and adds a non-stdlib dependency — defer that until someone
    actually hits the wall.
    """

    name = "denylist"

    def __init__(self) -> None:
        self._default_index: _CandidatesFn = _DEFAULT_INDEX
        self._registry: dict[str, _CandidatesFn] = _REGISTRY
        if not _LOADED_ENTRIES:
            log.info(
                "Denylist detector loaded with no default entries (DENYLIST_PATH "
                "is unset or the file has no entries). %d named alternative(s) "
                "registered. Backend=%s.",
                len(_REGISTRY), CONFIG.backend,
            )
        else:
            log.info(
                "Denylist detector ready — %d default entries, %d named "
                "alternative(s). Backend=%s.",
                len(_LOADED_ENTRIES), len(_REGISTRY), CONFIG.backend,
            )

    async def detect(
        self, text: str, *, denylist_name: str | None = None
    ) -> list[Match]:
        """Run the denylist against `text`.

        `denylist_name` selects a NAMED alternative from DENYLIST_REGISTRY.
        None / "default" → the default DENYLIST_PATH list. Unknown name →
        log a warning and fall back to the default — same behaviour as
        regex_patterns / llm_prompt overrides; keeps the request from
        being blocked by a typo in the override.
        """
        candidates_fn = self._default_index
        if denylist_name and denylist_name != "default":
            named = self._registry.get(denylist_name)
            if named is None:
                log.warning(
                    "Override denylist=%r isn't in DENYLIST_REGISTRY "
                    "(known: %s); falling back to the default denylist.",
                    denylist_name, sorted(self._registry) or "<empty>",
                )
            else:
                candidates_fn = named

        if not text:
            return []
        # Pass 1: ask the configured backend for every candidate match
        # span. Both regex and Aho-Corasick backends produce the same
        # (start, end, value, type) shape; word-boundary filtering is
        # already applied (baked into the regex pattern, post-filtered
        # for aho), so we only need to resolve overlaps below.
        candidates = list(candidates_fn(text))

        # Pass 2: greedy longest-first overlap resolution. Mirrors the
        # regex detector's `longest` strategy — same reasoning: when
        # "Acme" and "Acme Corp" both match at the same start, the
        # longer span is the more specific entity. Stable sort ensures
        # ties between equal-length spans break by start position then
        # by insertion order (case-sensitive matches were collected
        # first, so they win over case-insensitive duplicates).
        candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
        claimed: list[tuple[int, int]] = []
        results: list[Match] = []
        for start, end, value, etype in candidates:
            if any(s < end and start < e for s, e in claimed):
                continue
            claimed.append((start, end))
            results.append(Match(text=value, entity_type=etype))
        return results


def _denylist_call_kwargs(overrides: Any, _api_key: str | None) -> dict[str, Any]:
    return {"denylist_name": overrides.denylist}


SPEC = DetectorSpec(
    name="denylist",
    factory=DenylistDetector,
    module=sys.modules[__name__],
    prepare_call_kwargs=_denylist_call_kwargs,
    # Pure regex / Aho-Corasick scan against a small literal-string
    # set — no semaphore needed; same rationale as RegexSpec.
    has_semaphore=False,
    # Like regex, the denylist has no availability concept; a bad
    # entry is operator config, not an outage. No typed error.
    unavailable_error=None,
)


# In-process detector — no service to auto-start. Only env passthroughs.
LAUNCHER_SPEC = LauncherSpec(
    guardrail_env_passthroughs=[
        "DENYLIST_PATH",
        "DENYLIST_REGISTRY",
        "DENYLIST_BACKEND",
    ],
)


__all__ = ["DenylistDetector", "SPEC", "LAUNCHER_SPEC"]
