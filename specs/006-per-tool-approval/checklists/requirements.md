# Specification Quality Checklist: Per-Tool Human-in-the-Loop Approval

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-12
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

All items pass. No open [NEEDS CLARIFICATION] markers remain.

**Decisions recorded (see `## Clarifications` in spec.md):**

- **"Approve always" grants a persistent exception** (FR-015) that is a **user preference**, not
  assignment configuration — scoped to the granting user and one tool (FR-019), never shared with
  another user (FR-015a), and applying to **every run carrying that user's identity, interactive
  and autonomous alike** (FR-015b). Users self-serve view/revoke (FR-017); administrators get
  **no cross-user console** (FR-017a) and reach another user's preference through impersonation,
  dual-attributed in the audit trail (FR-017b).
- **Because one click also un-gates unattended execution**, the reviewer must be told so before
  confirming (FR-015c). An autonomous run still pauses on any gated call an exception does not
  cover (FR-015f), and that stall is deliberate: the owner is notified (FR-015d) and there is no
  timeout or auto-resolution (FR-015e).
- **The gate has its own per-agent switch** (FR-012), `approve_tool_calls`, separate from
  `human_in_the_loop` (FR-011). Unset follows an install default (FR-014): on for a fresh install,
  off for an upgraded one, whose agents — existing or created later — do not gate until an
  administrator turns approval on (FR-014a). No agent row is written by the upgrade.
- **Exemption comes from a platform-wide HITL whitelist, not per-tool classification**
  (FR-001, FR-001a–h, FR-003, FR-004). A tool on the list runs; anything absent gates, so a tool
  nobody has considered fails closed. The product ships a curated default so a fresh install is
  usable, stored as two layers: a product-owned shipped default replaced on upgrade, and an
  administrator overlay of additions/removals that upgrades never touch (FR-001d–f). Any tool may
  be whitelisted, including destructive ones — the safeguards are a pre-save warning (FR-005) and
  audit (FR-018), not a protected set.
- **The product-owned default has a second, derived half** (FR-001g/h): the
  read-classified tools of the *registered* Suite platforms, derived from the same read/write
  classifier the integration catalog presents rather than curated by name. It behaves as part of
  the default in every observable respect — subject to the overlay, and a removal survives an
  upgrade and a reconnection — and it is re-gatable per platform in one reversible action, because
  an escape hatch costing one action per entry would not be one. **Scope note**: this narrows
  FR-021a rather than contradicting it. A platform's mutating tools, and its consolidated
  `manage_…` tools whose behaviour depends on their arguments, still cannot be classified per tool
  and are still decided per platform.
- **Collecting an approval is a per-surface obligation** (FR-002b1, session 2026-08-26). Every
  surface that gates for an *identified* user must be able to carry that user's decision — browser
  socket, REST and the chat channels alike — by a mid-turn exchange where the transport allows one
  (FR-002c) and by propose-and-resume where it does not (FR-002c1/c2). Only the anonymous public
  embed may gate and explain, and it must never be given an approval channel. Delivering a decision
  to a turn still waiting in memory does not satisfy the resume requirement (FR-002c3). A proposal
  never expires on a clock but is retired when the conversation moves on (FR-002c4), and only the
  user whose call is paused may answer it (FR-008a). The **general assistant** has no
  switch of its own and follows the install default (FR-012a1).
  Surfaces are named canonically by the **Approval Surface** entity; "chatbot" now refers only to
  the external `packages/filigran-chatbot`.
- **Material tool changes invalidate standing exceptions** (FR-019a/b): a parameter-schema or
  classification change forces re-approval; cosmetic edits do not. Prevents a tool being rewritten
  under consent granted against an earlier version.

**Grounding note**: the gate switch is `agents.approve_tool_calls` (`backend/app/models/agent.py`)
and the install default `platform_settings.approve_tool_calls_default`, both resolved by
`resolve_approve_tool_calls` (`backend/app/services/hitl_whitelist.py`). `human_in_the_loop` and
`resolve_hitl` (`backend/app/services/assignment_helpers.py`) keep their prompt-driven meaning.

All artifacts (`plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`,
`tasks.md`) were regenerated for the whitelist model on 2026-08-12 and are consistent with
this spec. On **2026-08-26** `/speckit-analyze` found nine discrepancies and all were remediated:
`plan.md`'s Constitution Check was re-gated against constitution **v2.0.0** (I–X; five principles
had never been gated), its file map and Summary now describe the five chat surfaces the original
plan omitted entirely, the `run_general_chat`/`run_agent_chat` duplication is recorded in
Complexity Tracking as the Principle VIII exception that has already cost once, and the
retracted platform-tool counts (261/149) were corrected to 256/145 in `plan.md` and `tasks.md`.
`quickstart.md` gained Scenario 10. Still open: `data-model.md` and `contracts/` have not had the
surface-vocabulary sweep (tasks.md T162). FR-001g/h were added later, on 2026-08-25, and `plan.md`, `data-model.md`,
`contracts/rest-api.md`, `contracts/chat-approval-api.md`, `quickstart.md` and `tasks.md` were
updated with them — `research.md` was not, since the decision rests on a classifier that already
existed rather than on a new alternatives comparison.

**Product-owner decisions (confirmed by @AreDee-Bangs)**:

- **`code_interpreter` ships whitelisted**, so agents execute arbitrary generated code unattended
  by default, scheduled runs included. Endorsed by the product owner on the basis that the sandbox
  bounds blast radius and any deployment can remove the entry. It still leans on the sandbox as a
  boundary the constitution's Security Requirements decline to fully trust ("defense-in-depth, not
  a complete security boundary on their own"), so re-examine it if sandbox isolation guarantees
  change.

**Planning notes**:

- `backend/app/services/agentschema/mapping.py` declares `approvalMode` per agent: `never` when its
  `approve_tool_calls` is off, else `always`.
- Per-user exception resolution should follow the platform's existing per-user identity convention
  (`effective_user_id = run.user_id or agent.user_id`, plus `run_as_user_id` for company-managed
  agents — `backend/app/models/assignment.py:376-387`), and impersonation already exists in
  `backend/app/core/security.py` and `backend/app/api/deps.py`.
- **Operational risk to plan for**: wherever approval is on, every scheduled assignment touching a
  non-whitelisted tool pauses and waits indefinitely. `awaiting_human` is
  non-terminal (`assignment_engine.py:80-108`) and `assignment_queue_hold_on_awaiting_human`
  defaults `True` (`config.py:563`), so a stalled run can also hold its delivery queue. FR-015d
  (notification) is the agreed mitigation; an upgrade turns approval on for no agent (FR-014a).
