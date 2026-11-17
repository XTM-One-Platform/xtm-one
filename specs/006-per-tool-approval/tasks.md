---

description: "Task list for per-tool human-in-the-loop approval"
---

# Tasks: Per-Tool Human-in-the-Loop Approval

**Input**: Design documents from `/specs/006-per-tool-approval/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

> **Regenerated 2026-08-12** for the HITL whitelist model. The previous list built a per-tool `is_state_changing` classification on `ToolDefinition`; that design is gone. `ToolDefinition` is now unchanged, and exemption comes from a computed whitelist.

**Tests**: MANDATORY. This feature is backend-heavy and touches security-relevant gating, so Constitution Principle III (CI-Enforced Quality Gates) applies in full — unit tests by default, `integration`-marked tests for behaviour touching real Postgres/Redis, and the migrations CI job for the schema change. Test tasks are not optional here.

**Organization**: Tasks are grouped by user story. Stories map to spec.md US1–US4.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1, US2, US3, US4 — user story phases only
- Exact file paths included in every task

## Path Conventions

Web application: `backend/app/` (FastAPI), `backend/tests/`, `backend/alembic/versions/`, `frontend/src/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create module skeletons so later parallel tasks don't collide on file creation.

- [X] T001 [P] Create empty module `backend/app/tools/approval.py` with docstring describing it as the pure gate-decision layer (no I/O)
- [X] T002 [P] Create empty module `backend/app/services/hitl_whitelist.py` for effective-membership resolution
- [X] T003 [P] Create empty module `backend/app/services/hitl_whitelist_default.py` for the shipped default, with a docstring stating it is reviewed as **data** and that no code may branch on its version
- [X] T004 [P] Create empty module `backend/app/services/tool_approval_service.py` for standing-exception persistence
- [X] T005 [P] Create empty module `backend/app/models/tool_approval.py` for `ToolApprovalPreference` and `HitlWhitelistOverride`
- [X] T006 [P] Create empty routers `backend/app/api/v1/hitl_whitelist.py` and `backend/app/api/v1/tool_approvals.py`, and register both in the v1 API router

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Whitelist resolution, gate logic, and schema. Every user story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Whitelist

- [X] T007 Define `SHIPPED_DEFAULT_WHITELIST: frozenset[str]` and `SHIPPED_WHITELIST_VERSION: int` in `backend/app/services/hitl_whitelist_default.py`, seeded with the ~54 read-only built-in tools plus `code_interpreter` — the latter carrying a prominent comment that it executes arbitrary code, that its exemption is a deliberate usability decision leaning on sandbox isolation, and that it is the first entry to remove for a stricter posture (Principle IV)
- [X] T008 Deliberately exclude the four remaining ambiguous built-ins from the shipped default in `backend/app/services/hitl_whitelist_default.py` — `browser`, `web_fetch`, `check_task_status`, `stix_bundle_validator` — with a comment per entry explaining why each gates
- [X] T009 [P] Implement `HitlWhitelistOverride` model in `backend/app/models/tool_approval.py` per data-model.md §2b, with `UNIQUE (tool_key)` and `action` constrained to `add`/`remove`
- [X] T010 Implement `resolve_whitelist(db) -> frozenset[str]` in `backend/app/services/hitl_whitelist.py` computing `(shipped_default − removals) ∪ additions`, with a comment on why membership is computed rather than materialised (FR-001d/e/f)
- [X] T011 Make `resolve_whitelist` raise rather than return an empty set on read failure in `backend/app/services/hitl_whitelist.py`, so callers can fail closed instead of mistaking a partial read for "nothing whitelisted"

### Data model

- [X] T012 [P] Implement `ToolApprovalPreference` model in `backend/app/models/tool_approval.py` per data-model.md §3, with the partial unique index `UNIQUE (user_id, tool_key) WHERE revoked_at IS NULL` and `ON DELETE CASCADE` on `user_id`
- [X] T013 [P] Add `requires_approval` column (default `True`) to `Integration` in `backend/app/models/integration.py` — one toggle per integration (FR-021)
- [X] T014 [P] Add `pending_approvals` and `approval_decisions` JSON columns to `AssignmentRun` in `backend/app/models/assignment.py`, with a comment explaining why `human_input_response` could not be reused
- [X] T015 Add the nullable `approve_tool_calls` column to `Agent` in `backend/app/models/agent.py` and `approve_tool_calls_default` (default `True`) to `PlatformSettings` in `backend/app/models/platform_settings.py` (FR-012, FR-014)
- [X] T016 Extend `HumanInputRequestedError` in `backend/app/tools/base.py:346-361` with an optional `proposals` field defaulting to `None`, preserving all existing fields (FR-011)

### Pure decision logic

- [X] T017 Implement `fingerprint(definition) -> str` in `backend/app/tools/approval.py` hashing only the canonically-serialised `input_schema`, with a comment on the cosmetic-vs-material distinction and why whitelist membership is excluded (FR-019a)
- [X] T018 Implement `evaluate_gate(...) -> GateDecision` in `backend/app/tools/approval.py` per the decision table in data-model.md §6, taking `whitelist` and `active_exceptions` but **no** interactivity input, and documenting why the function is total and must never raise
- [X] T019 [P] Implement `is_interactive_run(run) -> bool` in `backend/app/services/assignment_helpers.py` returning `run.triggered_by_user_id is not None`, used for notification routing only, with a comment stating it must not become a gate input

### Foundational tests

- [X] T020 [P] Unit tests for `evaluate_gate` covering all five decision-table rows plus the lookup-failure path, and asserting the decision is identical for interactive and autonomous runs, in `backend/tests/test_tool_approval_gate.py`
- [X] T021 [P] Unit tests for `resolve_whitelist` covering add-only, remove-only, both, and the layering rules FR-001e/FR-001f, in `backend/tests/test_hitl_whitelist.py`
- [X] T022 [P] Unit tests for `fingerprint` proving description/name changes do not alter it and schema changes do, in `backend/tests/test_tool_approval_fingerprint.py`
- [X] T023 [P] Unit test asserting every name in `SHIPPED_DEFAULT_WHITELIST` resolves to a real registered tool, so the shipped default cannot silently drift from the tool catalogue, in `backend/tests/test_hitl_whitelist_default.py`
- [X] T024 [P] Unit tests for `is_interactive_run` across manual, schedule, webhook and integration_event runs, in `backend/tests/test_run_interactivity.py`

### Migration

- [X] T025 Write the Alembic migration `backend/alembic/versions/e5rel3298_reland_per_tool_approval_schema.py` creating `hitl_whitelist_overrides` and `tool_approval_preferences` and adding the columns from T013–T014
- [X] T026 Write `backend/alembic/versions/j1atc0173_add_approve_tool_calls_switches.py`: add `agents.approve_tool_calls` (nullable) and `platform_settings.approve_tool_calls_default` `FALSE` on the existing row, then set its column default `TRUE`; write no agent row (FR-014, FR-014a)
- [X] T027 Implement the `downgrade()` function in the same T025 migration file in `backend/alembic/versions/`: drop both new tables and the new columns and restore the previous agent default
- [X] T028 [P] Integration test in `backend/tests/test_approval_upgrade_migration.py` (`integration` marker) — an upgrade over HITL-off and HITL-on agents and a HITL-on assignment rewrites no agent row and gates none; an agent created after the upgrade is ungated; turning one agent on gates only it; a re-run keeps the default off; a fresh install gates (FR-014, FR-014a, SC-007, SC-009)

**Checkpoint**: Whitelist resolution, gate logic and schema exist and are unit-tested. User story work can begin.

---

## Phase 3: User Story 1 - Gated tools always pause (Priority: P1) 🎯

**Goal**: A tool absent from the whitelist pauses the run before execution, deterministically, regardless of what the LLM chooses to do.

**Independent Test**: Ensure a tool is not on the whitelist, trigger a run that calls it, and confirm the run enters `awaiting_human` before the tool executes with no side effect — while a whitelisted tool in the same run completes unattended.

### Tests for User Story 1

> Write these first and confirm they fail before implementing.

- [X] T029 [P] [US1] Integration test: a non-whitelisted tool call pauses the run before execution and produces no side effect, in `backend/tests/test_tool_approval_gate_runtime.py`
- [X] T030 [P] [US1] Integration test: whitelisted tools in the same run execute with no prompt (SC-008), in `backend/tests/test_tool_approval_gate_runtime.py`
- [X] T031 [P] [US1] Regression test: a multi-call turn mixing gated and whitelisted tools executes **nothing** and surfaces all proposals together, then replays with no orphaned `tool_use` block, in `backend/tests/test_tool_approval_batch.py`
- [X] T032 [P] [US1] Integration test: an autonomous run honours a standing exception and does not pause for that tool, while still pausing and notifying for a gated tool with no exception (FR-015b, FR-015f), in `backend/tests/test_tool_approval_autonomous.py`
- [X] T033 [P] [US1] Test that an aliased or reordered tool name still hits the gate, in `backend/tests/test_tool_approval_gate_runtime.py`

### Implementation for User Story 1

