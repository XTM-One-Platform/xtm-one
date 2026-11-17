---
description: "Task list for Minimal A2A Server (Inbound)"
---

# Tasks: Minimal A2A Server (Inbound)

**Input**: Design documents from `/specs/006-a2a-server-inbound/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

**Tests**: REQUIRED. This is a backend change, so per the constitution (Principle III: CI-Enforced Quality Gates) test tasks are blocking, not optional — unit tests by default, plus `integration`-marked tests for behaviour touching real Postgres/Redis.

**Organization**: Grouped by user story so each can be implemented and tested independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1–US4)
- Task ids are stable and assigned in creation order. Ids added by a later analysis pass appear at the end of the block they belong to rather than in numeric sequence; they are never reused or renumbered.

## Path Conventions

Web app: `backend/` (FastAPI, Python 3.14) + `frontend/` (React). Tests run from `backend/` as `CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies, configuration and package scaffold

- [X] T001 Add `a2a-sdk==1.1.2` to `backend/requirements.txt` (new dependency — confirmed absent, research R1)
- [X] T002 [P] Add `A2A_SERVER` to both `FeatureFlag` Literal and `KNOWN_FEATURE_FLAGS` frozenset in `backend/app/core/feature_flags.py` (the two must stay in sync by hand)
- [X] T003 [P] Add A2A settings to `backend/app/core/config.py`: task-creation limit (10/min), read-cancel limit (120/min), dedup window (24 h), per research R8/R9
- [X] T004 Create `backend/app/a2a/__init__.py` with the package docstring stating the containment rule: `a2a_sdk` may be imported ONLY from `app/a2a/protocol.py`, and why (research R1)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, routing correction and protocol translation that every user story depends on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 Create `A2AExposure` and `A2ATask` models in `backend/app/models/a2a.py` per [data-model.md](./data-model.md), including the partial unique index on `agent_id WHERE revoked_at IS NULL` and the unique `(client_id, message_id)` constraint
- [X] T006 Register the new models in `backend/app/models/__init__.py`
- [X] T007 Add nullable `a2a_exposure_id` FK (`ondelete="CASCADE"`) to the OAuth client model in `backend/app/api/v1/mcp_oauth.py` — nullable is load-bearing so existing MCP clients keep working
- [X] T008 Create additive Alembic revision in `backend/alembic/versions/` for `a2a_exposures`, `a2a_tasks` and the client FK; verify idempotent re-run and downgrade round-trip locally before pushing; assert the migration creates zero exposure rows so an upgraded deployment has nothing externally reachable (SC-004)
- [X] T009 Add `"a2a"` to `_API_NAMESPACES` in `backend/app/main.py:3501` — without it an unknown card URL returns HTTP 200 + `index.html` instead of a JSON 404 (research R2)
- [X] T010 [P] Add `backend/tests/test_a2a_spa_catchall.py` installing the catch-all on a throwaway app via `_install_spa_catchall` and asserting `/a2a/<unknown>` returns a JSON 404, not HTML
- [X] T011 Create `backend/app/a2a/protocol.py` — the only module importing `a2a_sdk`: v1.0↔v0.3 shaping (`Part` union naming, `canceled`/`cancelled`), version negotiation with absent-header ⇒ v1.0, and the structured error envelope
- [X] T012 [P] Add `backend/tests/test_a2a_protocol_shaping.py` covering both version shapes, the absent-header default, and an unrecognised version producing a client error naming supported versions

**Checkpoint**: Schema, routing and protocol translation ready — user story work can begin

---

## Phase 3: User Story 1 - Expose an agent under existing ownership rules (Priority: P1) 🎯 MVP

**Goal**: An auditable, revocable per-agent exposure register whose authority comes entirely from the existing ownership model.

**Independent Test**: Verify exposure can be turned on and off exactly by those who can already manage the agent, that it is off for every agent initially, and that each change is audit-logged — all with no external protocol surface in existence.

### Tests for User Story 1

