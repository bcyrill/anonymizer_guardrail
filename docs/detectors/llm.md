# LLM detector

Calls an OpenAI-compatible Chat Completions endpoint with a JSON-mode
prompt that asks the model to enumerate sensitive entities. Catches
contextual stuff regex cannot: org names, personal names, internal
product/project codenames embedded in prose.

When pointed at LiteLLM, the model alias used here MUST NOT have the
guardrail attached — otherwise every detection call would re-enter the
guardrail and recurse forever. See
[LiteLLM integration](../litellm-integration.md) for the recommended
config pattern.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LLM_API_BASE` | `http://litellm:4000/v1` | OpenAI-compatible endpoint. |
| `LLM_API_KEY` | *(empty)* | Bearer token if the endpoint needs one. |
| `LLM_USE_FORWARDED_KEY` | `false` | Use the caller's Authorization header instead of `LLM_API_KEY` (so detection cost attributes back to the user's virtual key). See [Forwarding the caller's API key](#forwarding-the-callers-api-key) below. |
| `LLM_MODEL` | `anonymize` | Model alias used for detection. |
| `LLM_TIMEOUT_S` | `30` | Per-call timeout (seconds) on LLM detector HTTP requests. |
| `LLM_MAX_CHARS` | `200000` | Hard cap; inputs above this are refused (raises `LLMUnavailableError` → `LLM_FAIL_CLOSED` decides BLOCKED vs degrade). |
| `LLM_MAX_CONCURRENCY` | `10` | Semaphore on in-flight LLM detector calls. Surfaced as `llm_in_flight`/`llm_max_concurrency` on `/health`. |
| `LLM_FAIL_CLOSED` | `true` | Block requests if the LLM detector errors. Independent from the other detectors' fail-mode flags. |
| `LLM_SYSTEM_PROMPT_PATH` | *(empty → bundled `llm_default.md`)* | Override the bundled detection prompt. Accepts `bundled:NAME` or a filesystem path. See [Customising the prompt](#customising-the-detection-prompt) below. |
| `LLM_SYSTEM_PROMPT_REGISTRY` | *(empty)* | Comma-separated `name=path` list of NAMED alternative prompts callers can opt into per-request via `llm_prompt`. See [per-request overrides → Named alternatives](../per-request-overrides.md#named-alternatives). |
| `LLM_CACHE_MAX_SIZE` | `0` | LRU cap on the LLM detector's result cache. `0` disables caching (default). When enabled, repeat calls with the same `(text, llm_model, llm_prompt)` skip the LLM round-trip. See [operations → Detector result caching](../operations.md#detector-result-caching) for the trade-offs. |
| `LLM_INPUT_MODE` | `per_text` | How the pipeline dispatches `req.texts` to this detector. `per_text` (default) calls the detector once per text; `merged` concatenates all texts with a sentinel separator and makes one call per request. See [operations → Merged-input mode](../operations.md#merged-input-mode) for the trade-offs. Mutually exclusive with `LLM_CACHE_MAX_SIZE`: setting both logs a warning at boot and the cache is bypassed. |

## Per-request overrides

Two LLM-specific keys can be passed in
`additional_provider_specific_params` (see
[per-request overrides](../per-request-overrides.md) for the general
shape):

| Override key | Type | Effect |
|---|---|---|
| `llm_model` | `string` | Override `LLM_MODEL` for this call. |
| `llm_prompt` | `string` | Name of a registered alternative LLM detection prompt. Looked up in `LLM_SYSTEM_PROMPT_REGISTRY`; unknown names log a warning and fall back to the default prompt. |

## Customising the detection prompt

Four prompts ship with the package under
`src/anonymizer_guardrail/prompts/`:

| Prompt | Tuned for | Size |
|---|---|---|
| `llm_default.md` | General-purpose, model-neutral. Loaded when `LLM_SYSTEM_PROMPT_PATH` is unset. | ~250 tokens |
| `llm_pentest.md` | Security-engagement output. Verbatim port of the [DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI/blob/main/src/llm_detector.py) system prompt — handles cracked-password artifacts, NetBIOS names, K8s namespace conventions, pentest tool noise to ignore, etc. | ~2,400 tokens |
| `llm_default_claude.md` | Same coverage as `llm_default.md`, distilled for Claude's instruction-following. Use this when `LLM_MODEL` is a Claude alias. | ~220 tokens |
| `llm_pentest_claude.md` | Same coverage as `llm_pentest.md`, distilled for Claude. Drops enumerated examples in favour of categorical rules. | ~1,000 tokens |

The Claude-targeted variants are byte-for-byte smaller because Claude
generalises from one canonical example per category — repeating
"flag this and this and this" doesn't add recall, just cost. The
non-`_claude` variants stay verbose because models that follow
instructions less reliably benefit from the redundancy. Pick the pair
that matches your `LLM_MODEL`. See
[LiteLLM integration → Using Claude as the detection LLM](../litellm-integration.md#using-claude-as-the-detection-llm)
for the matching backend wiring.

To swap in one of these (or your own — extra entity types, domain-specific
guidance, a different language), set `LLM_SYSTEM_PROMPT_PATH`. Two forms
are accepted:

- `bundled:<filename>` — a file shipped inside the package, e.g.
  `bundled:llm_pentest_claude.md`. Resolved via `importlib.resources`, so
  the env var is independent of the Python version embedded in the
  site-packages path.
- A regular filesystem path — typically a mounted volume:

```bash
# Use the bundled pentest prompt — no path-juggling needed.
podman run --rm -p 8000:8000 \
  -e LLM_SYSTEM_PROMPT_PATH=bundled:llm_pentest.md \
  anonymizer-guardrail:latest

# Or mount your own:
podman run --rm -p 8000:8000 \
  -v $PWD/my_prompt.md:/etc/anonymizer/prompt.md:ro \
  -e LLM_SYSTEM_PROMPT_PATH=/etc/anonymizer/prompt.md \
  anonymizer-guardrail:latest
```

The prompt is loaded once at startup; restart the container to pick up
edits. A missing/unreadable override path is a hard error rather than a
silent fall-back to the bundled prompt — if you set the variable, we
assume you mean it.

## Forwarding the caller's API key

Set `LLM_USE_FORWARDED_KEY=true` to authenticate to the detection LLM
with the same key the user authenticated to LiteLLM with, instead of a
shared `LLM_API_KEY`. Detection cost and rate limits then attribute
back to the caller's virtual key.

This requires opting into header forwarding on the LiteLLM side as well
— LiteLLM redacts non-allowlisted headers to `"[present]"` by default,
so without `extra_headers`, this guardrail will silently fall back to
`LLM_API_KEY`:

```yaml
litellm_settings:
  guardrails:
    - guardrail_name: anonymizer
      litellm_params:
        guardrail: generic_guardrail_api
        mode: [pre_call, post_call]
        api_base: http://anonymizer:8000
        unreachable_fallback: fail_closed
        extra_headers: [authorization]   # ← forwards Bearer <user-key> to us
```

If the header is missing or arrives as `[present]`, we fall back to
`LLM_API_KEY`. If both are empty, the LLM call goes out without an
`Authorization` header (fine for local/dev backends; everything else
will likely return 401, which routes through `LLM_FAIL_CLOSED`).

## Failure handling

`LLM_FAIL_CLOSED` (default `true`) governs what happens when the LLM
detector errors out. When the detector raises `LLMUnavailableError`
(connect / timeout / non-200 / oversized input / unparseable 200 OK
body or content / any unexpected exception under fail-closed), the
guardrail returns `BLOCKED`. With fail-open, the error is logged and
the request proceeds with coverage from the remaining detectors.

A 200 OK with garbage in it counts as unavailable: the backend
replied but didn't say anything actionable. Per-entry malformed
entries inside an otherwise-valid `{"entities": [...]}` payload still
drop silently — those only invalidate one match, not the whole
response.

The flag is independent from `PRIVACY_FILTER_FAIL_CLOSED` and
`GLINER_PII_FAIL_CLOSED` — operators can fail closed on the LLM and
open on the others, or vice versa.

## Determinism — what to expect

LLM detection is **not deterministic in the way regex / denylist
are**. Same input + same configuration can produce slightly
different entity lists across:

- **Model versions.** A provider's silent point-update to the
  underlying weights (e.g. `gpt-4o-mini-2024-07-18` → a successor
  alias under the same `gpt-4o-mini` route) shifts what gets
  flagged.
- **Backends.** Two endpoints serving "the same" model (a hosted
  OpenAI-compatible service vs. a local Ollama / vLLM build) will
  not agree exactly, because tokeniser, sampling implementation,
  and quantisation all matter.
- **Re-runs.** Even at `temperature=0`, providers sometimes
  introduce residual non-determinism — batched-inference effects,
  GPU non-associativity, etc. The detector pins `temperature: 0`
  in the request payload to *reduce* variance, not eliminate it.

What this means in practice:

- **Don't rely on the LLM detector alone for shape-anchored PII**
  (emails, IPs, IBANs, JWTs, CCs, well-known token formats). The
  regex layer is deterministic and high-precision for those —
  layer them: `DETECTOR_MODE=regex,denylist,llm`. With regex
  listed first, type-resolution conflicts go to the
  shape-classified type, not to the LLM's interpretation.
- **Don't pin tests to exact LLM output.** The fake-llm test
  backend (rules-driven) is the way to write deterministic
  end-to-end tests of the LLM-detector path; real LLMs are too
  noisy for that. See
  [`services/fake_llm/README.md`](../../services/fake_llm/README.md).
- **Pin a model version when you can.** A request for
  `gpt-4o-mini` is shorthand for "whatever's currently behind that
  alias" — pin the explicit dated tag in `LLM_MODEL` (e.g.
  `gpt-4o-mini-2024-07-18`) when the deployment cares about
  behaviour stability across provider updates.

The hallucination guard (substring-must-be-in-source check, see
implementation in `_parse_entities`) is the floor against
LLM-introduced incorrectness: an entity the model returns that
isn't actually in the input is dropped before it reaches the vault
or the surrogate generator. That covers "the model invented a
name"; it doesn't cover "the model missed a name that was there"
— hence the layered-detection recommendation above.
