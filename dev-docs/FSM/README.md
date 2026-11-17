# Durable Orchestration Engine (FSM + Outbox)

This directory documents the durable orchestration engine that backs every
long-running multi-step platform job: **assignment dispatch**, **evaluation
runs (+ episode callbacks)**, and **sub-agent tasks**.

| Doc | Contents |
|-----|----------|
| [00-executive-summary.md](00-executive-summary.md) | What it is, why it replaced the old patchwork |
| [02-contracts-and-invariants.md](02-contracts-and-invariants.md) | Engine API, transition table, invariants |
| [03-functional-schema.md](03-functional-schema.md) | The two tables + columns |
| [04-flow-diagrams.md](04-flow-diagrams.md) | Lifecycle + dispatch/recovery flow |
| [05-failure-and-recovery.md](05-failure-and-recovery.md) | Crash recovery, leases, deadlines, dead-letter |
| [06-testing-strategy.md](06-testing-strategy.md) | What's tested where |

## TL;DR

- One table — `orchestration_executions` — is the single source of truth for
  scheduling (`run_at`), in-flight ownership (`lease_owner` / `lease_expires_at`),
  hard timeouts (`deadline_at`), and retries (`attempt_count` / `max_attempts`).
- Producers (cron, manual trigger, webhooks, evaluation API) create an execution
  via `orchestration_engine.create_execution(...)` and commit. No Redis
  `delayed_run:*` keys, no SAQ scheduled set for these jobs.
- The **reconciler** (`orchestration_reconciler.py`) runs in the **worker**
  process. Each cycle it sweeps deadlines, reclaims expired leases, claims due
  executions (`FOR UPDATE SKIP LOCKED`) and runs each via its **adapter** under a
  heartbeated lease, then drives the transactional **outbox** for resume events.
  Concurrency is capped by a shared semaphore sized `max_concurrent_runs +
  max_concurrent_sub_agent_tasks`; an adapter may declare a positive
  `concurrency_limit` (`OrchestrationAdapter.concurrency_limit`) to run on its
  OWN dedicated semaphore + claim accounting instead, isolating a long-lived
  workload from the shared pool (the autonomous pentest adapter uses this via
  `MAX_CONCURRENT_AUTONOMOUS_RUNS`). An adapter with a long-running `execute` may
  also declare `cancel_poll_seconds` to opt into a **fast cancel watcher**: the
  reconciler polls the row and fires `AdapterContext.cancel_event` the instant it
  goes terminal out-of-band (operator cancel), so the in-flight agent loop aborts
  between iterations instead of at the end of its cycle (autonomous uses this via
  `AUTONOMOUS_CANCEL_POLL_SECONDS`); the lease heartbeat is the backstop.
- Crash recovery is intrinsic: a dead worker's leases expire and are reclaimed
  (re-run) or dead-lettered automatically — there is no startup recovery pass and
  no per-consumer stale-run sweep.
