# Phase 0 Research: Per-Tool Human-in-the-Loop Approval

All findings are grounded in the current codebase, with `file:line` references. Nothing here was assumed from the issue text.

---

## D1. Where the gate goes

**Decision**: Insert the approval gate in `CompositeToolExecutor.execute_tool` (`backend/app/tools/base.py:970`), raising an extended `HumanInputRequestedError`.

**Rationale**: The executor stack is
`run_agent_loop → [ScopedToolExecutor] → CompositeToolExecutor → CallableToolExecutor | MCPManager | _SafeIntegrationExecutor`.

`CompositeToolExecutor` is the only layer holding **both** `_definitions` and `_executors` (`base.py:901-902`), and it already exposes `get_tool_source(name)` (`:956`) and `get_tool_entity_id(name)` (`:963`) — so `requires_approval` lookup is a natural sibling. Critically, the gate sits **after** name resolution (exact → normalised token key → `_KNOWN_TOOL_ALIASES`, `:970-1029`), so a reordered or aliased tool name cannot route around it.

It also covers the callers that bypass the agent loop entirely — `mcp_platform.py:485`, `integrations.py:2922`, `channel_bots.py:803`, `mcp_servers.py:636`, and the pre/post-hook executions in `agent_chat.py:904, 967, 3718, 4059` — which a gate in `agent_loop.py` would silently miss.

**Alternatives considered**:
- `CallableToolExecutor` (`base.py:790`) — leaf executor, but it only knows `name → handler` and has **no access to `ToolDefinition`**; a gate there needs a parallel flags map, duplicating state.
- `ScopedToolExecutor` (`base.py:1037`) — already receives full `ToolDefinition` objects, but it is applied at only 4 sites (`public_chat.py:1015`, `assignment_engine.py:3261`, `agent_chat.py:3854`, `mcp_platform.py:396`), so it is not universal.
- `agent_loop.py` dispatch sites (`:6089` sequential, `:6486` parallel) — sees only `ToolCall`, no definition, and would need duplicating across both paths.

---

## D2. Batch pre-screen (the orphaned `tool_use` hazard)

**Decision**: Screen **all** tool calls in a turn for approval *before executing any of them*. If any require approval, pause with the full set of proposals and execute nothing.

**Rationale**: This is the single most dangerous finding of Phase 0. In the parallel path, `asyncio.gather(..., return_exceptions=True)` (`agent_loop.py:6588-6593`) converts the HITL exception into a *result value*; the post-gather loop (`:6643`) then walks results **in tool_calls order**, appending each `tool` message inline (`:6783, 6846, 6876`) and re-raising at the first HITL (`:6664-6667`).

Consequence: when call *i* is gated, calls *after* `i` have **already executed** but their `tool_result` blocks are never appended. The persisted conversation holds an assistant message with N `tool_use` blocks and only i+1 `tool_result` blocks — orphaned `tool_use`, which strict-mode Anthropic/OpenAI reject on resume. Worse, the post-`i` calls already ran, so their side effects happened without approval and without a record.

This is latent today because `request_human_input` is the only HITL-raising tool and is typically called alone. Once arbitrary tools can gate, multi-call batches are the *normal* case.

Pre-screening also satisfies the spec's edge case ("execution of the run does not proceed past a pending proposal") without partial execution.

**Alternatives considered**:
- Raise on first gated call and synthesise `tool_result` blocks for siblings on resume — reconstructs correctness after the fact, but the un-approved siblings have already run. Rejected: it lets un-approved side effects happen.
- Force sequential execution when any gated tool is present — correct but discards the parallel path's latency benefit for every run containing a gated tool, which on an agent with approval on is most runs.

---

## D3. Whitelist strategy and storage

**Decision**: A single platform-wide whitelist whose effective membership is **computed**, never materialised:

```
effective = (shipped_default − admin_removals) ∪ admin_additions
```

The shipped default is a versioned constant in the codebase (`services/hitl_whitelist_default.py`); the overlay is a small table of explicit administrator additions and removals.

**Rationale**: This shape makes FR-001d/e/f properties of the storage rather than logic that could be got wrong.

