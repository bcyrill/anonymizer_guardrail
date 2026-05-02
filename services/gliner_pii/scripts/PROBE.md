# `probe.py` — gliner-pii service probe

`scripts/probe.py` hits a running gliner-pii service with a text +
label list and prints the matches plus a coverage summary (which
labels produced matches, which didn't). Use it to figure out which
zero-shot labels work on your data before wiring them into the
guardrail's `GLINER_PII_LABELS`.

Stdlib-only — runs from any checkout without installing the
service's Python deps. To start the service first, see the parent
[README](../README.md) or run
`scripts/launcher.sh -d gliner_pii --gliner-pii-backend service`.
The default URL is `http://localhost:8002`; override with `--url`.
See `python services/gliner_pii/scripts/probe.py --help` for the
full flag list.

## Input shapes

```bash
# Single inline text, comma-separated labels:
python services/gliner_pii/scripts/probe.py \
    --text "Alice Smith works at Acme Corp" \
    --labels person,organization

# Repeatable --label flags compose with --labels:
python services/gliner_pii/scripts/probe.py \
    --text-file sample.txt \
    --label person --label company --label address

# Read from stdin:
cat sample.txt | python services/gliner_pii/scripts/probe.py \
    --text-file - --labels ssn,credit_card

# Non-default URL (CI runner, remote host, …):
python services/gliner_pii/scripts/probe.py \
    --url http://gliner.internal:8002 \
    --text "..." --labels phone_number

# Raw JSON for scripting:
python services/gliner_pii/scripts/probe.py \
    --text "..." --labels person --json | jq '.matches[].score'
```

## Exploring zero-shot labels

GLiNER takes the label list as a soft prompt, so the *string* of
each label matters: it can identify entities for labels that aren't
explicitly in its training data, especially when the label name is
descriptive (`project_codename`) rather than abstract (`X`). The
coverage summary at the bottom of the table tells you which labels
landed.

```bash
# Niche label not in the bundled DEFAULT_LABELS — does the model
# generalize to it?
python services/gliner_pii/scripts/probe.py \
    --text "Project Zephyr launches Q3; lead is bob@acme.com." \
    --labels project_codename,email

# Domain-specific labels (medical):
python services/gliner_pii/scripts/probe.py \
    --text "Patient prescribed Lisinopril for hypertension." \
    --labels medication,diagnosis

# Side-by-side comparison against the bundled engagement transcript.
# `engagement_notes.txt` is a synthetic red-team transcript shipped
# alongside the script (IPs, hostnames, AWS keys, GitHub PATs, NTLM
# hashes, JWTs, plaintext passwords, license plates, MAC addresses).
#
# Labels here use the verbatim training-set names from the table in
# docs/detectors/gliner-pii.md (e.g. `company_name` not `organization`,
# `ipv4` not `ip_address`). See "Empirical findings" below for which
# of these actually fire on this fixture and why.
python services/gliner_pii/scripts/probe.py \
    --text-file services/gliner_pii/scripts/engagement_notes.txt \
    --labels person,company_name,ipv4,mac_address,url,password,api_key,license_plate

# Lower the threshold to surface marginal matches when probing
# whether an unusual label registers at all (default cutoff is tuned
# for production precision, not exploration):
python services/gliner_pii/scripts/probe.py \
    --text "User @alice_42 sent 0.5 BTC to bc1qxy2…" \
    --labels username,cryptocurrency_address --threshold 0.2

# Same text, two label phrasings — does the model prefer one over the
# other? (Run twice and compare the score column.)
python services/gliner_pii/scripts/probe.py \
    --text "Reach Bob at +1 415-555-0123." --labels phone_number
python services/gliner_pii/scripts/probe.py \
    --text "Reach Bob at +1 415-555-0123." --labels telephone
```

## Empirical findings

Numbers below come from running the example commands above against
`engagement_notes.txt` as it ships in this repo. The point isn't the
exact scores — those will drift as the model and fixture evolve —
but the *patterns* that come out of probing.

**1. Context dominates threshold.** The same plaintext password
hits very differently depending on what's around it:

| Run | Threshold | Result |
|---|---|---|
| `password` over the whole transcript | 0.05 | no matches |
| `password` over the whole transcript | 0.20 | no matches |
| `password` over a single prose sentence with explicit `weak password (…)` cue | 0.50 | **0.999** on `jenkinsCI123` |

Lowering the threshold further didn't help on the full file. GLiNER
isn't searching the whole document for password-shaped strings; it
scans for entity-shaped spans whose *immediate* surrounding prose
anchors the label. Tabular contexts (NTDS dumps, `.env`-style
`KEY = VALUE` blocks, embedded URL credentials like
`postgres://user:pw@host`) lose that anchor and the entity becomes
invisible to the model.

**2. Distinctive prefixes only carry you so far.** Running
`api_key` over the same transcript caught the GitHub PAT
(`ghp_a1b2…` → 1.000) but missed the AWS access key
(`AKIA4QWERTYUIOPASDFG`) and the JWT, even at threshold 0.20.
Both AWS keys and JWTs have unmistakable shapes — but they sit in
the same `.env`-style block as everything else the model also
missed. Shape isn't enough; GLiNER wants prose context.

**3. `url` doesn't fire on bare FQDNs.** None of the
`*.acmecorp.local` hostnames matched `url`, despite `url` being a
training-set label. The label appears tuned for full URLs with a
scheme (`https://…`); bare hostnames are out of distribution.

**4. Verbatim training-set names matter.** `company_name` matched
`Globex Industries` (0.998) but the older `organization` phrasing
returned nothing on prior runs. Same story for `ipv4` vs
`ip_address`, `license_plate` vs `vehicle_registration`. When in
doubt, copy the label string verbatim from the table in
`docs/detectors/gliner-pii.md`.

**5. Confidence drops on partial / weakly-anchored mentions.**
"Mike Hernandez" matched `person` at 0.998; a later bare "Mike"
matched at 0.577. Anything below ~0.6 is worth treating as
exploratory until you've audited a sample.

**6. Three buckets emerged for this corpus:**

| Class | What works |
|---|---|
| **Prose-anchored entities** (person, company_name, license_plate, mac_address as standalone tokens) | GLiNER, default threshold |
| **Shape-anchored entities with rare prefixes** (api_key for `ghp_*` PATs, ipv4 for `10.0.x.y` in prose) | GLiNER, but only when a prose sentence frames them |
| **Structured-text entities** (NTLM / SHA hashes, AWS access keys in `.env` blocks, JWT bearer tokens, bare FQDNs, `postgres://user:pw@host` strings) | **Regex.** GLiNER won't fire reliably regardless of threshold or label phrasing. |

**Practical takeaway:** GLiNER earns its keep on contextual
entities the model can pick out of *flowing prose*. For shape-driven
secrets buried in dumps, configs, or one-line credential blobs,
pair it with a regex detector — that's what
`docs/detectors/regex.md` and the `_regex_pentest.yaml` pattern set
exist for. Don't try to make GLiNER do a regex's job by hunting for
the right zero-shot label string; the misses above hold across
threshold and phrasing.
