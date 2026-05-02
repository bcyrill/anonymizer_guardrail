#!/usr/bin/env python3
"""Probe the gliner-pii service with a text + label list.

Use this to find out which labels the model picks up reliably on
your data before wiring them into the guardrail. Hit a running
service (default http://localhost:8002) with one or more labels
and read the matches it returns; the coverage summary at the
bottom calls out which labels produced matches and which didn't.

Stdlib-only on purpose — runs from any checkout without installing
the service's Python deps. To start the service first, see
`services/gliner_pii/README.md` or
`scripts/launcher.sh -d gliner_pii --gliner-pii-backend service`.

Examples — input shapes:

    # Single inline text, comma-separated labels:
    python services/gliner_pii/scripts/probe.py \\
        --text "Alice Smith works at Acme Corp" \\
        --labels person,organization

    # Repeatable --label flags compose with --labels:
    python services/gliner_pii/scripts/probe.py \\
        --text-file sample.txt \\
        --label person --label company --label address

    # Read from stdin:
    cat sample.txt | python services/gliner_pii/scripts/probe.py \\
        --text-file - --labels ssn,credit_card

    # Non-default URL (CI runner, remote host, …):
    python services/gliner_pii/scripts/probe.py \\
        --url http://gliner.internal:8002 \\
        --text "..." --labels phone_number

    # Raw JSON for scripting:
    python services/gliner_pii/scripts/probe.py \\
        --text "..." --labels person --json | jq '.matches[].score'

Examples — exploring zero-shot labels:

GLiNER takes the label list as a soft prompt, so the *string* of
each label matters: it can identify entities for labels that aren't
explicitly in its training data, especially when the label name is
descriptive ("project_codename") rather than abstract ("X"). The
coverage summary at the bottom of the table tells you which labels
landed.

    # Niche label not in the bundled DEFAULT_LABELS — does the model
    # generalize to it?
    python services/gliner_pii/scripts/probe.py \\
        --text "Project Zephyr launches Q3; lead is bob@acme.com." \\
        --labels project_codename,email

    # Domain-specific labels (medical):
    python services/gliner_pii/scripts/probe.py \\
        --text "Patient prescribed Lisinopril for hypertension." \\
        --labels medication,diagnosis

    # Side-by-side comparison: do creative labels add coverage on top
    # of the standard PII set, or duplicate it?
    python services/gliner_pii/scripts/probe.py \\
        --text-file engagement_notes.txt \\
        --labels person,organization,internal_hostname,vehicle_registration,api_key

    # Lower the threshold to surface marginal matches when probing
    # whether an unusual label registers at all (default cutoff is
    # tuned for production precision, not exploration):
    python services/gliner_pii/scripts/probe.py \\
        --text "User @alice_42 sent 0.5 BTC to bc1qxy2…" \\
        --labels username,cryptocurrency_address --threshold 0.2

    # Same text, two label phrasings — does the model prefer one over
    # the other? (Run twice and compare the score column.)
    python services/gliner_pii/scripts/probe.py \\
        --text "Reach Bob at +1 415-555-0123." --labels phone_number
    python services/gliner_pii/scripts/probe.py \\
        --text "Reach Bob at +1 415-555-0123." --labels telephone
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


_DEFAULT_URL = "http://localhost:8002"
_DEFAULT_THRESHOLD = 0.5


def _flatten_labels(values: list[str]) -> list[str]:
    """Accept --labels as comma-separated AND --label as repeatable;
    flatten and dedupe (preserving first-seen order). Lets the
    operator mix `-l a,b -l c` without thinking about which form
    the script wants."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        for label in v.split(","):
            label = label.strip()
            if label and label not in seen:
                seen.add(label)
                out.append(label)
    return out


def _read_text(text: str | None, text_file: str | None) -> str:
    if text is not None:
        return text
    if text_file == "-":
        return sys.stdin.read()
    assert text_file is not None  # argparse guarantees one is set
    with open(text_file, encoding="utf-8") as f:
        return f.read()


def _post_detect(
    url: str, text: str, labels: list[str], threshold: float, timeout: float,
) -> dict:
    body = json.dumps(
        {"text": text, "labels": labels, "threshold": threshold},
    ).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/detect",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _format_table(matches: list[dict], labels: list[str]) -> str:
    """Render matches as a fixed-width table plus a coverage summary.

    The coverage block is the point of this whole script: a quick
    visual cue for "label X picked up Y matches; label Z picked up
    nothing". For long label vocabularies it tells the operator at a
    glance which terms the zero-shot model handles on their data.
    """
    header = ("entity_type", "score", "span", "text")
    rows: list[tuple[str, str, str, str]] = [header]
    for m in matches:
        rows.append((
            m.get("entity_type", ""),
            f"{float(m.get('score', 0.0)):.3f}",
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

    matched = sorted({m.get("entity_type", "") for m in matches if m.get("entity_type")})
    unmatched = [label for label in labels if label not in matched]
    lines.append("")
    lines.append(f"Labels with matches:    {', '.join(matched) or '(none)'}")
    lines.append(f"Labels with no matches: {', '.join(unmatched) or '(all matched)'}")
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
        "--labels", "-l",
        action="append", default=[], required=True,
        help=(
            "Zero-shot labels for the model to look for. Comma-"
            "separated and/or repeatable (e.g. -l person,email "
            "-l ssn). At least one is required."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float, default=_DEFAULT_THRESHOLD,
        help=f"Confidence cutoff in [0,1]. Default {_DEFAULT_THRESHOLD}.",
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

    labels = _flatten_labels(args.labels)
    if not labels:
        parser.error("at least one non-empty label is required")

    try:
        text = _read_text(args.text, args.text_file)
    except OSError as exc:
        print(f"error: cannot read text source: {exc}", file=sys.stderr)
        return 1

    try:
        result = _post_detect(args.url, text, labels, args.threshold, args.timeout)
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
            f"  scripts/launcher.sh -d gliner_pii --gliner-pii-backend service",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(_format_table(result.get("matches", []), labels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
