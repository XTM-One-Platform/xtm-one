---

description: "Task list for A2A-compliant handover"
---

# Tasks: A2A-Compliant Handover

**Input**: Design documents from `/specs/010-a2a-handover-compliance/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Required. This is a backend change, so Constitution VI (CI-Enforced
Quality Gates) makes pytest tasks blocking rather than optional. Tests are flat
in `backend/tests/`; anything touching real Postgres or Redis carries the
`integration` marker.

**Organization**: One phase per user story. The four are independent by design
(see plan.md's phase table); only the shared harness in Phase 2 blocks them.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1..US3, matching spec.md

---

## Phase 1: Setup (Shared Configuration)

**Purpose**: the four bounds research deferred to implementation. Every story
reads at least one.

- [X] T001 Add `a2a_task_max_lifetime_seconds` (FR-005 parked ceiling) to `backend/app/core/config.py` with a fail-closed default shorter than one day
- [X] T002 [P] Add `a2a_push_max_attempts` (FR-020) and `a2a_push_configs_per_task` (FR-022) to `backend/app/core/config.py`
- [X] T004 [P] Assert every new setting rejects an out-of-range value at startup in `backend/tests/test_settings_a2a_bounds.py` (Constitution II)

---

## Phase 2: Foundational (Blocking Prerequisite)

**Purpose**: one shared test harness. Nothing else blocks a story — the
migrations, models and modules each belong to exactly one phase.

**⚠️ CRITICAL**: every story's tests build on T005

- [X] T005 Add `backend/tests/a2a_server_stubs.py` — the inbound mirror of `a2a_client_stubs.py`: ordered fake session, the task/run/exposure/agent quartet the executor loads, and an executor harness, so each story's tests express behaviour rather than re-rolling doubles

**Checkpoint**: stories may now proceed in any order or in parallel

---

## Phase 3: User Story 1 - Delegated work finishes where the partner can see it (Priority: P1) 🎯 MVP

**Goal**: a task reports `working` while work it delegated runs, its output lands on the task, and cancellation stops it.

**Independent Test**: submit to a delegating agent, poll to terminal, confirm the delegated output is in the artifacts; separately cancel mid-flight and confirm the child stopped.

### Tests for User Story 1

- [X] T006 [P] [US1] Assert `resolve_state` reports `working` while a child of the run is non-terminal, and the run's own state once none are, in `backend/tests/test_a2a_delegated_lifetime.py` (FR-001)
- [X] T007 [P] [US1] Assert the lease keeps being renewed while a task is parked, so the reaper does not report it abandoned, in `backend/tests/test_a2a_delegated_lifetime.py` (FR-002)
- [X] T008 [P] [US1] Assert a child's output appears among `build_artifacts`' results, attributed to the agent that produced it, in `backend/tests/test_a2a_delegated_lifetime.py` (FR-003)
- [X] T009 [P] [US1] Assert `tasks/cancel` marks the children cancelled and that a child completing afterwards adds no artifact, in `backend/tests/test_a2a_delegated_lifetime.py` (FR-004)
- [X] T010 [P] [US1] Assert a task past `a2a_task_max_lifetime_seconds` is driven terminal with a partner-visible reason, in `backend/tests/test_a2a_reaper.py` (FR-005)
- [ ] T011 [P] [US1] Assert a child's quota charge is audit-attributable to the submitting client, in `backend/tests/test_a2a_delegated_lifetime.py` (FR-006)
- [X] T012 [P] [US1] Assert the child lookup matches the indexed JSON paths and statuses, in `backend/tests/test_a2a_delegated_lifetime.py`

### Implementation for User Story 1

- [X] T013 [US1] Add an Alembic revision creating the partial index on `sub_agent_tasks.metadata_json->>'parent_run_id'` for `status IN ('pending','running')` in `backend/alembic/versions/`, built `CONCURRENTLY` outside a transaction per Constitution V
- [X] T014 [US1] Add `live_delegated_work(db, run_id)` to `backend/app/a2a/tasks.py`, querying the indexed JSON path (research R1)
- [X] T015 [US1] Fold T014 into `resolve_state` in `backend/app/a2a/tasks.py` so a run with live children reports `working` (FR-001)
- [X] T016 [US1] Stop the reaper reporting a task abandoned while its delegated work runs, in `backend/app/a2a/tasks.py` — the obligation FR-002 states, reached by asking rather than by heartbeating for work in another process (FR-002)
- [X] T017 [US1] Extend `build_artifacts` in `backend/app/a2a/tasks.py` to append each finished child's output as its own artifact, reusing the `respondingAgent` metadata shape (FR-003)
- [X] T018 [US1] Call `mark_sub_agent_tasks_cancelled_for_runs` from the cancel path in `backend/app/a2a/tasks.py`, and refuse a late child's artifact once the task is terminal (FR-004)
- [X] T019 [US1] Add the parked-task ceiling to `reap_stale_tasks` in `backend/app/a2a/tasks.py`, with its own curated reason added to `_PARTNER_VISIBLE_ERROR_PREFIXES` (FR-005, research R6)
- [ ] T020 [US1] Attribute a child's quota consumption to the submitting client in `backend/app/a2a/tasks.py`, extending `_audit_quota_exhaustion`'s actor to post-terminal charges (FR-006)

**Checkpoint**: US1 is independently shippable — the surface stops reporting something untrue

---

## Phase 4: User Story 2 - A transfer carries the conversation (Priority: P2)

**Goal**: a context is a partner-scoped entity that records who answers, and a follow-up reaches that agent.

**Independent Test**: submit to a transferring agent, then send a second message carrying the returned `contextId` and confirm the transfer target answers.

### Tests for User Story 2

- [X] T021 [P] [US2] Assert a submission with no `contextId` mints one, and one carrying an issued id is grouped under it, in `backend/tests/test_a2a_context.py` (FR-007, FR-008)
- [X] T022 [P] [US2] Assert an unissued `contextId` and a foreign one return the identical not-found outcome, in `backend/tests/test_a2a_context.py` (FR-009, FR-010)
- [X] T023 [P] [US2] Assert a transfer moves `responsible_agent_id` and that consult and delegate do not, in `backend/tests/test_a2a_context.py` (FR-011)
- [X] T024 [P] [US2] Assert a follow-up is executed by the recorded responsible agent, in `backend/tests/test_a2a_context.py` (FR-012)
- [X] T025 [P] [US2] Assert a follow-up is refused when the responsible agent is no longer reachable by that client, and while the previous task is non-terminal, in `backend/tests/test_a2a_context.py` (FR-013, FR-014)
- [ ] T026 [P] [US2] Assert the migration backfills one context per existing `context_id` value, marked `integration`, in `backend/tests/test_migrations_a2a_context.py` — the upgrade/re-run/downgrade round-trip was verified by hand against `copilot_b`; this is the in-suite version

### Implementation for User Story 2

- [X] T027 [P] [US2] Add the `A2AContext` model per data-model.md to `backend/app/models/a2a.py`
- [X] T028 [US2] Add an Alembic revision creating `a2a_contexts`, backfilling one row per distinct existing `a2a_tasks.context_id`, then adding the foreign key, in `backend/alembic/versions/`
- [X] T029 [US2] Add `backend/app/a2a/contexts.py` with mint, ownership-scoped resolve, and responsible-agent read (FR-007..FR-012)
- [X] T030 [US2] Set the context on task creation in `backend/app/a2a/tasks.py`, minting or resolving through T029
- [X] T031 [US2] Replace the `taskId` continuation refusal in `backend/app/api/v1/a2a_server.py` with a resume that routes to the responsible agent (research R5)
- [X] T032 [US2] Refuse a follow-up whose responsible agent is unreachable or whose previous task is non-terminal, in `backend/app/api/v1/a2a_server.py` (FR-013, FR-014)
- [X] T033 [US2] Update the context's responsible agent when a transfer completes, in `backend/app/a2a/tasks.py`, reusing the agent id `execute_task_run` already captures (FR-011)

**Checkpoint**: US2 is independently shippable — transfer means what the protocol says

---

## Phase 5: User Story 3 - A partner learns the outcome without polling (Priority: P3)

**Goal**: a partner registers a callback and is notified on state changes, presenting the credential it registered.

**Independent Test**: register a callback, disconnect, and confirm the notification arrives on the terminal transition carrying no content.

### Tests for User Story 3

- [X] T034 [P] [US3] Assert the card declares `pushNotifications: true` in both protocol versions, in `backend/tests/test_a2a_card.py` (FR-015)
- [X] T035 [P] [US3] Assert all four config methods are served under both vocabularies and scoped to the owning client, in `backend/tests/test_a2a_push.py` (FR-016)
- [X] T036 [P] [US3] Assert a notification body carries task identity and state only, with the registered `token` echoed, in `backend/tests/test_a2a_push.py` (FR-017, FR-018)
- [X] T037 [P] [US3] Assert a callback address resolving inside the deployment is refused at registration, in `backend/tests/test_a2a_push.py` (FR-019)
- [X] T038 [P] [US3] Assert delivery stops at `a2a_push_max_attempts`, trips the breaker on a dead endpoint, and never changes the task's outcome, in `backend/tests/test_a2a_push.py` (FR-020, FR-021)
- [X] T039 [P] [US3] Assert registrations beyond `a2a_push_configs_per_task` are refused rather than evicting, in `backend/tests/test_a2a_push.py` (FR-022)
- [X] T040 [P] [US3] Assert `token` and `auth_credentials` are encrypted at rest and absent from logs, in `backend/tests/test_a2a_push.py` (Constitution III)

### Implementation for User Story 3

- [X] T041 [P] [US3] Add the `A2APushConfig` model per data-model.md to `backend/app/models/a2a.py`, with Fernet-encrypted `token` and `auth_credentials`
- [X] T042 [P] [US3] Add an Alembic revision creating `a2a_push_configs` in `backend/alembic/versions/`
- [X] T043 [US3] Replace `is_push_notification_method` and its refusal in `backend/app/a2a/protocol.py` with the four operations in both vocabularies (contracts/a2a-push-notifications.md)
- [X] T044 [US3] Add `backend/app/a2a/push.py` with config CRUD, address validation through `a2a/client/destinations.py`, and credential presentation (FR-016..FR-019, research R2, R4)
- [X] T045 [US3] Add delivery to `backend/app/a2a/push.py`, bounded and breaker-guarded, dispatched so it cannot delay a task (FR-020, FR-021)
- [X] T046 [US3] Route the four methods in `backend/app/api/v1/a2a_server.py`, refusing over the per-task bound (FR-022)
- [X] T047 [US3] Trigger delivery on every partner-observable transition in `backend/app/a2a/tasks.py`
- [X] T047b [US3] Register a callback supplied inline as `configuration.pushNotificationConfig` on `message/send`, in `backend/app/api/v1/a2a_server.py` — the path a partner uses when it has no task id yet (FR-031)
- [X] T048 [US3] Declare `pushNotifications: true` in `backend/app/a2a/card.py` (FR-015)

**Checkpoint**: US3 is independently shippable

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T062 Decide and implement delivery observability — the spec's one outstanding gap: an operator has nothing to look at when a partner's callback fails. Add counters to `backend/app/a2a/metrics.py` and an audit entry on exhausted delivery
- [X] T064 [P] Add review scenarios for delegated-task lifetime, continuation and callbacks to `dev-docs/a2a-review-scenarios.md`
- [X] T065 [P] Mark FR-016 superseded in `specs/006-a2a-server-inbound/spec.md` and correct its Out-of-scope entry, which states partners poll
- [X] T066 [P] Update `specs/006-a2a-server-inbound/contracts/a2a-jsonrpc.md` where it records the push family as refused
- [X] T070 [US3] Add `terminal_announced_at` to `A2ATask` in `backend/app/models/a2a.py` with a lock-safe revision in `backend/alembic/versions/`, backfilling already-terminal tasks so the sweep does not announce months of history
- [X] T071 [US3] Stamp `terminal_announced_at` from `announce_state` in `backend/app/a2a/tasks.py`, conditionally so one replica wins, and announce un-announced terminal tasks from the reaper sweep — which is how a task whose delegated work has finished gets the notification nobody currently sends
- [ ] T067 Run [quickstart.md](./quickstart.md) end to end against two instances, restarting them first so they are not serving stale code
- [X] T068 Verify the round-trip (upgrade, re-run, downgrade) for all five revisions in `backend/alembic/versions/` against a scratch database, including the context backfill over existing tasks, before opening the PR

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: T005 depends on Phase 1 only; blocks every story's tests
- **User Stories (Phases 3–5)**: each depends only on Phase 2. US1 first by policy, not by dependency — it is the only phase fixing something untrue, and US3 has little value before it
- **Polish (Phase 7)**: T062 needs US3; the doc tasks need whichever story they describe

### Within Each User Story

- Tests before implementation, failing first
- Model → migration → service module → route
- One exception: T013's index precedes T014, since T014's query is only safe with it

### Parallel Opportunities

- T002, T003, T004 in parallel after T001
- Every test task within a story is [P] — different assertions, one file per story, so write the file once then fan out
- T027 and T041 are model additions to the same file, so they are [P] only across stories, never with each other
- All four stories in parallel with four developers, after T005

---

## Parallel Example: User Story 1

```bash
# After T005, write the story's test file, then fan out its cases:
Task: "T006 working state while a child is live"
Task: "T007 lease renewed while parked"
Task: "T008 child output in artifacts"
Task: "T009 cancel cascades to children"

# Implementation is mostly sequential inside tasks.py:
# T013 (index) → T014 (query) → T015 (state) → T016..T020
```

---

## Implementation Strategy

### MVP (User Story 1 only)

Phase 1 → Phase 2 → Phase 3, then stop and validate against quickstart §1.
That alone removes the surface's one untruth: a task reporting completion while
its work runs. Shippable without any protocol change.

### Incremental delivery

1. Setup + Foundational
2. US1 → validate → ship (MVP)
3. US2 → validate → ship (transfer becomes real)
4. US3 → validate → ship (partners stop polling)

US2 is independent of US1 and can go first if continuation is the more urgent
partner request; US3 should not precede US1, since it exists to serve
long-running tasks.

---

## Notes

- 55 tasks: 4 setup, 1 foundational, 15 US1, 13 US2, 15 US3, 7 polish
- Commit per task or logical group; the migrations are four separate revisions on one head
- The two running dev instances do not reload — restart before validating, and never migrate under them
