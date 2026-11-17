# Contracts & Invariants

## Engine API (`app/services/orchestration_engine.py`)

Every mutator **flushes but never commits** — the caller owns the transaction.

| Function | Purpose |
|----------|---------|
| `create_execution(db, *, adapter, payload, run_at=, deadline_at=, dedup_key=, max_attempts=)` | Insert a `pending` execution. Idempotent on `dedup_key` via `INSERT ... ON CONFLICT DO NOTHING`. |
| `claim_due(db, *, adapter, worker_id, limit, lease_seconds=)` | `FOR UPDATE SKIP LOCKED` claim of due `pending` rows, oldest `run_at` first (a job's place in the queue), past the `payload['not_before']` of a deferral that kept its place → `running` + lease + `attempt_count++`. |
| `extend_lease(db, *, execution_id, worker_id, lease_seconds=)` | Heartbeat; locked re-read; raises `LeaseLostError` if stolen/terminal. |
| `complete(db, *, execution_id, worker_id=, require_lease=, payload_update=, adapter_state=)` | `running → completed`, clears lease. |
| `fail(db, *, execution_id, worker_id=, require_lease=, error, retryable=, backoff_seconds=)` | Retry (`→ pending` + backoff), give up (`→ failed`), or exhaust (`→ dead_letter`). |
| `await_event(db, *, execution_id, worker_id=, require_lease=, adapter_state, deadline_at=)` | Pause a `running` row (release lease) to wait for an outbox event / deadline. |
| `defer(db, *, execution_id, worker_id=, require_lease=, reason, delay_seconds, adapter_state, payload_update=, place=)` | Hold a claimed row: `running → pending`, hold recorded in `payload['hold']`. *place* (`DEFER_PLACES`) is where it waits: `after` (default) - `run_at` moved by `delay_seconds`; `keep` - `run_at` (its place) kept, claimable once `payload['not_before']` passed, ahead of what was queued after it; `head` - first in its adapter's queue (`head_of_queue`), due now. The first deferral records the attempts and runnable time left and every deferral restores them (attempts back to the pre-hold count, `deadline_at` = when it may run again + that runnable time); on the outbox path (`require_lease=False`) no claim is given back. Locked re-read with `refresh`. Used while agentic AI is unavailable (`ai_hold.py`), by a scene preview waiting its turn (`keep`) and a render a stopping worker hands back (`head`); the adapter bounds it. |
| `end_hold(db, *, execution_id, worker_id, held_adapter_state=)` | The held work is starting: drops `payload['hold']` (and the `held:<code>` state) under the lock, so a later hold starts its own bound. Called by the adapters (`ai_hold.close_hold_if_ai_is_back`) before the body runs; a never-held execution never reaches it. |
| `rearm_for_resume(db, *, execution_id)` | `running → pending` (attempts reset) — used by the resume outbox handler. |
| `requeue_execution(db, *, execution_id, run_at=)` | Operator retry: `failed | dead_letter → pending`. |
| `cancel_execution(db, *, execution_id)` | Cancel a non-terminal row. |
| `reclaim_expired_leases(db, *, limit, adapter=, head_adapters=)` | `running` w/ expired lease → `pending` (first in the queue for the adapters of `head_adapters`) or `dead_letter` if exhausted. Returns dead-lettered rows. |
| `leased_owners(db, *, adapters)` / `reclaim_from_gone_owners(db, gone, *, adapters, limit, reason_of=)` | The holders of live leases, and the `running` rows of a holder known gone (`worker_presence.gone`) taken back at once - `pending` first in the queue, `adapter_state` `worker_gone`, the holder's last record in `payload['lease_expired']` - or `dead_letter` if exhausted; only for the adapters that set `resume_when_worker_gone`. Returns dead-lettered rows. |
| `sweep_deadlines(db, *, limit, adapter=)` | `pending|running` past `deadline_at` → `dead_letter`. Returns swept rows. |
| `emit_outbox_event(db, *, execution_id, event_type, dedup_key, payload=, available_at=)` | Idempotent outbox insert (`ON CONFLICT DO NOTHING`). |
| `claim_outbox_events(db, *, worker_id, limit, visibility_seconds=)` | Claim due `pending` events (SKIP LOCKED); attempts++ + visibility timeout; dead-letter over max. |
| `mark_outbox_processed` / `mark_outbox_failed` | Terminate / backoff-or-dead-letter an event. |
| `notify_orchestration_wake()` | Best-effort Redis pub/sub nudge so the reconciler runs sooner. |

## Transition table (`_ALLOWED_TRANSITIONS`)

```
pending     → running, cancelled
running     → completed, failed, dead_letter, pending, cancelled
failed      → pending          (operator requeue)
dead_letter → pending          (operator requeue)
completed   → (terminal)
cancelled   → (terminal)
```

Every pair in this table is reachable and tested — there are no dead
transitions.

> **The AWAIT pause is a state-preserving self-transition not shown above.**
> The table is the literal `_ALLOWED_TRANSITIONS` map, which governs only
> *state changes*. `await_event` does **not** change `state` — it keeps the row
> `running` while releasing the lease and recording `adapter_state` (a
> `running → running` self-transition). The paused row sits `running`-but-
> unleased until an inbound outbox event (resume → `rearm_for_resume`, which
> takes it back to `pending`) or the deadline sweep acts on it. So a
> `running → running` "transition" is expected and relied on by the
> engine / API / flow diagrams even though it is intentionally absent from the
> state-change map above (see invariant 4).

## Invariants

1. **Lease ownership** — `complete` / `fail` / `extend_lease` / `await_event`
   re-read the row under `FOR UPDATE` and verify `lease_owner == worker_id`
   before mutating (the session is `expire_on_commit=False`, so a stale
   in-memory row must never overwrite a reclaimed/dead-lettered DB row). A
   transition that decides on the row's live state or lease ownership and whose
   caller may already hold the row re-reads with `refresh` (`populate_existing`),
   or the check would read that held copy: `complete` / `fail` / `await_event`,
   `rearm_for_resume` and `cancel_execution`. `reset_execution` also preloads
   then locks, but it overwrites every mutable field unconditionally and reads
   none to decide, so it keeps the plain `_locked_get` (see
   `05-failure-and-recovery.md` > Lease races).
2. **At-least-one-attempt accounting** — `claim_due` increments `attempt_count`,
   so a worker that crashes mid-run eventually exhausts attempts and
   dead-letters rather than spinning forever. `defer` is the one transition
   that gives the attempt back: the work did not run, it was held for a reason
   outside it, and the adapter (not the attempt count) bounds how long.
3. **Deadline guard** — a retry (`fail` → `pending`) past `deadline_at` is
   redirected to `dead_letter`; the deadline sweep is the backstop. The work an
   execution runs is bounded by `bound_by_deadline(budget, deadline_at)`, so a
   re-run after a crash winds down inside what the deadline leaves instead of
   racing the sweep; work with nothing left does not start, and a
   timeout the deadline cut past the grace is credited back free like the
   dead-letter it replaces.
4. **AWAIT releases the lease** — a paused (`running`, no lease) row is ignored
   by `claim_due` and `reclaim_expired_leases`; only an inbound outbox event or
   the deadline sweep acts on it.
5. **Outbox idempotency** — `(execution_id, event_type, dedup_key)` is unique;
   re-emitting is a no-op.
