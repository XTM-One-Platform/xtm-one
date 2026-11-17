# Executive Summary

## The problem it replaced

Before this engine, three independent subsystems each encoded their state
implicitly with their own ad-hoc recovery:

- **Assignment dispatch** — SAQ scheduled set + `delayed_run:*` Redis fallback
  keys + a ~400-line startup recovery loop (`recover_interrupted_runs`) + a
  per-tick stale-run sweep (`_cleanup_stale_runs`) + a live delayed-run poller
  (`_process_delayed_runs`).
- **Evaluation runs** — SAQ `run_evaluation_job` / `resume_evaluation_episode_job`
  + an episode `waiting_callback` timeout sweep + a stale-eval sweep + a startup
  recovery pass.
- **Sub-agent tasks** — in-process fire-and-forget `asyncio.create_task` + a
  startup "fail them all" pass + a stale-task sweep.

Operators had no single place to answer "what is this job doing right now," and
on-call had a different recovery story for each consumer.

## What it is now

A single durable **finite-state-machine + transactional-outbox** engine in
PostgreSQL. Every long-running unit of work is one `orchestration_executions`
row. A single **reconciler** loop in the worker drives all of them through a
common lifecycle with leases, deadlines, retries, and crash recovery. Per-job
behaviour is supplied by typed **adapters** (`assignment`, `evaluation`,
`sub_agent`).

The legacy `delayed_run:*` Redis keys, the three `recover_interrupted_*` startup
passes, the three `_cleanup_stale_*` sweeps, and the `_process_delayed_runs`
poller are all **deleted**.

## Why it's better

- **Durable & observable** — state lives in one Postgres table; transitions are
  auditable; one query shows every in-flight job.
- **Correct recovery** — crash recovery is intrinsic (lease expiry + deadline
  sweep), not a bespoke reconstruction per consumer.
- **Multi-worker by construction** — `FOR UPDATE SKIP LOCKED` claiming lets any
  number of worker replicas share the load with no external lock.
- **One contract** — adding a new long-running job type means writing one
  adapter; it inherits scheduling, retries, leases, deadlines, and recovery for
  free.
