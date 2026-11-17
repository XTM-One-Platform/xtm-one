# Data model

Two existing tables gain columns. No new table: the delegation record is already
the correlation point between a partner's task and the work that should collect
it, and a second record could only disagree with it.

## `a2a_client_targets` — what a partner declares

| Field | Type | Notes |
| --- | --- | --- |
| `declares_callbacks` | boolean, nullable | From the card's capabilities. NULL means not yet re-verified, which reads as "not declared" — the safe interpretation |

Written by the single writer that already records the other card facts, on the
same verification sweep, at the same staleness threshold. **Neither joins the
card fingerprint** — see [research.md](./research.md) R3: adding them would
suspend every existing registration on the next sweep, and a capability changes
how the platform waits rather than what it may send (FR-009).

## `a2a_delegations` — the exchange

This table is outbound-A2A only, and this feature owns every user of it: the
client writes it, the ownership check reads it, the operator read model displays
it. Local hand-offs between this platform's own agents — consult, transfer,
background task — live in `agent_delegations`, a different table with a
different writer, and are untouched here.

| Field | Type | Notes |
| --- | --- | --- |
| `mechanism` | string, nullable | How this delegation waited: asking directly, or being called back (FR-020) |
| `callback_secret` | encrypted string, nullable | Minted per delegation when a callback is registered; the partner presents it back. Fernet at rest, like every other partner credential here |
| `outcome` | *(existing)* | NULL while the task is outstanding |
| `terminal_at` | *(existing)* | NULL while the task is outstanding |

**Outstanding is the absence of a conclusion**, not a state column. The migrated
schema has no `state` column and none is added: a row with no `outcome` and no
`terminal_at` is a delegation still in flight, which cannot disagree with itself
the way a separate state field could.

### The row's lifecycle changes

Today the row is created at conclusion, so a delegation in flight has no row —
which is why two live delegations were invisible in `a2a_delegations` during
manual verification. Three things now need it earlier:

- a collect, for its ownership check;
- the cancel cascade, to know what to cancel;
- a callback, to find the run to resume.

So the row is written when the task is handed back, and updated when the
exchange concludes.

```
submitted ──► outstanding ──► completed | partner_failed | timeout | cancelled | …
                   │
                   └── row written, outcome NULL, terminal_at NULL
```

## Entities that are not rows

**Callback reachability** — one value for the deployment, not per partner. An
explicit operator setting, falling back to a derivation over the configured
public address. Reported where a partner's health verdict is already reported,
because that is where an operator looks to find out why a mechanism is not being
used.

**Wait mechanism** — chosen per delegation from what the target declares and
what the deployment can receive, recorded on the row above. Not stored on the
target: a partner that declares callbacks may still never send one, and the row
records what happened rather than what was hoped.

## Relationships this feature depends on and does not change

- `A2ADelegation.run_ref` → `assignment_runs`, NULL on the chat path. That NULL
  is what distinguishes the two resumption mechanisms: a row with a run parks
  and resumes; a row without one belongs to a conversation and wakes it.
- `A2ADelegation.partner_task_id` — the identifier a partner names in a
  callback, and the one a calling agent is handed to collect with.
- `A2ADelegation.user_id` — what scopes ownership on the chat path, where there
  is no run to scope it (`009` FR-046).

## Validation rules

- A collect is refused unless a delegation row matches the caller's integration,
  target and user, plus the run where the caller has one (FR-003).
- A callback is refused unless it authenticates against the secret minted for
  that delegation and names that delegation's partner task (FR-017).
- A delegation transitions out of `outstanding` exactly once, whichever
  mechanism reports it (FR-019).
- Capability columns are advisory: the platform never attempts a mechanism a
  target does not declare (FR-008), and never suspends a target because one
  changed (FR-009).
