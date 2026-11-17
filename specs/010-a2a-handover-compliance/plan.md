# Implementation Plan: A2A-Compliant Handover

**Branch**: `feat/follow-up-A2A-handover` | **Date**: 2026-09-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/010-a2a-handover-compliance/spec.md`

## Summary

Make the inbound A2A surface represent handover faithfully. One correctness
change and three protocol capabilities, in that order:

1. a task's state derives from the work its run delegated, not only from the run
   (FR-001..FR-006);
2. a context becomes a first-class, partner-scoped entity so a transfer can own
   the conversation the protocol says it owns (FR-007..FR-014);
3. push notifications for a task nobody is watching (FR-015..FR-022);
The ordering is load-bearing, not a preference: (1) is what makes a task
long-lived, and (3) and (4) exist to serve long-lived tasks. Every phase is
independently shippable.

## Technical Context

**Language/Version**: Python 3.14 (backend); no frontend work

**Primary Dependencies**: FastAPI, SQLAlchemy async, Alembic, Redis (pub/sub),
`a2a-sdk` (protocol types), `httpx` (webhook delivery). No new dependency.

**Storage**: PostgreSQL — two new tables (`a2a_contexts`, `a2a_push_configs`),
a foreign key over `a2a_tasks.context_id`, and one partial index on
`sub_agent_tasks`

**Testing**: pytest (`CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest`)

**Target Platform**: Linux server, N concurrent replicas

**Project Type**: Web service — backend-only feature

state change it reports; a push notification is attempted within 5s of a terminal
transition. Neither may add latency to the run itself (FR-021).

**Constraints**: a resubscribe can land on any replica and delegated work runs in
connection for a task's whole life, which is now unbounded by design

**Scale/Scope**: ~10 partner registrations per deployment, single-digit
concurrent external tasks each. Four phases, ~30 requirements.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status |
|-----------|------|--------|
| III. Security by Default | A partner's callback credential is a stored secret | **PASS** — no new key material: the platform presents what the partner registered (research R2). The registered `token` and `credentials` are Fernet-encrypted at rest like every integration credential and never logged. |
| IV. Concurrency-Safe, Multi-Replica | State derivation, the parked-task ceiling and delivery retries all run on every replica | **PASS** — derivation is a read, so it is idempotent by construction; the ceiling sweep extends the existing A2A reaper, which already holds the single-replica discipline; delivery is claimed per row. |
| V. Alembic-Only, Lock-Safe Migrations | Two tables, one constraint, one index | **PASS** — additive DDL only, guarded by the existing `terminate_lock_holders` + `set_lock_timeout` recipe, one head, reversible. |
| VI. CI-Enforced Quality Gates | ruff, pytest, migration round-trip | **PASS** — no exemption sought. |
| VII. Rationale-Documented Complexity | Why the event path leaves the process at all is the non-obvious part | **PASS** — the "why" belongs at the publisher and subscriber, per research R3. |
| VIII. Shared Utilities Over Duplication | Every phase has an in-house precedent | **PASS** — see research R1, R3, R4, R5, R6. No second egress path, no second pub/sub, no second reaper, and no key infrastructure. |
| IX. TanStack Query / X. Design System | Frontend principles | **N/A** — no UI. |
| XI. Minimal, Reviewable Writing | These artifacts | **PASS** — asserted by their length; each fact stated once and cited by identifier. |

No violations to justify; Complexity Tracking is therefore omitted.

**Re-checked after Phase 1 design.** One gate gained detail; none failed:

- **V** — the migration count is two, and one adds a foreign key over populated
  data (`a2a_tasks.context_id`), so it backfills a context row per existing value
  before constraining. Additive, guarded, reversible, and the backfill is bounded
  by the number of existing tasks.

No gate fails, and no phase needs an exemption.

## Project Structure

### Documentation (this feature)

```text
specs/010-a2a-handover-compliance/
├── plan.md              # This file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── a2a-push-notifications.md # tasks/pushNotificationConfig/*, webhook payload
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
backend/
├── alembic/versions/            # 2 revisions: contexts + push configs, task context fk
├── app/
│   ├── a2a/
│   │   ├── tasks.py             # state derivation (FR-001), lease (FR-002),
│   │   │                        #   artifacts from children (FR-003), cancel
│   │   │                        #   cascade (FR-004), ceiling (FR-005)
│   │   ├── contexts.py          # NEW — mint, resolve, responsible-agent binding
│   │   ├── push.py              # NEW — config CRUD, delivery, signing
│   │   ├── card.py              # capabilities now true (FR-015)
│   │                            #   continuation at the current refusal
│   ├── models/a2a.py            # A2AContext, A2APushConfig, task.context fk
│   └── main.py                  # ceiling sweep on the A2A reaper
└── tests/                       # one file per phase, named for its behaviour
```

**Structure Decision**: the existing `app/a2a/` package, one new module per
capability. `tasks.py` is already 1,100 lines and owns the lifecycle, so state
derivation lands there rather than in a fifth module; contexts, push and events
are new concerns with their own tables or transports and get their own files.
No frontend directory appears because nothing in the spec is operator-facing —
which is itself the outstanding observability question the spec defers.

## Phase ordering and independence

| Phase | Requirements | Ships alone? | Depends on |
|-------|--------------|--------------|------------|
| 1. Delegated work lifetime | FR-001..FR-006 | Yes | — |
| 2. Context and continuation | FR-007..FR-014 | Yes | — |
| 3. Push notifications | FR-015..FR-022 | Yes, but pointless before 1 | 1 for value, not for function |

Phases 1 and 2 are independent of each other and could run in parallel. Phase 1
first regardless: it is the only phase fixing something untrue, and it is what
gives 3 and 4 a reason to exist.