- [X] T034 [US1] Add a canonical-name accessor to `CompositeToolExecutor` in `backend/app/tools/base.py:970` alongside the existing `get_tool_source`/`get_tool_entity_id`, so the gate keys on the resolved name
- [X] T035 [US1] Wire the gate into `CompositeToolExecutor.execute_tool` in `backend/app/tools/base.py`, after alias/normalisation resolution, raising the extended `HumanInputRequestedError` when `evaluate_gate` returns `pause`
- [X] T036 [US1] Implement batch pre-screening in `backend/app/tools/agent_loop.py` so all tool calls in a turn are evaluated before **any** executes, with a comment citing the orphaned-`tool_use` hazard from research.md D2
- [X] T037 [US1] Update the sequential dispatch path in `backend/app/tools/agent_loop.py:6089` to route through the pre-screen
- [X] T038 [US1] Update the parallel dispatch path in `backend/app/tools/agent_loop.py:6486` and the post-gather result loop at `:6643-6667` so a gated batch never leaves partially-appended tool results
- [X] T039 [US1] Resolve per-run gate inputs once in `backend/app/services/assignment_engine.py` — `hitl_enabled` via the unchanged `resolve_hitl`, the effective whitelist, and the user's active exceptions — caching them for the run's lifetime so membership is never queried per tool call
- [X] T040 [US1] Treat any failure resolving the whitelist or exceptions as "gate everything" in `backend/app/services/assignment_engine.py`, with a comment citing Principle I
- [X] T041 [US1] Extend the pause block in `backend/app/services/assignment_engine.py:3341-3390` to persist `pending_approvals` when the error carries proposals, leaving the free-text path untouched
- [X] T042 [US1] Implement `list_active_exceptions(user_id)` in `backend/app/services/tool_approval_service.py`, returning `{tool_key: fingerprint}`
- [X] T043 [US1] Make the durable notification unconditional for autonomous pauses in `backend/app/services/notify.py:120-125`, so a company-managed autonomous pause is not invisible (FR-015d, contracts/events.md §7)
- [X] T044 [US1] Include the pending tool name and how to act on it in the pause notification title/body in `backend/app/services/notify.py`
- [X] T045 [P] [US1] Integration test: an autonomous company-managed pause produces a durable notification record, in `backend/tests/test_tool_approval_autonomous.py`

**Checkpoint**: Gating is deterministic and safe. Runs pause correctly but can only be resolved via the existing free-text path.

---

## Phase 4: User Story 2 - Structured proposal, approve/edit/reject (Priority: P1)

**Goal**: A reviewer sees the full proposed action and can approve it, edit its arguments, reject it, or accept it permanently.

**Independent Test**: From a paused run, fetch the proposals, then exercise approve / approve-with-modifications / reject / approve-always and confirm the tool executes with the right arguments (or not at all) and the agent's next step reflects the decision.

### Tests for User Story 2

- [X] T046 [P] [US2] Contract test for `GET /assignments/runs/{run_id}/pending-approvals` covering both `kind` values, in `backend/tests/test_pending_approvals_api.py`
- [X] T047 [P] [US2] Contract test for the extended `POST /respond` covering every decision, in `backend/tests/test_approval_respond_api.py`
- [X] T048 [P] [US2] Test that a partial decision set (one pending id omitted) returns 422 and leaves the run paused, in `backend/tests/test_approval_respond_api.py`
- [X] T050 [P] [US2] Integration test: rejection yields a structured result the agent can act on, and the run neither crashes nor stalls (SC-004), in `backend/tests/test_approval_rejection.py`
- [X] T051 [P] [US2] Backward-compatibility test: a run paused by `request_human_input`, including one paused before the upgrade with no `pending_approvals`, still resumes from free text (FR-011), in `backend/tests/test_hitl_response_persistence.py`

### Implementation for User Story 2

- [X] T052 [P] [US2] Add Pydantic schemas for Action Proposal and Approval Decision in `backend/app/api/v1/schemas/assignment.py` per data-model.md §7–§8
- [X] T053 [US2] Implement `GET /assignments/runs/{run_id}/pending-approvals` in `backend/app/api/v1/assignments.py`, reusing `require_run_access` with the same rule as `/respond`
- [X] T054 [US2] Extend `RespondToHumanInputRequest` and the `POST /respond` handler in `backend/app/api/v1/assignments.py:1367-1416` to accept `decisions`, keeping the existing `response` body valid
- [X] T055 [US2] Validate the decision set in `backend/app/api/v1/assignments.py` — every pending `tool_call_id` present exactly once — returning 422 otherwise, with a comment on why partial sets are unsafe
- [X] T057 [US2] Rework the resume path in `backend/app/services/assignment_engine.py:3118-3181` to match pending calls by `tool_call_id` instead of the literal name `"request_human_input"`, branching on `approval_decisions` and falling through to the existing free-text behaviour when absent
- [X] T058 [US2] Implement approved execution on resume in `backend/app/services/assignment_engine.py` — run the tool with the proposed arguments and inject its real `ToolResult` under the original `tool_call_id`
- [X] T059 [US2] Implement rejection on resume in `backend/app/services/assignment_engine.py` — inject a structured denial as the tool result under the original `tool_call_id`, carrying the reviewer's reason (FR-010)
- [X] T060 [US2] Ensure every decided `tool_call_id` in the batch receives a tool result before the loop continues in `backend/app/services/assignment_engine.py`, so no `tool_use` block is left orphaned
- [X] T061 [US2] Implement `grant_exception(user_id, definition, admin_id)` upsert in `backend/app/services/tool_approval_service.py`, keyed on `(user_id, tool_key)` so concurrent grants converge (Principle II)
- [X] T062 [US2] Call `grant_exception` when a decision is `approve_always` in `backend/app/api/v1/assignments.py` (FR-015)
- [X] T063 [P] [US2] Add `ActionProposal` and `ApprovalDecision` types to `frontend/src/types/index.ts`
- [X] T064 [P] [US2] Build the structured proposal renderer in `frontend/src/components/ui/action-proposal.tsx` — tool name, description, and every argument driven by `input_schema`, showing each argument's declared description alongside its value (FR-006)
- [X] T065 [US2] Wire the approval UI into `frontend/src/components/ui/run-card.tsx:393-470`, branching on `kind` so free-text pauses keep their current textarea
- [X] T066 [US2] Add No / Yes / Yes-always actions to `frontend/src/components/ui/run-card.tsx`, with Yes-always disclosing before confirmation that the preference also covers scheduled runs (FR-015c, SC-010)
- [X] T067 [P] [US2] Add `usePendingApprovals` and `useSubmitApprovalDecisions` hooks in `frontend/src/hooks/api/useAssignmentApi.ts`

**Checkpoint**: The full approve / edit / reject / always loop works end to end. This plus US1 is a shippable increment.

---

## Phase 5: User Story 3 - Real-time proposal rendering (Priority: P2)

**Goal**: A reviewer watching a run sees the approval request appear live, without polling.

**Independent Test**: Subscribe to the activity stream, trigger a gated tool call, and confirm an `action_proposal` event carrying the same structured data arrives at the moment the run pauses.

### Tests for User Story 3

- [X] T068 [P] [US3] Test that pausing publishes an `action_proposal` payload matching the contract, on the same channel routing as `_publish_run_status`, in `backend/tests/test_action_proposal_event.py`
- [X] T069 [P] [US3] Test channel selection for company-managed vs personal runs and the `is_autonomous` flag, in `backend/tests/test_action_proposal_event.py`

### Implementation for User Story 3

- [X] T070 [US3] Implement `publish_action_proposal(...)` in `backend/app/services/assignment_helpers.py` mirroring `_publish_run_status:189-232`, emitting `type: "action_proposal"` per contracts/events.md §1
- [X] T071 [US3] Emit the event from the pause block in `backend/app/services/assignment_engine.py:3341-3390`, alongside the existing status publish
- [X] T072 [US3] Handle the `action_proposal` case in the `data.type` switch in `frontend/src/hooks/useActivityStream.ts:53-56`, invalidating the run query so the proposal renders immediately

**Checkpoint**: Proposals appear live in the UI.

---

## Phase 6: User Story 4 - Whitelist administration, self-service exceptions, auditability (Priority: P2)

**Goal**: Admins manage the whitelist with a warning before weakening a gate, users manage their own standing exceptions, administrators reach others' only via impersonation, and every tool call is logged with its gate outcome.

**Independent Test**: Add and remove whitelist entries and see the warning immediately; simulate an upgraded shipped default and confirm admin edits survive; grant and revoke a standing exception from your own preferences; confirm no cross-user console exists; confirm all tool calls appear in the audit log.

### Tests for User Story 4

- [X] T073 [P] [US4] Contract tests for `GET`/`POST`/`DELETE /hitl-whitelist` including provenance labelling and `removed_from_default`, in `backend/tests/test_hitl_whitelist_api.py`
- [X] T074 [P] [US4] Test that a destructive tool can be whitelisted without the API refusing (FR-003), and that an audit record is written, in `backend/tests/test_hitl_whitelist_api.py`
- [X] T075 [P] [US4] Layering test: with a simulated new shipped default, an admin removal still holds, an admin addition still holds, and a brand-new default entry is active with no admin action (FR-001e, FR-001f, SC-012), in `backend/tests/test_hitl_whitelist.py`
- [X] T076 [P] [US4] Test that `GET /tool-approvals` returns only the caller's rows and that another user's preference id returns 404 rather than 403, in `backend/tests/test_tool_approvals_api.py`
- [X] T077 [P] [US4] Test that revocation while impersonating records both identities (FR-017b), in `backend/tests/test_tool_approvals_api.py`
- [X] T078 [P] [US4] Test that a material tool change invalidates an exception and a cosmetic one does not (FR-019a), in `backend/tests/test_exception_invalidation.py`
- [X] T079 [P] [US4] Test that every tool call is logged with its gate outcome, including whitelisted and exception-run calls as distinct outcomes (FR-020, SC-005), in `backend/tests/test_tool_call_audit.py`

