# Testing Strategy

The unit suite uses lightweight fake sessions (matching the existing
`FakeSession` style) — no live Postgres. Postgres-specific guarantees
(`FOR UPDATE SKIP LOCKED`, `INSERT ... ON CONFLICT`) are exercised by the
real-DB integration jobs in CI.

## Runnable today (no DB)

- `tests/test_orchestration_engine.py` — transition table, lease ownership,
  retry/deadline routing in `fail`, `await_event`, `extend_lease`,
  `requeue_execution`, `rearm_for_resume`, `reclaim_expired_leases`,
  `sweep_deadlines`, `claim_due` mutations, `create_execution` (non-dedup), and
  outbox `processed` / backoff / dead-letter.
- `tests/test_orchestration_adapters.py` — registry (register/get/list,
  double-register, missing → `AdapterNotFoundError`), and each adapter's
  `execute` result mapping (assignment status → COMPLETED/AWAIT/FAILED,
  evaluation run vs `resume_episode`, sub-agent payload validation).
- `tests/test_orchestration_reconciler.py` — `_execution_timeout`
  (deadline-derived vs default), worker-id format, and `_apply_result` →
  engine transition mapping.
- `tests/test_assignment_engine_schedule_spread.py` — spread-offset computation
  and `trigger_assignment` → `create_execution` (pending vs delayed, dedup key,
  wake nudge).
- `tests/test_sub_agent_per_run_cap.py` — the durable DB-count per-run cap.

Run:

```bash
uv run python -m pytest tests/test_orchestration_engine.py tests/test_orchestration_adapters.py tests/test_orchestration_reconciler.py tests/test_assignment_engine_schedule_spread.py tests/test_sub_agent_per_run_cap.py
```

## Validated by the real-DB path (CI)

- `FOR UPDATE SKIP LOCKED` claim semantics under concurrency.
- A transition decides on the locked row, not on a copy its session already
  holds (`tests/test_orchestration_stale_lease_postgres.py`).
- `INSERT ... ON CONFLICT DO NOTHING RETURNING` dedup for `create_execution` /
  `emit_outbox_event`.
- Migration round-trip (`upgrade` → `downgrade` → `upgrade`) — the
  orchestration migration (`o9r8c7h6e5s4`) is reversible by construction (full
  `downgrade`). Because the branch landed alongside the filename-widen head
  (`w1d2e3n4f5g6`), a merge migration (`z7y6x5w4v3u2`) stitches the two heads
  back into one. A linear `downgrade -1` walk is mechanically undefined at a
  merge revision (Alembic raises "Ambiguous walk" — two parents, no canonical
  one), so CI's downgrade round-trip stops its walk when it reaches a merge
  point; merge migrations have empty bodies, so nothing is skipped, and the
  two constituent migrations remain individually reversible.
- `test_alembic_single_head` — single head preserved (`z7y6x5w4v3u2`, the merge
  of `o9r8c7h6e5s4` + `w1d2e3n4f5g6`).
