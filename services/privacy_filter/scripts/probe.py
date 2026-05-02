#!/usr/bin/env python3
"""Probe the privacy-filter service with a text input.

Use this to see how openai/privacy-filter classifies entities in
your data before wiring it into the guardrail. Hit a running
service (default http://localhost:8001) and read back the matches
grouped by entity type. (No per-span confidence: the service's
opf-based decoder doesn't expose one — see PROBE.md for the
migration context.)

Stdlib-only on purpose — runs from any checkout without installing
the service's Python deps. To start the service first, see
`services/privacy_filter/README.md` or
`scripts/launcher.sh -d privacy_filter --privacy-filter-backend service`.

Unlike the gliner-pii probe, privacy-filter is *not* zero-shot:
the entity-type vocabulary is baked into the model at training time
(PERSON, EMAIL_ADDRESS, PHONE, URL, ADDRESS, DATE_OF_BIRTH,
IDENTIFIER, CREDENTIAL — see _LABEL_MAP in services/privacy_filter/
main.py). There's no `--labels` flag for that reason; you take the
vocabulary the model knows and see what it finds.

For runnable examples (input shapes, comparing against gliner-pii)
see `services/privacy_filter/scripts/PROBE.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


_DEFAULT_URL = "http://localhost:8001"


def _read_text(text: str | None, text_file: str | None) -> str:
    if text is not None:
        return text
    if text_file == "-":
        return sys.stdin.read()
    assert text_file is not None  # argparse guarantees one is set
    with open(text_file, encoding="utf-8") as f:
        return f.read()


def _post_detect(url: str, text: str, timeout: float) -> dict:
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/detect",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _format_table(matches: list[dict]) -> str:
    """Render matches as a fixed-width table plus a per-entity-type
    count summary.

    The summary is the point of this script for privacy-filter: with
    no zero-shot label dial to turn, the operator's question becomes
    "what does the model actually find on my data, and how often?"
    A type histogram answers that at a glance.
    """
    # No `score` column: the wire format dropped that field when the
    # service migrated from `transformers.pipeline` to opf. opf's
    # DetectedSpan doesn't expose a per-span confidence, and a
    # synthetic constant (we briefly shipped 1.0) was worse than
    # nothing — operators reading it would assume real signal.
    header = ("entity_type", "span", "text")
    rows: list[tuple[str, str, str]] = [header]
    for m in matches:
        rows.append((
            m.get("entity_type", ""),
            f"[{m.get('start')}:{m.get('end')}]",
            m.get("text", ""),
        ))

    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*header)]
    lines.append(fmt.format(*("-" * w for w in widths)))
    for r in rows[1:]:
        lines.append(fmt.format(*r))
    if len(rows) == 1:
        lines.append("(no matches)")

    counts: dict[str, int] = {}
    for m in matches:
        t = m.get("entity_type", "")
        if t:
            counts[t] = counts.get(t, 0) + 1
    lines.append("")
    if counts:
        lines.append("Matches by type:")
        # Sorted by count desc, then type asc — most common first
        # is what an operator skims for.
        for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {t:<16} {n}")
    else:
        lines.append("Matches by type: (none)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--text", "-t",
        help="Inline text to scan.",
    )
    src.add_argument(
        "--text-file", "-f",
        help="Path to read text from. Use '-' for stdin.",
    )
    parser.add_argument(
        "--url",
        default=_DEFAULT_URL,
        help=f"Service base URL. Default {_DEFAULT_URL}.",
    )
    parser.add_argument(
        "--timeout",
        type=float, default=30.0,
        help="Per-request HTTP timeout in seconds. Default 30.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw JSON response instead of a formatted table.",
    )
    args = parser.parse_args(argv)

    try:
        text = _read_text(args.text, args.text_file)
    except OSError as exc:
        print(f"error: cannot read text source: {exc}", file=sys.stderr)
        return 1

    try:
        result = _post_detect(args.url, text, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"error: {args.url} returned HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        if body:
            print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(
            f"error: cannot reach {args.url}: {exc.reason}\n"
            f"Is the service running? Try:\n"
            f"  scripts/launcher.sh -d privacy_filter --privacy-filter-backend service",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(_format_table(result.get("matches", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
