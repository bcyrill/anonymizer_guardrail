"""Shared parser for the `name=path,name=path` registry env vars.

Used by both the regex detector (REGEX_PATTERNS_REGISTRY) and the LLM
detector (LLM_SYSTEM_PROMPT_REGISTRY) to declare named alternatives
that callers can opt into per-request via
additional_provider_specific_params. The format mirrors the codebase's
existing convention for list-shaped env vars (DETECTOR_MODE,
FAKER_LOCALE).

The format:
    name=path,name=path,...

- Whitespace around `=` and `,` is stripped.
- Names must be unique within a registry.
- The reserved name "default" is rejected — the default lives in the
  matching `*_PATH` env var, not the registry, so configuring a
  registry never silently changes no-override behaviour.
- Empty input → empty dict (no named alternatives configured).
"""

from __future__ import annotations


_RESERVED_NAMES = frozenset({"default"})


def parse_named_path_registry(raw: str, var_name: str) -> dict[str, str]:
    """Parse a `name=path,...` env var into a dict.

    Raises RuntimeError on malformed input or reserved-name use, with
    a message that names the offending env var so an operator typo
    surfaces immediately at boot rather than later at first request.
    """
    if not raw or not raw.strip():
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise RuntimeError(
                f"{var_name}: malformed entry {entry!r} — expected "
                f"`name=path` form."
            )
        name, _, path = entry.partition("=")
        name = name.strip()
        path = path.strip()
        if not name or not path:
            raise RuntimeError(
                f"{var_name}: malformed entry {entry!r} — name and path "
                f"both required."
            )
        if name in _RESERVED_NAMES:
            raise RuntimeError(
                f"{var_name}: name {name!r} is reserved — the default "
                f"path lives in the matching *_PATH env var, not the "
                f"registry. Pick a different name."
            )
        if name in out:
            raise RuntimeError(
                f"{var_name}: duplicate name {name!r}."
            )
        out[name] = path
    return out