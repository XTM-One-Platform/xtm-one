# Implementation Plan: Per-Tool Human-in-the-Loop Approval

**Branch**: `feat/173-per-tool-approval` (spec dir `006-per-tool-approval`) | **Date**: 2026-08-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/006-per-tool-approval/spec.md`

> **Revised 2026-08-12** — the original plan designed a per-tool `is_state_changing` classification baked into `ToolDefinition`. That was replaced by an administrator-editable **HITL whitelist**. This document reflects the whitelist model; the classification design is gone.

## Summary

Make human approval of tool calls **deterministic** rather than dependent on the LLM voluntarily calling `request_human_input`. A single platform-wide whitelist names the tools that may run unattended; everything else pauses before execution and surfaces a structured proposal the reviewer can approve, reject, or accept permanently as a personal preference.

Five findings shape the technical approach:

1. **`CompositeToolExecutor` is the single choke point.** It is the only layer that sees both the `ToolDefinition` and every dispatch, *after* name normalisation and alias canonicalisation — so a gate there cannot be bypassed by a reordered or aliased tool name, and it also covers the eight non-agent-loop `execute_tool` callers that a gate in `agent_loop.py` would miss.

2. **The whitelist needs no data migration.** Effective membership is computed live as `(shipped_default − admin_removals) ∪ admin_additions`. The shipped default is a versioned constant in the codebase, so an upgrade replaces it by virtue of deploying new code, and the overlay table is never touched. FR-001d/e/f fall out of this for free rather than needing merge logic.

3. **The existing resume path cannot express this feature and must be reworked.** It matches the pending call by the literal string `"request_human_input"`, stores the answer in a single `Text` column, and injects one free-text tool message. There is nowhere to put a decision or the proposed action.

4. **Gating mid-batch would corrupt the conversation.** In the parallel path, when call *i* raises HITL, calls *after* `i` have already executed but their `tool` result messages are never appended — leaving orphaned `tool_use` blocks that strict-mode Anthropic/OpenAI reject on resume. This is latent today (only one HITL tool exists, usually called alone) but becomes the *normal* case once arbitrary tools can gate. The design therefore screens the **whole batch before executing any of it**.

5. **There is no single "chat" — there are five surfaces, and they do not share a transport.** A gated call is reached through the browser socket, plain REST, the platform SSE stream, the chat channels (Slack, Teams, email) and the anonymous public embed, and each differs in whether it can hold a turn open to ask. The gate is common to all of them (finding 1); the *channel to answer it* cannot be, so each surface needs its own wiring onto shared mechanisms (FR-002b1, and the **Approval Surface** entity in spec.md that names them). This finding was added on 2026-08-26: the original plan described only the assignment/run path, which is why its file map below omitted every chat file this feature touches, and why the general assistant shipped gated but unable to ask.

## Technical Context

**Language/Version**: Python 3.11+ (backend), TypeScript 5.x / React 18 (frontend)

**Primary Dependencies**: FastAPI, SQLAlchemy 2.0 (async, `Mapped[...]`), Alembic, Pydantic v2, Redis (pub/sub), React Query, shadcn/ui

**Storage**: PostgreSQL (primary), Redis (SSE pub/sub fan-out)

**Testing**: pytest (unit by default; `integration` marker for real Postgres/Redis/MinIO), Vitest + `tsc --noEmit` + eslint on the frontend, dedicated Alembic migrations CI job (idempotent re-run + downgrade round-trip)

**Target Platform**: Linux server, multi-replica deployment

**Performance Goals**: Whitelist membership is now on the hot path for **every** tool call. Effective membership MUST be resolved once per run into an in-memory set and reused — O(1) per call, one query per run, no per-call DB round-trip.

**Constraints**: `ToolDefinition` is a `frozen=True` dataclass with ~2,486 construction sites — it gains **no** new fields under this design. The pause/resume state machine (`awaiting_human`) is non-terminal and has no timeout; preserved deliberately (FR-015e). Rolling deploys mean replicas running the old and new shipped default coexist briefly.

**Scale/Scope**: ~115 built-in tool definitions and ~2,353 integration tool definitions across ~110 providers. The shipped default whitelist covers built-ins; integrations remain behind a single per-integration toggle this phase (FR-021), and MCP-specific *configuration* is deferred — MCP **tools** gate like everything else, since absence from the whitelist is what gates and none of them ships on it (FR-022).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- [x] **Fail-Closed by Default** (Principle II): Absence from the whitelist is the default state, so an unknown, newly shipped, or newly authored tool gates (FR-001a). The gate lives in `CompositeToolExecutor` after alias normalisation, so an unrecognised or aliased name cannot route around it. If whitelist resolution or the standing-exception lookup fails (DB unreachable), the run gates rather than assuming exemption.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle IV): The whitelist overlay is a small table with `UNIQUE (tool_key)` and upsert semantics, so concurrent admin edits converge. Standing exceptions use `UNIQUE (user_id, tool_key)` likewise. No new scheduled job. Rolling deploys are safe because the shipped default is code, not state — a replica simply uses the default it was deployed with, and the overlay is authoritative on top.
- [x] **CI-Enforced Quality Gates** (Principle VI): Unit tests for whitelist resolution and the gate decision, `integration`-marked tests for the pause→respond→resume round-trip against real Postgres, and the migrations CI job covering the overlay table and the approval switches. Frontend must pass `tsc --noEmit` and eslint.
- [x] **Rationale-Documented Complexity** (Principle VII): The batch pre-screen (orphaned `tool_use` blocks), the overlay-not-snapshot storage choice (why a removal must outlive an upgrade), the exception fingerprint (why consent binds to schema not name), and the install default (why an upgrade gates no agent) all carry "why" comments.
- [x] **Alembic-Only, Lock-Safe Migrations** (Principle V): Migrations add the two tables, the run columns and the approval switches; `j1atc0173` adds the install default `FALSE` on an existing row and writes no agent row (FR-014a). `UPGRADING.md` describes an opt-in feature with nothing to do on upgrade. Downgrade drops the new structures. Whitelist content itself never migrates, which removes an entire class of upgrade bug.
- [x] **Assignment-Driven Automation** (Principle I): This feature adds no background job, script or scheduled step — it gates the existing agentic loop from inside `CompositeToolExecutor` and reuses the existing `awaiting_human` pause. Noted rather than waved through: the **general assistant** is an execution path with no Assignment behind it, which predates this feature; this feature neither widens nor narrows that, and FR-012a1 has it follow the install default because there is no agent switch to read.
- [x] **Security by Default** (Principle III): `prompt_guard` is applied on all four chat paths that can now pause (`general_chat.py`, `agent_chat.py`, `channels/router.py`, `public_chat.py`) — verified, not assumed. Every approval endpoint requires JWT; the only unauthenticated surface is the public embed, which FR-002b1 forbids giving an approval channel at all, so no decision is ever accepted from an anonymous caller. FR-008a adds the authorisation rule that a decision counts only from the user whose call is paused. No new secret is stored.
- [x] **Shared Utilities Over Duplication** (Principle VIII): **Gated with a recorded exception** — see the `run_general_chat` row in Complexity Tracking. The gate itself lives in one place, and the whitelist, fingerprint and decision-applier are each single implementations reused by every surface. The divergence is confined to the two chat services.
- [x] **TanStack Query for Server State** (Principle IX): `useHitlWhitelist`, `useToolApprovals` and `useSubmitChatApprovals` are React Query hooks; no server state is held in component state or a bespoke store.
- [x] **Design System Compliance** (Principle X): the whitelist screen, the profile row and the in-conversation approval prompt use shared shadcn primitives and token-based surfaces, reusing `ActionProposalList` rather than re-implementing a proposal renderer, and ship all four locales.

Constitution is **v2.0.0** (ratified 2026-07-23, last amended 2026-08-24) — all ten gates evaluated, not skipped.

> **Re-gated 2026-08-26.** This section previously enumerated the constitution's old five principles and cited "v1.0.0". The constitution was amended to ten principles on 2026-08-24, and its own front matter records that existing plans were not retroactively re-checked and that whoever next ran `/speckit-plan` or `/speckit-analyze` should re-evaluate. `/speckit-analyze` did, on 2026-08-26, and this is that re-evaluation: the five original gates are renumbered (I→II, II→IV, III→VI, IV→VII, V retitled to the Alembic principle it always described) and the five never previously gated — I, III, VIII, IX, X — are assessed above. Only Principle VIII required a new entry in Complexity Tracking.

### Post-Design Re-Check (after Phase 1)

All five gates still pass. Phase 1 surfaced three things worth recording:

- **Principle I tightened**: the gate decision table ([data-model.md §6](./data-model.md)) is total and `evaluate_gate` is contractually forbidden from raising. Whitelist resolution failure and exception-lookup failure both fall through to `pause`.
- **Principle V simplified**: because effective membership is computed rather than stored, there is no whitelist data migration, no merge step, and no "applied default version" to track. FR-001e (a removal outlives an upgrade) is a property of the storage shape rather than logic that could be got wrong.
- **New obligation (Principle I)**: `_persist_notification_activity` skips durable records for company-managed runs (`notify.py:120-125`). For an *autonomous* company-managed pause the stall is therefore invisible — a silent failure of the kind Principle I exists to prevent. [contracts/events.md §7](./contracts/events.md) makes a durable notification mandatory for autonomous pauses regardless of `is_company_managed`. This is a fix to existing behaviour that FR-015d depends on and must not be dropped as "out of scope".

## Increment: answering from the executions list (FR-026–FR-037b)

*Recorded here rather than in a new plan for the same reason the requirements went into this spec:
it is a second door onto the existing decision, not a new mechanism.*

### Technical Context (delta only)

**Storage**: unchanged. No table, no column, no migration — [data-model.md §10](./data-model.md)
records what was checked and why each concept has nowhere to live.

**Primary Dependencies**: unchanged. One new React Query mutation for the batch; the read path
reuses `useAllAssignmentRuns`, which the page already calls.

**Constraints**: FR-027 requires a proposal's complete arguments before any decision, so the surface
must have room for them — which is what makes the executions list the right one and rules out a
popup. FR-034 is then satisfied by construction, since the list renders proposals expanded.

**Scale/Scope**: one new endpoint and one page's worth of interaction. No change to what gates, to
the whitelist, or to the wholesale approval switches.

**Authorisation is `require_run_access`, unchanged**: the owner for a personal run, any
administrator for a company-managed one, per FR-008b. The batch applies it per member so bundling a
run with ones the caller owns grants no reach over it (FR-028).

### Constitution Check (increment)

- [x] **Fail-Closed by Default** (II): the server remains the arbiter of whether a proposal is still
  answerable — the client never decides. A stale list answers `409`, never a silent success
  (FR-030). The group action fails closed on reading too: unavailable until every member has been
  expanded (FR-034), rather than available with a warning.
- [x] **Concurrency-Safe, Multi-Replica** (IV): the batch is N independent decisions, each guarded by
  the same `status == "awaiting_human"` check that already serialises two tabs. No batch-level lock
  and no cross-run transaction — there is nothing to make atomic, and pretending otherwise would
  invent a rollback FR-033 explicitly does not want.
- [x] **Security by Default** (III): every batch member is authorised individually, so bundling a run
  with ones you own grants no reach over it. No new unauthenticated surface. "Approve always" is
  refused in a batch (FR-032) rather than downgraded, so a caller is never told it got the durable
  thing it asked for.
- [x] **CI-Enforced Quality Gates** (VI): unit tests for the batch's per-member results and partial
  failure, an `integration`-marked test for the two-tab race (SC-023), and frontend `tsc --noEmit`
  + eslint. `validate-i18n.mjs` for the four locales.
- [x] **Alembic-Only, Lock-Safe Migrations** (V): **not applicable** — no migration. Asserted rather
  than skipped: [data-model.md §10](./data-model.md) is the record of that check.
- [x] **Assignment-Driven Automation** (I): no background job, no scheduled step. This shortens a
  human's path to an existing decision.
- [x] **Rationale-Documented Complexity** (VII): the one non-obvious choice — client-side, per-sitting
  read-tracking rather than a server field — is argued in [research.md §D15](./research.md): the
  requirement is that the reviewer was *shown* each proposal, which is a client-side fact, and a
  persisted flag would claim a stronger thing no storage can know.
- [x] **Shared Utilities Over Duplication** (VIII): the batch composes the existing
  `_apply_decisions_to_run` / `_grant_standing_approvals` / `resume_assignment_run` rather than
  reimplementing them, and the page reuses `RunCard`/`ActionProposalList`. FR-029 makes this a
  requirement, not a preference.
- [x] **TanStack Query for Server State** (IX): one new mutation hook; selection and read-tracking
  are component state because neither is server state.
- [x] **Design System Compliance** (X): shadcn primitives, token surfaces, all four locales.

### Post-Design Re-Check

All gates still pass. One obligation Phase 1 surfaced: the existing `respond` endpoint refuses
correctly but collapses three reasons into one `400`. FR-030 requires the reviewer be told which
happened, so [contracts/executions-approvals.md §3](./contracts/executions-approvals.md) makes that
distinction an obligation on the **existing** endpoint — a fix to shipped behaviour this increment
depends on, and not one to drop as out of scope.

## Project Structure

### Documentation (this feature)

```text
specs/006-per-tool-approval/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── rest-api.md
│   └── events.md
├── checklists/requirements.md
├── spec.md
└── tasks.md             # Phase 2 (/speckit-tasks — regenerate after this plan)
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── models/
│   │   ├── tool_approval.py            # NEW: ToolApprovalPreference, HitlWhitelistOverride
│   │   ├── agent.py                    # approve_tool_calls (nullable)
│   │   ├── platform_settings.py        # approve_tool_calls_default, tool_approvals_enabled
│   │   └── assignment.py               # AssignmentRun: pending_approvals, approval_decisions
│   ├── tools/
│   │   ├── base.py                     # CompositeToolExecutor gate (NO new ToolDefinition fields)
│   │   ├── approval.py                 # NEW: gate decision + fingerprint (pure)
│   │   └── agent_loop.py               # batch pre-screen; sibling-result correctness
│   ├── services/
│   │   ├── hitl_whitelist.py           # NEW: shipped default + overlay; resolve_approve_tool_calls
│   │   ├── hitl_whitelist_default.py   # NEW: the versioned shipped default (data)
│   │   ├── tool_approval_service.py    # NEW: standing exceptions CRUD + invalidation
│   │   ├── agent_chat.py               # agent chat on every surface
│   │   ├── general_chat.py             # the general assistant (follows the install default)
│   │   ├── chat_turn_guard.py          # pending proposals + decision inbox (Redis)
│   │   ├── assignment_engine.py        # pause with proposals; resume with decisions
│   │   ├── assignment_helpers.py       # resolve_hitl unchanged (prompt + request_human_input)
│   │   └── notify.py                   # autonomous-pause notification (FR-015d)
│   ├── api/v1/
│   │   ├── hitl_whitelist.py           # NEW: admin whitelist endpoints
│   │   ├── tool_approvals.py           # NEW: self-service preference endpoints
│   │   ├── assignments.py              # /respond extended; pending-approvals endpoint
│   │   ├── chat.py                     # browser socket asks in-turn; REST proposes
│   │   ├── platform.py                 # platform stream asks; decision POST
│   │   └── public_chat.py              # gates and explains — never asks (FR-002b1)
│   ├── channels/
│   │   └── router.py                   # chat channels: Slack, Teams, email
└── alembic/versions/                   # e5rel3298 (tables, run columns), j1atc0173 (switches)

