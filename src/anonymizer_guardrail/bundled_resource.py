"""Shared loader for `bundled:NAME` and filesystem-path resource specs.

Used by every detector that exposes a `*_PATH` env var
(REGEX_PATTERNS_PATH, DENYLIST_PATH, LLM_SYSTEM_PROMPT_PATH) plus their
matching `*_REGISTRY` named alternatives. Centralizes:

  * The `bundled:NAME` prefix convention — bare filename in the package's
    bundled subdirectory; insulates the env var from the Python version
    embedded in the site-packages path.
  * The path-separator rejection — `bundled:` is for opaque bare names
    only, never path traversal into the package.
  * The "fail loud at boot rather than silently fall back" policy when
    an operator-specified path is unreadable.
  * The `(text, source_label, file_dir)` triple shape, where `file_dir`
    lets recursive loaders (regex.py's `extends:`) resolve relative
    references against the parent of the file they came from.

Detector modules wrap these helpers with their own bundled subdirectory
and either fall back to a packaged default when the spec is empty
(regex, llm) or signal "no resource configured" (denylist).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_BUNDLED_PREFIX = "bundled:"
_PACKAGE = "anonymizer_guardrail"


def read_bundled(name: str, *, bundled_dir: str, label: str) -> str:
    """Read `{bundled_dir}/{name}` from the package's bundled resources.

    `name` must be a bare filename. Path separators are rejected so a
    `bundled:NAME` env-var value can't be used to traverse the package
    layout — the prefix is meant to be opaque, not a sub-shell for
    filesystem paths.
    """
    if not name or "/" in name or "\\" in name:
        raise RuntimeError(
            f"{label}=bundled:{name!r}: name must be a bare filename "
            f"(no path separators). Use a filesystem path if you want "
            f"a file outside the bundled {bundled_dir}/."
        )
    try:
        return (
            resources.files(_PACKAGE)
            .joinpath(f"{bundled_dir}/{name}")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            f"{label}=bundled:{name!r} not found in bundled "
            f"{bundled_dir}/: {exc}"
        ) from exc


def read_bundled_default(relpath: str) -> str:
    """Read a fixed packaged file by relative path.

    Used by detectors that have a packaged default (e.g. the bundled
    `patterns/regex_default.yaml`) — distinct from `read_bundled`
    because the relpath is a code-defined constant, not operator input,
    so the bare-filename guard doesn't apply.
    """
    return (
        resources.files(_PACKAGE)
        .joinpath(relpath)
        .read_text(encoding="utf-8")
    )


def resolve_spec(
    spec: str, *, bundled_dir: str, label: str
) -> tuple[str, str, Path | None]:
    """Resolve a non-empty spec to (text, source_label, file_dir).

    `spec` is either:
      * `"bundled:NAME"` — bare filename in the package's bundled_dir/
      * a filesystem path (absolute or relative)

    `file_dir` is the parent directory when the spec resolves to an
    on-disk file (needed by recursive loaders that resolve nested
    references relative to the including file), else None for the
    bundled case (no on-disk parent — nested references must use
    bundled lookups themselves).

    Raises RuntimeError on unreadable paths or malformed bundled names.
    Caller is responsible for the empty-spec case (regex/llm fall back
    to a packaged default; denylist signals "not configured").
    """
    if spec.startswith(_BUNDLED_PREFIX):
        name = spec[len(_BUNDLED_PREFIX):].strip()
        text = read_bundled(name, bundled_dir=bundled_dir, label=label)
        return text, f"bundled {bundled_dir}/{name}", None
    path = Path(spec)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"{label}={spec!r} could not be read: {exc}"
        ) from exc
    return text, str(path), path.parent


__all__ = ["read_bundled", "read_bundled_default", "resolve_spec"]
