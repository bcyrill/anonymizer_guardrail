# Privacy-filter detector

Local NER backed by
[openai/privacy-filter](https://huggingface.co/openai/privacy-filter)
(Apache 2.0). Encoder-only token classifier, ~1.5 B params (50 M active
via MoE). Detects 8 PII categories: people, emails, phones, URLs,
addresses, dates, account numbers, secrets. Coverage is a strict
subset of what the LLM prompt picks up (no orgs, hostnames, IP/MAC,
etc.) so it's a *complement* to — not a replacement for — the LLM
detector.

Two implementations back the `privacy_filter` detector — same name in
`DETECTOR_MODE`, same canonical entity types, same span semantics on
the wire. Operators pick which one runs by the topology they want, not
by changing the detector list.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `PRIVACY_FILTER_URL` | *(empty)* | When set, the detector talks HTTP to a standalone privacy-filter-service instead of loading the model in-process; the slim image then covers `privacy_filter`. See [Remote](#remote-privacy_filter_url-set) below. |
| `PRIVACY_FILTER_TIMEOUT_S` | `30` | Per-call timeout (seconds) on the remote privacy-filter HTTP requests. |
| `PRIVACY_FILTER_FAIL_CLOSED` | `true` | Block requests when the privacy_filter detector errors. Independent flag — operators can fail closed on one detector and open on another. Applies to both the in-process and remote variants. |
| `PRIVACY_FILTER_MAX_CONCURRENCY` | `10` | Semaphore on in-flight `privacy_filter` calls (in-process AND remote). Independent of `LLM_MAX_CONCURRENCY`. Surfaced as `pf_in_flight`/`pf_max_concurrency` on `/health`. |
| `HF_HUB_OFFLINE` | *(unset)* / `1` *(baked images)* | Set to `1` to stop transformers pinging HuggingFace Hub on every start. The `pf-baked` image flavour sets it automatically; runtime-download flavours leave it unset on first run. `scripts/cli.sh --hf-offline` / the menu offer it once the cache volume is populated. |

## In-process (default)

Loads the `openai/privacy-filter` model into the guardrail's own
process. Pulls in `torch`, `transformers`, and the full set of model
weights, so this option only works on the `pf` / `pf-baked` image
flavours — slim doesn't ship the dependencies. See
[deployment → Container images](../deployment.md#container-images) for
the size impact. Microsecond glue overhead per call; shares the
guardrail's CPU/memory budget.

Pick this when:

- Single-replica deployment.
- You don't already have a privacy-filter service running.
- Latency is more important than image size or independent scaling.

Optional dependency for direct-pip installs:
`pip install "anonymizer-guardrail[privacy-filter]"`. The container
side ships it via `--build-arg WITH_PRIVACY_FILTER=true`.

## Remote (`PRIVACY_FILTER_URL` set)

Off-loads inference to a standalone container running
`services/privacy_filter/main.py` — a thin FastAPI wrapper around the
same `transformers` pipeline plus identical span-merge post-processing.
The guardrail's `RemotePrivacyFilterDetector` posts each request's
text to `${PRIVACY_FILTER_URL}/detect` and parses the returned span
list. Output is byte-equivalent to the in-process detector for the
same input.

Pick this when:

- You want the slim guardrail image (no torch in the API container)
  but still need privacy-filter coverage.
- Multiple guardrail replicas should share one inference service
  (single model copy in memory; one place to attach a GPU).
- Model updates need to ship without rebuilding the guardrail image.

Build the service image via `scripts/build-image.sh -t pf-service`
(runtime download) or `pf-service-baked` (model in image). See
[`services/privacy_filter/README.md`](../../services/privacy_filter/README.md)
for the API contract, env vars, and standalone build/run commands.

## Selecting the backend

For interactive / single-host development, the launcher exposes the
choice as `--privacy-filter-backend`:

| Value      | What happens                                                   |
|------------|----------------------------------------------------------------|
| *(unset)*  | In-process. Requires `--type pf` or `--type pf-baked`.         |
| `service`  | Auto-start a `privacy-filter-service` container on the shared network and point the guardrail at it. Mirrors how `--llm-backend service` auto-starts fake-llm. |
| `external` | Use the URL given by `--privacy-filter-url` / `PRIVACY_FILTER_URL`. Nothing is auto-started. Use this for production deployments where the service is managed separately (Kubernetes, docker-compose, etc.). |

The auto-start path mounts the same `anonymizer-hf-cache` volume the
guardrail's `pf` flavour uses, so an operator who already pulled the
model via the in-process path doesn't pay the download again on
switching to remote.

## Failure handling

`PRIVACY_FILTER_FAIL_CLOSED` (default `true`) is independent from
`LLM_FAIL_CLOSED` and `GLINER_PII_FAIL_CLOSED` — operators can fail
closed on one detector and open on another. When the privacy-filter
detector raises `PrivacyFilterUnavailableError` (service unreachable,
timeout, non-200, unparseable 200 OK body or wrong-shape payload, or
any unexpected exception under fail-closed), the guardrail returns
`BLOCKED`. With fail-open, the error is logged and the request
proceeds with coverage from the remaining detectors. A 200 OK with
garbage in it counts as unavailable — soft-failing those would let
unredacted text through under fail-closed. Per-entry malformed
entries inside an otherwise-valid `{"matches": [...]}` payload still
drop silently. The flag applies to both the in-process and remote
variants — a torch crash inside the in-process detector triggers the
same fail-closed path as a connection error to the remote service.
