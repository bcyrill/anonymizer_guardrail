# Detector quality benchmark

`scripts/detector_bench.sh` scores a guardrail's detector mix
against a labelled corpus of texts. Where
`scripts/test-examples.sh` answers *"do the curl recipes still work
end-to-end?"*, this answers *"for THIS corpus on THIS detector mix,
what fraction of expected entities does the guardrail catch, and how
often does it falsely flag stuff that should be left alone?"*

Use it to:

- Pick a detector mix for a new workload (pentest vs legal vs
  healthcare have very different shapes — one corpus per workload).
- Decide whether a new detector (e.g. [`gliner_pii`](detectors/gliner-pii.md))
  earns its keep on your traffic before you turn it on in production.
- Detect quality regressions when a model / prompt / pattern set
  changes — bake the script into CI alongside `test-examples.sh`.

## Quick start

```bash
# Against a guardrail you're already running:
scripts/detector_bench.sh --config bundled:pentest

# Spawn a fresh test guardrail for the run, tear it down on exit.
# Run `scripts/launcher.sh --show-presets` for the full preset list.
scripts/detector_bench.sh --config bundled:pentest --preset regex-pentest

# Your own corpus, against a custom URL:
BASE_URL=http://my-host:8000 \
  scripts/detector_bench.sh --config tests/corpus/legal.yaml
```

The wrapper accepts the same `--preset`, `--port`, `--keep`
arguments as
[`test-examples.sh`](deployment.md#smoke-test) so the two scripts
slot into the same CI/dev-loop muscle memory. See
`scripts/detector_bench.sh --help` for the full flag list.

## Configuring the detector mix

There are two ways to choose which detectors run during the benchmark
— pick whichever fits the loop you're in:

**1. Run your own guardrail manually.** Start the guardrail however
you usually do (`scripts/launcher.sh …`, podman/docker, Kubernetes, etc.)
with whatever `DETECTOR_MODE` and per-detector env vars you want to
score. The benchmark calls `/health` on connect, prints the active
`DETECTOR_MODE`, and runs every case the running guardrail can serve.
Cases listing `requires: [name]` for a detector that isn't enabled
are skipped (not failed). Useful when you want to iterate on
detector tuning without restarting:

```bash
# Terminal 1 — running guardrail with your preferred config
scripts/launcher.sh -t default --detector-mode regex,denylist,privacy_filter --privacy-filter-backend service

# Terminal 2 — score it
scripts/detector_bench.sh --config bundled:pentest
```

The wrapper reads `$BASE_URL` (default `http://localhost:8000`) or
`--base-url`.

**2. Spawn a fresh test guardrail via `--preset NAME`.** The
benchmark backgrounds `scripts/launcher.sh --preset NAME` on port 8001,
waits for `/health`, runs the corpus, and tears the guardrail down on
exit (override with `--keep`). Use this for one-shot comparisons:

```bash
scripts/detector_bench.sh --config bundled:pentest --preset regex-default
scripts/detector_bench.sh --config bundled:pentest --preset regex-pentest
scripts/detector_bench.sh --config bundled:pentest --preset gliner-pii-service
scripts/detector_bench.sh --config bundled:pentest --preset regex-pentest-gliner-pii-service
```

Each preset bundles a coherent `--type` / `--detector-mode` / backend
choice — run `scripts/launcher.sh --show-presets` to see the full set
and what each one applies. Operator-supplied preset files
(`LAUNCHER_PRESETS_FILE` / `--presets-file`) work here too; the
benchmark just shells out to `launcher.sh --preset NAME`. To benchmark
a configuration that doesn't match any preset (e.g. just
`regex,denylist`), use the manual approach above.

The corpus's `overrides:` block layers
[per-request overrides](per-request-overrides.md) on top of either
mode, so the same corpus can score the same detector mix under
different prompt / pattern-set choices without restarting the
guardrail.

## Comparing detectors against the same corpus (`--compare`)

To answer *"which of my enabled detectors pulls its weight on this
corpus?"*, start the guardrail with **every** detector you want to
compare and pass `--compare`:

```bash
# Terminal 1 — guardrail with everything switched on
scripts/launcher.sh -t default --detector-mode regex,denylist,privacy_filter,llm \
    --privacy-filter-backend service --llm-backend service

# Terminal 2 — score each detector individually + the full mix
scripts/detector_bench.sh --config bundled:pentest --compare
```