- [X] T013 [P] [US1] Add `backend/tests/test_a2a_exposure_authority.py`: owner and admin can expose; a `config`-tier group member can expose another user's agent; a `read_only` member cannot; a company-managed agent is admin-only; every denial is audit-logged
- [X] T014 [P] [US1] Add `backend/tests/test_a2a_exposure_revocation.py`: revocation is immediate and idempotent (retargeted onto the flush hook by T071, which removed the manual path); auto-revocation fires on `is_company_managed` flip **in both directions**, on `shared_group_ids`/`shared_group_permissions` change, on agent disable/delete, and on boundary-user deactivation
- [X] T054 [P] [US1] Add `backend/tests/test_a2a_owner_notification.py`: exposing another user's agent notifies the owner; automatic revocation on tier or sharing change notifies the owner; the owner's own exposure action does not self-notify

### Implementation for User Story 1

- [X] T015 [US1] Create `backend/app/a2a/exposure.py` with the authority check delegating to `can_manage()` from `backend/app/api/v1/helpers.py` — no new permission concept
- [X] T016 [US1] Implement exposure creation in `backend/app/a2a/exposure.py`: mint a fresh opaque `card_token`, freeze `boundary_kind`/`boundary_user_id`, record `exposed_by_user_id` for audit only
- [X] T017 [US1] Implement idempotent revocation — revoking an already-revoked exposure is a no-op, because two automatic revocations can race across replicas. Lives in `hooks._revoke_live_exposures` since T071 removed the async helpers in `exposure.py` that had only the manual endpoint as a caller
- [X] T018 [US1] Wire automatic revocation in `backend/app/a2a/exposure.py`, triggered from agent tier/sharing/enabled changes and user deactivation, recording `revoked_reason`
- [X] T019 [US1] Create `backend/app/api/v1/a2a_admin.py` implementing [contracts/internal-management.md](./contracts/internal-management.md); mount it in `backend/app/main.py`. Reduced to its `GET` by T071
- [X] T020 [US1] Add audit logging via `log_audit()` from `backend/app/services/audit.py` for grants, revocations and denials, using entity type `a2a_exposure`
- [X] T021 [US1] Add the owner-notification path for `FR-005`/`FR-041` — `backend/app/services/notify.py` is run-oriented, so this needs a small non-run notification rather than reuse (research R12, the one place a surface is added)

**Checkpoint**: Exposure register complete and independently demonstrable

---

## Phase 4: User Story 4 - Externally submitted work stays inside the agent's own boundary (Priority: P1)

**Goal**: Boundary resolution and tool bounding, as pure functions of the existing ownership model.

**Independent Test**: Unit-testable before any endpoint exists — resolve a boundary for each agent tier and assert the produced filter clause. Full acceptance (running work that tries to escape) requires US3; this phase lands early deliberately, because it is where the "reuse the existing bounding" premise either holds or fails, and it fails cheaply here.

### Tests for User Story 4

- [X] T022 [P] [US4] Add `backend/tests/test_a2a_boundary.py` covering all four configurations from [quickstart.md](./quickstart.md): personally-owned agent, company-managed agent, agent exposed by a non-owner, and an exposure whose boundary user is an administrator; and that usage allowance is consumed only from the agent's owner of record (SC-011)
- [X] T023 [P] [US4] Add `backend/tests/test_a2a_tool_allowlist.py`: unset allow-list yields the agent's configured tools; an explicit list narrows; an entry naming a tool the agent no longer has is dropped rather than re-admitted
- [X] T052 [P] [US4] Add `backend/tests/test_a2a_escalation_guard.py`: an external run cannot create, modify or revoke an exposure, mint or revoke credentials, or change permissions — asserted with the boundary user being an administrator, since that is the only case where the underlying identity could otherwise succeed

### Implementation for User Story 4

- [X] T024 [US4] Implement `resolve_boundary(agent)` in `backend/app/a2a/exposure.py` per [data-model.md](./data-model.md) — company-managed ⇒ no individual; otherwise the agent's owner, never `exposed_by_user_id`
- [X] T025 [US4] Implement the visibility filter in `backend/app/a2a/exposure.py`: the company-managed-only arm for `FR-031`, and `user_visible_filter(..., is_admin=False)` **pinned** for the owner case — with a "why" comment, since `user_visible_filter` returns `sa_true()` when `is_admin` is true and would defeat the whole boundary
- [X] T026 [US4] Implement allow-list resolution in `backend/app/a2a/exposure.py`, intersecting with the agent's current configuration and emitting `session_builtin_tool_ids` / `session_custom_tool_ids` / `session_integration_ids` / `session_mcp_server_ids` / `session_knowledge_base_ids` for `run_agent_chat`
- [X] T027 [US4] Add the fail-closed guard in `backend/app/a2a/exposure.py`: where a boundary or visibility decision cannot be evaluated, deny rather than grant (`FR-033`)
- [X] T053 [US4] Enforce `FR-035` at two points in `backend/app/a2a/exposure.py`: exclude management-capable tools (agent-builder CRUD, platform-admin surfaces) from allow-list resolution, and reject exposure/credential/permission mutation when the calling context is an external run — with a "why" comment naming the escalation path being closed

