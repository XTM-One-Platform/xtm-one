# Implementation Plan: Minimal A2A Server (Inbound)

**Branch**: `006-a2a-server-inbound` | **Date**: 2026-08-18 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/006-a2a-server-inbound/spec.md`

## Summary

Expose selected XTM One agents to external A2A-speaking clients: an anonymously-fetchable Agent Card per exposed agent, and a JSON-RPC task surface (send / get / cancel) authenticated with per-exposure OAuth client credentials. Exposure authority and execution bounds are read from the platform's existing three-tier ownership model rather than from anything new.

The technical approach is dominated by one finding from Phase 0: **most of what this needs already exists as parameters on calls the codebase already makes.** Per-session tool narrowing, cancellation, quota metering, visibility filtering, rate limiting, audit logging, feature flagging and runs-without-an-assignment are all existing bricks. Three things are genuinely new — the OAuth client-credentials grant, the A2A protocol translation layer, and the exposure register itself — and one existing behaviour must be corrected for this surface (the SPA catch-all would answer an unknown card URL with HTML).

## Technical Context

**Language/Version**: Python 3.14 (backend, `backend/.venv`); TypeScript / React (frontend exposure toggle)

**Primary Dependencies**: FastAPI 0.136.1, SQLAlchemy 2.0.49 (asyncio), Alembic 1.18.4, Redis. **New**: `a2a-sdk==1.1.2`, contained to a single module (research R1)

**Storage**: PostgreSQL for the exposure register and task records (Alembic migration); Redis for rate-limit buckets and deduplication keys

**Testing**: `pytest`, run as `CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest` from `backend/`; the `integration` marker for behaviour touching real Postgres/Redis/MinIO

**Target Platform**: Linux server, deployed as multiple concurrent replicas

**Project Type**: Web application — `backend/` (FastAPI) + `frontend/` (React SPA)

**Performance Goals**: **None set, deliberately.** The spec's Out of Scope withdraws any bound on internal responsiveness under external load and names per-registration rate limiting as the sole control (10/min task creation, 1800/min read and cancel — research R8). Total external submission rate is therefore bounded by exposures × per-registration limit.

**Constraints**: Feature-flagged off by default; every external path fail-closed; no new tenancy, permission or identity concept; v0.3 protocol compatibility is required, not a fallback

**Scale/Scope**: Single-digit exposures and one to two partner integrations expected for the first release

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design — result recorded at the end of this section.*

- [x] **Fail-Closed by Default** (Principle I). Every new decision point denies on uncertainty: unevaluable visibility denies access (FR-033); an unknown protocol version is refused rather than guessed (FR-027); a card is refused when the connection cannot be confirmed encrypted (FR-017); the feature flag enables nothing when unset, which the existing flag system already guarantees. **One correction was required to satisfy this gate**: `settings.rate_limit_fail_mode` defaults to `"open"`, so a Redis outage would silently remove the only control the spec places on external load. This surface forces fail-closed locally, leaving the global default and internal behaviour untouched (research R8).

- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle II). External tasks execute as `AssignmentRun` rows through the existing engine, inheriting its terminal-state guard, which already refuses to re-execute a finished run when a retry and a lease reclaim race (research R4). Deduplication uses an atomic Redis `SET NX` so two concurrent retries cannot both start a run. The stale-run reaper this adds is scoped to external runs and must claim with `FOR UPDATE SKIP LOCKED`, matching `orchestration_engine`'s existing pattern. Exposure auto-revocation (FR-041) must be idempotent — revoking an already-revoked exposure is a no-op, not an error.

- [x] **CI-Enforced Quality Gates** (Principle III). All backend changes ship with tests. Unit tests cover exposure authority, boundary resolution, state mapping, version shaping and card projection. The `integration` marker covers the OAuth client-credentials grant, rate-limit buckets and deduplication (real Redis), and the task lifecycle (real Postgres). The migration is additive-only, so the migrations job's idempotent-re-run and downgrade-round-trip checks apply directly. `ruff` (including `flake8-bandit`) and frontend `tsc`/`eslint` are blocking as usual.

- [x] **Rationale-Documented Complexity** (Principle IV). Four places carry "why" comments because their reasoning is not recoverable from the code: the `a2a-sdk` containment boundary (why exactly one module may import it); the boundary-follows-the-agent rule (why the exposing actor's identity is deliberately not used); the local fail-closed rate-limit override (why it diverges from the global default); and the pinned `is_admin=False` (why administrator status is discarded rather than honoured).

- [x] **Backward-Compatible, Auditable Evolution** (Principle V). The migration is additive — two new tables plus one nullable foreign key on the OAuth client table. No column changes type or nullability, so a rolling deploy leaves old replicas ignoring tables they do not know about. There is no prior state to misinterpret, so Principle V's unrecognized-state requirement is met trivially. Every exposure change, submission and denial is audit-logged (FR-004, FR-037).

**Post-Phase-1 re-check**: all five gates still pass. The design added no scheduled job beyond the reaper (covered under Principle II), no new secret material, and no change to an existing security boundary other than the deliberate local rate-limit override recorded above. **Complexity Tracking is empty — no violations to justify.**

## Project Structure

### Documentation (this feature)

```text
specs/006-a2a-server-inbound/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── agent-card.md
│   ├── a2a-jsonrpc.md
│   └── internal-management.md
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
backend/
├── alembic/versions/
│   └── <rev>_a2a_server_inbound.py    # additive: a2a_exposures, a2a_tasks, client FK
├── app/
│   ├── a2a/
│   │   ├── __init__.py                # package docstring carries the boundary rule
│   │   ├── protocol.py                # ONLY module importing a2a_sdk; v1.0 <-> v0.3 shaping
│   │   ├── card.py                    # explicit card projection (never a model dump)
│   │   ├── exposure.py                # authority, run-as identity resolution
│   │   └── tasks.py                   # create/get/cancel, state mapping, dedup, reaper
│   ├── api/v1/
│   │   ├── a2a_server.py              # external: card fetch + JSON-RPC task surface
│   │   ├── a2a_admin.py               # internal: exposure management REST
│   │   └── mcp_oauth.py               # MODIFIED: client_credentials grant + AS metadata
│   ├── models/
│   │   └── a2a.py                     # A2AExposure, A2ATask
│   ├── core/
│   │   ├── config.py                  # MODIFIED: A2A settings
│   │   └── feature_flags.py           # MODIFIED: A2A_SERVER flag
│   └── main.py                        # MODIFIED: router mounts + _API_NAMESPACES += "a2a"
└── tests/
    ├── test_a2a_exposure_authority.py # FR-002..FR-005, FR-041
    ├── test_a2a_card.py               # FR-010..FR-018, FR-042
    ├── test_a2a_tasks.py              # FR-019..FR-028, FR-043
    ├── test_a2a_boundary.py           # FR-029..FR-034
    ├── test_a2a_escalation_guard.py   # FR-035 — external work cannot mutate exposure/credentials
    ├── test_a2a_oauth_client_credentials.py
    └── test_a2a_spa_catchall.py       # research R2 — card 404 must be JSON, not HTML

