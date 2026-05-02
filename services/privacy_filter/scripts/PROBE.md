# `probe.py` — privacy-filter service probe

`scripts/probe.py` hits a running privacy-filter service with a
text input and prints the spans it returns, plus a count of spans
per opf label.

The probe deliberately shows raw opf labels (`private_email`,
`private_person`, `private_phone`, `private_url`, `private_address`,
`private_date`, `account_number`, `secret`) rather than the
canonicalised names the guardrail emits (`EMAIL_ADDRESS`, `PERSON`,
…) — this is a window onto what the service does, not an end-to-end
view. The *Empirical findings* section below uses the canonical
names where it describes pre-migration HF-pipeline behaviour;
post-migration tables (here and in *After the opf migration*) use
raw opf labels.

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
  vocabulary baked in at training time. Labels you can expect:
  `private_person`, `private_email`, `private_phone`,
  `private_url`, `private_address`, `private_date`,
  `account_number` (catch-all for IBAN / CC / SSN-shaped strings),
  `secret`. To detect custom categories like
  `vehicle_registration`, use gliner-pii (zero-shot) or a regex.
- **No `--threshold`.** Different reason than gliner-pii: opf's
  `DetectedSpan` doesn't expose a per-span confidence at all, so
  there's no score to threshold on. The wire format omits the
  `score` field for that reason — for confidence-sensitive routing,
  layer the regex detector with stable shape anchors on top.
- **Span merging happens client-side.** The service emits raw
  `DetectedSpan` records straight from opf's Viterbi decoder. Label
  canonicalisation, paragraph-break split, and per-label gap-cap
  merging all run in the guardrail-side
  `RemotePrivacyFilterDetector._to_matches`. The probe shows opf's
  raw output — same tokens you'd see if you called `OPF.redact()`
  directly.
- **Default port 8001** (gliner-pii is on 8002, so both can run
  side-by-side on the same docker network).

## Examples

`sample.txt` is a synthetic customer-service ticket shipped
alongside the script — covers every entry in the model's label map
(person, email, phone, URL, address, date, IBAN / credit
card / SSN as `account_number`, password reference as `secret`).
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

