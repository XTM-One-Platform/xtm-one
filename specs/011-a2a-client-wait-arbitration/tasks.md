---
description: "Task list for feature implementation"
---

# Tasks: How a calling agent waits for a partner's answer

**Input**: Design documents from `/specs/011-a2a-client-wait-arbitration/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: Required. Every task below touches the backend, and this project's
CI-Enforced Quality Gates principle makes tests a blocking gate rather than an
option. `integration`-marked tests where behaviour needs real Postgres.

**Organization**: By user story, so each ships independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelisable — different files, no dependency on incomplete work
- Paths are repository-relative

---

## Phase 1: Setup (Shared Test Infrastructure)

**Purpose**: the partner behaviours every story below is tested against

- [X] T001 Extend `StubPartner` in `backend/tests/a2a_client_stubs.py` to declare `pushNotifications` and to answer a submission non-terminally before going terminal — the shape this platform's own inbound surface has, and the one every story here depends on

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: an outstanding delegation has to be a row before anything can collect, cancel or resume it (research R8)

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T002 Add `mechanism` and `callback_secret` columns and the outstanding-state semantics to `A2ADelegation` in `backend/app/models/a2a.py`, per [data-model.md](./data-model.md)
- [X] T003 Add the wait-mechanism vocabulary to `backend/app/models/a2a.py`, and record that an outstanding delegation is a row with no outcome and no `terminal_at` rather than a state of its own — `DELEGATION_OUTCOMES` stays the set of TERMINAL classifications
- [X] T004 Write a lock-safe ADD COLUMN revision for `a2a_delegations` in `backend/alembic/versions/`, on head `a2a9announce1`, following the probe → `terminate_lock_holders` → `set_lock_timeout` → guarded DDL recipe. No backfill: existing rows are all concluded
- [X] T005 Write the delegation row when the task is handed back rather than only at conclusion, and update it on conclusion, in `backend/app/a2a/client/delegation.py`
- [X] T006 [P] Assert the row's lifecycle in `backend/tests/test_a2a_client_models.py`: a row exists while outstanding with a state and no outcome, `terminal_at` unset, and the same row concludes rather than a second being written
- [X] T007 Verify the round-trip (upgrade, re-run, downgrade) for every revision in `backend/alembic/versions/` added by this feature, against a scratch database cloned from a real one, before opening the PR

**Checkpoint**: an in-flight delegation is discoverable — user stories can begin

---

## Phase 3: User Story 1 — A long partner task does not cost the calling agent its answer (Priority: P1) 🎯 MVP

**Goal**: an attempt that ends with the task live hands it back instead of failing, and the agent collects it later.

**Independent Test**: delegate work that outlasts the attempt; receive a still-running outcome naming the task; collect the answer on a later turn; confirm the partner was never asked to cancel.

### Tests for User Story 1

- [X] T008 [P] [US1] Assert a still-running result is not reachable through the failure branch, in `backend/tests/test_a2a_client_delegation.py` — a calling agent that cannot tell it from a timeout apologises for work that is still running
- [X] T009 [P] [US1] Assert the tool renders the fourth shape with the task's identity and no partner-authored text, in `backend/tests/test_a2a_client_delegation.py`, per [contracts/delegation-outcomes.md](./contracts/delegation-outcomes.md)
- [X] T010 [P] [US1] Assert a collect returns the result when finished and a further still-running outcome when not, in `backend/tests/test_a2a_client_continuation.py`
- [X] T011 [P] [US1] Assert a collect is refused for a task this caller did not create — including the chat case, where the user rather than the run is what scopes it — in `backend/tests/test_a2a_client_continuation.py`
- [X] T012 [P] [US1] Assert the ceiling no longer cancels a live task, in `backend/tests/test_a2a_client_delegation.py` (amends `009` FR-024)
- [X] T013 [P] [US1] Assert cancellation scope in `backend/tests/test_a2a_client_delegation.py`: an assignment run reaching its outcome cancels, a chat turn ending does not, and abandoning the conversation does

### Implementation for User Story 1

- [X] T014 [US1] End an attempt against a live task with a hand-back rather than a timeout, in `backend/app/a2a/client/delegation.py`, leaving the ceiling as what ends the attempt while nothing will report the outcome
- [X] T015 [US1] Accept `continue_task_id` with no `message` as a collect in `backend/app/a2a/client/delegation.py`, reusing `_may_continue` unchanged for ownership
- [X] T016 [US1] Make `message` optional when `continue_task_id` is present in the tool schema, and say what a collect is in the tool description, in `backend/app/a2a/client/tool.py`
- [X] T017 [US1] Render the still-running shape in `backend/app/a2a/client/tool.py`
- [X] T018 [US1] Track an outstanding partner task on the executor and cancel it when an assignment run reaches its outcome, in `backend/app/services/integration_tools.py` — extending the existing teardown, which today reaches only tasks a partner reported `input-required` for
- [X] T019 [US1] Cancel outstanding partner tasks from the conversation-level cancel cascade in `backend/app/services/sub_agent.py`, so a chat delegation is cancelled when the user abandons the conversation and not when the turn ends
- [X] T019a [US1] Add `conversation_ref` to `A2ADelegation` and `CallingRun`, populated from the tool context in `backend/app/services/integration_tools.py` — unplanned, and required by T019: a chat delegation had no link to its conversation, so nothing could scope the cancel to one

**Checkpoint**: US1 ships. A 631-second partner task now returns its answer.

---

## Phase 4: User Story 2 — The platform knows what each partner supports (Priority: P2)

**Goal**: what a partner declares becomes a recorded, operator-visible fact.

**Independent Test**: register two partners, one declaring callbacks and one declaring none; both are shown accurately with no delegation made.

### Tests for User Story 2

- [X] T020 [P] [US2] Assert declared capabilities are projected from the card and recorded on the target, in `backend/tests/test_a2a_client_card_interfaces.py`
- [X] T021 [P] [US2] Assert an absent `capabilities` object records "not declared" rather than failing, in `backend/tests/test_a2a_client_card_interfaces.py`
- [X] T022 [P] [US2] Assert a capability change does NOT change the card fingerprint and does NOT suspend the target, in `backend/tests/test_a2a_client_drift.py` — the regression that would otherwise suspend every existing registration on the next sweep

### Implementation for User Story 2

- [X] T023 [P] [US2] Add the two declared-capability fields to `PartnerCard` and read them in `project_card`, in `backend/app/a2a/client/sdk.py`
- [X] T024 [US2] Record them in `apply_verdict` in `backend/app/a2a/client/verification.py`, and leave `fingerprint_card` untouched (research R3)
- [X] T025 [P] [US2] Add the two nullable columns to `A2AClientTarget` in `backend/app/models/a2a.py`
- [X] T026 [US2] Write a lock-safe ADD COLUMN revision for `a2a_client_targets` in `backend/alembic/versions/` — the first migration to add a column to that table
- [X] T027 [P] [US2] Surface both in the operator read model in `backend/app/api/v1/a2a_client.py`
- [X] T028 [P] [US2] Show them alongside the other card facts in `frontend/src/pages/integration-detail/A2AVerificationCard.tsx` and its hook in `frontend/src/hooks/api/useA2AClientApi.ts`

**Checkpoint**: arbitration has something to arbitrate on.

---

## Phase 5: User Story 3 — A partner tells us when it is done (Priority: P3)

**Goal**: the platform holds nothing open while it waits, and the calling run resumes on the callback.

**Independent Test**: from a reachable deployment, delegate long work; the assignment run parks and resumes with the answer, and a chat conversation is woken with it — neither having asked.

**⚠️ This story adds an externally reachable route.** That is why it is last, and gated on an operator's decision rather than on a partner's card.

### Tests for User Story 3

- [X] T039 [P] [US3] Assert reachability resolves from the explicit setting first and from the configured public address otherwise, treating anything `009` FR-035 would refuse as unreachable, in `backend/tests/test_a2a_client_destinations.py`
- [X] T040 [P] [US3] Assert no callback is registered where the deployment is unreachable or the partner declares none, in `backend/tests/test_a2a_client_callbacks.py`
- [X] T041 [P] [US3] Assert the receiver refuses an unauthenticated, mis-tokened or foreign callback with a uniform not-found, and that a refusal resumes nothing, in `backend/tests/test_a2a_client_callbacks.py`
- [X] T042 [P] [US3] Assert a callback triggers a read of the partner's task rather than trusting the body, in `backend/tests/test_a2a_client_callbacks.py`, per [contracts/partner-callbacks.md](./contracts/partner-callbacks.md)
- [X] T043 [P] [US3] Assert a delegation concludes exactly once when a callback and the polling floor race, in `backend/tests/test_a2a_client_callbacks.py`
- [X] T044 [P] [US3] Assert the grace period ends the attempt only where a callback is registered, and that a task finishing inside it answers in the same turn, in `backend/tests/test_a2a_client_delegation.py` (SC-008)
- [X] T045 [P] [US3] Assert an assignment run parks rather than blocking, and resumes with the answer, in `backend/tests/test_a2a_client_callbacks.py`
- [X] T046 [P] [US3] Assert a chat conversation is woken with the answer, in `backend/tests/test_parent_wake.py`
- [X] T047 [P] [US3] Assert the polling floor still concludes a delegation whose callback never arrives, in `backend/tests/test_a2a_client_callbacks.py`

### Implementation for User Story 3

- [X] T048 [US3] Add the grace-period, reachability and floor-cadence settings to `backend/app/core/config.py`, adding each numeric bound to `_validate_a2a_bounds` by name — the validator has no wildcard — and cover them in `backend/tests/test_settings_a2a_bounds.py`
- [X] T049 [US3] End the attempt at the grace period where a callback is registered, in `backend/app/a2a/client/delegation.py`
- [X] T050 [US3] Add `backend/app/a2a/client/callbacks.py`: mint a per-delegation secret, register the callback through the protocol's push-config operation, and correlate an incoming notification back to its delegation
- [X] T051 [US3] Add the receiver route in `backend/app/api/v1/a2a_callbacks.py` — token in path, transport check, pre-auth rate limit, constant-time secret comparison before any state is read, uniform not-found
- [X] T052 [US3] Claim the conclusion on the delegation row so a callback and the polling floor cannot both conclude, in `backend/app/a2a/client/callbacks.py`
- [X] T053 [US3] Park an assignment run at the delegation and resume it on the callback, in `backend/app/a2a/client/delegation.py` and `backend/app/services/assignment_engine.py`, reusing the await/resume path human-input requests already use
- [X] T054 [US3] Generalise `backend/app/services/parent_wake.py` to wake a conversation from work that is not a `SubAgentTask` — the row lookup, the result/error attribute reads, and the tool name baked into the prompt's truncation branch are the only three couplings
- [X] T055 [US3] Keep a bounded direct-asking floor beneath a registered callback, in `backend/app/a2a/client/delegation.py`
- [X] T056 [P] [US3] Add `app/a2a/client/callbacks.py` and `app/api/v1/a2a_callbacks.py` to the swept module list in `backend/tests/test_a2a_client_no_dead_code.py`

**Checkpoint**: every mechanism available, each degrading to the one beneath it.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T057 [P] Record the mechanism each delegation used, and show it where a delegation's history is already shown, in `backend/app/a2a/client/delegation.py` and `backend/app/api/v1/a2a_client.py` (FR-020)
- [X] T058 [P] Assert a delegation's result, failure classes and reasons are identical whichever mechanism waited, in `backend/tests/test_a2a_client_failure_classification.py` (FR-021, SC-007)
- [X] T059 [P] Mark the two superseded Out-of-scope entries and FR-024 in `specs/009-a2a-client-outbound/spec.md`, and note FR-025's extension — a reader of `009` alone must not be left with a boundary this feature moved
- [X] T060 [P] Document the outbound mechanisms, the reachability setting and the wait ordering in `dev-docs/a2a-client-operations.md` — the task named the *server* doc; outbound behaviour belongs in the outbound one, which already existed
- [X] T061 [P] Add review scenarios for the hand-back, the collect, and a callback-resumed run to `dev-docs/a2a-review-scenarios.md`
- [ ] T062 Run [quickstart.md](./quickstart.md) end to end against two instances, restarting them first so they are not serving stale code

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup — BLOCKS every user story, because all three of collect, cancel and resume need the delegation row to exist while the task is outstanding
- **US1 (Phase 3)**: depends on Foundational only
- **US2 (Phase 4)**: depends on Foundational only — independent of US1
- **US3 (Phase 5)**: depends on US2 for what a partner declares, and on US1 for the hand-back the grace period shortens
- **Polish (Phase 6)**: depends on the stories it documents

### Why US3 comes last

- **After US2**: FR-008 forbids attempting a mechanism a partner does not declare, and nothing records what they declare until US2.
- **After US1**: the grace period changes *when* a hand-back happens; the hand-back itself is US1. Building US3 first would mean an attempt with nowhere to return.

US2 is genuinely independent of US1 and can go first or in parallel if the operator-visibility half is the more urgent request.

### Parallel Opportunities

- All tests within a story are `[P]` — different assertions, same or separate files, none depending on another's implementation
- T023/T025 (US2 projection and columns) are `[P]`; T024 depends on both
- T027/T028 (backend read model and frontend) are `[P]` with each other
- US1 and US2 can be built by different people once Phase 2 lands
- Phase 6's documentation tasks are all `[P]`

## Parallel Example: User Story 1

```bash
# Its six tests, together:
Task: "Assert a still-running result is not reachable through the failure branch"
Task: "Assert the tool renders the fourth shape with no partner-authored text"
Task: "Assert a collect returns the result when finished"
Task: "Assert a collect is refused for a task this caller did not create"
Task: "Assert the ceiling no longer cancels a live task"
Task: "Assert cancellation scope: assignment run yes, turn end no, conversation abort yes"
```

## Implementation Strategy

### MVP (User Story 1 only)

Phase 1 → Phase 2 → Phase 3, then stop and validate against quickstart §2 and
§3. That alone removes the failure this feature exists for: a partner task that
outlasts one attempt stops being a timeout, and stops being thrown away. It
needs no capability negotiation, no new transport, and no externally reachable
route.

### Incremental delivery

1. Setup + Foundational
2. US1 → validate → ship (MVP)
3. US2 → validate → ship (arbitration becomes possible; operators see more)
4. US3 → validate → ship (the wait stops costing anything)

Each of the last two is separately abandonable. If US3's externally reachable
route is judged not worth its guards, stopping after US2 leaves a coherent
feature: long work is handed back and collected, and an operator can see what
each partner supports.

## Notes

- 53 tasks: 1 setup, 6 foundational, 13 US1, 9 US2, 18 US3, 6 polish
- Two migrations, both ADD COLUMN, both on head `a2a9announce1` at time of writing — the second must be rebased onto the first
- The no-dead-code sweep over `app/a2a/client/` forbids landing a helper before its call site, so each new module lands with the code that calls it
- The two dev instances do not reload — restart before validating, and never migrate under them