frontend/
└── src/
    ├── components/ui/
    │   ├── run-card.tsx                # approval popup (approve/edit/reject/always)
    │   └── action-proposal.tsx         # NEW: structured proposal renderer
    ├── pages/
    │   ├── tools/ToolGateSection.tsx   # NEW: admin whitelist management, on the Tools page
    │   └── profile/ToolApprovalsRow.tsx # NEW: self-service view/revoke
    ├── hooks/api/
    │   ├── useHitlWhitelist.ts         # NEW
    │   └── useToolApprovals.ts         # NEW
    ├── hooks/api/useChatApi.ts         # supports_tool_approval; approval_decision
    ├── pages/chat/ChatMessages.tsx     # in-conversation prompt (reuses ActionProposalList)
    └── hooks/useActivityStream.ts      # handle action_proposal event
```

**Structure Decision**: Existing web-application split. The whitelist gets its own service module and a separate data module for the shipped default, so curating the default is a reviewable data change rather than a code change buried in logic. Pure decision logic lives in `tools/approval.py`; persistence in `services/`. `assignment_engine.py` is already ~5,000 lines and is not grown further than the pause/resume rework requires.

## Complexity Tracking

| Cost | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Reworking the resume path rather than reusing it | The current path matches the pending call by the literal name `"request_human_input"` and stores a free-text answer in one `Text` column. It cannot express "reject" for an arbitrary tool, nor name which call a decision belongs to. | Encoding structured decisions into the free-text field would still match the wrong tool call, silently falling back to the invalid id `"hitl"`. |
| Batch pre-screen before executing any call in a turn | Gating mid-batch leaves already-executed siblings without `tool_result` blocks — orphaned `tool_use` that strict-mode LLM APIs reject — and lets un-approved side effects run. | Raising on the first gated call is what the code does today; it is latent-broken and becomes the common case once arbitrary tools gate. |
| Two storage layers for the whitelist | FR-001e requires an administrator's removal to survive an upgrade that re-adds the tool, and FR-001f requires new default entries to apply with no admin action. | A single materialised list must be merged on every upgrade, and any merge rule either clobbers admin intent or silently re-exempts a deliberately gated tool. |
| Deriving the platform half of the default from the existing read/write tool classifier, rather than enumerating names | FR-001g. 256 platform tools, of which 112 classify as reads and 111 end up exempt; gating those teaches operators to exempt whatever is in front of them, which is how the consequential calls stop being read. Deriving means a tool added by a later platform release arrives classified instead of stale, and `_infer_tool_type` already fails safe to `write`. | A hand-written list of platform tool names, wrong the day it is written; and a `side_effect_free` flag on `ToolDefinition`, which would need 261 classification decisions and still could not describe a `manage_…` tool. |
| A checked-in snapshot of the derived set, guarded by a golden test | Deriving gives up the property that made the curated list reviewable ("this file is data, and is reviewed as data"): a new `list_opencti_*` in a provider PR would widen unattended execution with nothing in the diff. The snapshot puts every widening back in front of a reviewer. | Trusting review of the provider modules themselves: the classification is a side effect of the tool's *name*, which no provider-module reviewer is looking at through that lens. |
| A dedicated `tool_approval_preferences` table rather than a JSON column on `users` | Exceptions need individual revocation, uniqueness under concurrent grants, fingerprint comparison, and audit joins. | The `users` table holds JSON preference blobs already, but a blob makes per-row invalidation and audit awkward. |
| Per-integration toggle instead of per-tool for ~2,353 integration tools | FR-021 scopes this phase to one toggle per integration, validated end-to-end on one integration first. | Per-tool curation of 2,353 definitions across ~110 provider modules is not deliverable this phase. |
| A per-platform approval layer (capability + policy + provenance) on `platform_registrations`, above the per-integration toggle | FR-021a–d. A registered platform's *mutating* tools cannot be whitelisted by name — and neither can its `manage_…` tools, whose behaviour depends on their arguments — so on upgrade they gate at once, across every agent. The operator needs one decision per platform, and it has to outrank the older per-integration flag or the settings page can claim a gate that is not enforced. | A per-integration toggle alone: registration creates an integration per platform, so honouring both made exemption an OR — one flip and a platform deliberately set to *require* ran unattended. A version table mapping product → "supports approvals" was rejected earlier for encoding other teams' release schedules. |
| Recording that a *client* of a platform declared it can answer prompts, separately from the platform's own declaration | FR-021d. With policy `skip` and no declared capability, nothing ever revisits the operator's choice, so a capable client goes unasked indefinitely. The evidence already arrives on every message and was being discarded. | Promoting the observation to the capability column: one capable client does not speak for an unattended caller of the same platform, so it may inform an offer, never a gate. |
| A new `approve_tool_calls` switch and an install default, rather than reusing `human_in_the_loop` | FR-012, FR-014: `human_in_the_loop` means the prompt-driven review, so reusing it would hard-gate every agent and assignment enabled for that; and an upgraded install must leave even agents created later ungated, which only an install-level default expresses. | Reusing `human_in_the_loop` with a column default: gates agents that opted into a different feature, and cannot tell a fresh install from an upgraded one per row. |
| Two parallel chat services — `run_agent_chat` and `run_general_chat` — rather than one | The general assistant has no agent, so it resolves tools, prompt, quota and gating from platform configuration instead of an `Agent` row; the two entry points diverge before the loop starts. Converging them is a refactor of two of the largest services in the backend and was not deliverable inside this feature. | **Not rejected — deferred, and it has already cost once.** Principle VIII exists because "a locally redefined copy does not stay in sync; the drift surfaces later as a silent regression in whichever copy was not updated", and that is exactly what happened: `run_agent_chat` was given an approval channel, `run_general_chat` was not, and nothing caught it for the length of the feature. Mitigation, not a fix: every mechanism *underneath* the two (the gate, whitelist resolution, the fingerprint, the decision applier, the Redis inbox) is a single implementation both call, so what can drift is the wiring rather than the behaviour — and FR-002b1 now makes per-surface wiring a stated requirement rather than an assumption, so the next omission is a failed requirement instead of an invisible gap. Convergence remains the right long-term answer and is out of scope here. |

**Accepted operational consequence**: wherever approval is on, scheduled assignments calling tools that are neither whitelisted nor covered by the triggering user's standing exception pause and wait indefinitely. This is intended fail-closed behaviour, mitigated by FR-015d (notify the owner). An upgrade turns it on for no agent (FR-014a). Note the inherited asymmetry: company-managed runs get **no durable `Activity` row** today (`notify.py:120-125`), so that gap must be closed or company-managed autonomous pauses will be invisible.

**Sign-off status**: resolved. The maintainer decided that an upgrade switches approval on for no agent and that a fresh install starts with it on (FR-014, FR-014a). The product owner (@AreDee-Bangs) endorsed shipping `code_interpreter` whitelisted. No outstanding external dependencies.
