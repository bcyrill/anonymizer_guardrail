# `probe.py` — privacy-filter service probe

`scripts/probe.py` hits a running privacy-filter service with a
text input and prints the matches it returns, plus a count of
matches per entity type.

Stdlib-only — runs from any checkout without installing the
service's Python deps. To start the service first, see the parent
[README](../README.md) or run
`scripts/launcher.sh -d privacy_filter --privacy-filter-backend service`.
The default URL is `http://localhost:8001`; override with `--url`.
See `python services/privacy_filter/scripts/probe.py --help` for
the full flag list.

## privacy-filter vs gliner-pii (when probing)

If you've used [the gliner-pii probe](../../gliner_pii/scripts/PROBE.md),
the practical contrasts when running this one:

- **No `--labels`.** The privacy-filter model has a *fixed*
  vocabulary baked in at training time. Entity types you can
  expect: `PERSON`, `EMAIL_ADDRESS`, `PHONE`, `URL`, `ADDRESS`,
  `DATE_OF_BIRTH`, `IDENTIFIER` (catch-all for IBAN / CC / SSN-
  shaped strings), `CREDENTIAL`. Anything that doesn't fit a
  trained label is dropped or routed through `OTHER`. To detect
  custom categories like `vehicle_registration`, use gliner-pii
  (zero-shot) or a regex.
- **No `--threshold`.** The service returns every span the model
  emits with its raw confidence score. Filter on the `score`
  column yourself if you want to drop low-confidence matches.
- **Span merging happens server-side.** Adjacent same-type tokens
  are combined into one match (so "Alice Smith" comes back as one
  `PERSON` span, not two). See `_to_matches` in
  `services/privacy_filter/main.py` for the rules.
- **Default port 8001** (gliner-pii is on 8002, so both can run
  side-by-side on the same docker network).

## Examples

`sample.txt` is a synthetic customer-service ticket shipped
alongside the script — covers every entry in the model's label map
(person, email, phone, URL, address, date of birth, IBAN / credit
card / SSN as `IDENTIFIER`, password reference as `CREDENTIAL`).
Use it as a smoke fixture or as scaffolding for your own.

```bash
# Single inline text:
python services/privacy_filter/scripts/probe.py \
    --text "Alice Smith (alice@acme.com) lives at 123 Main St."

# Bundled fixture covering every trained label:
python services/privacy_filter/scripts/probe.py \
    --text-file services/privacy_filter/scripts/sample.txt

# Read from stdin (your own file or piped output):
cat services/privacy_filter/scripts/sample.txt | \
    python services/privacy_filter/scripts/probe.py --text-file -

# Non-default URL (CI runner, remote host, …):
python services/privacy_filter/scripts/probe.py \
    --url http://privacy.internal:8001 \
    --text-file services/privacy_filter/scripts/sample.txt

# Raw JSON for scripting — e.g. only high-confidence matches:
python services/privacy_filter/scripts/probe.py \
    --text-file services/privacy_filter/scripts/sample.txt \
    --json | jq '.matches[] | select(.score > 0.9)'
```

## Comparing detectors on the same fixture

The gliner-pii probe ships with a synthetic red-team transcript at
`services/gliner_pii/scripts/engagement_notes.txt`. It's a useful
comparison fixture for privacy-filter too — same prose, different
detector — to see where each model's strengths and gaps lie.

```bash
# Run privacy-filter against the same transcript gliner-pii uses:
python services/privacy_filter/scripts/probe.py \
    --text-file services/gliner_pii/scripts/engagement_notes.txt

# Then run the same fixture through gliner-pii (different port) and
# compare the two output tables:
python services/gliner_pii/scripts/probe.py \
    --text-file services/gliner_pii/scripts/engagement_notes.txt \
    --labels person,company_name,ipv4,mac_address,url,password,api_key,license_plate
```

What to look for:

- **Coverage overlap.** Both should fire on the obvious entities
  (people, addresses, emails). Where one fires and the other
  doesn't is informative — it tells you which detector to lean on
  for each entity class.