frontend/
└── src/pages/agent-detail/            # exposure toggle, tool narrowing, URL + credential display
```

**Structure Decision**: Web application. The backend gains one self-contained package (`app/a2a/`) plus two API modules, following the existing convention where an external protocol surface lives in `app/api/v1/` and its logic in a dedicated package. The frontend change is confined to the agent detail page, reusing the pattern already used for the per-agent public-web exposure toggle (`web_enabled` / `web_token`) rather than adding a new administrative screen — consistent with the spec's decision to introduce no new permission UI.

## Phase Sequencing

Ordered so each step is independently testable and the riskiest reuse assumption is proven first.

| Step | Delivers | Proves |
| --- | --- | --- |
| 1 | Models, migration, feature flag, `_API_NAMESPACES` fix | Additive migration round-trips; unknown `/a2a` paths return JSON 404 |
| 2 | Exposure register + authority + auto-revocation (FR-001..FR-009, FR-041) | User Story 1 — testable with no protocol surface in existence |
| 3 | Boundary resolution and the escalation guard (FR-029..FR-036) | User Story 4's bounds, unit-testable before any endpoint exists |
| 4 | Card projection + external fetch (FR-010..FR-018, FR-042) | User Story 2 |
| 5 | OAuth client-credentials grant | Partner can authenticate; the one genuinely new auth surface |
| 6 | Task surface + state mapping + dedup + reaper (FR-019..FR-028, FR-043) | User Story 3 |
| 7 | Frontend toggle; telemetry; operator docs | Operator-facing completion |

Steps 2 and 3 deliberately precede any endpoint: both are pure functions of the existing ownership model and are where the "reuse the existing bounding" premise either holds or fails. If it fails, it fails cheaply and before an external surface exists.

## Complexity Tracking

No constitution violations. Table intentionally empty.