**Checkpoint**: Bounding logic proven in isolation before any external surface exists

---

## Phase 5: User Story 2 - A partner platform discovers the agent (Priority: P1)

**Goal**: An anonymously-fetchable, self-describing Agent Card per exposed agent.

**Independent Test**: Expose one agent, point a standard A2A client at the card URL, and verify the card is well-formed and complete enough to configure a client — with no task surface exercised.

### Tests for User Story 2

- [X] T028 [P] [US2] Add `backend/tests/test_a2a_card.py`: card renders v1.0 when no version header is sent and v0.3 when that version is asked for; skills reflect the exposure's tool narrowing; streaming and push notifications are declared unsupported; output content types are separate from input and structured output appears only for a configured agent
- [X] T029 [P] [US2] Add `backend/tests/test_a2a_card_negative.py`: unknown token, revoked exposure, disabled agent and flag-off all return the **same** JSON 404; a non-encrypted connection is refused rather than redirected; no listing exists at the deployment root; every URL issued before a revoke stays dead after the agent is exposed again (SC-013, FR-042)

### Implementation for User Story 2

- [X] T030 [US2] Create `backend/app/a2a/card.py` building the card by explicit field assignment per [contracts/agent-card.md](./contracts/agent-card.md) — never a model dump, so an added internal field can never leak
- [X] T031 [US2] Create `backend/app/api/v1/a2a_server.py` with the card route `GET /a2a/{card_token}/.well-known/agent-card.json`, anonymous, returning the uniform not-found for every negative case
- [X] T032 [US2] Add the transport-security check in `backend/app/api/v1/a2a_server.py`, reading the forwarded protocol when `settings.trust_proxy_headers` is set and the request scheme otherwise; refuse, never redirect
- [X] T033 [US2] Mount the A2A router in `backend/app/main.py` behind the `A2A_SERVER` feature flag

**Checkpoint**: A partner platform can register the agent from its card URL alone

---

## Phase 6: User Story 3 - A partner agent gives the agent work (Priority: P1)

**Goal**: An authenticated JSON-RPC task surface: send, get, cancel.

**Independent Test**: Authenticate a test client against one exposed agent, submit a text message, poll the task to a terminal state, retrieve its result, and confirm a second client cannot read it.

### Tests for User Story 3

- [X] T034 [P] [US3] Add `backend/tests/test_a2a_oauth_client_credentials.py` (`integration` marker — real Postgres/Redis): the grant issues a token; the token is bound to one exposure; presenting it against another exposure returns the uniform not-found, not a 403; revoking the exposure revokes the credential
- [X] T035 [P] [US3] Add `backend/tests/test_a2a_tasks.py`: create/get/cancel happy paths; state mapping across every `AssignmentRun` status; a second client reading another's task gets not-found; `fcp-` keys are rejected and audit-logged; a completed task carries its result as artifacts; an agent not configured for structured output never returns a structured-data artifact
- [X] T036 [P] [US3] Add `backend/tests/test_a2a_dedup_and_limits.py` (`integration` marker): a repeated message id returns the existing task and starts no second run; rate limits refuse at 10/min creation and 120/min read; a Redis outage **refuses** rather than admits
- [X] T055 [P] [US3] Add `backend/tests/test_a2a_reaper.py` (`integration` marker): a run abandoned mid-execution is reclaimed and driven to a terminal state; two concurrent reapers do not double-claim the same run

### Implementation for User Story 3