- **Confidence shape.** Privacy-filter typically gives uniformly
  high scores within its trained vocabulary; gliner-pii's scores
  spread more, with the bottom end being weakly-anchored mentions
  (see [gliner-pii PROBE.md → Empirical findings](../../gliner_pii/scripts/PROBE.md#empirical-findings)).
- **Type granularity.** Privacy-filter advertises IBAN, credit
  card, and SSN as one `IDENTIFIER` umbrella, but the empirical
  findings below show the umbrella is leaky — IBAN fires, credit
  card doesn't. For finer types, lean on the regex detector
  (or gliner-pii's specific labels).

### What we observed (engagement_notes.txt run)

The two detectors had **near-orthogonal** coverage on this
red-team fixture:

| Class | privacy-filter | gliner-pii |
|---|---|---|
| Full-name persons (`Sarah Chen`, `Mike Hernandez`) | ✓ at 1.000 | ✓ at 0.998–0.999 |
| Bare-first-name persons (`Mike;`) | ✓ at 0.557 (weak) | ✓ at 0.577 (weak) |
| Bare FQDNs (`dc01.acmecorp.local`, `siem.acmecorp.local`) | ✓ as `URL` (~0.95) | ✗ (`url` needs a scheme) |
| AWS access key + secret block (`.env`-style `KEY = VALUE`) | ✓ as `CREDENTIAL` at 0.968 (merged span) | ✗ |
| JWT bearer token | ✓ as `CREDENTIAL` at 1.000 | ✗ |
| GitHub PAT (`ghp_…` in parenthetical prose) | ✗ | ✓ as `api_key` at 1.000 |
| MAC address (`04:7c:16:a2:f3:9b`) | ✗ (no label) | ✓ as `mac_address` at 1.000 |
| IPv4 addresses | ✗ (no label) | partial — 2 of 5 unique IPs (`10.0.7.18`, `10.0.7.42` at 0.8–0.99); missed `10.0.5.10`, `10.0.7.55`, `10.0.12.4` |
| Org names | ✗ (no label) | partial — `Globex Industries` at 0.998; **`AcmeCorp` missed** despite many mentions |
| License plate (`7XKR492, CA`) | ✓ as `IDENTIFIER` at 0.489 (weak) | ✓ as `license_plate` at 0.972 |
| NTLM hashes (NTDS dump line) | partial as `IDENTIFIER` at 0.361 | ✗ |
| Plaintext passwords (`Summer2024!`, `jenkinsCI123`) in dumps/configs | ✗ | ✗ (with `password` in the label list and `--threshold 0.05` retried — not a threshold issue) |

**Three takeaways:**

1. **They don't substitute for each other.** Privacy-filter's
   `CREDENTIAL` catches the multi-line AWS key block and the JWT
   that gliner-pii misses; gliner-pii's `mac_address`, `ipv4`,
   `company_name`, and `api_key` cover entities privacy-filter
   has no label for. On a pentest-style transcript, run **both**
   and compose — neither alone hits the majority of entities.

2. **The `\n\n` over-merge bug surfaces here too.** The AWS-key
   `CREDENTIAL` span at `[991:1088]` swallowed the next line *and*
   a `Spotted` heading; the JWT span at `[2079:2154]` trailed
   into the `SHA-256` heading on the following line. Same root
   cause as the over-merge on `sample.txt` —
   `services/privacy_filter/main.py:82`'s `_DEFAULT_GAP_PATTERN`
   matches `\n\n`, so adjacent same-type spans get merged across
   paragraph breaks.

3. **Plaintext passwords and NTLM hashes are a regex job.** Both
   detectors miss or weakly hit shape-stable secrets embedded in
   structured contexts (NTDS dumps, `KEY = VALUE` blocks,
   `postgres://user:pw@host` strings). The regex detector with
   credential patterns is the right layer for those — see
   `docs/detectors/regex.md` and the bundled
   `regex_pentest.yaml`.

## Empirical findings

Numbers below come from running the example commands above against
`sample.txt` and a couple of small isolation probes. Patterns that
hold beyond the exact fixture:

**1. Layout matters more than content for emails.** The same
email fires or misses depending on what's around it:

| Input shape | `EMAIL_ADDRESS` result |
|---|---|
| `Contact Jane Doe at jane.doe@example.com or call …` (clean prose, single line) | hit at 1.000 |
| `…reachable at\njane.doe@example.com or +1 …` (line-broken prose in `sample.txt`) | **missed entirely** |
| `Bob Roberts\n(bob.roberts@firstnationalbank.com, …)` (parenthetical after a name) | **absorbed into the `Bob Roberts` PERSON span** |

So the model can detect emails — it just loses them when the
prose layout breaks the local context (line breaks, parentheses
right after a name). For real-world data with quirky formatting
this is a recall risk; pairing privacy-filter with the regex
detector covers the gap.

**2. Span-merge over-extends across `\n\n` paragraph breaks.**
Two cases on `sample.txt`:

| Span | Captured text | What's wrong |
|---|---|---|
| `[164:191]` `DATE_OF_BIRTH` | `2026-04-17 09:42\n\nCustomer:` | "Customer:" header glued onto the date |
| `[803:827]` `DATE_OF_BIRTH` | `2026-04-17.\n\nResolution:` | "Resolution:" heading glued onto the date |

Source: `services/privacy_filter/main.py:82` —
`_DEFAULT_GAP_PATTERN = re.compile(r"\s+")` matches *any*
whitespace including double newlines, so adjacent same-type spans
get merged across paragraph breaks. Tightening that pattern (e.g.
forbidding `\n\n`, or capping merged-gap length) would fix this
without affecting normal in-paragraph merges.

**3. Credit cards don't fire as IDENTIFIER, even on the simplest
input.**

| Input | `IDENTIFIER` result |
|---|---|
| `IBAN DE89 3704 0044 0532 0130 00 …` (in `sample.txt`) | hit at 1.000 |
| `SSN 123-45-6789 was verified …` (in `sample.txt`) | hit at 0.895 (with `SSN ` prefix absorbed) |
| `Card on file: 4111-1111-1111-1111 (Visa).` (clean isolated probe) | **no matches** |
| `4111-1111-1111-1111` inside `sample.txt` | no matches |

The model card pitches `account_number` as a catch-all for
IBAN/CC/SSN-shaped strings, but in practice it skews heavily
toward IBAN. **Regex is the right tool for credit cards** (Luhn
check + 13-19 digit shape with optional separators) — see
`docs/detectors/regex.md` and the bundled `regex_pentest.yaml`
pattern set.

**4. Span boundaries pull in trailing punctuation.** Common
token-classification artifact, mostly cosmetic for redaction
purposes. Examples from `sample.txt`:

- `1987-03-22),` (DATE includes paren + comma)
- `+1 415-555-0181.` (PHONE includes period)
- `Marcus Chen,` (PERSON includes comma)
- `https://...8e5a2f1c.` (URL includes period)
- `SSN 123-45-6789` (IDENTIFIER includes the leading "SSN ")

For redaction this is tolerable — the surrogate replaces the noisy
span; the punctuation reappears where it belongs in the rendered
output. For downstream consumers that care about exact entity
strings (audit logs, dashboards), strip trailing `[.,;)]+` after
reading from the matches array.

**5. `CREDENTIAL` only fires on actual secret-shaped strings.**
The fixture's mentions of "temporary credentials", "password",
and "API token" produced zero `CREDENTIAL` matches. Expected: the
`secret` label maps to credential-shaped tokens (API keys, hashes,
auth strings), not to the abstract nouns. If you need to detect
the *concept* of credentials being discussed in prose, that's an
LLM-detector job, not privacy-filter's.

**Three buckets emerged for privacy-filter:**

| Class | What works |
|---|---|
| **Prose-anchored entities in clean prose** (PERSON, EMAIL_ADDRESS, PHONE, ADDRESS, URL, DATE_OF_BIRTH) | Privacy-filter, default config |
| **IBAN-shaped IDENTIFIERs in prose** | Privacy-filter |
| **Credit cards, SSNs you want tagged separately, anything embedded in tabular / parenthetical / line-broken layout** | **Regex.** Privacy-filter is layout-sensitive and CC-shy regardless of input simplicity. |

**Practical takeaway:** privacy-filter is excellent on prose-style
inputs (support tickets, ticketing notes, conversational
transcripts) for the standard PII set, but loses recall when the
text layout fragments local context. Layer the regex detector on
top — that's what `_to_matches` and the merge logic in the
in-process detector are designed to compose with.
