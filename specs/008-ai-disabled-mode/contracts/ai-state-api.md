# Contract: reading and changing the AI state

> **Surface names** follow [spec.md](../spec.md). "AI" means anything needing a
> model or embedding provider, directly or transitively.

Two things clients need: to **know** whether AI is enabled, and — for an
administrator — to **change** it.

## Reading it

The state rides on the platform config payload every client already fetches.
There is no new read endpoint.

```
GET /api/v1/platform/config          # unauthenticated
```

```json
{ "ai_enabled": false, "agentic_enabled": false, "edition": "..." }
```

(Written against the retired three-tier payload as `agentic_ai_enabled` /
`deployment_tier`; the licence answer is now `agentic_enabled` and the
XTM One edition is `edition` — `enterprise` / `community`. The contract below is
unchanged.)

**A change can take a few seconds to appear.** The resolved state is cached
briefly on each replica so gating a request costs no database read (FR-003a), so
a client polling immediately after a toggle may still see the old value. Poll
again rather than treating the first response as final.

**`ai_enabled` is the primary signal**, and it is deliberately available before
authentication (FR-011) so the sign-in screen can state the condition. Render the
disabled state from this, not from caught errors: a disabled button is better than
a refused click, and on the sign-in screen there is no request to refuse.

`ai_enabled` is **independent of** `agentic_enabled`. The latter answers "does
the licence permit AI"; this answers "is AI switched on here". Both false is
ordinary. `ai_enabled: false` with `agentic_enabled: true` is the interesting
case — a licensed platform deliberately running without AI — and clients must
handle it rather than treating either as implying the other.

## Changing it

```
PUT  /api/v1/platform/settings       # administrator only
```

| Body | Effect |
|---|---|
| `{"ai_enabled": true}` | AI on, explicitly, overriding the deployment default |
| `{"ai_enabled": false}` | AI off, explicitly, overriding the deployment default |
| `{"ai_enabled": null}` | Reset: follow `PLATFORM_MODE` again |

**One polarity on the wire.** Reads and writes both use `ai_enabled`, where `true`
always means AI is on. The database column is named `ai_disabled` because it
follows `PLATFORM_MODE=ai_disabled`, and the inversion happens exactly once, in
`services/ai_state.py`. An earlier draft of this contract had clients *reading*
`ai_enabled` and *writing* `ai_disabled` — two names differing only by a negation,
at the boundary where getting it backwards means AI runs on a platform that
forbids it. Never expose both polarities to a client.

Three distinct operations. `null` is **not** the same as `false` — see
[data-model.md](../data-model.md) §1 — and a client that sends `false` for "reset"
pins the platform to disabled and hides any later change to the deployment
default.

- Administrator authorisation required (FR-024), via the existing `can_manage`
  check rather than a new permission.
- Audit-logged with who, when and the value (FR-023).
- Concurrent writes converge on one value, both attributable (FR-026).

### What happens on the way out

**Turning AI on** provisions the built-in content a normal boot would create, and
is safe to repeat (FR-012). The response must not claim completion before that has
finished, or an administrator will see an enabled platform with nothing in it and
report it as a bug.

**Turning AI off** stops new dispatch and refuses new executions within one
scheduler interval (SC-003). Work already running is allowed to finish under its
own existing timeout rather than being cancelled (FR-013) — **the interface must
say so before the administrator confirms**, because "off" that leaves runs
finishing for another few minutes is otherwise indistinguishable from a bug.

## What does not change

`PLATFORM_MODE` remains an environment setting, unreadable and unwritable through
the API. The runtime override exists so a restart is not needed; it does not turn
deployment configuration into an API surface.