### Implementation for User Story 4

- [X] T080 [US4] Implement whitelist add/remove with upsert-on-`tool_key` semantics in `backend/app/services/hitl_whitelist.py`, flipping an existing override rather than inserting a second row
- [X] T081 [US4] Implement `GET`, `POST` and `DELETE /hitl-whitelist` in `backend/app/api/v1/hitl_whitelist.py` per contracts/rest-api.md §5–§7, admin-only, including provenance and `removed_from_default`
- [X] T082 [P] [US4] Emit `whitelist_tool_added` and `whitelist_tool_removed` audit records via `AuditLog` in `backend/app/services/hitl_whitelist.py`
- [X] T083 [US4] Implement `revoke_exception` and `invalidate_stale_exceptions` in `backend/app/services/tool_approval_service.py`, setting `revoked_reason` to `user_revoked` or `tool_changed`
- [X] T084 [US4] Implement `GET /tool-approvals` and `DELETE /tool-approvals/{id}` in `backend/app/api/v1/tool_approvals.py` per contracts/rest-api.md §3–§4, including the `stale` flag
- [X] T085 [US4] Trigger exception invalidation from the gate path when a fingerprint mismatch is detected, in `backend/app/tools/approval.py` and `backend/app/services/tool_approval_service.py` (FR-019a)
- [X] T086 [P] [US4] Emit `approval_granted`, `approval_revoked` and `approval_invalidated` audit records in `backend/app/services/tool_approval_service.py`, including `impersonated_by` from `deps.py:244-245`
- [X] T087 [US4] Emit a `tool_call_gated` audit record for every tool call and its gate outcome in `backend/app/tools/base.py` or the assignment engine, covering whitelisted and exception-run calls too (FR-020)
- [X] T088 [US4] Add `requires_approval` to the integration update schema and handler in `backend/app/api/v1/integrations.py`, and apply it to that provider's definitions in `backend/app/services/integration_tools.py:3661` (FR-021)
- [X] T089 [P] [US4] Build the whitelist admin screen in `frontend/src/pages/settings/HitlWhitelist.tsx` — effective entries with provenance, add/remove, and removed-from-default visibility
- [X] T090 [US4] Add the pre-save warning to `frontend/src/pages/settings/HitlWhitelist.tsx`, rendered the moment a tool is added and more prominent for one that writes, deletes, or acts outside the platform (FR-005, SC-003)
- [X] T091 [P] [US4] Add `useHitlWhitelist` hooks (list/add/remove) in `frontend/src/hooks/api/useHitlWhitelist.ts`
- [X] T092 [P] [US4] Build the self-service exceptions list with revoke actions in `frontend/src/pages/profile/ToolApprovalsRow.tsx`, surfacing the `stale` state
- [X] T093 [P] [US4] Add `useToolApprovals` and `useRevokeToolApproval` hooks in `frontend/src/hooks/api/useToolApprovals.ts`

**Checkpoint**: All four user stories are independently functional.

---

## Phase 6b: User Story 5 - Chat is gated too (Priority: P1)

**Goal**: A tool that pauses on a schedule also pauses in chat. Closes the gap where the same agent calling the same tool behaved differently depending on how it was reached.

**Independent Test**: Call a non-whitelisted tool from an agent chat and confirm the conversation stops and shows the approval prompt rather than executing.

**Why this arrived late**: the spec described the feature entirely in terms of runs and assignments, and HITL had always been assignment-only in this product, so the implementation inherited that boundary. Confirmed with the product owner as a spec miss.

### Tests for User Story 5

- [X] T101 [P] [US5] Test that a chat calling a non-whitelisted tool pauses instead of executing, in `backend/tests/test_chat_approval_gate.py`
- [X] T102 [P] [US5] Test that a whitelisted tool still runs in chat with no prompt, in `backend/tests/test_chat_approval_gate.py`
- [X] T103 [P] [US5] Test that a standing approval granted in a run also suppresses the prompt in chat, and vice versa — one preference, both surfaces, in `backend/tests/test_chat_approval_gate.py`
- [X] T104 [P] [US5] Test that chat resolves gating from the agent's `approve_tool_calls` via `resolve_approve_tool_calls`, never `human_in_the_loop`, in `backend/tests/test_chat_approval_gate.py`

### Implementation for User Story 5

- [X] T105 [US5] Resolve gate inputs once per chat turn and install the gate on both composite executors in `backend/app/services/agent_chat.py`
- [X] T106 [US5] Install the gate on the general assistant's executor in `backend/app/services/general_chat.py`
- [X] T107 [US5] Add an optional `request_approval` awaitable to `run_agent_loop` in `backend/app/tools/agent_loop.py`: when the batch pre-screen finds proposals and a channel is supplied, await the decision and continue the turn instead of unwinding. Absent a channel it raises as today, which is what assignment runs and REST rely on
- [X] T107a [US5] Apply decisions inline in the loop by reusing the existing decision applier, so approved calls execute with their proposed arguments and rejections become structured results
- [X] T108 [US5] Wire `request_approval` into the WebSocket chat handler in `backend/app/api/v1/chat.py`: emit the proposals as an event, await the client's decision, return it to the loop
- [X] T108a [US5] **Reopened 2026-08-26** (see T154, FR-002c3 — only the first half shipped: proposals are attached, no request can submit decisions). Keep the REST path on end-the-turn: attach pending proposals to the assistant message and accept decisions on a follow-up request. Carry them out of `run_agent_chat` via an output parameter, matching the existing `messages_out`/`error_out` convention — the current return value is a plain string, so an attribute cannot ride on it **Closed 2026-08-26** — the follow-up request half now exists (T154/T155): `decisions` on the send endpoint, refused with 409 when the set is settled and 422 when it is partial.
- [X] T109 [US5] Render the approval prompt inside the conversation in `frontend/src/pages/Chat.tsx` (or the shared message renderer), reusing `ActionProposalList`
- [X] T110 [US5] Add a `useSubmitChatApprovals` hook in `frontend/src/hooks/api/useChatApi.ts`

- [X] T111 [US5] Settle and publish the approval contract for external consumers. The `filigran-ui` chatbot is being updated in parallel in its own repository, so this task is about pinning the shape it codes against — the pending-approvals payload and the decisions endpoint — so both sides code against the same shape. No other external consumer is believed to exist.
- [X] T112 [US5] State the API contract change in `UPGRADING.md`. Reframed once mid-turn approval became opt-in: the honest headline is that existing clients keep working, so the section leads with that and explains why the opt-in exists (a client that ignores a prompt never answers it, so pausing anyway would wait forever).

**Checkpoint**: the same tool behaves identically whether reached from a chat or a schedule, and every known external consumer has a recorded position.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T094 Document the feature in `UPGRADING.md` as opt-in with nothing to do on upgrade: how to turn approval on per agent or install-wide, that `human_in_the_loop` keeps its meaning, and that a paused run waits with no timeout (FR-014c)
- [X] T095 [P] Document `TOOL_APPROVALS_ENABLED` and `TOOL_APPROVALS_PLATFORM_DEFAULT` in `docs/docs/deployment/configuration.md`
- [X] T096 [P] Update the agent-builder tool's HITL read/write handling in `backend/app/services/builtin_tools/agent_builder.py:1824` and `:3862-3867` for the new default
- [X] T097 Correct the hardcoded `"approvalMode": {"kind": "never"}` declaration in `backend/app/services/agentschema/mapping.py:482-484`, which becomes wrong once gating lands
- [X] T098 [P] Add operator documentation in `docs/` covering the whitelist, its shipped default, how admin edits survive upgrades, and that a user's "Yes, always" also applies to their scheduled runs (FR-015c)
- [X] T099 Run the full local gate: `./scripts/ci-local.sh`, backend unit and `integration` suites, and `tsc --noEmit` plus eslint in `frontend/`
- [X] T100 Walk every scenario in `specs/006-per-tool-approval/quickstart.md` against a running stack via `./dev.sh`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup — **blocks all user stories**
- **US1 (Phase 3)**: depends on Foundational
- **US2 (Phase 4)**: depends on Foundational; needs US1's pause path (T041) to have something to resolve
- **US3 (Phase 5)**: depends on Foundational and US1's pause block (T041); independent of US2
- **US4 (Phase 6)**: depends on Foundational; the whitelist-admin half needs only T010/T080, the exception half needs T061 from US2
- **Polish (Phase 7)**: depends on all desired stories

### Critical path

`T007 → T010 → T018 → T035 → T036 → T041 → T057 → T058`

The shipped default, whitelist resolution, the decision function, the executor hook, the batch pre-screen, pause persistence, and the resume rework. Everything else schedules around this spine.

### Within each user story

- Tests written and failing before implementation
- Models before services, services before endpoints
- Backend before the frontend that consumes it

### Parallel opportunities

- All of Phase 1 runs in parallel
- T009/T012/T013/T014 (separate model files) run in parallel; T015 and T016 touch shared files and should not
- T020–T024 (five separate test files) run in parallel
- Once Foundational completes, US1 and the whitelist-admin half of US4 (T080–T082, T089–T091) can proceed in parallel — the admin surface depends only on `resolve_whitelist`, not on the gate
- US3 is small and independent of US2 — a second developer can take it as soon as T041 lands
- All frontend tasks marked [P] are in distinct files

---

## Parallel Example: Foundational tests