- **FR-001f** (new default entries apply with no admin action): deploying new code changes the constant, so a newly shipped read-only tool is exempt the moment the release lands.
- **FR-001e** (a removal outlives an upgrade): removals live in the overlay, which upgrades never touch, and are subtracted *after* the default is applied.
- **FR-001d** (upgrade replaces the default wholesale): true by construction — the default is code.

There is consequently **no whitelist data migration at all**, which removes an entire class of upgrade bug. It also makes rolling deploys safe without coordination: a replica uses the default it was deployed with, and the overlay is authoritative on top, so the worst case during a rollout is that a newly-shipped exemption takes effect on some replicas a few minutes before others — never that a gated tool runs un-gated.

Curating the shipped default remains real work — the same judgement a classification map would have needed, covering ~115 built-in tools. Roughly 54 follow read-only verb conventions (`get_*`, `list_*`, `search_*`, `read_*`, `inspect_*`, `compare_*`, `analyze_*`, `compute_*`, `validate_*`) and are the natural default-whitelist population. `agent_builder.py` alone contributes 55 definitions with full CRUD over platform objects; none belong in the default.

Of the five genuinely ambiguous tools, four are deliberately **excluded** so they gate — `browser`, `web_fetch`, `check_task_status`, `consult_agent` — the first two because they reach outside the platform, the last because it can reach gated tools indirectly through another agent. (`stix_bundle_validator` was excluded here initially, on a suspicion that parsing untrusted third-party STIX had side effects; it does not, and it now ships exempt.)

**`code_interpreter` is included** in the shipped default (product decision, 2026-08-12), despite executing arbitrary LLM-authored code. The reasoning is usability: an agent that pauses before every computation is impractical, and the sandbox bounds the blast radius. This is the shipped default's highest-risk entry and the one most likely to be removed by a security-conscious deployment — which the whitelist makes a one-line admin action rather than a fork. Note the tension with the constitution's Security Requirements, which describe sandbox module/import blocklists as "defense-in-depth, not a complete security boundary on their own": the decision leans on the sandbox as a boundary the constitution declines to fully trust, so it should be revisited if the sandbox's isolation guarantees change.

Integration tools (~2,353 across ~110 providers) are not individually whitelisted this phase — they stay behind the single per-integration toggle (FR-021). MCP is out of scope (FR-022).

**Alternatives considered**:
- *Materialised list merged on upgrade* — needs a merge rule that either clobbers admin intent or silently re-exempts a deliberately gated tool. Rejected: FR-001e forbids the latter, which is the dangerous direction.
- *Per-tool classification on `ToolDefinition`* — the original design. Rejected in clarification: it hard-codes judgement into code an operator cannot correct for their environment, and forces edits across ~2,486 construction sites' worth of definitions to change a decision.
- *Storing an "applied default version"* — unnecessary once membership is computed live.

---

## D3a. Deriving the platform half of the default

**Decision**: A registered Suite platform's **read-classified** tools are part of the shipped default, and membership is **derived from the existing read/write tool classifier** rather than listed by name:

```
default   = shipped_default ∪ platform_reads(registered providers)
effective = (default − admin_removals) ∪ admin_additions
```

`platform_reads` is computed by `services/hitl_whitelist_platform.py` from `_infer_tool_type` (`api/v1/integration_catalog.py`) — the same classifier behind the "Read" badge in the integration catalog — minus a small `HAND_GATED` set. 111 exempt of 256 platform tools today.

**Rationale**: D3 left these tools gating wholesale, which was defensible when the alternative was classifying 256 tools by hand, but wrong in practice. Roughly half of a platform's surface only reads, and an agent that pauses before reading a report teaches an operator to click "always allow" on whatever is in front of them — which is how the calls that *do* change something stop being read. The classification already existed and was already trusted for a security-relevant display; the whitelist was simply ignoring it.

Deriving rather than enumerating is what makes it survive contact with other teams' release cycles. A tool added by a future platform release arrives classified, so the list cannot go stale the way a hand-written one does — and the classifier fails safe to `write`, so an unrecognised verb gates rather than slipping through.

Two things bound the trust placed in it:

- Its failure mode is a tool that keeps its name while gaining a mutating parameter, which no name-based check can see. That is not a regression — the hand-maintained entries D3 describes carry the same risk identically — but it is the reason the classification is not treated as a security boundary on its own.
- The consolidated `manage_…` tools (54 of the 256) take an `action` enum mixing `list`/`get` with `create`/`update`/`delete`. They classify `write` and stay gated, because no name-keyed exemption can be right about them. Argument-aware exemption is the follow-on question this decision deliberately does not answer.

**On the hand-held-back set**: `HAND_GATED` holds one entry, `download_opencti_file`, which loads a third-party document nobody has looked at. It briefly held four OpenGRC identity/RBAC reads as well, on the reasoning that "who can do what" deserves a human even though it changes nothing. That was withdrawn as incoherent rather than refined: the RBAC *overview* asked while `list_opengrc_users` (which returns emails), `get_opengrc_user`, `get_opengrc_group` and `get_opengrc_role` stayed exempt, so an agent blocked on the summary assembled the same picture from four exempt calls. The choice was to extend to the whole identity family or drop the four; dropping won, because a control the product only appears to enforce is worse than none, and a deployment that wants identity gated has a real control on the whitelist screen.

**Cost accepted**: deriving gives up the property that makes D3's list safe — that it is data, reviewed as data. A new `list_opencti_*` in a provider module would widen unattended execution with nothing in the diff to review. `tests/data/hitl_platform_read_whitelist.txt` holds the effective set, checked in and pinned by a golden test, so every widening is a diff line naming the tool.

**Scope**: registered platform providers only, resolved from live registrations — not the ~2,353 third-party integration tools, which stay behind the per-integration toggle of FR-021. A platform read runs against the user's own per-user credential and so can return nothing the user could not fetch themselves; "read someone's mailbox" is a different argument and is not being made.

**Alternatives considered**:
- *A hand-written list of platform tool names* — the shape D3 uses for built-ins. Rejected: 261 names across three products on independent release cycles, wrong the day it is written, and every future platform release silently degrades it.
- *A `side_effect_free` flag on `ToolDefinition`* — appealing because the classification would travel with the definition. Rejected twice over: it needs 261 individual decisions to land the first version, it reopens D4 (which exists precisely to keep `ToolDefinition` unchanged), and it still cannot describe a `manage_…` tool, whose safety depends on an argument rather than on the tool.
- *Letting each platform declare its own exempt list at registration* — the original request, and the natural reading of "the platform knows what its tools do". Rejected **for now**, not on timing but on trust: `/register` is the heartbeat, so version drift is a solved problem there (the `intents` declaration already works this way), but a declaration riding on it lets a platform release widen unattended execution across every deployment with no human in the loop. Revisit as a *proposal* an operator accepts — the same fact/policy split FR-021a/b already draws — when platforms contribute tools XTM One does not author.

---

## D4. Why `ToolDefinition` is unchanged

**Decision**: Add **no** fields to the frozen `ToolDefinition` dataclass (`base.py:368-391`).

**Rationale**: With whitelist membership as the sole source of truth, `requires_approval` is a derived property of `(tool_name, whitelist)` rather than an attribute of the definition. Keeping it off the dataclass avoids touching ~2,486 construction sites, avoids two sources of truth that could disagree, and means an administrator's whitelist edit takes effect immediately without rebuilding definitions.

The gate needs only the tool's **canonical name** — which `CompositeToolExecutor` has already resolved through alias normalisation by the time it dispatches — plus the resolved whitelist set.

**Alternatives considered**: a `requires_approval` field populated at definition-build time from the whitelist. Rejected: definitions are constructed per run, so an admin edit mid-run would leave a stale value, and it reintroduces the divergence risk the single-source-of-truth design removes.

---

## D5. Reworking pause/resume

**Decision**: Extend `HumanInputRequestedError` with the pending action(s); persist proposals and decisions as JSON on `assignment_runs`; match on **`tool_call_id`**, never on tool name.

**Rationale**: Every element of the current resume path is hard-wired to free-text `request_human_input`:

- Matching walks `run.messages` in reverse, stops at the first message with any `tool_calls`, and looks for one literally named `"request_human_input"` (`assignment_engine.py:3154`). An approval raised from `github_create_issue` would never match; `hitl_tc_id` falls back to the string `"hitl"` — an invalid `tool_call_id` that orphans the block.
- The injected message is hardcoded `role="tool"`, `name="request_human_input"`, content `f"Human response: {…}"` (`:3163-3165`). No branch can re-execute with edited args or synthesise a denial.
- Storage is a single `Text` column `human_input_response` (`models/assignment.py:458`); `human_input_history` (`:466`) records only `resolution ∈ {"answered","dismissed"}`.
- `RespondToHumanInputRequest` accepts only non-empty text (`assignments.py:1401-1404`).
- `HumanInputRequestedError` (`base.py:346`) carries only `question` — nothing about the proposed action survives the pause.

Good news: `serialize_messages` (`builtin_tools/prompting.py:352-370`) already persists `tool_calls` with full `arguments`, so the proposed arguments survive the pause without new serialisation work.

**Backward compatibility** (FR-011): the `request_human_input` builtin keeps working unchanged. The resume path branches on whether structured `approval_decisions` are present; if absent, it falls through to today's free-text behaviour. Existing paused runs mid-upgrade therefore resume correctly.

---

## D6. Determining run interactivity

**Decision**: A run is **interactive** iff `triggered_by_user_id` was supplied at creation; otherwise **autonomous**. This distinction drives **notification only** — it does not affect whether a standing exception applies.

> **Revised after Phase 0** (product decision, 2026-08-12): an earlier version of this feature had autonomous runs ignore standing exceptions. That was reversed — an exception now applies to every run carrying the user's identity, scheduled ones included (FR-015b), with the reviewer told of that reach at grant time (FR-015c). Interactivity therefore no longer appears in the gate decision table; it only determines whether a pause notifies the assignment owner (FR-015d).

**Rationale**: Run identity precedence is `triggered_by_user_id or assignment.run_as_user_id or assignment.user_id` (`assignment_engine.py:4327`, mirrored in `autonomous.py:229` and `:4731`). Only the first arm implies a human initiated this run and is plausibly watching. Schedule/webhook/integration_event triggers have no `triggered_by_user_id` (`assignment_engine.py:4716` notes exactly this for webhooks).

Absent signal → autonomous → notification is sent rather than skipped, which is the safe direction for this use. `Assignment.trigger_type` (`models/assignment.py:120`: `"schedule" | "webhook" | "integration_event" | "manual"`) is a useful cross-check but is a property of the *assignment*, not the *run* — a scheduled assignment can still be triggered manually, so the run-level signal is authoritative.

**Note on a related-but-different signal**: `deps.py:187` tags `auth_credential_kind ∈ {session_jwt, api_key, platform_jwt}` to distinguish an interactive session from a machine credential, and `user.impersonated_by` (`deps.py:244-245`) is set transiently from the JWT. Those describe the *HTTP caller*, not the *run*, and must not be conflated — but `impersonated_by` is exactly what FR-017b needs for dual attribution.

---

## D7. Standing exceptions: storage and identity

**Decision**: New table `tool_approval_preferences`, keyed `UNIQUE (user_id, tool_key)`, holding a fingerprint of the approved tool shape. Owner is `run.user_id`.

**Rationale**: The approver identity must be `run.user_id` — that is what `_publish_run_status` and `deliver_notification` already target, and what `require_run_access` enforces on `/runs/{id}/respond` (`assignments.py:1390`). Research flags explicitly that `effective_user_id` (which falls back to `agent.user_id`, `assignment_engine.py:1270`) is **not** the notification/permission identity, and the two must not be crossed.

A dedicated table over a JSON column on `users` (which does hold blobs like `ga_builtin_tool_ids`, `models/user.py:168`): exceptions need individual revocation, uniqueness under concurrent grants, fingerprint comparison, and audit joins. `UNIQUE (user_id, tool_key)` also makes the concurrency story trivial (upsert), satisfying Principle II without a lock.

