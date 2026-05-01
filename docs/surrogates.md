# Surrogates

Each detected entity is replaced with a *realistic* substitute of the same
type — `acmecorp.local` → `quasarware.local`, not `[ORGANIZATION_7F3A2B]` —
generated with [Faker](https://faker.readthedocs.io/) seeded by a hash of
the original. Determinism means the same input always maps to the same
surrogate within a process, so the upstream model sees consistent
substitutions across multi-turn conversations.

Opaque tokens are still used for things where realism would be misleading
(`HASH`, `JWT`, `CREDENTIAL`, `TOKEN`, `AWS_ACCESS_KEY`).

The cross-call consistency invariant (same input → same surrogate) is
backed by an in-process LRU cache; see [Surrogate cache](#surrogate-cache)
below for the sizing knob and observability.

## Surrogate cache

The surrogate generator memoizes `(entity_type, original_text) →
surrogate` so the same input always yields the same surrogate —
within one request *and* across many. This is what lets an upstream
model see a stable view of a given email/hostname/UUID over a
multi-turn conversation, even though each turn is a separate LiteLLM
call (and therefore a separate [vault](vault.md) entry).

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `SURROGATE_CACHE_MAX_SIZE` | `100000` | LRU cap on the cache. Raise if your traffic shape sees the same originals over a longer window than the cache holds. |

### Lifecycle

- **Populated** by every detected match. Outlives the surrounding
  request/response cycle — that's the whole point.
- **Eviction:** LRU at `SURROGATE_CACHE_MAX_SIZE` entries (default
  100 000). No time-based expiry; entries stay until newer ones push
  them out.
- **Scope:** in-memory, per-process, not shared across replicas.
- **Determinism across restart:** every surrogate is derived from a
  keyed BLAKE2b hash of the original. The key is `SURROGATE_SALT`.
  - Default (random salt per process start): same input → same
    surrogate **within** a process; the cache speeds it up but the
    derivation is itself deterministic. After a restart the salt
    changes, so old surrogates are no longer reproducible.
  - With `SURROGATE_SALT` set to a stable string: same input → same
    surrogate forever. Useful for log-correlation. See
    [Surrogate salt](#surrogate-salt-privacy-hardening) below for the
    privacy trade-off.
- **Collision handling:** if two distinct originals would produce the
  same surrogate, the generator salts and retries. After a small
  number of failed attempts it falls back to a guaranteed-unique
  opaque token (bounds the worst-case; effectively never happens in
  practice).

### Observability

`/health` exposes `surrogate_cache_size` and `surrogate_cache_max`.
A `surrogate_cache_size` near `surrogate_cache_max` for sustained
periods means LRU eviction is firing — the oldest cross-request
consistency invariants are quietly being lost. Bump
`SURROGATE_CACHE_MAX_SIZE` if that matters for your traffic shape.

See [operations → Observability](operations.md#observability) for the
full `/health` shape.

## Surrogate salt (privacy hardening)

Surrogate generation uses keyed BLAKE2b under the hood — both the seed for
Faker and the opaque-token digest. The key is taken from `SURROGATE_SALT`,
which defaults to a fresh 16-byte random value at process start.

**Why it matters:** without keying, the opaque-token surrogate is literally
a hash of the original (`[IPV4_ADDRESS_…]`). For low-entropy entity types
— IPs, phones, MACs, names from a known list — an attacker with access to
the surrogates (model-provider logs, LiteLLM access logs, anyone reading
the upstream traffic) can pre-compute hashes for plausible inputs and
recover the originals offline. The keyed hash defeats this: without the
salt, candidate hashes don't match.

**Defaults are safe.** If you set nothing, you get a fresh random 128-bit
salt every restart. Surrogates from before a restart are uncorrelatable
with surrogates from after.

**Set `SURROGATE_SALT` to a stable string** if you want surrogates to
remain stable across restarts — useful for log-correlation analysis but
gives up the brute-force protection if the salt leaks. Pick one per
deployment; never share between unrelated environments.

```bash
# Default: process-random salt, strongest privacy.
podman run anonymizer-guardrail:latest

# Stable surrogates across restarts (operator opted in):
podman run -e SURROGATE_SALT=$(openssl rand -hex 32) anonymizer-guardrail:latest
```

## Disabling Faker

Set `USE_FAKER=false` to replace every realistic surrogate with an opaque
deterministic token (`alice` → `[PERSON_AE708E5D]`, `acmecorp` →
`[ORGANIZATION_77116DCC]`). The token's prefix still encodes the entity type so the
upstream model can tell categories apart, but no Faker output ever reaches
it. Useful when:

- Realistic surrogates would mislead a downstream tool (e.g. an automation
  that grep's for company names in the model's response).
- You want a hard guarantee that the model never sees plausibly-real PII.
- Faker-related behaviour is causing trouble and you want to take it out
  of the loop entirely (Faker isn't even instantiated in this mode).

The opaque tokens are still deterministic, so the same input always maps
to the same surrogate within a process — round-trip restoration works
identically.

## Localising surrogates

Set `FAKER_LOCALE` to control the locale Faker uses for generated names,
companies, addresses, phone numbers, etc. Examples:

```bash
-e FAKER_LOCALE=pt_BR              # Brazilian Portuguese
-e FAKER_LOCALE=de_DE              # German
-e FAKER_LOCALE=ja_JP              # Japanese
-e FAKER_LOCALE=pt_BR,en_US        # try pt_BR first, fall back to en_US
                                   # for providers it doesn't implement
```

Empty (the default) means Faker's own default (`en_US`). Invalid locales
fail at startup with a clear message — the
[Faker docs](https://faker.readthedocs.io/) list every supported one.
Surrogates for opaque-token types (`HASH`, `JWT`, `CREDENTIAL`, etc.) are
unaffected; locale only changes the realistic-substitute types
(`PERSON`, `ORGANIZATION`, `EMAIL_ADDRESS`, …).

For per-request locale overrides via
`additional_provider_specific_params.faker_locale`, see
[per-request overrides](per-request-overrides.md).