```bash
Task: "Unit tests for evaluate_gate in backend/tests/test_tool_approval_gate.py"
Task: "Whitelist layering tests in backend/tests/test_hitl_whitelist.py"
Task: "Fingerprint tests in backend/tests/test_tool_approval_fingerprint.py"
Task: "Shipped-default drift test in backend/tests/test_hitl_whitelist_default.py"
Task: "Interactivity tests in backend/tests/test_run_interactivity.py"
```

## Parallel Example: User Story 1 tests

```bash
Task: "Gate pauses before execution in backend/tests/test_tool_approval_gate_runtime.py"
Task: "Batch pre-screen regression in backend/tests/test_tool_approval_batch.py"
Task: "Autonomous honours exceptions, pauses otherwise, in backend/tests/test_tool_approval_autonomous.py"
```

---

## Implementation Strategy

### MVP scope

US1 alone pauses runs but leaves reviewers with only the legacy free-text path, which cannot express approve-with-edits or reject. **The shippable MVP is US1 + US2** (Phases 1–4): deterministic gating plus a usable decision surface. US3 and US4 are genuine follow-on increments.

Note that without US4 the whitelist is not yet editable at runtime — the shipped default is the whole list. That is acceptable for an internal MVP but not for release, since FR-001b is the escape hatch that makes a wrong default correctable.

### Incremental delivery

1. Setup + Foundational → whitelist, gate logic and schema in place
2. **+ US1** → gating is deterministic; validate with quickstart Scenarios 1, 3, 6
3. **+ US2** → reviewers can approve, reject, always; validate Scenarios 2, 4, 8 → **internal MVP**
4. **+ US4 whitelist-admin half** → the default becomes correctable; validate Scenario 5b → **releasable**
5. **+ US3, + rest of US4** → live rendering, self-service exceptions, full audit; validate Scenarios 5, 7

### Highest-risk tasks

- **T036/T038** (batch pre-screen): the correctness centre of the feature. Gating mid-batch corrupts the conversation and lets un-approved side effects run. T031 is the regression guard and should be written first.
- **T057** (resume rework): touches a path every existing HITL run depends on. T051's backward-compatibility test, including a run paused before the upgrade, is what keeps this safe.
- **T039** (per-run resolution): whitelist membership is consulted on every tool call. Implemented as a per-call query this is a real performance regression, and the mistake is easy to make and invisible in tests.
- **T026** (install default): the upgrade must leave every agent ungated, including agents created after it, so the default is added `FALSE` on the existing row and only then made `TRUE` for new rows. It must write no agent row; T028 asserts that on a real database rather than by reading the migration.
- **T007/T008** (shipped default): a tool wrongly included is silently un-gated everywhere until an admin notices. Review by hand rather than by verb heuristic; T023 guards drift but not judgement. `code_interpreter` is the highest-risk entry — it ships exempt by product decision, so its comment must make that deliberate rather than looking like an oversight.

---

## Notes

- [P] = different files, no dependency on incomplete work
- Backend tests are mandatory here (Constitution Principle III), not optional
- T025–T027 must land as a single migration so the CI idempotent-re-run and downgrade round-trip cover it
- `ToolDefinition` gains no fields under this design — if a task seems to need one, the gate is being wired in the wrong place
- Commit after each task or logical group; stop at any checkpoint to validate a story independently

---

## Phase 8: Convergence

**Purpose**: Close the gaps between the code as it stands and what spec.md, plan.md and this list call for. Appended by `/speckit-converge`; nothing above this line was modified.

- [X] T113 CRITICAL — fix the `ruff check` failure at `backend/tests/test_chat_approval_gate.py:362` (SIM300 Yoda condition) so the blocking lint gate passes, per Constitution III (contradicts)
- [X] T114 Pass an `outcome_sink` to `build_approval_gate` on all three chat surfaces — `backend/app/services/agent_chat.py`, `backend/app/services/general_chat.py`, `backend/app/api/v1/public_chat.py` — and persist each call's gate outcome, so a chat tool call is as answerable as a run's per FR-020, SC-005 (scope extended to chat by FR-002a) (partial)
- [X] T115 Pass a `stale_sink` on the same three chat surfaces and drain it through `invalidate_stale_approvals`, as `backend/app/services/assignment_engine.py:3614` already does, so an exception invalidated during a chat is revoked and visible to its owner per FR-019a, FR-019b (partial)
- [X] T116 Record in `spec.md` which control satisfies US4/AC1 — the FR-005 whitelist warning, since FR-001 removed the per-tool `requires_approval` switch that criterion names — or add the missing control per US4/AC1 (partial)
- [X] T117 Reconcile the per-platform approval subsystem with the artifacts: `PlatformRegistration.supports_approval_prompts` / `approval_policy` / `approval_policy_set_by` / `approval_support_seen_at`, migrations `b2p1lat0173` and `c3q2obs0173`, `platform_gates_tools`, the Settings card and its capability nudge, plus the integration-detail approval control — none has an FR, plan decision or task, and it narrows FR-021 by refusing the per-integration toggle (409) for a registered platform. Add the requirements it implements or record why it stays undocumented (unrequested)

---

## Phase 9: Convergence

**Purpose**: Second convergence pass, after Phase 8. Appended by `/speckit-converge`; nothing above this line was modified. All three findings concern what a standing approval leaves behind — the preference itself, and the trail of it.

- [X] T118 Persist the standing approval when a chat decision is `approve_always`: `_apply_approval_decisions` in `backend/app/services/assignment_engine.py:1262` runs it as a plain approve, and neither `backend/app/api/v1/chat.py` (WebSocket) nor `POST /chat/messages/approve` in `backend/app/api/v1/platform.py` reaches `grant_exception` — only the run path does, at `backend/app/api/v1/assignments.py:1466`. The shared proposal UI meanwhile discloses that the preference also covers scheduled runs, so today the reviewer is told a preference was saved that was not, per FR-015, FR-002b, US2/AC4 (missing)
- [X] T119 Emit an audit record when a standing approval is granted, alongside the existing `grant_exception` call in `backend/app/api/v1/assignments.py:1466` (and wherever T118 adds one), capturing the tool, the owning user, the actor including `impersonated_by`, and the time — revocation is already audited in `backend/app/api/v1/tool_approvals.py:94`, granting is not, per FR-018 (missing)
- [X] T120 Record exception invalidation in the audit trail rather than only the application log: `invalidate_stale_approvals` in `backend/app/services/tool_approval_service.py:187` emits `logger.info("tool_approval_invalidated")` and no `AuditLog` row, per FR-019b (partial)

---

## Phase 10: Convergence

**Purpose**: Third convergence pass, after Phase 9, against the intent added in the 2026-08-20 clarification session. Appended by `/speckit-converge`; nothing above this line was modified. All five findings concern one capability: an administrator setting HITL on a built-in agent and its assignments. FR-012c needs no task — the seed-durability half already landed in `1fe56692`.

- [X] T121 Let an administrator set `approve_tool_calls` on a built-in agent: `BUILTIN_WRITABLE_AGENT_FIELDS = {"enabled", "approve_tool_calls"}` in `backend/app/api/v1/agents.py`, reusing the existing `can_manage` authorisation and refusing every other field — `human_in_the_loop` and its prompt included. Built-in agents' assignments stay read-only. Tests in `backend/tests/test_builtin_hitl_admin_writable.py`, per FR-012b
- [X] T123 Surface the control: `frontend/src/pages/agent-detail/config/ToolApprovalCard.tsx` (Follow the install default / On / Off, saved on change, sending only `approve_tool_calls`), rendered by `frontend/src/pages/agent-detail/ConfigTab.tsx` for anyone who can manage the agent and for an administrator on a built-in agent, per FR-012b, SC-014
- [X] T124 Confirm before any change that may stop prompts — including while the install default is still loading — stating that it reaches the agent's **chats** as well as its runs, and report a failed save, in `ToolApprovalCard.tsx`, per FR-012e, SC-015
- [X] T125 Add `approve_tool_calls` to the audit `details` of `update_agent` in `backend/app/api/v1/agents.py`, so the record identifies the value set and not only that the agent was updated; per-call gate outcomes (FR-020) record what a weakened gate allowed, never who weakened it, per FR-012e, SC-015

---

## Phase 11: Convergence

**Purpose**: Fourth convergence pass, after Phase 10 delivered the admin-settable HITL switch on built-in agents and their assignments. Appended by `/speckit-converge`; nothing above this line was modified. The capability itself is in place — these three close the coverage, discoverability and validation gaps around it. No finding for FR-012c or FR-012e, which are satisfied.

- [X] T126 Cover the endpoint wiring in `backend/tests/test_builtin_hitl_endpoint_wiring.py`: `PUT /agents/{id}` on a built-in agent accepts `approve_tool_calls` (on, off, null) and audits it, refuses `human_in_the_loop`, its prompt and other fields with 403, refuses a non-admin; a built-in agent's assignment stays read-only and no call site passes a field set to the assignment guard, per FR-012b, Constitution III
- [X] T127 Document the control in `docs/docs/administration/ai-governance.md`: the per-agent **Tool approvals** setting and the install-wide **Agents ask by default**, that an administrator may set it on a built-in agent, and that `human_in_the_loop` does not gate, per FR-014c, FR-012e
- [X] T128 Add Scenario 9 to `specs/006-per-tool-approval/quickstart.md` for setting tool approvals on a built-in agent — including that the value survives a restart that re-seeds built-in agents — per SC-014, SC-015

---

## Phase 12: Convergence

