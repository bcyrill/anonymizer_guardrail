# Wiring it into LiteLLM

The guardrail implements LiteLLM's
[Generic Guardrail API](https://docs.litellm.ai/docs/adding_provider/generic_guardrail_api).
LiteLLM calls it before forwarding a request upstream
(`input_type="request"`) to anonymize sensitive substrings, then again
after the upstream model responds (`input_type="response"`) to
deanonymize them. A short-lived in-memory mapping keyed by
`litellm_call_id` connects the two sides of the round-trip — see
[vault](vault.md) for the lifecycle.

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