- [X] T037 [US3] Add the `client_credentials` grant to the token endpoint dispatch in `backend/app/api/v1/mcp_oauth.py` (currently handles only `authorization_code` and `refresh_token`) and add it to `grant_types_supported` in the AS metadata
- [X] T038 [US3] Implement exposure-bound credential minting and revocation in `backend/app/a2a/exposure.py`, issuing clients with `grant_types = ["client_credentials"]` only
- [X] T039 [US3] Create `backend/app/a2a/tasks.py` with task creation: content-type validation, deduplication via Redis `SET NX` backed by the unique constraint, and creation of an `AssignmentRun` with `assignment_id = NULL`
- [X] T040 [US3] Implement run-status → A2A-state projection in `backend/app/a2a/tasks.py` per the mapping in [research.md](./research.md) R4, including `REJECTED` for pre-run refusal
- [X] T057 [US3] Implement artifact projection in `backend/app/a2a/tasks.py`: map the completed run's result and generated files to A2A text and file artifacts, emitting structured-data artifacts only for an agent configured to produce them (`FR-024`)
- [X] T058 [US3] Return quota exhaustion as a structured protocol error in `backend/app/a2a/tasks.py` rather than a generic failure, alongside the audit entry in T046 (`FR-036`)
- [X] T041 [US3] Implement task read and cancel in `backend/app/a2a/tasks.py`, scoping every read to the creating client and using the cross-replica `cancel_requested_at` signal plus `run_agent_chat`'s `cancel_event`
- [X] T042 [US3] Implement `message/send` continuation for a task in `INPUT_REQUIRED` in `backend/app/a2a/tasks.py`; refuse continuation in any other state
- [X] T043 [US3] Add the JSON-RPC dispatch route to `backend/app/api/v1/a2a_server.py` per [contracts/a2a-jsonrpc.md](./contracts/a2a-jsonrpc.md), rejecting `fcp-`/`mcp-` platform keys with an audit-logged rejection
- [X] T044 [US3] Apply `enforce_rate_limit` from `backend/app/core/rate_limit.py` bucketed by registration id, **forcing fail-closed locally** regardless of the global `rate_limit_fail_mode` default of `"open"` — with a "why" comment (research R8)
- [X] T045 [US3] Add the stale-run reaper for external runs in `backend/app/a2a/tasks.py`, claiming with `FOR UPDATE SKIP LOCKED` per `orchestration_engine`'s pattern, so no task remains indefinitely pollable
- [X] T046 [US3] Add audit logging in `backend/app/a2a/tasks.py` for every external submission and for quota exhaustion, naming the responsible external client

**Checkpoint**: Feature complete end to end

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T047 [P] Add the exposure toggle, tool narrowing and one-time URL/credential display to `frontend/src/pages/agent-detail/`, following the existing `web_enabled`/`web_token` pattern rather than adding a new admin screen
- [X] T048 [P] Add operational telemetry distinguishing external from internal task traffic in `backend/app/core/metrics.py`
- [X] T056 [P] Add `backend/tests/test_a2a_metrics.py`: external task traffic increments the external-labelled counters and leaves internal counters untouched
- [X] T049 [P] Document `ENABLED_FEATURES=A2A_SERVER` and the `TRUST_PROXY_HEADERS` requirement in `docs/` — without proxy-header trust behind a reverse proxy, every card fetch is refused
- [ ] T050 Run the full validation sweep in [quickstart.md](./quickstart.md) against a live test deployment, including the SPA catch-all check that cannot be reproduced against a test double
- [ ] T051 Complete the `a2a-sdk` supply-chain review for the pin added in `backend/requirements.txt` before it ships, per the constitution's security posture

---

## Phase 8: Built-in and company-managed agents (2026-08-25)

Follows the move to implicit exposure: with the opt-in relocated into
`integration_bindings`, two rules written for an explicit switch stopped fitting
a binding. See the 2026-08-25 clarifications in [spec.md](./spec.md).

