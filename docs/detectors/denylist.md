# Denylist detector

Literal-string match against an operator-supplied YAML list. Useful
for org-specific terms regex can't shape-match and the LLM may miss:
employee names, project codenames, customer identifiers, internal
product names. Deterministic, no false positives, no LLM round-trip.

Loads with no entries when `DENYLIST_PATH` is unset, so registering
it under `DETECTOR_MODE` before configuring the file is safe.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DENYLIST_PATH` | *(empty → no entries)* | Path to the denylist YAML (literal-string match). Empty means the detector loads as a no-op. Accepts `bundled:NAME` or a filesystem path. |
| `DENYLIST_REGISTRY` | *(empty)* | Comma-separated `name=path` list of NAMED alternative denylists callers can opt into per-request via `denylist`. See [per-request overrides → Named alternatives](../per-request-overrides.md#named-alternatives). |
| `DENYLIST_BACKEND` | `regex` | `regex` (Python `re` alternation, stdlib only) or `aho` (Aho-Corasick via `pyahocorasick`). The bundled image ships both; for direct `pip install` users, `aho` requires the `denylist-aho` extra. See [Backend choice](#backend-choice) below. |

The denylist detector has no concurrency cap — literal matching runs
in microseconds.

## Per-request overrides

One denylist-specific key can be passed in
`additional_provider_specific_params` (see
[per-request overrides](../per-request-overrides.md) for the general
shape):

| Override key | Type | Effect |
|---|---|---|
| `denylist` | `string` | Name of a registered alternative denylist. Looked up in `DENYLIST_REGISTRY`; unknown names log a warning and fall back to the default list. |

## Schema

Each entry needs a `type` and `value`; `case_sensitive` and
`word_boundary` are optional and default to `true`:

```yaml
# Loaded at startup. Override with DENYLIST_PATH=/path/to/your.yaml.
#
# Schema (per entry):
#   type:              one of the entity types declared in
#                        src/anonymizer_guardrail/detector/base.py → ENTITY_TYPES
#                      (unknown types fall back to OTHER and produce
#                      opaque-token surrogates).
#   value:             the literal string to match (required, non-empty).
#   case_sensitive:    optional bool, default true. When false, the entry
#                      matches any casing of `value` in the text.
#   word_boundary:     optional bool, default true. When true, `\b` is
#                      attached around the value's word-character edges
#                      so "Bob" doesn't match inside "Bobby". Set to
#                      false to allow substring matches.

entries:
  # Project codename — exact casing only.
  - type: ORGANIZATION
    value: Project Aurora

  # Employee name — match any casing the user typed.
  - type: PERSON
    value: Maria Schwarz
    case_sensitive: false

  # Internal product designator that may appear inside identifiers
  # like "ORION-DB-PROD" — disable word boundaries for substring match.
  - type: IDENTIFIER
    value: ORION
    word_boundary: false
```

## Behaviour notes

- **Overlap resolution** is longest-first within the denylist: when
  `Acme` and `Acme Corp` both appear, `Acme Corp` wins as a single
  span. Across detectors, dedup is by matched text (first detector
  to claim a substring keeps its type).
- **No path traversal from clients**: the per-request `denylist`
  override accepts only registered names, never paths. See
  [per-request overrides → Named alternatives](../per-request-overrides.md#named-alternatives).

## Casing default — read this before authoring a denylist

`case_sensitive` defaults to **`true`** per entry. An entry of
`AcmeSecret` will match the literal string `AcmeSecret` and **not**
match `acmesecret`, `ACMESECRET`, or `Acmesecret`.

This bias is "no false positives" rather than "maximum recall":

- Strict default suits **codenames, project IDs, and case-meaningful
  identifiers** — e.g. `Java` (the language) shouldn't match every
  capitalisation of `JAVA` (the org you're protecting).
- Strict default is **risky for PII shapes that humans type
  inconsistently** — names, domains, customer identifiers. If your
  denylist is mostly those, every entry should set
  `case_sensitive: false`.

The safest authoring pattern: decide per entry. The schema example
below illustrates both modes — the project codename keeps the strict
default; the employee name opts in to case-insensitive.

If your denylist is overwhelmingly PII (employee names, customer
names, etc.), consider authoring a small wrapper that loads with
`case_sensitive: false` as the default for every entry, or move that
list into a separate `DENYLIST_REGISTRY` entry where you can
maintain it under that posture.

## Backend choice

Two backends ship, controlled by `DENYLIST_BACKEND`:

- **`regex`** *(default)* — two compiled `re` alternations
  (case-sensitive + case-insensitive). Pure stdlib, fast up to
  low-thousands of entries; lower constant factor than Aho-Corasick
  on small lists.
- **`aho`** — Aho-Corasick via [`pyahocorasick`](https://pypi.org/project/pyahocorasick/).
  Sub-linear in pattern count; flat scan time even when the list has
  tens of thousands of entries. Word boundaries are post-filtered
  (Aho-Corasick is pure literal matching), so behaviour matches the
  regex backend exactly — both paths share the cross-backend test
  suite.

Switch with `DENYLIST_BACKEND=aho`. The Containerfile bakes in both
so this is a runtime flip, no rebuild needed. Direct `pip install`
users opting into aho need the `denylist-aho` extra:

```bash
pip install "anonymizer-guardrail[denylist-aho]"
```

An invalid value crashes loud at boot; selecting `aho` without
`pyahocorasick` installed raises a `RuntimeError` naming the extra
to install.
