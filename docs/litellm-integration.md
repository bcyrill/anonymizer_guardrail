# Wiring it into LiteLLM

The guardrail implements LiteLLM's
[Generic Guardrail API](https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api).
LiteLLM calls it before forwarding a request upstream
(`input_type="request"`) to anonymize sensitive substrings, then again
after the upstream model responds (`input_type="response"`) to
deanonymize them. A short-lived mapping keyed by `litellm_call_id`
connects the two sides of the round-trip (in-memory by default;
Redis-backed under `VAULT_BACKEND=redis` for multi-replica
deployments) — see [vault](vault.md) for the lifecycle and backend
selection.

A working `config.yaml` snippet (see `litellm.config.example.yaml` for the
full file):

```yaml
litellm_settings:
  guardrails:
    - guardrail_name: anonymizer
      litellm_params:
        guardrail: generic_guardrail_api
        mode: [pre_call, post_call]      # both — that's how round-trip works
        api_base: http://anonymizer:8000
        unreachable_fallback: fail_closed

model_list:
  # Models that should be anonymized — these opt in via the `guardrails` field
  # in client requests, OR set default_on: true on the guardrail above.
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

  # Detection model used by the LLM detector layer. CRITICAL: this model must
  # NOT be guarded by the anonymizer — otherwise every detection call would
  # re-enter the guardrail and recurse forever.
  - model_name: anonymize
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
```

Client-side, callers tag the request:

```python
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "..."}],
    guardrails=["anonymizer"],
)
```

For per-request control over detector mode, surrogates, and detection
prompt, see [per-request overrides](per-request-overrides.md).

For forwarding the caller's API key through to the detection LLM (so
detection cost attributes back to the user's virtual key), see
[LLM detector → Forwarding the caller's API key](detectors/llm.md#forwarding-the-callers-api-key).

## Using Claude as the detection LLM

The detector talks the standard OpenAI Chat Completions shape
(`POST /chat/completions`, `Authorization: Bearer <key>`,
`messages: [...]`, reads `body.choices[0].message.content`), so wiring
Claude in is a config change rather than a code change. Two paths,
ranked by fit for this project's architecture:

### Path A — LiteLLM proxy (recommended)

Add an Anthropic-backed alias to your existing `model_list` and point
the detector at it. The recursion guard is automatic: the alias has no
`guardrails: [...]` field, so detection calls bypass the guardrail.

```yaml
model_list:
  # Existing user-facing models stay as they are. Add one alias for
  # the LLM detector backend:
  - model_name: anonymize                      # alias the detector calls
    litellm_params:
      model: anthropic/claude-haiku-4-5        # cheap + fast — fits the
                                               # "called once per anonymized
                                               # request" budget
      api_key: os.environ/ANTHROPIC_API_KEY
```

On the guardrail container:

```bash
LLM_API_BASE=http://litellm:4000/v1
LLM_API_KEY=$LITELLM_MASTER_KEY            # or a virtual key
LLM_MODEL=anonymize                        # the alias above
LLM_SYSTEM_PROMPT_PATH=bundled:llm_default_claude.md
```

Why this is the right shape:

- **Recursion guard is structural.** The Claude alias has no
  `guardrails:` field, so it can never reach the anonymizer. If you
  later add the guardrail by accident, the alias name (`anonymize`)
  makes the misconfiguration self-evident.
- **You get LiteLLM's machinery for free.** Retries, timeouts, cost
  tracking, virtual-key forwarding, and the Messages-API translation
  are all handled upstream of the detector.
- **Claude-specific prompts are bundled.** `bundled:llm_default_claude.md`
  and `bundled:llm_pentest_claude.md` are token-optimised for Claude's
  instruction following — see
  [LLM detector → Customising the prompt](detectors/llm.md#customising-the-detection-prompt)
  for the trade-offs.

### Path B — Anthropic's OpenAI-compatible endpoint (skip LiteLLM)

Anthropic ships an OpenAI-compatible layer at
`https://api.anthropic.com/v1/`. Point the detector straight at it:

```bash
LLM_API_BASE=https://api.anthropic.com/v1
LLM_API_KEY=$ANTHROPIC_API_KEY
LLM_MODEL=claude-haiku-4-5
LLM_SYSTEM_PROMPT_PATH=bundled:llm_default_claude.md
```

Caveats:

- **`response_format: {"type": "json_object"}` is best-effort guidance,
  not a hard JSON-mode flag.** Claude doesn't have a true JSON-mode
  toggle. The bundled `*_claude.md` prompts compensate with explicit
  "Return ONLY this JSON, no prose, no fences" wording, and the
  detector's response parser tolerates fenced output via
  `_extract_json_object`.
- **No virtual-key forwarding.** `LLM_USE_FORWARDED_KEY=true` only
  works when LiteLLM is in the path forwarding the caller's
  `Authorization` header.
- **No cost attribution.** Without LiteLLM you don't get per-virtual-key
  spend tracking — every detection call bills against the single
  `ANTHROPIC_API_KEY`.

Take this path only when LiteLLM isn't already in your stack.
