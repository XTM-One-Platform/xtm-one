# Implementation Plan: How a calling agent waits for a partner's answer

**Branch**: `011-a2a-client-wait-arbitration` | **Date**: 2026-09-09 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/011-a2a-client-wait-arbitration/spec.md`

## Summary

Three increments on the outbound A2A client, in dependency order. The first
changes when a delegation stops waiting: today an attempt that outlasts its
ceiling is a timeout that cancels the partner's work, which lost a completed
631-second result to a caller that had already gone. It becomes a still-running
outcome the calling agent collects later, and the wait ends at a short grace
period wherever the platform will be told the outcome by other means.

The remaining two add the means. Whether a partner will call back becomes a
recorded fact on the target, refreshed by the existing verification sweep and
deliberately excluded from the fingerprint that suspends a target for operator
acceptance. Callback consumption registers a callback and resumes the calling
run through the mechanism that run path already has — the durable execution for an assignment run, the existing
conversation wake for chat.

No new wait machinery: each increment reuses a mechanism this codebase already
runs, and the plan's main risk is the temptation to build parallels instead.

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript/React (operator surface)

**Primary Dependencies**: FastAPI, SQLAlchemy 2.0 (async), httpx 0.28.1, Alembic,
Postgres, Redis; TanStack Query on the frontend

**Storage**: Postgres — new columns on `a2a_client_targets` (declared
capabilities) and `a2a_delegations` (the mechanism used, and an outstanding
delegation's open state). No new tables.

**Testing**: pytest, from `backend/` as
`CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest`

**Target Platform**: Linux server, multi-replica

**Project Type**: Web application (backend + frontend)

**Performance Goals**: a delegation whose partner task finishes inside the grace
period costs the calling agent one turn, not two (SC-008); a partner's hand-off
is visible to the caller before the task terminates (SC-004)

**Constraints**: no bound of this feature's own on outstanding tasks (FR-022);
every mechanism degrades to the one beneath it rather than failing the
delegation (FR-013, FR-018); a delegation concludes exactly once across
mechanisms (FR-019)

**Scale/Scope**: bounded by the calling run's existing budgets (`009` FR-048)
and by each partner's own limits

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Verdict |
|---|---|---|
| **II. Fail-Closed by Default** | Callback reachability is decided, not assumed: an explicit operator setting, and where unset a derivation that treats any address `009` FR-035 would refuse as unreachable. An unauthenticated or uncorrelated callback resumes nothing (FR-017). | PASS |
| **III. Security by Default** | Registering a callback discloses this deployment's address to a partner, which the explicit setting exists to let an operator refuse (FR-010). Everything arriving over a callback is partner-authored and goes through the same guard as any other partner output (`009` FR-036/FR-047). | PASS |
| **IV. Concurrency-Safe, Multi-Replica** | A callback may land on any replica, and the polling floor may conclude the same delegation concurrently. FR-019 requires exactly one conclusion; the delegation row is the correlation point and the claim. | PASS — see [research.md](./research.md) R6 |
| **V. Alembic-Only, Lock-Safe Migrations** | Two ADD COLUMN revisions on head `a2a9announce1`, following the probe → `terminate_lock_holders` → `set_lock_timeout` → guarded DDL recipe. No backfill is owed: absent capabilities mean "not declared", which is the correct reading for a target not yet re-verified. | PASS |
| **VI. CI-Enforced Quality Gates** | ruff, the full suite, single-head migration integrity, and the no-dead-code sweep over `app/a2a/client/` — which forbids landing a helper before its call site. | PASS |
| **VII. Rationale-Documented Complexity** | Two wait mechanisms is real complexity. The spec's ordering is the justification: each is unreachable before its predecessor, and each degrades to it. | PASS |
| **VIII. Shared Utilities Over Duplication** | The whole plan. Resumption reuses each run path's existing mechanism; cancellation joins the existing conversation cascade; destination checks reuse `assert_allowed_async`; bounded reads reuse the pattern already in `verification.py`; capabilities ride the existing verification sweep. | PASS |
| **IX. TanStack Query for Server State** | The operator surface showing declared capabilities extends the existing A2A verification card and its hook. | PASS |
| **X. Design System Compliance** | Same surface, same components. | PASS |
| **XI. Minimal, Reviewable Writing** | Four increments, each independently shippable and separately reviewable. | PASS |

No violations. Complexity Tracking is therefore omitted.

### Post-design re-check

Two verdicts changed once the design was worked out, and one obligation was
found that specification had missed.

**III. Security by Default — the feature's dominant risk.** US3 adds an
externally reachable route, which `009` had none of; it is the most
consequential thing here. It stays PASS on the strength of the guards being
existing ones — transport check,
pre-auth rate limit, constant-time secret comparison before any state is read,
uniform not-found so a partner cannot probe which delegations exist — and of
the body carrying no result, so nothing partner-authored is trusted from an
unauthenticated route. It is also why callbacks are the last increment and are
gated on an operator's decision rather than on a partner's card alone.

**IV. Concurrency-Safe, Multi-Replica — a second race, already solved
upstream.** Parking an assignment run introduces the resume-beats-pause race
the orchestration engine already names: `rearm_for_resume` refuses while the
lease is still held, and the reconciler backs off. Reusing that path means not
re-solving it. The conclude-once claim on the delegation row handles the other
race, between a callback and the polling floor.

**I. Assignment-Driven Automation** was not in the pre-design table and belongs
there: parking a run mid-delegation is a change to how an assignment executes.
It stays PASS because the park is the mechanism human-input requests already
use, not a new one.

## Project Structure

### Documentation (this feature)

```text
specs/011-a2a-client-wait-arbitration/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── delegation-outcomes.md
│   └── partner-callbacks.md
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── a2a/
│   │   ├── client/
│   │   │   ├── delegation.py      # the wait: grace period, hand-back, collect
│   │   │   ├── sdk.py             # project_card: read capabilities
│   │   │   ├── verification.py    # apply_verdict: record them; fingerprint UNCHANGED
│   │   │   ├── tool.py            # the still-running result the model sees
│   │   │   └── callbacks.py       # NEW: register, receive, correlate
│   │   └── push.py                # inbound sender — mirrored, not reused
│   ├── api/v1/
│   │   ├── a2a_client.py          # operator read model
│   │   └── a2a_callbacks.py       # NEW: the callback receiver
│   ├── models/a2a.py              # target capabilities, delegation mechanism/state
│   ├── services/
│   │   ├── integration_tools.py   # tool schema, outstanding-task teardown
│   │   ├── parent_wake.py         # generalised: wake on non-SubAgentTask work
│   │   └── sub_agent.py           # the conversation cancel cascade
│   └── core/config.py             # grace period, reachability, floor age
├── alembic/versions/              # two revisions on head a2a9announce1
└── tests/                         # test_a2a_client_*.py alongside their subjects

frontend/src/
├── hooks/api/useA2AClientApi.ts   # capabilities in the target read model
└── pages/integration-detail/A2AVerificationCard.tsx
```

**Structure Decision**: Web application. The work is overwhelmingly backend and
lands in the existing `app/a2a/client/` package, which already holds the
outbound direction's verification, credentials, delegation and sweep. Two new
modules there rather than in `app/a2a/` proper, because both are client-side
concerns; the callback *receiver* is a route and belongs with the other v1
routes. The frontend change is confined to the existing verification card.