`--compare` runs the corpus once per active detector (using the
per-request `detector_mode` override to filter the active set down
to one detector at a time) plus once with the full mix as a
baseline, then prints a side-by-side table:

```
                 Comparison (corpus: pentest)
┏━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━┓
┃ metric         ┃ regex ┃ denylist ┃ privacy_filter ┃  llm ┃ all ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━┩
│ recall         │  50%  │   0%     │  17%           │  58% │ 75% │
│ strict recall  │  75%  │   0%     │  25%           │  75% │100% │
│ type accuracy  │  50%  │   0%     │  17%           │  58% │ 75% │
│ precision      │ 100%  │ 100%     │ 100%           │ 100% │100% │
│ avg latency ms │   84  │  67      │ 312            │ 1240 │1530 │
└────────────────┴───────┴──────────┴────────────────┴──────┴─────┘
Highlighted cells = best value per row. Latency is 'lower wins'; the other
metrics are 'higher wins'.
```

Read it like this:

- A detector with low recall *but high precision* is doing
  something useful but narrow — keep it on if "all" is also high
  (it adds non-overlapping coverage), drop it if "all" loses
  nothing.
- A detector with poor precision (leaks `must_keep`) needs tuning
  before you ship it — it's actively flagging benign text.
- The "all" column is the upper-bound coverage you can get from
  the union; if it doesn't beat the single best detector by much,
  you may not need the whole mix.

`--compare` always exits 0 — it's an exploratory tool, not a CI
gate (use a single-variant run for that).

**Caveats:**

- `--compare` only makes sense when ≥ 2 detectors are active. With
  one (or zero), the script prints a hint and exits.
- Per-request `detector_mode` is a SUBSET filter — it can narrow
  the active set but cannot add a detector that wasn't built at
  boot. Start the guardrail with the full superset.
- The "all" row uses every detector that was active at startup,
  not necessarily every detector that exists. If `gliner_pii` isn't
  in your guardrail's DETECTOR_MODE, "all" excludes it.

### Worked example: pentest comparison end-to-end

A complete recipe — build the images, start the guardrail with every
detector wired in, then run `--compare` on the bundled pentest corpus.

```bash
# 1. Build every image needed for this run. Comma-separated -t builds
#    them all in one shot (added in this sprint).
./scripts/image_builder.sh --preset minimal-fakellm

# 2. Start the guardrail with everything switched on. Worth knowing
#    which flag does what:
#      -t default                       API container; ML deps live in sidecars.
#      -d ...                        every detector enabled.
#      --*-backend service           auto-start each sidecar (pf, gliner, fake-llm).
#      --regex-patterns bundled:...  pentest pattern set (cloud creds, hashes, …).
#      --llm-prompt bundled:...      pentest detection prompt.
#      --llm-model fake-model        fake-llm doesn't validate model names.
#      --llm-fail-open               degrade silently on LLM errors so the
#                                    benchmark scores detection quality, not
#                                    error policy. Production stays fail-closed.
#      --rules ...                   deterministic fake-LLM responses for the
#                                    benchmark (correct calls, mistypings,
#                                    hallucinations, malformed output).
./scripts/launcher.sh -t default -d regex,denylist,privacy_filter,gliner_pii,llm \
    --privacy-filter-backend service \
    --gliner-pii-backend service \
    --llm-backend service \
    --regex-patterns bundled:regex_pentest.yaml \
    --llm-prompt bundled:llm_pentest.md \
    --llm-model fake-model \
    --llm-fail-open \
    --rules ./services/fake_llm/rules.pentest.yaml

# 3. In another terminal, run the comparison:
./scripts/detector_bench.sh --config bundled:pentest --compare
```

Expected output (numbers vary with model / hardware):

```
Loading corpus: tests/corpus/pentest.yaml
Synthetic snippets typical of a security engagement: cracked
password artefacts, cloud creds, internal hostnames, employee
names embedded in prose, NTLM hashes, JWTs, etc.
Guardrail: http://localhost:8000  DETECTOR_MODE: regex,denylist,privacy_filter,gliner_pii,llm

Running variant: regex
Running variant: denylist
Running variant: privacy_filter
Running variant: gliner_pii
Running variant: llm
Running variant: all

                Comparison (corpus: Pentest engagement transcript)
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━┳━━━━━━┓
┃ metric           ┃ regex ┃ denylist ┃ privacy_filter ┃ gliner_pii ┃ llm ┃  all ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━╇━━━━━━┩
│ recall           │   67% │       0% │            33% │        40% │ 60% │  93% │
│ strict recall    │  100% │       0% │            30% │        30% │ 50% │ 100% │
│ type accuracy    │   40% │       0% │            13% │        27% │ 40% │  67% │
│ precision        │  100% │     100% │           100% │       100% │ 50% │  50% │
│ avg latency ms   │     3 │        2 │            536 │       1681 │   7 │ 7095 │
│ blocked / scored │   0/9 │      0/9 │            0/9 │        0/9 │ 0/9 │  0/9 │
└──────────────────┴───────┴──────────┴────────────────┴────────────┴─────┴──────┘
```