**Purpose**: Fifth convergence pass, after Phase 11. Appended by `/speckit-converge`; nothing above this line was modified. One finding remains, and it is documentation rather than code: FR-012b/c/d and SC-014/015 are implemented, tested and validated.

- [X] T129 List turning tool approvals off for the agents an assistant drives, built-in ones included, in the remedy list of `docs/docs/deployment/chat-approvals-in-clients.md` — the one remedy that covers XTM One's own gating tools, which `TOOL_APPROVALS_PLATFORM_DEFAULT=skip` does not, per FR-012e

---

## Phase 13: Read-classified platform tools

**Purpose**: Deliver FR-001g/h. Not a convergence pass — a scoped extension of the whitelist, added while this feature was still in review because it changes what the upgrade does on day one. Every platform tool was already classified `read`/`write` for the integration catalog's badge and the whitelist ignored it, so a platform on `require` paused before reading a report. Nothing above this line was modified except the artifacts named in T136.

**Independent Test**: With a platform registered and its policy set to `require`, an agent looks something up with no prompt and still pauses before changing anything — including a `manage_…` call whose arguments only list. Re-gating a platform's reads in one action survives the platform re-registering.

- [X] T130 Derive the exempt set instead of listing it: `backend/app/services/hitl_whitelist_platform.py` classifies each Suite platform's tools with `_infer_tool_type`, caches the result for the process, and holds five reads back by hand in `HAND_GATED` with their reasons. A builder that raises contributes nothing rather than a partial list, per FR-001g
- [X] T131 Layer it as the second half of the product default in `backend/app/services/hitl_whitelist.py` — `platform_read_default` (scoped to live registrations, failing closed) and `default_membership`, with the overlay's subtraction applied to **both** halves so a re-gated read survives an upgrade and a reconnection, per FR-001g, FR-001e
- [X] T132 Widen the membership test in `backend/app/api/v1/hitl_whitelist.py`: add/delete consulted the shipped constant only, so re-gating a derived read fell through to `already_gated`, wrote nothing, and left the screen claiming a tool was gated while it ran unattended. Same for `describe_membership`, which would have reported an exempt read as `not_whitelisted` — `platform_reads` is now a required keyword argument so it cannot be called wrongly, per FR-001g
- [X] T133 Add the bulk controls and provenance: `POST /hitl-whitelist/platform-reads/{provider}/require-approval` and `/restore-default`, a `platform_read` source with its `provider`, and a `platform_reads` per-platform summary that reports re-gated names too — the state in which an operator most needs the control that restores them. Restore **clears** the overrides rather than pinning additions, so a tool reclassified as a write later starts gating again, per FR-001h
- [X] T134 Buy back reviewability, which deriving gives up: `backend/tests/data/hitl_platform_read_whitelist.txt` holds the effective set, `backend/scripts/dump_platform_read_whitelist.py` regenerates it (`--check` for CI), and `backend/tests/test_hitl_whitelist_platform.py` fails on any widening and names the tools. Without this a new `list_opencti_*` in a provider PR would widen unattended execution with nothing in the diff to review, per FR-001g, Constitution III
- [X] T135 [P] Group the reads per platform on the whitelist screen with one reversible action to re-gate a whole platform's set, in `frontend/src/pages/settings/HitlWhitelistSection.tsx` and `frontend/src/hooks/api/useHitlWhitelist.ts`, plus the four locales. Rejecting 111 entries one chip at a time is not an escape hatch, per FR-001h
- [X] T136 Correct the artifacts that asserted the opposite. "A platform's tools are not on the shipped whitelist and never can be" was load-bearing in this feature's design and appeared verbatim in `spec.md` (FR-021a), `plan.md` and `contracts/chat-approval-api.md`; `data-model.md` §2a-bis and `contracts/rest-api.md` §5/§7a/§7b document the new layer; `quickstart.md` Scenario 5c validates it against a real platform; `docs/docs/administration/ai-governance.md` and `UPGRADING.md` carried the same claim to operators. `research.md` was deliberately left alone — the decision rests on a classifier that already existed, not on a new alternatives comparison, per FR-001g, FR-014c

**Checkpoint**: `require` now means *ask before anything that changes this platform*. 111 of 256 platform tools run unattended; the other 145 gate, including all 54 `manage_…` tools. (Corrected 2026-08-26: the earlier 107/261/149 came from a regex scrape that over-counted by five; the builder-derived figures in `research.md` and `data-model.md` are authoritative.)

---

## Phase 14: Convergence

**Purpose**: Sixth convergence pass, after Phase 13 delivered the read-classified platform tools. Appended by `/speckit-converge`; nothing above this line was modified. Every requirement predating Phase 13 assessed clean — all five findings are in the layer Phase 13 added, which had been self-assessed rather than independently reviewed.

- [X] T137 **CRITICAL** — Stop a transient registration-read failure from turning "require approval" into a silent no-op. `platform_read_default` (`backend/app/services/hitl_whitelist.py`) converts any failure into an empty set, which is right for the gate (those tools gate) and wrong for the admin write paths: `remove_from_whitelist` then finds the tool absent from `default_membership`, takes the `already_gated` branch, writes nothing and returns 204, so the administrator is told the tool now asks while it keeps running unattended once the database recovers — the same defect T132 fixed for the shipped constant, resurfacing under a different trigger. The bulk endpoints report `changed: 0` the same way. Distinguish "unreadable" from "empty" as `WhitelistUnavailableError` already does for the overlay, keep the empty-set behaviour for `resolve_whitelist`, and make `POST`/`DELETE /hitl-whitelist` and both bulk endpoints fail loudly instead of reporting success, per Constitution I, FR-001g (contradicts)
- [X] T138 Reword the platform approval card, which still tells operators the opposite of what the gate now does. `frontend/src/pages/settings/PlatformApprovalSection.tsx` states verbatim that "Tools contributed by a connected platform are not on the whitelist and cannot be, because XTM One does not know what they do" — the claim corrected in `spec.md`, `plan.md`, `data-model.md`, `contracts/chat-approval-api.md`, `ai-governance.md`, `UPGRADING.md` and `chat-approvals-in-clients.md` — and tells a platform set to *require* that "Its tools ask for approval before running" while 111 of its 256 tools do not. SC-013 measures exactly this: zero screens claiming approval is required while those tools run unattended. Cover the row text, the card description, the success toast and the switch's aria-label, in all four locales, per SC-013, FR-021a, FR-001g (contradicts)
- [X] T139 Warn before the un-gating direction of the bulk platform-read control, not the gating one. `frontend/src/pages/settings/HitlWhitelistSection.tsx` raises its confirm dialog only when re-gating, so *restoring* exempts up to 58 tools with no pre-save disclosure — inverted relative to FR-005, which requires the warning when a tool stops asking, and to SC-003, which admits zero such paths. State the count and that the exemption reaches scheduled runs, matching the wording already used for a single add, per FR-005, SC-003 (partial)
- [X] T140 Make `set_override` a real upsert in `backend/app/services/hitl_whitelist.py`. It selects then `db.add`s, so two concurrent writers both see no row and collide on `UNIQUE (tool_key)`; `data-model.md` §2b claims concurrent admin edits "converge via upsert, satisfying Principle II without a lock", which they do not — they raise. Pre-existing at one row per request, but the bulk endpoints of T133 write up to 58 rows per transaction and widen the window accordingly. Use `pg_insert(...).on_conflict_do_update` on `tool_key`, as `platform.py` already does for `PlatformIntentDeclaration`, per Constitution II, plan: two storage layers (partial)
- [X] T141 Record the derivation decision in `specs/006-per-tool-approval/research.md`, which has no entry for it: why membership is computed from the read/write classifier rather than enumerated by name, why a `side_effect_free` flag on `ToolDefinition` was rejected (261 decisions and still unable to describe a `manage_…` tool), and why the checked-in snapshot is the price of deriving. `plan.md` carries the decision and its alternatives, but a reader asking "why derive?" reaches `research.md` first, per plan: derivation decision (missing)

**Checkpoint**: T137 is the only one that changes what the product enforces; T138 is the only one an operator can currently be misled by.

---

## Phase 15: Convergence

**Purpose**: Seventh convergence pass, over the Phase 14 fixes. Appended by `/speckit-converge`; nothing above this line was modified. Both Phase 14 findings that mattered are closed — the Principle I fail-open in the administration paths, and the Principle II select-then-insert, the latter verified against real Postgres. What remains is one tail: the backend now distinguishes "no platform reads" from "we could not read them", and the screen still does not.

- [X] T142 Let the screen say the platform reads are *unknown*, rather than showing nothing. `list_whitelist` returns `platform_reads_available`, and `contracts/rest-api.md` §5 calls it mandatory for exactly one reason: an empty `platform_reads` otherwise reads as "this deployment has none", which an operator takes as "they were re-gated". Nothing consumes it — `frontend/src/hooks/api/useHitlWhitelist.ts` does not declare the field and `frontend/src/pages/settings/HitlWhitelistSection.tsx` does not read it — so when the registrations cannot be read the platform groups simply vanish. T137 fixed the half of this that writes; this is the half the operator looks at. Render an explicit unknown state and keep it distinct from "none", in all four locales, per FR-001g, FR-001h, contracts/rest-api.md §5 (partial)
- [X] T143 Drop the `await db.refresh(row)` at the end of `set_override` in `backend/app/services/hitl_whitelist.py`, along with the comment justifying it. It costs one extra SELECT per row — 58 per bulk platform-read action — to guard against a stale identity-map entry that cannot occur: every caller reads the overlay through `load_overrides`, which selects *columns* rather than entities and so populates no ORM instance, and the `RETURNING` clause already fills every column (probed: `created_at` is persisted and populated without it). Keep the upsert itself, which is what T140 was for, per plan: two storage layers (unrequested)
- [X] T144 Add a step to Scenario 5c in `specs/006-per-tool-approval/quickstart.md` for the state where the registered platforms cannot be read: the write endpoints refuse with 503 and change nothing, and the whitelist screen shows the curated half while saying the platform section is unknown. Simulate it by pointing the deployment at an unreachable database replica or by revoking `SELECT` on `platform_registrations` for the app role. Unit tests cover the refusal; nobody has looked at the screen in that state, which is how T142 went unnoticed, per FR-001g (missing)