The fingerprint stores a hash of the tool's `input_schema`, implementing FR-019a: cosmetic edits (description, display name) do not change it; a parameter-schema change does, invalidating consent. Whitelist membership is deliberately **not** part of the fingerprint — whitelisting a tool makes the exception moot rather than invalid, since a whitelisted tool never gates for anyone.

**Audit** (FR-018, FR-017b): the existing `AuditLog` model (`models/audit_log.py:13`) already carries `user_id`, `user_email`, `action`, `entity_type`, `entity_id`, `summary`, and a `details` JSON — sufficient for grant/revoke/invalidate records with the impersonating admin in `details`.

---

## D8. `action_proposal` event delivery

**Decision**: Publish via a helper mirroring `_publish_run_status`, carrying `type: "action_proposal"` on the existing Redis channels.

**Rationale**: `_publish_run_status` (`assignment_helpers.py:189-232`) publishes JSON to `activity:{user_id}`, or `activity:company` when `is_company_managed`. The SSE endpoint `GET /api/v1/activity/stream` (`api/v1/activity.py:1184`) is a **raw passthrough** — it emits no SSE `event:` names, so all routing happens via the JSON `type` field. The frontend already switches on `data.type` in `useActivityStream.ts:53-56` (currently only `background_task_update`), so adding a case is the whole client-side change.

**Alternatives considered**: A dedicated SSE endpoint or named SSE events — would fork the transport for one event type and require a second connection per client.

---

## D9. Notification for autonomous pauses (FR-015d)

**Decision**: Reuse `deliver_notification` (`notify.py:29-84`), and explicitly handle the company-managed gap.

**Rationale**: HITL pauses already force `run.notification_sent = True` (`assignment_engine.py:3349`) and call `deliver_notification` (`:3382`), which persists a durable `Activity` row titled `"{assignment.name} — awaiting input"` (`notify.py:114-115`) and dispatches to Teams/Slack/WhatsApp/email per `assignment.trigger_config["notify_via"]`.

**The gap**: `_persist_notification_activity` is **skipped entirely** when `run.is_company_managed` or `run.user_id` is falsy (`notify.py:120-125`) — company-managed runs get only an ephemeral `activity:company` broadcast. For an interactive run someone is watching, that is acceptable. For an **autonomous** company-managed run — precisely the case FR-015d exists for — it means the pause is invisible to anyone not connected at that instant, and the run stalls silently. The plan therefore requires a durable record for autonomous pauses regardless of `is_company_managed`.

---

## D10. Where the gate's switch lives, and how an upgrade leaves it off

**Decision**: A nullable `agents.approve_tool_calls` plus an install default `platform_settings.approve_tool_calls_default`, added by migration `j1atc0173`: the default column is added `FALSE` on the existing row, then its column default is set `TRUE`.

**Rationale**: `human_in_the_loop` already means the prompt-driven review, so reusing it would switch on a hard gate for every agent and assignment an operator had enabled for a different purpose (the Mail Desk "Draft replies" assignment ships with it). The install default has to be decided per install, not per row: an upgraded install must leave agents created after the upgrade ungated too, which a per-row column default cannot express. The two-statement add is metadata-only and writes no agent row; `create_all` on a fresh install takes the model default `TRUE`.

**Alternatives considered**: A column default `FALSE` then `TRUE` on `agents` — gates every agent created after an upgrade, which the maintainer decision excludes.

---

## D11. Answering from the executions list (FR-026–FR-037b)

### Decision: extend the existing run endpoints; add one batch, no new state

The read path is already there. `AssignmentRunDetailResponse` carries `pending_approvals`, and the
executions list already fetches `awaiting_human` runs through `useAllAssignmentRuns`. So FR-027's
full-argument disclosure needs **no new endpoint and no second fetch**.

The write path is also there. `POST /runs/{run_id}/respond` accepts `body.decisions` and calls
`_apply_decisions_to_run` → `_grant_standing_approvals` → `resume_assignment_run`
(`assignments.py:1663`). That last call is why FR-037 costs almost nothing: "approve always" is
already carried by the decision shape, so offering it from a new surface is a UI affordance over
an existing contract rather than a new mechanism. FR-029's "one path, not two" is therefore the
default outcome rather than something to engineer.

