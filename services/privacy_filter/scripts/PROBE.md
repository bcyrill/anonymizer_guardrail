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
- **Type granularity.** Privacy-filter collapses IBAN, credit
  card, and SSN under one `IDENTIFIER` umbrella. If you need them
  separately tagged, the regex detector (or gliner-pii's specific
  labels) will give you finer types.