**Checkpoint**: T142 is the only one an operator can be misled by. T143 is pure waste. T144 is the check that would have caught T142.

---

## Phase 16: Convergence

**Purpose**: Eighth convergence pass, over the Phase 15 fixes. Appended by `/speckit-converge`; nothing above this line was modified. Phase 15 delivered what it aimed at. Both findings here are small, and the first exists because T142 added an on-screen claim without auditing every control that claim covers — the fourth pass in a row whose finding is a surface saying something the code does not do, and the third caused by remediating the previous one. The gate itself has produced no finding in any pass.

- [X] T145 Make the "editing is unavailable" notice true for every control, not three of four. `frontend/src/pages/settings/HitlWhitelistSection.tsx` has four mutation call sites; the add button, `handleRemove` and the bulk buttons all check `platformReadsUnknown`, but the restore control in the "Shipped exempt, but re-gated here" section calls `addTool.mutate` directly rather than going through `handleAdd`, so it fires while the notice above it says editing is unavailable and answers with a raw 503 toast. Route it through the guarded path or disable it alongside the others, per FR-001b, T142 (partial)
- [X] T146 Resolve `describe_membership` in `backend/app/services/hitl_whitelist.py` one way or the other: nothing in application code calls it, and the provenance FR-001b requires reaches the screen through the API's `source` field instead. It is exported, documented and covered by a test, so it reads as load-bearing — and its signature was widened in Phase 13 for a consumer that does not exist. Either delete it with its test, or make the listing endpoint derive `source` through it so one function owns that answer, per FR-001b (unrequested)

**Checkpoint**: Neither changes what the gate enforces. T145 is the only one an operator can bump into.

---

## Phase 17: Every signed-in surface can ask

**Purpose**: Deliver the requirements added by the 2026-08-26 clarification — FR-002b1 (the obligation to collect an approval is per surface), FR-002c2/c3 (what "continue" means, and that delivering to a still-waiting turn does not satisfy it), FR-008a (only the paused user may answer), FR-012a1 (the general assistant is gated unconditionally, with no switch). Not a convergence pass: the gate was correct on every surface, and what was missing was the channel to answer it. Nothing above this line was modified except **T108a**, reopened because FR-002c3 defines its second half as unbuilt.

Split deliberately in two. The first half shipped first and covers the browser socket. The second half is the mechanism every other signed-in surface needs, and it is **not** general-assistant-specific — a chat channel cannot collect an approval for an *agent* either, so it is built once for both.

**Independent Test**: On the browser socket, the general assistant pauses on a write-classified builtin (`create_agent`, `create_template`) and the *same* turn continues with the tool result after approval. On a chat channel and on REST, the same request completes after a decision submitted on a **later** request — with the process that ran the paused turn restarted in between, per FR-002c3. A second user's decision on the same proposal is refused and recorded, per FR-008a.

### Phase 1 — the browser socket (shipped)

- [X] T147 Give `run_general_chat` an approval channel in `backend/app/services/general_chat.py`: `request_approval` and `impersonated_by` parameters, threaded to `run_agent_loop` as `request_approval` / `on_approve_always` / `on_approval_wait` / `approval_outcomes`, mirroring `run_agent_chat`. The gate was already installed unconditionally by T106; what was missing was any way to answer it, so every gated call unwound the turn, per FR-002b1, FR-002c
- [X] T148 Wire the channel at the WebSocket call site in `backend/app/api/v1/chat.py`, passing `_ws_request_approval` only when the client advertised `supports_tool_approval` and `None` otherwise. No frontend change: `buildSendPayload` already sends the flag on every send and both the `approval_required` case and the pending-approval replay are agent-agnostic, per FR-002c, FR-021b
- [X] T149 Move the turn budget from `asyncio.wait_for` to `asyncio.timeout` with the deadline rescheduled around the wait, in `backend/app/services/general_chat.py`. A human reviewer now sits inside that window, so a reviewer slower than `chat_loop_timeout` was answering a turn that had already been cancelled. `run_agent_loop` extends `wall_clock_deadline` itself; only the caller can move the outer one, per FR-015e
- [X] T150 Grant the standing exception on "always" from the general assistant, and discard the matching entry from its stale-approval sink, in `backend/app/services/general_chat.py`. Without the discard the post-loop drain revokes the row just granted, so the tool prompts again next turn and the user's own preference list explains it as a tool change, per FR-015, FR-019a
- [X] T151 Answer a pause instead of reporting a failure on the chat channels, in `backend/app/channels/router.py`. `run_agent_chat` catches its own pause; `run_general_chat` re-raises, and this caller's blanket `except Exception` told Slack, Teams and email users "I encountered an error while processing your message" for the gate working as designed. Interim only — FR-002b1 requires these surfaces to *ask*, which T156 delivers, per FR-002b1
- [X] T152 [P] Cover it in `backend/tests/test_chat_approval_gate.py` (four cases, with an `rpartition`-based helper because the REST call site comes first in the module and is meant to stay channel-less) and `backend/tests/test_gate_outcomes_unattended_surfaces.py`
- [X] T153 Correct `specs/006-per-tool-approval/contracts/chat-approval-api.md` open point 3, which asserted the general assistant has no approval channel at all

### Phase 2 — REST, and the shared resume path (open)

> **Re-scoped 2026-08-26.** The chat channels moved to [`tasks-3118.md`](./tasks-3118.md) and out of the browser-socket change: that change ships the browser socket and the honest explanation, and the channels need propose-and-resume plus a decision surface plus an authorisation rule, none of which the socket needed. T156-T158 are now Phase 18. What stays here is the **shared** resume path, which the channel tasks depend on and must not reimplement.

- [X] T154 Build the resume path FR-002c1 has always required and nothing has ever fed: `approved_call_ids` is declared on `run_agent_chat` (`backend/app/services/agent_chat.py:776`) and consumed internally, but **no caller supplies it**, so proposals are written and never answerable. Accept decisions for a turn that has already ended, execute each approved call with the arguments the reviewer was shown, append the results and let the loop carry on — reconstructed from the stored conversation plus the attached proposals, with no durable store of intermediate loop state, per FR-002c1, FR-002c2. Retiring a proposal is a state change, not a deletion: record it so the surface can render it as expired (FR-002c4) **Done 2026-08-26** — `backend/app/services/chat_approval_resume.py` holds the shared pieces (load, validate, rebuild the gated assistant message, retire); both chat services accept `resume_proposals`/`resume_decisions` and apply them through the run path's `_apply_approval_decisions`. Retirement records rather than deletes, so the expired state has something to render.
- [X] T155 Consume that path from the REST turn in `backend/app/api/v1/chat.py`, which today writes `pending_approvals` onto the assistant message (`:3687`) and offers nothing that reads a decision back. The test pinning REST as deliberately channel-less must go with it: it pins a decision FR-002b1 reverses, per FR-002b1, FR-002c1, T108a. FR-008a needs no separate work on this surface: the resume request is authenticated as the user whose call is paused, so a bystander cannot reach it — the case that needs enforcing is a proposal visible in a shared channel, which is the channel tasks' T157 **Done 2026-08-26** — `decisions` on `SendMessageRequest`; the endpoint loads the pending set, refuses a settled one with 409 and a partial one with 422, passes the approved ids so the resumed turn does not re-gate, marks the set answered afterwards, and retires a pending set when a new message arrives instead. No user message is stored for a resume, and neither service appends an empty one.
- [X] T164 [P] Render a retired proposal as **expired** rather than removing it, in `frontend/src/pages/chat/ChatMessages.tsx` and the shared `ActionProposalList`, plus the four locales. T109 shipped the prompt before an expired state existed, so today a superseded proposal would simply vanish on the user's next message — which reads as the product having dropped the request and leaves no trace of a gated call that never ran, per FR-002c4 **Done 2026-08-26** — `expired_approvals` on `MessageResponse`, populated only when the retirement marker is present; `ActionProposalList`/`ActionProposalCard` gained a read-only `expired` mode (no buttons, no footer, no scroll-into-view) rendered in the thread by `ChatMessages.tsx`, plus the four locales.
- [X] T159 [P] State in `docs/docs/administration/ai-governance.md` that the general assistant has no switch of its own and follows the install default; the platform switch, a user's allow-all and the whitelist reach it, per FR-012a1
- [X] T160 [P] Add a general assistant scenario **Done 2026-08-26** — Scenario 10, with the unlink warning as an admonition ahead of step 1 and a coverage-map row. to `specs/006-per-tool-approval/quickstart.md` with the reachability preamble it needs: in `xtm_one` mode, startup links the general assistant to the CTEM Assistant on every boot (`backend/app/main.py:1352-1392`) and the picker hides the standalone entry whenever a linked agent is set (`frontend/src/pages/chat/ConversationSidebar.tsx:141`), so a reviewer who skips the unlink exercises `run_agent_chat`, sees approvals work, and concludes the surface is fixed. Same shape as the refusal trap already annotated in Scenario 5c, per FR-002b1
- [X] T161 [P] Give the requirements added on 2026-08-26 the acceptance criteria they lack **Done 2026-08-26** — SC-016 (per-surface completion, measured per surface), SC-017 (second-reviewer refusal), and three acceptance scenarios on User Story 2 covering resume-after-restart, refusal, and supersession. — the two items this clarification turned red in `specs/006-per-tool-approval/checklists/requirements.md`. FR-002b1, FR-002c2/c3 and FR-008a have no acceptance scenario and no success criterion, and no user story covers a signed-in surface that must resume after its turn ended, per SC coverage, checklists/requirements.md
- [X] T162 [P] Adopt the canonical surface names across the artifacts — browser socket, REST, platform stream, chat channels, public embed — reserving "chatbot" for the external `packages/filigran-chatbot`, in `specs/006-per-tool-approval/plan.md`, `data-model.md`, `contracts/` and `quickstart.md`. The word named five different things until the 2026-08-26 clarification, which is how the scope of this phase was misread once already, per Approval Surface (Key Entities) **Done 2026-08-26** — narrower than written: every `chatbot` in `contracts/` already referred to the external `filigran-chatbot` package or its URL paths, which is the reserved use, so nothing needed renaming. What was missing was the pointer, now at the head of `contracts/rest-api.md` and `contracts/events.md` and in `data-model.md` §7.
- [X] T163 Correct the stale notes **Done 2026-08-26.** in `specs/006-per-tool-approval/checklists/requirements.md`: "All items pass" is no longer true, and two bullets still claim standing exceptions apply to "interactive runs only" and that "autonomous runs ignore exceptions", both contradicting FR-015b as it now stands. Predates this phase; carried here because nothing else will pick it up, per FR-015b (contradicts)