What is genuinely missing is **one batch endpoint**. A group of N spans N runs, and N sequential
POSTs give no atomic per-member reporting, resume runs in an order the reviewer did not choose, and
turn one partial failure into an ambiguous half-finished state. FR-033 wants per-member results
from one action.

**Rationale**: the alternative — a client loop over the existing endpoint — was rejected on FR-033
alone. It can report per-member results, but only by inventing its own aggregation, which puts the
"which ones failed and why" logic in the browser where it cannot be tested against the real
concurrency it exists to handle.

**Alternatives considered**: a new aggregated `GET /assignments/runs/pending-approvals` was
considered and rejected — it would duplicate a listing that already exists and would need its own
pagination story. The 20-run cap on the existing call is a real limit (see D13), but it belongs to
that listing and should be fixed there rather than routed around.

---

## D12. `RunCard` was one prop from already doing this

`RunCard` accepts an `onApprove` callback and renders `ActionProposalList` when it
is given one. The executions page never passed it, so a run the viewer could answer displayed
*"Waiting for approval from someone with access to this run."* — to the person with access. Passing
the prop is most of FR-026.

**Authorisation reuses `require_run_access`** (FR-008b): the owner for a personal run, any
administrator for a company-managed one. Applied per member in the batch, so a caller gains no
reach over a run by bundling it with ones they own (FR-028).

---

## D13. Finding what is waiting (FR-035)

**Decision**: the executions list's existing `status=awaiting_human` filter, plus a shortcut that
selects exactly the visible rows carrying proposals.

**Rationale**: approvals sit interleaved with completed-run notices that cannot be answered at all,
so "select all" is rarely the useful selection. Filtering narrows; the shortcut selects.

**Alternatives considered**: a badge counting pending approvals was considered and dropped — it
answers "is anything waiting?" but leaves the reviewer to find them, which the filter does directly.

---

## D14. Disclosure without tracking (FR-034)

**Decision**: the executions list renders every pending proposal expanded, so FR-027's disclosure is
satisfied by construction and nothing is tracked.

**Rationale**: the requirement is that the reviewer was *shown* each proposal. A surface that always
shows them needs no evidence that it did. Tracking would also claim the stronger thing — that they
read it — which nothing can know.

**Alternatives considered**: a `reviewed_at` per proposal on the server was rejected as
unenforceable (any client can post it) and as implying a server-side guarantee it is not.

---

## D15. Optimistic answering and the already-answered race (FR-030, SC-023)

**Decision**: the server stays the arbiter. `respond` already refuses a run whose status is not
`awaiting_human`, which is the whole guarantee — two tabs cannot both
succeed. The work is in the *reporting*: distinguish "already answered", "run cancelled" and "run
failed" so FR-030's "tell the reviewer which of those happened" is answerable, where today all
three collapse into one 400.

**Alternatives considered**: a client-side guard was rejected as unable to see another tab.

---

## Resolved unknowns

| Unknown from Technical Context | Resolution |
|---|---|
| Where a gate can be inserted without being bypassable | D1 — `CompositeToolExecutor`, post-alias-normalisation |
| Whether mid-batch gating is safe | D2 — it is not; pre-screen the batch |
| How exemption is decided and stored | D3 — computed whitelist, shipped default plus admin overlay, no data migration |
| Whether a connected platform's tools can be exempted at all | D3a — its read-classified ones are, derived from the existing classifier; everything that changes the platform stays per-platform (FR-021a) |
| Whether `ToolDefinition` needs new fields | D4 — no; membership is derived, keeping one source of truth |
| Whether the resume path can express approve/edit/reject | D5 — no; rework required, with fall-through for backward compatibility |
| How to tell interactive from autonomous runs | D6 — `triggered_by_user_id` presence |
| Where standing exceptions live and who owns them | D7 — new table, owner is `run.user_id` |
| How to deliver `action_proposal` | D8 — existing Redis/SSE passthrough, new `type` |
| Whether autonomous pauses notify reliably | D9 — not for company-managed runs today; must be fixed |
| How an upgrade leaves every agent ungated | D10 — own switch plus an install default added `FALSE` |

No `NEEDS CLARIFICATION` items remain.
