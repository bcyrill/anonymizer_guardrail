#!/usr/bin/env python3
"""Probe the privacy-filter service with a text input.

Use this to see how openai/privacy-filter classifies entities in
your data before wiring it into the guardrail. Hit a running
service (default http://localhost:8001) and read back the spans it
returns, grouped by opf label. (No per-span confidence: opf's
DetectedSpan doesn't expose one — see PROBE.md for the migration
context.)

Labels are emitted **raw** — `private_person`, `private_email`,
`private_phone_number`, `private_url`, `private_address`,
`private_date_of_birth`, `private_identifier`, `private_credential`
— the same shape opf returns. The guardrail-side
`RemotePrivacyFilterDetector` is what canonicalises these into
`PERSON` / `EMAIL_ADDRESS` / etc. Showing raw labels here keeps
this script a thin window onto what the service does, separate
from how the guardrail interprets it.

Stdlib-only on purpose — runs from any checkout without installing
the service's Python deps. To start the service first, see
`services/privacy_filter/README.md` or
`scripts/launcher.sh -d privacy_filter --privacy-filter-backend service`.

Unlike the gliner-pii probe, privacy-filter is *not* zero-shot:
the label vocabulary is baked into the model at training time.
There's no `--labels` flag for that reason; you take the vocabulary
the model knows and see what it finds.

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


def _format_table(spans: list[dict]) -> str:
    """Render spans as a fixed-width table plus a per-label count summary.

    The summary is the point of this script for privacy-filter: with
    no zero-shot label dial to turn, the operator's question becomes
    "what does the model actually find on my data, and how often?"
    A label histogram answers that at a glance.
    """
    # No `score` column: opf's DetectedSpan doesn't expose a per-span
    # confidence, and a synthetic constant would be worse than its
    # absence — operators reading it would assume real signal.
    header = ("label", "span", "text")
    rows: list[tuple[str, str, str]] = [header]
    for s in spans:
        rows.append((
            s.get("label", ""),
            f"[{s.get('start')}:{s.get('end')}]",
            s.get("text", ""),
        ))

    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*header)]
    lines.append(fmt.format(*("-" * w for w in widths)))
    for r in rows[1:]:
        lines.append(fmt.format(*r))
    if len(rows) == 1:
        lines.append("(no spans)")

    counts: dict[str, int] = {}
    for s in spans:
        label = s.get("label", "")
        if label:
            counts[label] = counts.get(label, 0) + 1
    lines.append("")
    if counts:
        lines.append("Spans by label:")
        # Sorted by count desc, then label asc — most common first
        # is what an operator skims for. Widen the label column
        # because opf labels (`private_phone_number`, …) are longer
        # than the canonical names the script used to print.
        label_w = max(len(t) for t in counts)
        for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {label:<{label_w}}  {n}")
    else:
        lines.append("Spans by label: (none)")
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

    print(_format_table(result.get("spans", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