**Checkpoint**: After Phase 1, the general assistant is complete on the browser socket and honest everywhere else — which is the scope of the browser-socket change. After Phase 2, REST can carry a decision too, and the resume path the channel tasks need exists. T154 is the one task everything else depends on.

---

---

## Phase 18: The chat channels collect an approval — moved

Deliberately out of the browser-socket change. The tasks live in
[`tasks-3118.md`](./tasks-3118.md) — moved out of this file on 2026-08-26 so a
`/speckit-implement` run against this feature stops at the end of Phase 17.

They satisfy requirements of *this* spec — FR-002b1's channel half, FR-002c1/c2, FR-008a, SC-016
and SC-017 — so the coverage they provide still counts; it is recorded in the sibling file rather
than here. **T156-T158** are those tasks, and the IDs are not reused. They are blocked on **T154**
above: a channel turn is a webhook and cannot be held open for a person, so the channels consume
the shared resume path rather than reimplementing it.

---

---

## Phase 19: Convergence

**Purpose**: Ninth convergence pass, over the Phase 17 implementation. Appended by `/speckit-converge`; nothing above this line was modified. No constitution violation and nothing missing outright — the gate, the resume path and the decision applier all do what the spec says. Both findings are the shape every pass on this feature has produced: a surface saying something the code does not do. SC-016 is also unmet on the chat channels, deliberately and tracked in `tasks-3118.md`, so no task is appended for it.

- [X] T165 Retire a pending proposal on **every** send path, not only REST, and clear the Redis pending set with it. `retire_pending` is called once, from the REST handler (`backend/app/api/v1/chat.py`), and rewrites message metadata only — so the browser socket's send (`:5334`) leaves the proposal live, and `GET /conversations/{id}` keeps serving it from Redis (`get_pending_approvals`, `:1863`) after retirement. The user abandons a prompt, types something else, reloads, and is shown the same prompt as though it were still answerable; submitting it then answers with 409. The socket also never writes proposals onto the assistant message, so there is nothing for the expired state to render there at all. Cover it with a test that retires and then re-reads the conversation, per FR-002c4 (partial) **Done 2026-08-26** — `retire_pending` now reads and clears the Redis set as well as the message metadata, and carries the socket's proposals onto the message so the expired state has something to render there. Wired into the WebSocket send path as well as REST, and best-effort at both: housekeeping that fails must not cost the user the message they just sent.
- [X] T166 Record a refused decision instead of dropping it. Ownership is enforced on all three decision paths — the conversation SELECT is scoped to `Conversation.user_id`, so another user gets 404 — but nothing is written when that happens, and FR-008a requires the refusal be recorded "never silently ignored" while SC-017 requires every refusal to be attributable in the audit trail. Same for a decision arriving for a retired or already-answered set, which FR-002c4 also requires be recorded rather than merely refused. `backend/app/services/chat_turn_guard.py` and the decision endpoints in `chat.py` and `platform.py`, per FR-008a, SC-017, FR-020 (partial) **Done 2026-08-26** — `log_security_event` on all five refusal paths: another user's conversation, a settled set and a partial set on REST; ownership and no-turn-awaiting on the platform stream. Deliberately **not** on the malformed-id branch, where "belonging to another user" would be false — a wrong entry in a security trail is worse than a missing one.
- [X] T167 [P] Drop the unused `db` parameter from `mark_answered` in `backend/app/services/chat_approval_resume.py`, or use it. It mutates the ORM object and relies on the caller's commit, which is right — but a signature that takes a session and never touches it reads as though it persists on its own, which is exactly the misreading that leaves a mutation uncommitted somewhere else later, per plan: shared utilities (unrequested) **Done 2026-08-26** — `mark_answered(message)` is synchronous and takes no session.

**Checkpoint**: T165 is the only one a user can currently be misled by. T166 is a hole in the audit trail rather than in the gate — nothing runs that should not, but a rejected attempt leaves no trace.

---

---

## Phase 20: Convergence

**Purpose**: Tenth convergence pass, over the Phase 19 fixes. Appended by `/speckit-converge`; nothing above this line was modified. Both findings are in Phase 19's own remediation, and they are the same mistake twice: T165 and T166 each covered **two of the three** surfaces that hold a pending approval. REST and the browser socket retire; the platform stream does not. REST and the platform stream record a refusal; the browser socket does not. Neither gap is in the gate — the eleventh consecutive pass with no finding there.

- [X] T168 Record the refusal on the socket's decision frame in `backend/app/api/v1/chat.py`. When `push_approval_decision` returns false the handler tells the client "that approval could not be applied" and writes nothing, so the one surface most decisions arrive on is the one leaving no trace. T166 added `log_security_event` to REST's three refusal paths and the platform stream's two and stopped there. Same call, same `entity_type="chat_tool_approval"`, `reason="no_pending_proposal"`, per FR-008a, SC-017, FR-002c4 (partial) **Done 2026-08-26** — the socket's decision frame now records the refusal. It opens its own session: this endpoint holds no request-scoped one and the per-turn sessions are long closed by the time a decision frame arrives — the first attempt used `db` and `user`, neither of which exists in that scope.
- [X] T169 Retire a pending set on the platform stream's send path — `send_platform_chat_message` in `backend/app/api/v1/platform.py` — which never calls `retire_pending`. It holds its pending proposals in the same Redis key the browser socket uses, so it keeps precisely the behaviour T165 fixed elsewhere: abandon a prompt, send another message, and `GET pending-approvals` still serves the old proposals as answerable. Best-effort like the other two call sites. **Three call sites is the argument for one**: consider a single "a new turn starts here" helper the three sends share, because this finding exists only because the rule was applied per call site rather than in one place, per FR-002c4 (partial) **Done 2026-08-26** — retirement added to `send_platform_chat_message`, and **the shared helper was built rather than the third copy patched**: `retire_for_new_turn` and `record_refused_decision` in `chat_approval_resume.py` now carry the rules and their guards, with all three send paths and all six refusal paths converted. The best-effort guard and the never-500 guard live in the helpers, so a caller cannot omit them — which is how both Phase 19 gaps happened.

**Checkpoint**: T169 is the one a user can be misled by; T168 is a hole in the audit trail on the busiest surface. After both, retirement and refusal-recording hold on all three surfaces that can carry an approval — the chat channels stay out until their tasks land.

---

---

## Phase 21: Convergence

**Purpose**: Eleventh convergence pass, over the Phase 20 fixes. Appended by `/speckit-converge`; nothing above this line was modified. Phase 20's own work is clean — all three send paths retire through the shared helper and all three persist it, all six refusal paths record, and the socket writes its proposals onto the message so the expired state has something to render. One finding, and it is a requirement that only became reachable *because* Phase 17 gave REST a resume path. Twelfth pass with no finding in the gate itself.

- [X] T170 Forward `impersonated_by` from both REST call sites in `backend/app/api/v1/chat.py` — `run_agent_chat` (`:3679`) and `run_general_chat` (`:3719`). The socket passes `_ws_impersonated_by` and the platform stream passes `getattr(user, "impersonated_by", None)`; REST passes nothing, so `grant_exception` receives `granted_by_admin_id=None` and an impersonating administrator's "approve always" is attributed to the impersonated user alone. `deps.py:244` already puts the claim on the request's `user`, so nothing needs plumbing — it is simply not forwarded. Latent until T155: before REST could resume, `approve_always` was unreachable there, so this is a gap the resume path opened rather than one it left behind. Cover it with a test asserting all three surfaces forward it, per FR-017b (partial) **Done 2026-08-26** — both REST branches forward `getattr(user, "impersonated_by", None)`. Tested per surface rather than per module: a count over the whole file is exactly what would have hidden this.

