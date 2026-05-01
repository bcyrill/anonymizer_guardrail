# Regex detector

High-precision patterns for things with recognizable shapes: IPs,
CIDRs, emails, hashes, JWTs, AWS keys, GitHub tokens, OpenAI-style
keys, internal hostnames (`*.local`, `*.internal`, etc.). Stateless,
deterministic, no external dependencies.

Enabled by including `regex` in `DETECTOR_MODE`. The default config
loads the bundled conservative pattern set; override by pointing
`REGEX_PATTERNS_PATH` at a different file.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `REGEX_PATTERNS_PATH` | *(empty → bundled `regex_default.yaml`)* | Override the bundled regex patterns YAML. Accepts `bundled:NAME` or a filesystem path. |
| `REGEX_PATTERNS_REGISTRY` | *(empty)* | Comma-separated `name=path` list of NAMED alternative regex pattern files callers can opt into per-request via `regex_patterns`. See [per-request overrides → Named alternatives](../per-request-overrides.md#named-alternatives). |
| `REGEX_OVERLAP_STRATEGY` | `longest` | `longest` (longest match wins on overlapping spans) or `priority` (first pattern in YAML order wins). See [Overlap resolution](#overlap-resolution) below. |

The regex detector has no concurrency cap — patterns run against the
input string in microseconds, so throttling would only add latency.

## Per-request overrides

Two regex-specific keys can be passed in
`additional_provider_specific_params` (see
[per-request overrides](../per-request-overrides.md) for the general
shape):

| Override key | Type | Effect |
|---|---|---|
| `regex_overlap_strategy` | `"longest"` or `"priority"` | Override `REGEX_OVERLAP_STRATEGY` for this call. |
| `regex_patterns` | `string` | Name of a registered alternative regex pattern set. Looked up in `REGEX_PATTERNS_REGISTRY`; unknown names log a warning and fall back to the default pattern set. |

## Customising the patterns

Two pattern files ship with the package under
`src/anonymizer_guardrail/patterns/`:
`src/anonymizer_guardrail/patterns/`:

- `regex_default.yaml` — small, conservative, low-FP set (loaded by default).
- `regex_pentest.yaml` — `extends: regex_default.yaml` plus all 173 patterns
  ported verbatim from
  [DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI/blob/main/src/regex_detector.py)
  (cloud creds, NTDS dumps, hashcat output, Pacu, Volatility, BloodHound,
  K8s secrets, Slack/Teams formats, AD CS templates, etc.). Tuned for
  pentest output and noisy in non-security contexts — opt in deliberately.

`REGEX_PATTERNS_PATH` accepts the same two forms as
`LLM_SYSTEM_PROMPT_PATH`: `bundled:<filename>` for in-package files, or a
filesystem path. To start from the pentest set:

```bash
podman run --rm -p 8000:8000 \
  -e REGEX_PATTERNS_PATH=bundled:regex_pentest.yaml \
  anonymizer-guardrail:latest
```

Or supply your own file. Each entry is `{type, pattern, flags?}`; `extends:`
inherits another file's patterns (bare filename → bundled lookup, path with
`/` → on-disk). Your file's own patterns load **first**, then the inherited
chain — child-overrides-parent semantics, so a stricter local pattern wins
over a looser one inherited from default. When a pattern declares one or
more capturing groups, the first non-None group's span is treated as the
entity (lets a labeled pattern like `password:\s+(\S+)` anonymize only the
value, not the label). Patterns without groups still anonymize the full
match. All patterns compile at startup; any bad regex, unknown flag, or
unreadable extends path crashes the boot rather than silently dropping
rules.

## Overlap resolution

When two patterns from the loaded YAML match overlapping spans, the
`REGEX_OVERLAP_STRATEGY` env var picks the winner:

- **`longest`** *(default)* — the longer span wins. Ties broken by
  earliest start, then by YAML order. Recommended whenever you load
  the pentest set or any other large pattern bundle, where a narrow
  pattern in one file can accidentally match a substring of a wider
  pattern in another. Concretely, the pentest YAML's `\b\d{12}\b` AWS
  Account ID pattern would otherwise eat the trailing 12-digit group
  of any UUID whose last segment is all-digits, leaving the regex
  layer with only the inner span instead of the whole UUID.
- **`priority`** — first pattern in YAML order wins (the pre-v0.2
  behaviour). Useful when patterns are deliberately ordered
  most-specific-first and that ordering is load-bearing.

Both strategies pay the same regex cost: every pattern still scans the
text via `finditer` (Python's `re` engine has no API to skip
already-claimed regions). The strategy only changes how candidate
matches are resolved at adoption time.
