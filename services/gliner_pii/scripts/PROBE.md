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

# Side-by-side comparison: do creative labels add coverage on top of
# the standard PII set, or duplicate it?
python services/gliner_pii/scripts/probe.py \
    --text-file engagement_notes.txt \
    --labels person,organization,internal_hostname,vehicle_registration,api_key

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