**Checkpoint**: An audit-attribution gap, not a gating one — nothing runs that should not. It matters because FR-017a deliberately routes an administrator's action on someone else's preference through impersonation, and FR-017b is the record that makes that route accountable.

---

---

## Phase 22: Convergence

**Purpose**: Twelfth convergence pass, over the Phase 21 fix. Appended by `/speckit-converge`; nothing above this line was modified. FR-017b now forwards on all three surfaces and Phase 20's rules still hold. One finding, and it is in a sentence this feature's own remediation added: the Path B correction promised a field to a client that has no way to read it. Thirteenth pass with no finding in the gate.

- [X] T171 Correct the `expired_approvals` promise in `specs/006-per-tool-approval/contracts/chat-approval-api.md`. It states that a retired proposal set "comes back on its message as `expired_approvals`, so a client can render it as expired" — true for the surfaces that serve messages, and **false for the platform stream, which is the surface this contract is written for**. `filigran-chatbot` reads `/chat/sessions` (conversation metadata only — title, `updated_at`, `message_count`) and `/chat/conversations/{id}/pending-approvals` (live proposals); it never calls an endpoint that returns `MessageResponse`, where the field lives. Scope the claim to the message-serving surfaces, and say what the stream client can actually rely on: it already holds the proposals from the `approval_required` event, and `pending-approvals` returning empty is how it learns the set is gone. FR-002d says what this repository owes that consumer is a stable contract and enough notice, which a promise it cannot consume does not satisfy, per FR-002d, contracts/chat-approval-api.md (contradicts) **Done 2026-08-26** — the claim is now split by surface, naming what the platform stream can actually rely on (it holds the proposals from `approval_required`, and an empty `pending-approvals` is how it learns the set is gone) and stating plainly that keeping it visible is the client's job there. The 409 row in the errors table carried the same promise and was aligned with it.

**Checkpoint**: Documentation only — nothing a user reaches changes. It matters because a team in another repository is coding against this file right now.

---

---

## Phase 23: Convergence

**Purpose**: Thirteenth convergence pass, run after `/code-review` found ten defects in the Phase 17-22 work and they were fixed. Appended by `/speckit-converge`; nothing above this line was modified. The fixes themselves check out. The finding is about how the resume path is *verified*, and it is a constitution violation rather than a behavioural gap — the first CRITICAL any pass has produced, and the explanation for why twelve clean-looking passes preceded a review that found four HIGH defects.

- [X] T172 **CRITICAL** — Add `integration`-marked coverage for the chat resume round-trip. `backend/tests/test_chat_approval_resume.py` carries 38 tests and **52** `inspect`/`getsource` references: it asserts that the source contains certain strings, not that the path works. No `integration`-marked test touches the resume at all, while the path reads and writes message metadata in Postgres and clears a Redis key — squarely what Principle VI requires integration coverage for, and what `plan.md`'s own Constitution Check already claims exists ("`integration`-marked tests for the pause→respond→resume round-trip against real Postgres"; true of the run path, false of this one). Cover, against real Postgres and Redis: a turn pauses and attaches its proposals; a later request submits decisions and the approved call executes **with the arguments the reviewer was shown**; its result is appended and the conversation continues; the set is unanswerable afterwards (409) and a partial set is refused (422); retirement clears the Redis key and leaves the proposals renderable as expired. **Why this is CRITICAL rather than housekeeping**: two of the code review's HIGH findings — `content` required so the documented resume shape returned 422, and `message_content` dead while the services received `body.content` — were structurally present and behaviourally broken, so every source-inspection test passed while the feature did not work as documented. Source inspection has its place (it caught the surface Phase 20 missed) but it cannot catch a path that never runs, per Constitution VI, plan: CI-Enforced Quality Gates (missing) **Done 2026-08-26** — `backend/tests/test_chat_approval_resume_postgres.py`, 6 `integration`-marked tests against a temporary database and real Redis: the set is found from the database alone by a *different session*; the approved call executes once with the displayed arguments while the rejected one becomes a structured refusal; every proposal gets exactly one paired result; an answered set is unanswerable; retirement clears Redis and keeps both copies renderable; an orphaned Redis set does not relabel a settled message. It earned its place twice while being written — a skip reading "Cannot reach Redis" was really the module-level client bound to a dead event loop (fixed with a rebinding fixture, because a guard that swallows a fixture bug is a false pass), and my assertion about the stored state after `mark_answered` was simply wrong.

**Checkpoint**: Nothing a user reaches changes. What changes is whether the next defect of that shape is caught here or by a reviewer.

---

---

## Phase 24: Convergence

**Purpose**: Fourteenth convergence pass, over the T172 integration coverage. Appended by `/speckit-converge`; nothing above this line was modified. The coverage itself does what it was asked to — it caught two real problems while being written. The finding is in the fixture that made it possible: fixing a false pass in my own test at the cost of every Redis test scheduled after it.

- [X] T173 Restore the Redis singleton after `fresh_redis` in `backend/tests/test_chat_approval_resume_postgres.py`. The fixture calls `app.core.redis._build_client()` and yields with no teardown, and `_build_client` reassigns the module-level `_shared_client`/`_pool` without closing what it replaces — so after this test the shared client is bound to *this* test's event loop and the previous pool is leaked. Any later Redis-using test in the same session inherits a client on a dead loop and fails with "Event loop is closed", which is precisely the failure this fixture was added to fix, now exported to everything after it. Order-dependent, so it surfaces as an unrelated flake rather than as a failure here — and the repo already carries one order-dependent Redis flake, which makes a second one especially hard to attribute. Save `_shared_client`, `_pool` and `_sentinel`, rebuild for the test, and restore them on teardown so the module is left exactly as found, per Constitution VI (contradicts) **Done 2026-08-26** — the rebuild lives in a `_rebound_redis()` context manager that saves `_shared_client`/`_pool`/`_sentinel` and restores them in a `finally`; the fixture is a two-line wrapper around it. Made a context manager specifically so the invariant is asserted directly, by `test_the_redis_fixture_leaves_the_module_as_it_found_it`, which compares the module globals before and after and therefore holds in any suite order. The pool built inside is left to garbage collection rather than disconnected: closing it needs the loop that is about to disappear, and a teardown clever enough to work around that is a worse risk than two short-lived pools — my first attempt did exactly that and was removed. Verified by running the Redis-touching neighbourhood together in random order (`a2a`, `turn_guard`, the approval files): 1341 passed.

**Checkpoint**: No product behaviour is involved. What is involved is whether the suite that guards this feature can be trusted not to fail for reasons unrelated to the code under test.

---

---

## Phase — The gate's own switch

**Purpose**: FR-012, FR-014, FR-014a, FR-014b: gate on `approve_tool_calls`, never on `human_in_the_loop`, with an install default an upgrade leaves off.

- [X] T174 Add `resolve_approve_tool_calls(db, agent)` and `gate_off_outcome(agent)` to `backend/app/services/hitl_whitelist.py`: the agent's own value, else the install default read in a savepoint, `True` when unreadable or no row exists (FR-012, FR-014)
- [X] T175 Resolve the gate from it on every entry point — `execute_assignment_run` in `backend/app/services/assignment_engine.py` (every trigger), `run_agent_chat` in `backend/app/services/agent_chat.py` (chat and channel bots), `backend/app/services/general_chat.py` (install default) and `backend/app/api/v1/public_chat.py` — keeping `resolve_hitl` for the `request_human_input` tool only (FR-011, FR-012a, FR-012a1)
- [X] T176 Record `auto_run_install_default_off` (`backend/app/tools/approval.py`) when the install default let a call run, on the run path (`_actions_taken`) and every chat drain (FR-014b, FR-020)
- [X] T177 Expose the install default: `approve_tool_calls_default` on `GET`/`PUT /api/v1/settings/platform` in `backend/app/api/v1/settings.py` (admin, strict boolean, audited) and read-only on `GET /api/v1/tool-approvals` in `backend/app/api/v1/tool_approvals.py` (FR-014)
- [X] T178 Add `approve_tool_calls` to the agent schemas in `backend/app/api/v1/schemas/agent.py` and to create, update and response in `backend/app/api/v1/agents.py`; copy it on duplicate and catalog clone, export it only for the AgentSchema `approvalMode` in `backend/app/models/agent_copy.py` and `backend/app/services/agentschema/mapping.py` (FR-012)
- [X] T179 Add the **Agents ask by default** switch to the Tool Approvals card in `frontend/src/pages/settings/ParametersSection.tsx`, and use the effective value (`effectiveApproval` in `frontend/src/lib/hitl.ts`) for the gate icon and gated-agent count in `frontend/src/pages/Agents.tsx` (FR-014, FR-046)
- [X] T180 Put `ask_the_caller` on the shipped whitelist in `backend/app/services/hitl_whitelist_default.py`: it only asks the partner that tasked the agent, like `request_human_input`
- [X] T181 Unit tests in `backend/tests/test_approve_tool_calls_switch.py`: the resolver's precedence, fail-closed read with no rollback of the caller's session, the install-default outcome, and every entry point pinned to the resolver; `backend/tests/test_builtin_seed_preserves_hitl.py` pins that the seed never writes `approve_tool_calls` (FR-012c)
- [X] T182 E2E agents not about approvals opt out, since CI is a fresh install: `approve_tool_calls: false` in `frontend/tests_e2e/a2a/seed.ts` and the report agent of `frontend/tests_e2e/csp-violations.spec.ts`