- [X] T059 [US1] Add `allow_builtin` to `writable_agent_error` in `backend/app/api/v1/helpers.py`, dropping the XTM One read-only gate for built-ins and NOTHING else — company-managed stays administrator-only — and have `exposure.user_can_expose` pass it (FR-044)
- [X] T060 [US1] Add `_is_integration_binding_change` to `backend/app/api/v1/agents.py` and widen `update_agent`'s built-in carve-out to it: an admin may configure a built-in's integrations — all of them, A2A included — and nothing else. A **diff**, not a field-presence check, so a seeded field cannot ride along in the same request
- [X] T060a [US1] ~~`_assert_a2a_bindings_are_genuine`~~ — dropped with the A2A-only restriction it existed for. While only A2A keys were writable, a request could name a GitHub integration under an `oauth_a2a` key and bind real GitHub tools to a read-only built-in (`run_agent_chat` resolves a session integration id to its own provider from the database, not to the key it arrived under). Admitting every integration removes the incentive and the guard together
- [X] T061 [P] [US1] Add `backend/tests/test_a2a_builtin_exposure.py` covering the carve-out's boundaries — bind, unbind, either provider, a non-A2A provider moving in the same map, any other field present, a no-op, and reordering — and update the built-in cases in `test_a2a_exposure_authority.py`, which asserted the old refusal
- [X] T062 [US4] Add `exposure.resolve_session_scope` in `backend/app/a2a/exposure.py` resolving `integration_template_bindings` against the exposure's run-as user via `template_candidate_clause` / `resolve_template_integration`, and call it from `tasks.py` in place of `resolve_allowed_tools` (FR-045). Without it every built-in answers a partner with no product data — successfully, which is why nothing surfaced it
- [X] T063 [P] [US1] Make the Tools tab's integration cards editable on a built-in behind a `canManageIntegrations` gate threaded from `AgentDetail.tsx`. `A2ACardUrl` stays read-only — it briefly carried a binding picker of its own, which was only ever needed while the Tools tab was closed to built-ins
- [X] T064 Reconcile [spec.md](./spec.md) (FR-031, FR-044, FR-045), [data-model.md](./data-model.md), [quickstart.md](./quickstart.md), [contracts/internal-management.md](./contracts/internal-management.md) and `dev-docs/a2a-server-operations.md`, which all still described the explicit-choice rule
- [X] T066 [US3] Scope an A2A integration's activity to its own partner. Extract `partner_client_ids` into `backend/app/a2a/providers.py` — the single definition of "which registrations belong to this integration", now shared by the last-activity read and the run listing — and add `a2a_integration_id` to `/assignments/runs/all` and `/runs/count`, filtering on the registration stamped on each run rather than on the agents the integration is bound to (FR-046)
- [X] T067 [US3] Record the credential FORM on each external run (`a2a_server._authenticate` → `tasks.create_task`). A federated caller on a legacy exposure reuses that exposure's OAuth-shaped registration on purpose, so the id alone reported Foundry submissions as OAuth ones — on the Foundry integration's own activity page
- [X] T068 [P] [US3] Add the provider badge to `frontend/src/components/ui/run-card.tsx`, and switch `integration-detail/ActivityTab.tsx` to the partner-scoped fetch
- [X] T070 [US3] Resolve the badge SERVER-side (`a2a.providers.resolve_submitter_providers`, surfaced as `AssignmentRunDetailResponse.a2a_provider`) from what each registration is owned by, not from its id's shape. Deriving per-surface made one run read "Foundry A2A" on the integration page and "OAuth A2A" in the agent's activity log — the same wrong guess, disagreeing with itself. A registration nothing owns (legacy per-exposure, or deleted with its exposure) resolves to no provider and renders as a plain `A2A` badge, which is the honest answer rather than a coin flip
- [X] T069 [P] [US3] Add `backend/tests/test_a2a_activity_attribution.py`: two integrations share no registration, an empty registration list matches nothing rather than everything, and the stamped form separates legacy traffic. Plus the `_authenticate_entra` case where id and form deliberately disagree
- [X] T071 [US1] Drop the explicit expose/revoke endpoints (`POST` / `DELETE /api/v1/agents/{id}/a2a`), leaving the router read-only. They were a second way to reach a state binding already owns, and the two disagree the moment either is used — an exposure with no binding is reachable and invisible, which `a2a2intmodel1` had to clean up once already. The cascade is the point: `expose_agent`, `ExposureDeniedError`, `revoke_exposure`, `revoke_for_agent`, the per-exposure `mint_credential`, `revoke_credentials_for_exposure` and `notify_owner_exposed` all had this endpoint pair as their only production caller, and every one of them was tested. `REVOKED_MANUAL` and `exposed_by_user_id` stay as historical values with no writer
- [X] T072 [US1] Move the FR-005 notification onto the binding hook (`hooks._notify_exposed`) before removing its old caller. It was wired to the expose endpoint alone — never to the implicit path — so the mitigation the exposure-authority decision rests on had already stopped firing for the ordinary case. It now tells the owner AND the run-as user, and names no actor: a `before_flush` listener has no request context, and the audit trail records who edited the binding
- [X] T073 [US1] Give `a2a_exposure_changes_total` its first call site. The counter and `record_exposure_change` had never had one, so the series read 0 forever — worse than absent, since a dashboard built on it says "nothing changed" rather than "not instrumented". Both directions now increment from the flush hook, and `reason` lost its `"manual"` default, which would have mislabelled automatic revocations
- [X] T074 [US1] Make `a2a.providers` the only import path for `ENTRA_A2A_PROVIDER`, dropping the re-export from `a2a.federation`. Two spellings of one key had `integrations.create_integration` importing it from `federation` while importing `A2A_PROVIDERS` from `providers` in the same function
- [X] T075 Reconcile [spec.md](./spec.md) (FR-004, FR-005, FR-029, FR-031, FR-034, FR-039, SC-013), [contracts/internal-management.md](./contracts/internal-management.md) and `dev-docs/a2a-server-operations.md` with the removal — including the two capabilities that go with it and have no replacement: nominating a run-as user, and setting an exposure's tool allow-list
- [ ] T076 Built-in agents' integration configuration is only half owned by the administrator: `seed_builtin_integrations` re-adds a built-in integration binding they removed, `_sync_agent_fields` restores a removed `integration_template_bindings` provider, and retiring a built-in slug cascade-deletes its exposure with no revocation record. Not fixed here — it predates this feature and reaches beyond A2A
- [ ] T065 Re-run the [quickstart.md](./quickstart.md) §4 probes against a live XTM One deployment, including the **inverse** one: an exposed built-in must actually reach its OpenCTI / OpenAEV tools. That failure mode is silent — the partner gets a plausible reply from an agent with no data