# Raw JSON for scripting — e.g. just the email spans (opf labels
# are emitted raw; the guardrail-side detector is what canonicalises
# `private_email` → `EMAIL_ADDRESS`):
python services/privacy_filter/scripts/probe.py \
    --text-file services/privacy_filter/scripts/sample.txt \
    --json | jq '.spans[] | select(.label == "private_email")'
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
- **Confidence shape.** Asymmetric: gliner-pii's wire format
  carries per-span scores you can compare side-by-side (see
  [gliner-pii PROBE.md → Empirical findings](../../gliner_pii/scripts/PROBE.md#empirical-findings)).
  Privacy-filter's opf-based decoder doesn't expose one, so the
  comparison is binary (matched / didn't match) rather than
  graded. For confidence-sensitive routing across both, the
  regex detector with stable shape anchors is the disambiguator.
- **Type granularity.** Privacy-filter advertises IBAN, credit
  card, and SSN as one `account_number` umbrella, but in practice
  the umbrella is leaky — IBAN fires, credit card doesn't (see
  finding 3 in *Empirical findings*). For finer types, lean on
  the regex detector (or gliner-pii's specific labels).

### What we observed (engagement_notes.txt run)

Coverage on this red-team fixture (probed against the post-opf
service — span offsets and labels from a fresh run):

| Class | privacy-filter | gliner-pii |
|---|---|---|
| Full-name persons (`Sarah Chen`, `Mike Hernandez`) | ✓ as `private_person` | ✓ at 0.998–0.999 |
| Bare-first-name `Mike;` | ✓ as `private_person` | ✓ at 0.577 (weak) |
| Bare FQDN `dc01.acmecorp.local` | ✓ but as `secret` — model misclassification of an FQDN that happens to look credential-shaped | ✗ (`url` needs a scheme) |
| Bare FQDN `siem.acmecorp.local` | ✓ as `private_url` | ✗ (`url` needs a scheme) |
| AWS access key + secret block (`.env`-style `KEY = VALUE`) | ✓ as `secret` — clean span `[992:1079]` covers `AKIA…` through the end of the `AWS_SECRET_ACCESS_KEY = …` line. Post-opf decoder no longer swallows the next-paragraph heading. | ✗ |
| GitHub PAT (`ghp_…` in parenthetical prose) | ✓ as `secret` `[1113:1153]` | ✓ as `api_key` at 1.000 |
| JWT bearer token | ✓ as `secret` `[2080:2145]` — JWT alone, no trailing heading bleed | ✗ |
| SHA-256 binary-hash hex string | ✓ as `secret` `[2185:2249]` — separate clean span | ✗ |
| Plaintext password `Sp4rkl3-Pony!@` (in `postgres://user:pw@host`) | ✓ as `secret` | ✗ (`password` label not configured) |
| Plaintext password `jenkinsCI123` (in parenthetical prose) | ✓ as `secret` | ✗ (`password` label not configured) |
| NTDS dump line (`bob.smith:1107:aad…:88…:::`) | ✓ as one tight `secret` span `[1606:1689]` | ✗ |
| Service account `svc_jenkins` | ✓ but as `private_person` — model treats `svc_*` like a personal handle (false-positive label) | ✗ |
| MAC address (`04:7c:16:a2:f3:9b`) | ✗ (no MAC label) | ✓ as `mac_address` at 1.000 |
| IPv4 addresses | partial — 1 of 6 mentions (`10.0.5.10` parenthetical) labelled as `private_url`; the five `nmap` table IPs all missed | partial — 2 of 5 unique IPs (`10.0.7.18`, `10.0.7.42` at 0.8–0.99); missed `10.0.7.55`, `10.0.12.4` |
| Org names | ✗ (no label) | partial — `Globex Industries` at 0.998; **`AcmeCorp` missed** despite many mentions |
| License plate `7XKR492` (in flowing prose) | ✗ | ✓ as `license_plate` at 0.972 |
| License plate `4FFL339` (rental, parenthetical) | ✓ as `account_number` `[1491:1498]` — but the canonical-name path maps `account_number` → `IDENTIFIER`, semantically wrong here | ✓ as `license_plate` |
| Plaintext password `Summer2024!` (NT hash crack quote) | ✗ | ✗ |
| Plaintext password `staging-DB-2024` | ✗ | ✗ |

**Three takeaways:**

1. **opf shifted the privacy-filter / gliner-pii balance toward
   privacy-filter for credential-shaped secrets.** Pre-migration
   privacy-filter missed the GitHub PAT, the postgres password,
   and `jenkinsCI123`; opf catches all three as `secret`. The
   AWS key block and JWT — already privacy-filter strengths —
   are now cleaner (no trailing-heading bleed). The detectors
   are still complementary, but the gap shrank: gliner-pii is
   still the only path for `mac_address`, `company_name`, and
   most `ipv4`s, plus the `7XKR492` plate; privacy-filter now
   carries most credential / plaintext-password work.

2. **Two model misclassifications worth knowing about.**
   `dc01.acmecorp.local` → `secret` (treats the FQDN as a
   credential because it sits next to `AWS_SECRET_ACCESS_KEY` in
   a `.env`-style block); `svc_jenkins` → `private_person` (the
   `svc_` prefix isn't enough context for the model to disambiguate
   service account from human handle). Neither is a decoder
   issue — they're training-data artefacts. The regex layer with
   shape anchors is the disambiguator if these occur in your
   corpus.

3. **NT hash plaintext (`Summer2024!`, etc.) is still a regex
   job.** opf catches the *NTLM hash dump* (`bob.smith:1107:…:::`)
   tightly but not the plaintext password quoted from the crack
   step. Same goes for credential-like strings inside structured
   layouts the model wasn't shown at training. Layer the regex
   detector with credential patterns — see
   `docs/detectors/regex.md` and the bundled
   `regex_pentest.yaml`.

## Empirical findings

> **Note (post-Viterbi migration):** the findings below were
> captured against the original `transformers.pipeline(
> aggregation_strategy="first")` integration. The service has
> since switched to opf's constrained Viterbi decoder
> (`OPF.redact()`, see TASKS.md → "opf-based Viterbi decoding"),
> which produces materially different output: zero `\n\n`
> over-merges, tighter span boundaries (no trailing
> `,`/`.`/`)` absorbed), and higher recall on the entities
> below. See "After the opf migration" further down for the
> updated picture; the older findings are kept for historical
> context and to motivate the per-label gap caps and
> paragraph-split pass that still run as defense in depth.

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

**2. Span-merge no longer over-extends across `\n\n` paragraph
breaks (fixed).** Pre-fix, three over-merged spans on
`sample.txt`:

| Span (pre-fix) | Captured text | What was wrong |
|---|---|---|
| `[164:191]` `DATE_OF_BIRTH` | `2026-04-17 09:42\n\nCustomer:` | "Customer:" header glued onto the date |
| `[803:827]` `DATE_OF_BIRTH` | `2026-04-17.\n\nResolution:` | "Resolution:" heading glued onto the date |
| `[300:367]` `ADDRESS` | `742\nEvergreen Terrace, Springfield, IL 62701, United States.\n\nIssue` | "Issue" header from the next paragraph silently absorbed into the address |

Two layers of defense, both in
`src/anonymizer_guardrail/detector/remote_privacy_filter.py`
(post-processing now lives client-side; the service emits raw
opf `DetectedSpan` records and the guardrail's `_to_matches`
canonicalises labels and applies the merge / split passes):

- **Merge side** (`_DEFAULT_GAP_PATTERN`): refuses to combine two
  adjacent same-type spans when the gap between them contains a
  `\n\n`. Pattern tightened from `\s+` to
  `[^\S\n]*\n?[^\S\n]*` — at most one newline allowed inside a
  gap, with arbitrary non-newline whitespace on either side.
  Single-newline merges (in-paragraph line wrapping, `\r\n`)
  still work normally.
- **Split side** (`_PARAGRAPH_BREAK`, new step 3 in `_to_matches`):
  breaks any *single* span the pipeline already aggregated across
  a `\n\n`. This is the case that bit `sample.txt` — HF's
  `aggregation_strategy="first"` collapsed adjacent same-entity
  tokens regardless of whitespace in the source, so the merge
  regex never saw two spans to keep apart. The split pass mirrors
  the merge rule from the opposite direction.

Post-fix on `sample.txt`, the same three spans become six:
`2026-04-17 09:42` + `Customer:` (separate DATE_OF_BIRTH spans),
`2026-04-17.` + `Resolution:`, and `742\nEvergreen Terrace,
Springfield, IL 62701, United States.` + `Issue`. The half-spans
that are still model misclassifications (`Customer:`,
`Resolution:`, `Issue`) are now isolated rather than corrupting a
correctly-tagged neighbor — a downstream confidence filter or
regex layer can drop them without taking a real entity along.

Locked in by `tests/test_privacy_filter.py::test_paragraph_break_blocks_merge`,
`test_paragraph_break_splits_one_span`, and
`test_single_newline_still_merges`.

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
guardrail-side `RemotePrivacyFilterDetector` are designed to
compose with.

## After the opf migration

Captured by the Phase-1 spike at
`services/privacy_filter/scripts/spike_opf.py` and the post-
migration probes against `sample.txt` and `engagement_notes.txt`.
The decoder swap (HF aggregation → opf Viterbi) changed enough
that the table-by-table comparisons above are no longer
representative — this section captures the new picture.

**1. Layout sensitivity is largely gone for emails.** All three
shapes now fire as `private_email`:

| Input shape | `private_email` result |
|---|---|
| `Contact Jane Doe at jane.doe@example.com …` (clean prose) | hit |
| `…reachable at\njane.doe@example.com …` (line break) | hit |
| `Bob Roberts\n(bob.roberts@firstnationalbank.com, …)` (parenthetical after a name) | hit (the leading `(` bleeds in cosmetically — `_to_matches`'s `.strip()` doesn't reach inside, but the surrogate replaces the noisy span and the `(` reappears in the rendered output) |

This was the headline pre-migration regression for
privacy-filter; opf's Viterbi decodes the `Bob Roberts`
`private_person` span and the email span as separate entities
rather than one person-span glued together.

**2. `\n\n` over-merges no longer occur.** The three over-merged
spans on `sample.txt` (`[164:191]` `2026-04-17 09:42\n\nCustomer:`,
`[300:367]` `742\nEvergreen Terrace, …\n\nIssue`, `[803:827]`
`2026-04-17.\n\nResolution:`) all produced clean spans
post-migration without the split pass having to fire. The split
pass is kept as defense in depth.

**3. Span boundaries are tighter.** opf doesn't absorb trailing
`,`/`.`/`)` into spans:

| Pre-migration (canonical) | Post-migration (raw opf) |
|---|---|
| `1987-03-22),` (DATE_OF_BIRTH) | `1987-03-22` (`private_date`) |
| `+1 415-555-0181.` (PHONE) | `+1 415-555-0181` (`private_phone`) |
| `+1 212-555-0107),` (PHONE) | `+1 212-555-0107` (`private_phone`) |
| `https://...8e5a2f1c.` (URL) | `https://...8e5a2f1c` (`private_url`) |
| `Marcus Chen,` (PERSON) | `Marcus Chen` (`private_person`) |
| `88421.` (IDENTIFIER) | `88421` (`account_number`) |

The `SSN 123-45-6789` prefix-absorption case is unchanged —
that's the model labeling the prefix word as part of the entity,
not a decoder boundary issue.

**4. Recall improvements on `sample.txt`.** opf catches entities
the HF integration missed (pre-migration column = canonical names
from the old wire format; post-migration = raw opf labels):

| Entity | Pre-migration (canonical) | Post-migration (raw opf) |
|---|---|---|
| `jane.doe@example.com` | missed | `private_email` |
| `SVC-2026-04-117` (ticket id) | missed | `account_number` |

**5. Recall improvements on `engagement_notes.txt` are bigger.**
opf labels things HF privacy-filter missed entirely; previously
these had to be picked up by gliner-pii or the regex layer:

| Entity | Pre-migration (canonical) | Post-migration (raw opf) |
|---|---|---|
| `ghp_a1b2c3d4…` (GitHub PAT) | missed | `secret` |
| `Sp4rkl3-Pony!@` (postgres password) | missed | `secret` |
| `jenkinsCI123` (plaintext password) | missed | `secret` |
| `3a7bd3e2…` (SHA-256 binary hash) | missed | `secret` |
| `siem.acmecorp.local` | `CREDENTIAL` (wrong type) | `private_url` |
| NTDS dump `bob.smith:1107:…` | partial `IDENTIFIER` 0.361 | one tight `secret` span |
| `10.0.5.10` (IP) | missed | partial — 1 of 6 mentions caught as `private_url`; the five `nmap`-table IPs still missed |

**6. New label-set surprises.** opf is willing to label more
strings as `secret` than HF was, including some that aren't
secrets in any meaningful sense (`dc01.acmecorp.local` → `secret`,
`4FFL339` → `account_number`, `svc_jenkins` → `private_person`).
These are model-side training-data choices, not decoder issues —
the regex layer or gliner-pii's specific labels are the right
place to disambiguate. See TASKS.md → "Shape-anchored regex
override tier in privacy-filter" for the planned fix.

**7. Calibration tuning is now optional.** The spike's three
calibration profiles (default / anti-merge / privacy-parser)
produced identical span tables on `sample.txt` and differed on
exactly one span on `engagement_notes.txt` — the NTDS hash
boundary. Default Viterbi is the production decoder; the
`PRIVACY_FILTER_CALIBRATION` env var is reserved for future
corpora that need tuning.

**Disposition of the merge / split / per-label gap caps:**

All three post-processing passes are **kept as defense in
depth**. The migration spike showed they don't fire on either
fixture, but production traffic is broader than fixtures and the
runtime cost is negligible. Re-evaluate removal after collecting
production data showing none of them ever fires across N
requests.