What this run tells you:

- **`regex` strict recall = 100%** — every entity the corpus marks as
  non-tolerated (creds, hashes, JWT, hostname, IP) has a regex shape
  and gets caught deterministically. Cheap and exhaustive on shape-
  driven entities.
- **`all` recall = 93% vs regex's 67%** — the other detectors pull
  weight on contextual entities regex can't shape-match (org names,
  addresses, codenames). The bump is mostly from `llm` + `gliner_pii`.
- **`denylist` = 0% across the board** — expected, the bundled corpus
  doesn't include any denylist terms. Set `DENYLIST_PATH` to your
  org's terms and the column lights up.
- **`llm` and `all` precision = 50%** — the bundled `rules.pentest.yaml`
  has a deliberate "France → ORGANIZATION" rule for the
  `must_keep: ["capital of France"]` sentinel. That's the false
  positive. Drop the rule (or fix the prompt) and precision goes back
  to 100%.
- **`type accuracy = 67%` for `all`** — the LLM rules deliberately
  mis-type AcmeCorp (PERSON instead of ORGANIZATION), the AWS secret
  key (AWS_ACCESS_KEY instead of CREDENTIAL), and the address
  (IDENTIFIER instead of ADDRESS). Same root cause as precision —
  fix the rules / prompt to fix the score.

#### Why is `all` latency so much higher than the individual columns?

`all` shows ~7s, but the slowest individual detector (`gliner_pii`)
is ~1.7s. Per-case wall clock for `all` should be roughly
`max(latencies)` if everything truly ran in parallel, *plus* per-case
machinery overhead. Two things drive the gap:

1. **CPU contention between the ML sidecars.** `privacy-filter-service`
   (~536 ms solo) and `gliner-pii-service` (~1.7 s solo) both run
   inference on the same CPU when launched as sidecars on a single
   host. When the guardrail fires both detectors in parallel for one
   case, each container's inference thread fights the other for the
   same cores — both end up several times slower than their
   uncontested baseline. On a multi-node deployment (one container
   per node, or GPU for the heavy ones) this contention disappears.
2. **Sequential per-case execution.** The benchmark sends one corpus
   case at a time and waits for the response before sending the next.
   Within a case, the guardrail fires detectors concurrently; across
   cases, latency stacks. Production traffic with many concurrent
   requests amortises this differently — `--compare` measures the
   single-stream cost, which is the worst case.

The takeaway for an operator picking a detector mix: **single-stream
latency for `all` ≠ what your production p50/p99 will look like.**
Use `--compare` to score detection quality and to surface ordering
of cost (regex < llm < pf < gliner here), then load-test on the
target topology to size hardware.

## Exit code

Exits **0** when every executed case passed. A case "fails" when:

- a non-tolerated expected entity wasn't redacted (recall miss), OR
- a `must_keep` substring was redacted (false positive), OR
- the guardrail returned **`BLOCKED`** for the case (typically
  `LLM_FAIL_CLOSED` tripped by an upstream error — needs
  investigation even if it's an availability issue rather than a
  detection bug).

Skipped cases (because the running guardrail's `DETECTOR_MODE` doesn't
include a detector the case requires) don't affect the exit code —
matches the policy in `test-examples.sh`.

**Recommendation for benchmarking runs:** start the guardrail with
`--llm-fail-open` so a flaky LLM degrades silently (other detectors
still contribute) instead of zeroing-out the cases that need it.
Fail-closed is correct for production deployments; fail-open is
correct when you're measuring detection quality.

## Output

For each case the script prints a row with recall, type accuracy,
precision, latency, and a summary of any missed / leaked entities:

```
                                   Corpus: pentest (against http://localhost:8001)
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┓
┃ case                                     ┃ recall ┃ type acc.  ┃ precision ┃ latency ┃ notes      ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━┩
│ cracked-creds-table                      │ 3/3    │ 3/3       │ —         │ 287ms   │ ✓          │
│ aws-creds-in-prose                       │ 2/2    │ 2/2       │ —         │ 109ms   │ ✓          │
│ contextual-org-and-codename              │ 2/3    │ 2/3       │ —         │ 1.2s    │ missed:    │
│                                          │        │           │           │         │ Northstar  │
│ documentation-uuid-fp                    │ —      │ —         │ 1/1       │ 84ms    │ ✓          │
└──────────────────────────────────────────┴────────┴───────────┴───────────┴─────────┴────────────┘

Aggregate
  recall            83%
  strict recall    100%
  type accuracy     83%
  precision        100%
  avg latency      420 ms
  1 case(s) skipped
  1 case(s) blocked (excluded from aggregate metrics; see per-case rows)
```

The aggregate divides over **scored** cases only — those that ran end-
to-end and produced a measurable response. Skipped cases (no request
sent) and blocked cases (request sent, guardrail refused) are
surfaced as separate counters at the bottom so a flaky LLM doesn't
look like a recall bug.

## Metrics

The three quality metrics each answer a different question, and a
detector mix can be strong on one while weak on another. Read them
together — recall alone tells you nothing about how much benign text
got mangled, and precision alone tells you nothing about leaks.

| Metric | Question | Failure mode |
|---|---|---|
| **recall** | "Of the things I expected to be redacted, how many actually were?" | **Low recall = sensitive data leaked through.** The worst failure for a guardrail. |
| **strict recall** | Same as recall, but ignores entities marked `tolerated_miss: true` in the corpus. | Same as recall, but for the entities the operator actually expects the chosen mix to catch. This is the score that gates CI exit code. |
| **type accuracy** | "Of the things that *were* redacted, how many got the right entity type?" | **Low type accuracy = wrong-shape surrogate** (e.g. an organisation name replaced with a person name). Doesn't leak data, but confuses the upstream LLM by handing it a substitute that breaks domain expectations. |
| **precision** | "Of the things I said to leave alone (`must_keep`), how many survived?" | **Low precision = false positives.** The detector replaced benign text with a surrogate, mangling meaning the upstream LLM relies on. |
| **avg latency** | Wall-clock per case. | Informational only — for proper load testing, point a load generator at a running guardrail. |

**Worked example.** Suppose the corpus has one case:

- Input text: `Alice Smith lives at 123 Main St; this is a documentation example.`
- `expect`: `Alice Smith` (PERSON), `123 Main St` (ADDRESS).
- `must_keep`: `documentation example`.

The guardrail returns:

```
[PERSON_AB12CD34] lives at 123 Main St; this is a [ORGANIZATION_FF223344].
```

What each metric scores:

- **Recall = 1/2 = 50%.** Alice was redacted; the address wasn't.
- **Type accuracy = 1/1 = 100%.** The one redaction (Alice → PERSON
  token) matches the expected type. We don't ding type accuracy for
  the missed address — it never made it into the response, so there's
  no token to score.
- **Precision = 0/1 = 0%.** `documentation example` got replaced with
  an `[ORGANIZATION_*]` token — a false positive. The downstream LLM
  now sees a fabricated organisation name where the corpus author
  said "this phrase is benign, leave it alone."

Mnemonic: **recall** = "did we catch it?" / **type accuracy** = "did
we label it?" / **precision** = "did we leave alone what we should?"

The benchmark sets `use_faker: false` per request via
[per-request overrides](per-request-overrides.md) so the response
carries opaque `[TYPE_HEX]` tokens instead of realistic Faker
substitutes — that's what lets the script recover types from the
response without needing per-entity attribution from the guardrail.

## Corpus format

A corpus is a YAML file listing cases. Each case has a `text` to
send and either an `expect:` list (substrings that should be
redacted) or a `must_keep:` list (substrings that must NOT be
redacted), or both.

