# fake-llm

A small OpenAI-compatible Chat Completions server that returns
deterministic responses for testing the anonymizer guardrail's LLM
detector. Rules are defined in YAML; the first rule whose matcher hits
the user message decides what comes back.

## Why

The LLM detector talks to whatever lives at `LLM_API_BASE` and parses
back JSON entities. To reproduce edge cases — overlap with regex
matches, hallucinations the parser should drop, malformed JSON, 5xx
forcing `LLM_FAIL_CLOSED`, slow responses tripping `LLM_TIMEOUT_S` — you
want a backend that responds *exactly* the way you ask. This is that
backend.

## Build and run

```bash
podman build -t fake-llm:latest \
    -f services/fake_llm/Containerfile services/fake_llm/

# Or via the build script:
scripts/build-image.sh -t fake-llm

# With the bundled example rules:
podman run --rm -p 4000:4000 --name fake-llm fake-llm:latest

# With your own rules file:
podman run --rm -p 4000:4000 \
  -v $PWD/services/fake_llm/my-rules.yaml:/app/rules.yaml:ro \
  --name fake-llm fake-llm:latest
```

The server listens on port 4000 and exposes:

- `GET /health` — `{"status":"ok","rules":N}`
- `GET /v1/models` — minimal list-models response
- `POST /v1/chat/completions` — the matcher endpoint

## Wiring it to the guardrail

The guardrail container needs to reach the fake-llm container. The
simplest setup is a shared Podman/Docker network:

```bash
podman network create anonymizer-net 2>/dev/null || true

podman run --rm -d --name fake-llm \
  --network anonymizer-net \
  -p 4000:4000 \
  fake-llm:latest

# Now point the guardrail at it (any non-empty key is fine — fake-llm
# doesn't authenticate). Use the container name as the hostname.
podman run --rm --name anonymizer \
  --network anonymizer-net \
  -p 8000:8000 \
  -e DETECTOR_MODE=regex,llm \
  -e LLM_API_BASE=http://fake-llm:4000/v1 \
  -e LLM_API_KEY=any-non-empty \
  -e LLM_MODEL=fake \
  -e LOG_LEVEL=debug \
  anonymizer-guardrail:latest
```

`scripts/run_container.sh` accepts passthrough args after `--`, so the
same wiring works through the helper script:

```bash
scripts/run_container.sh -t slim -- \
  --network anonymizer-net \
  -e LLM_API_BASE=http://fake-llm:4000/v1 \
  -e LLM_API_KEY=any \
  -e LLM_MODEL=fake \
  -e DETECTOR_MODE=regex,llm \
  -e LOG_LEVEL=debug
```

## Rules schema

Each entry under `rules:` declares matchers and a response. Matching
is first-rule-wins. Within a rule, every set matcher must succeed
(`match`/`match_regex` are OR'd together, then AND'd with
`match_model`). At least one matcher is required.

```yaml
rules:
  - description: "human-readable label, used in logs"
    match: "literal substring"          # case-sensitive substring of user msg
    match_regex: 'Python\s+regex'       # .search semantics on user msg
    match_model: "invalid-model"        # substring of the request's `model`
                                        # field — useful for verifying that
                                        # llm_model overrides reach us
    entities:                           # default response shape
      - text: "string from the input"
        type: PERSON                    # any string; the guardrail will
                                        # normalize unknown types to OTHER

    # Optional escape hatches (use one at a time):
    raw_content: '{"entities": [...]}'  # arbitrary string used verbatim
                                        # as the assistant message content
                                        # — overrides `entities`
    status_code: 503                    # forces non-200 (with stub error
                                        # body); skips entities/raw_content
    delay_s: 35                         # sleep before responding

default:
  entities: []                          # response when no rule matches
```

The bundled `rules.example.yaml` covers the typical scenarios — see
that file for fully-formed examples.

## Quick smoke tests

Once both containers are running:

```bash
# 1. Health.
curl -s http://localhost:4000/health
# → {"status":"ok","rules":8}

# 2. Direct call to the fake LLM (mimics what the guardrail does).
curl -s http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "fake",
    "messages": [
      {"role": "system", "content": "..."},
      {"role": "user", "content": "test 550e8400-e29b-41d4-a716-446655440000."}
    ]
  }' | python -m json.tool

# 3. End-to-end through the guardrail.
curl -s http://localhost:8000/beta/litellm_basic_guardrail_api \
  -H 'Content-Type: application/json' \
  -d '{
    "texts": ["test 550e8400-e29b-41d4-a716-446655440000."],
    "input_type": "request"
  }' | python -m json.tool
```

With `LOG_LEVEL=debug` on the guardrail you should see, in order:

```
LLM parsed 1 entities, dropped 0 hallucinations: kept=[('446655440000', 'IDENTIFIER')] dropped=[]
Detector regex returned 1 matches: [('550e8400-e29b-41d4-a716-446655440000', 'UUID')]
Detector llm returned 1 matches: [('446655440000', 'IDENTIFIER')]
After dedup: 2 matches: [...]
```

If `After dedup` shows only 1, that's where the loss happens.

## Iterating on rules

The image bakes `rules.example.yaml` in at build time, but mounting
your own file with `-v $PWD/my-rules.yaml:/app/rules.yaml:ro` is
faster — `podman restart fake-llm` then picks up edits without a
rebuild.