---

## Dependencies

```
Phase 1 (Setup)
   └─> Phase 2 (Foundational)  ← BLOCKING
          ├─> Phase 3 (US1: exposure register)
          │      └─> Phase 4 (US4: boundary)      needs the exposure record
          │             ├─> Phase 5 (US2: card)   needs boundary for skill narrowing
          │             └─> Phase 6 (US3: tasks)  needs boundary + card token routing
          └─────────────────────> Phase 7 (Polish)
```

**Story order is dependency-driven, not priority-driven** — all four stories are P1. US4 is sequenced before US2 and US3 deliberately: it is pure logic over the existing ownership model, and it is where the whole feature's premise is proven or falsified most cheaply.

## Parallel Opportunities

| Phase | Parallel tasks |
| --- | --- |
| 1 | T002, T003 (different files) |
| 2 | T010, T012 (test files, after their subjects exist) |
| 3 | T013, T014, T054 (independent test files) |
| 4 | T022, T023, T052 |
| 5 | T028, T029 |
| 6 | T034, T035, T036, T055 (independent test files) |
| 7 | T047, T048, T049, T056 |
| 8 | T061, T063, T068, T069 |
| 9 | T071–T075 (sequential — T072 must land before T071 removes the old caller) |

Within implementation blocks, tasks touching the same file (T015–T018 and T024–T027/T053 all in `exposure.py`; T039–T042, T045, T057, T058 in `tasks.py`) are **not** parallelizable.

## Implementation Strategy

**MVP scope**: Phases 1–3 (T001–T021, T054). That delivers an auditable, revocable exposure register with authority derived entirely from the existing ownership model — standalone security value even if no A2A protocol work ever ships, which is exactly what the spec claims for User Story 1.

**Incremental delivery**: Phase 4 next as risk reduction (cheap falsification of the reuse premise), then Phase 5 for a partner-demonstrable card, then Phase 6 for the full round trip.

**Riskiest tasks, worth reviewing hardest**: T009 (silent HTML-instead-of-404), T025 (pinned `is_admin=False` — the line that most easily restores full-platform reach), T053 (escalation guard — its tool deny-list will drift as tools are added; the path-level half is the durable one), T037 (new auth grant), T044 (fail-closed override diverging from the platform default).