```yaml
# tests/corpus/pentest.yaml
name: "Pentest engagement transcript"
description: |
  Synthetic snippets typical of a security engagement.

# Optional: per-request overrides applied to every case. Same shape as
# `additional_provider_specific_params` (see per-request-overrides.md).
overrides:
  regex_patterns: pentest
  llm_prompt: pentest

cases:
  # ── Case with positive expectations (recall + type accuracy) ──────
  - id: cracked-creds-table
    text: |
      Internal hostname dc01.acmecorp.local is reachable from
      10.0.7.42. Cracked password from NTDS dump:
      bob.smith:1107:aad3b435b51404eeaad3b435b51404ee:8846f7eaee8fb117ad06bdd830b7586c:::
    requires: [regex]                # skip case if 'regex' isn't in DETECTOR_MODE
    expect:
      - text: "dc01.acmecorp.local"
        type: HOSTNAME
      - text: "10.0.7.42"
        type: IPV4_ADDRESS
      - text: "8846f7eaee8fb117ad06bdd830b7586c"
        type: HASH

  # ── Case with a tolerated miss (counts toward `recall`, not `recall_excl`) ──
  - id: contextual-org-and-codename
    text: "AcmeCorp engineering is rolling out Project Zephyr next quarter."
    requires: [llm]
    expect:
      - text: "AcmeCorp"
        type: ORGANIZATION
        tolerated_miss: true   # weaker LLMs may not catch this; don't penalize

  # ── False-positive sentinel (precision) ──
  - id: documentation-uuid-fp
    text: "Request id 11111111-2222-3333-4444-555555555555 is a documentation example."
    must_keep: ["documentation example"]
```

### Field reference

**Top-level**

| Key | Required | Notes |
|---|---|---|
| `cases` | yes | Non-empty list of case mappings (below). |
| `name` | no | Display name for the run header. Defaults to the file stem. |
| `description` | no | Free text printed at the start of the run. |
| `overrides` | no | Per-request `additional_provider_specific_params` applied to every case. The benchmark always forces `use_faker: false` regardless. |

**Per case**

| Key | Required | Notes |
|---|---|---|
| `id` | yes | Unique within the corpus. Used in the result table and as `litellm_call_id`. |
| `text` | yes | The text to anonymize. |
| `expect` | one of `expect` / `must_keep` required | List of `{text, type, tolerated_miss?}`. `text` must be a substring of the case `text` (validated at load). `type` is one of the canonical entity types (case-insensitive). |
| `must_keep` | one of `expect` / `must_keep` required | List of substrings that MUST stay untouched. Each must appear in the case `text`. |
| `requires` | no | List of detector names. Cases requiring detectors absent from the running guardrail are skipped, not failed. |

### Authoring tips

- **Validate at load time.** Every `expect` / `must_keep` substring is
  checked against the case text — typos surface immediately rather
  than as mysterious zero-recall scores.
- **Use `tolerated_miss: true` generously** for entities that depend
  on the LLM detector or other models with non-deterministic output.
  The `recall` number includes them; `recall (excl. tolerated)` is
  the score that gates CI exit code.
- **Keep corpora small** (~10-30 cases each). Large datasets belong
  in operator-side fixtures; the bundled corpora exist as starters
  to fork from.
- **One corpus per scenario.** Don't try to mix pentest + legal +
  healthcare in one file — different scenarios warrant different
  detector mixes, different `requires:`, different precision/recall
  trade-offs.

## Bundled corpora

| Corpus | Spec | Focus |
|---|---|---|
| `bundled:pentest` | `tests/corpus/pentest.yaml` | Cracked-credential artefacts, cloud keys, internal hostnames, JWTs, hashes. Mostly regex-driven. |

The starter set is small on purpose. Fork into your own
`tests/corpus/<workload>.yaml` (or anywhere outside the repo via a
filesystem path) for the workloads that matter to your deployment.

## Comparing detector mixes

Run the same corpus against several configurations to see which mix
wins for your traffic shape:

```bash
# Regex only with the pentest pattern set (sub-millisecond per case)
scripts/detector_bench.sh --config bundled:pentest --preset regex-pentest

# Add the gliner-pii NER (~ms per case, picks up names / orgs / IDs)
scripts/detector_bench.sh --config bundled:pentest \
  --preset regex-pentest-gliner-pii-service

# Privacy-filter (HF variant) replaces gliner — different model, different
# tradeoffs (see services/privacy_filter_hf/COMPARE.md).
scripts/detector_bench.sh --config bundled:pentest \
  --preset privacy-filter-service
```

Latency in the per-case rows tells you the cost of each addition;
recall tells you the benefit. The right mix is whichever one clears
your minimum recall target while staying inside your latency budget.

## See also

- [Per-request overrides](per-request-overrides.md) — for the full
  `additional_provider_specific_params` shape (`overrides:` in the
  corpus uses the same keys).
- [Detectors](detectors/index.md) — pick which detectors to enable
  for the corpus you're scoring.
- [Examples](examples.md) — the curl recipes
  `scripts/test-examples.sh` runs (smoke-style tests, not benchmark).
