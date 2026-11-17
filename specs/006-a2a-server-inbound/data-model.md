# Phase 1 Data Model: Minimal A2A Server (Inbound)

**Date**: 2026-08-18 | **Plan**: [plan.md](./plan.md) | **Spec**: [spec.md](./spec.md)

Two new tables and one nullable foreign key on an existing table. Everything else is read from models that already exist.

---

## New: `a2a_exposures`

The per-agent opt-in register. One row per exposure attempt; a revoked row is retained for audit rather than deleted, which is what makes FR-042's "a revoked URL never resolves again" enforceable — the retired token stays on record and can be recognised as dead.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | UUID | no | PK |
| `agent_id` | UUID FK → `agents.id` | no | `ondelete="CASCADE"` — FR-007's "agent deleted" case |
| `card_token` | String(64) | no | unique, indexed. Opaque, minted fresh per exposure (FR-012, FR-042) |
| `run_as_user_id` | UUID FK → `users.id` | no | the identity external work executes as, resolved ONCE when the agent becomes reachable (FR-029..FR-031). `ondelete="CASCADE"` — FR-007's "user deleted" case. NOT NULL: an external task has no invoking human to fall back to. Always a real user, never a `boundary_kind`-style company scope — a company-managed agent has no data access of its own anywhere in this platform, so the identity is inherited from the agent's owner (mirrors `Assignment.run_as_user_id`) |
| `exposed_by_user_id` | UUID FK → `users.id` | yes | the actor; recorded for audit, **never** used for access (FR-029). NULL on everything created since exposure became implicit: a `before_flush` listener has no request context to read an actor from, and who edited the bindings is in the agent's own audit trail |
| `revoked_at` | DateTime(tz) | yes | NULL = live. Revocation is a state change, not a delete |
| `revoked_reason` | String(40) | yes | `unbound` \| `tier_change` \| `owner_change` \| `sharing_change` \| `agent_disabled`, plus historical `manual` on rows the removed expose/revoke endpoints wrote. No `owner_deactivated`: FR-007's deactivated run-as user is a live filter on resolution, not a revocation, because deactivation is reversible |
| `created_at` / `updated_at` | DateTime(tz) | no | platform convention |

**Indexes**: unique on `card_token`; partial index on `agent_id WHERE revoked_at IS NULL` (at most one live exposure per agent, and the lookup every management check makes).

**Invariants**

1. At most one row per `agent_id` with `revoked_at IS NULL`. Enforced by a partial unique index, not by application check — two concurrent exposure grants on different replicas must not both win.
2. Personally-owned agent ⇒ `run_as_user_id` = the agent's `user_id` at exposure time, never `exposed_by_user_id` (FR-029, FR-030). An owner transfer therefore auto-revokes (`owner_change`).
3. Company-managed agent ⇒ `run_as_user_id` = the agent's stored `user_id` (the seeding admin), inherited and never nominated (FR-031). A transfer does **not** revoke: the identity was pinned on the exposure, so the agent's provenance moving does not move what a partner reaches. Where `user_id` is NULL there is nothing to inherit and no exposure is created. Legacy rows may carry an administrator's earlier explicit nomination.
4. `card_token` is never reused across rows, including revoked ones (FR-042).

**Lifecycle**

```
(none) --bind--> live --unbind/auto-revoke--> revoked  [terminal]
                     |                            ^
                     +--agent tier/sharing change-+   (FR-041, automatic)
                     +--agent owner transferred---+   (FR-041, automatic)
                     +--agent disabled/deleted----+   (FR-007 / FR-041)
```

A deactivated run-as user is deliberately NOT a revocation: external resolution
filters on `users.is_active` (FR-007's "cease resolving"), so the exposure goes
dark everywhere at once and comes back if the account is reactivated —
deactivation is reversible, revocation is not.

Revoked is terminal. Re-exposing creates a **new row** with a new `card_token` and new credentials — never a resurrection of the old one.

---

## New: `a2a_tasks`

The protocol-shaped view of an external submission. Deliberately thin: it references the `AssignmentRun` that holds execution state rather than mirroring it, so the two can never disagree (research R4).

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | UUID | no | PK — the task id partners see |
| `context_id` | UUID | no | indexed. A2A conversational grouping |
| `exposure_id` | UUID FK → `a2a_exposures.id` | no | the exposure it was submitted against |
| `client_id` | String(255) | no | indexed. The registration that created it (FR-021) |
| `run_id` | UUID FK → `assignment_runs.id` | yes | `ondelete="SET NULL"`. NULL only between row creation and run creation, and for `REJECTED` |
| `responding_agent_id` | UUID | yes | which agent produced the answer, when a transfer moved it off the agent the partner addressed. NULL = no transfer, so NULL is not "unknown". No FK: it attributes a historical task, and deleting the agent later must neither erase that nor block the delete |
| `message_id` | String(128) | no | the client-minted id deduplication keys on (FR-043) |
| `inbound_parts` | JSON | yes | the verbatim submitted content |
| `rejected_reason` | String(40) | yes | set when refused before a run started → `REJECTED` |
| `cancel_requested_at` | DateTime(tz) | yes | cross-replica cancel signal (research R6) |
| `lease_heartbeat_at` | DateTime(tz) | yes | indexed. Execution lease: refreshed every quarter of `a2a_run_lease_seconds` by the run supervisor; the reaper drives runs with a stale lease terminal (SC-002), and the generic stale-run sweep skips runs whose lease is fresh |
| `reaped` | Boolean | no | default false — marks tasks the reaper already finalized so they are not reclaimed |
| `created_at` / `updated_at` | DateTime(tz) | no | |

**Indexes**: PARTIAL unique on `(client_id, message_id) WHERE rejected_reason IS NULL` — the durable half of deduplication (Redis provides the fast path, the constraint the guarantee), partial so a rejection does not consume the message id and a corrected re-send under the same id succeeds. Composite on `(client_id, id)` for the ownership-scoped lookup every read performs.

**Derived, not stored**: the A2A task **state** and **artifacts** are projected from `run_id`'s `AssignmentRun` at read time via the mapping in research R4. Storing them would create two sources of truth for the same fact.

**Retention**: no independent purge. Rows live as long as the run they mirror (FR-038).

---

## Modified: OAuth client registration

The existing client table gains one nullable column.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `a2a_exposure_id` | UUID FK → `a2a_exposures.id` | yes | `ondelete="CASCADE"`. NULL for every pre-existing MCP OAuth client, and for every A2A credential minted since the partner relationship moved onto an integration |
| `a2a_integration_id` | UUID FK → `integrations.id` | yes | `ondelete="CASCADE"`. Set on an `oauth_a2a` integration's credential: one registration per **partner**, valid on every agent that binds it |

Nullable is load-bearing on both: existing MCP OAuth clients have no exposure and must keep working untouched. A row with either set is an A2A partner credential — `grant_types = ["client_credentials"]` only, and dying with whichever parent it hangs from (FR-040, FR-006).

The two columns are the two eras. `a2a_exposure_id` is per-agent, minted by the removed expose endpoint; nothing creates new ones, and existing rows stay valid credentials bound to precisely one exposure. `a2a_integration_id` is per-partner and is the accepted widening of that blast radius: a partner integrating with three agents holds one secret instead of three — which is what makes the credential manageable once exposure follows bindings — but a leak reaches all three and regenerating re-keys all three.

---

## Read-only dependencies

| Model | Used for |
| --- | --- |
| `Agent` | `user_id`, `is_company_managed`, `shared_group_ids`, `shared_group_permissions`, `enabled`, plus name/description/tools for the card |
| `User` | authority evaluation via `can_manage()`; `is_active` gates external resolution (FR-007) |
| `Group` | authority evaluation via `get_group_permission()` |
| `AssignmentRun` | execution state, `status`, `result_summary`, artifacts. CREATED by this feature (with `assignment_id = NULL`, `user_id = run_as_user_id`) but its schema is untouched |

No existing schema is modified except the one nullable column above.

---

## Boundary resolution

The single rule the whole feature turns on, stated once as a function of existing fields (`exposure.resolve_run_as`):

```
resolve_run_as(agent):
    if agent.user_id is None:
        raise RunAsRequiredError        # nothing to inherit: no exposure
    return agent.user_id                # FR-030 — never exposed_by_user_id
```

**The signature is the guarantee.** It took a `requested` override while an
expose endpoint existed to supply one; that parameter went with the endpoint
rather than remaining as a choice the platform appears to offer and no surface
can make. There is nothing to pass but the agent, so the identity cannot come
from anywhere else — least of all from whoever edited the bindings.

A company-managed agent (**every built-in is one**) therefore inherits its
stored owner, which is whoever seeded it. That is an accepted trade-off rather
than a good answer, and the reason the resolved identity is surfaced in the UI,
notified to the people it concerns, and recorded in the audit trail rather than
left implicit; see the 2026-08-25 clarifications.

There is NO separate visibility filter: external work runs through
`run_agent_chat` as the recorded user and sees exactly what that user sees,
resolved the same way chat, `/mcp/user` and assignments already resolve it.
Inventing an A2A-specific filter would have meant a second, divergent access
mode. Nor is the tool surface bounded per-exposure: the task path passes no
`session_*_ids` at all, so the agent's own configuration — and every handover
peer it has — is what runs, under that identity (see `a2a.exposure`).

---

## Validation rules

| Rule | Requirement |
| --- | --- |
| Exposure requires `can_manage(actor, agent, group_ids)` | FR-002, FR-003 |
| Company-managed agent requires `actor.role == "admin"` (inherited from `can_manage`) | FR-002 |
| Submission rejected when the agent cannot accept the inbound content type | FR-024, edge case |
| Structured output rejected unless the agent is configured to produce it | FR-015, FR-024 |
| `(client_id, message_id)` collision returns the existing task | FR-043 |
| Task read requires `task.client_id == caller` | FR-021 |
| Every negative outcome returns the same not-found shape | FR-026 |
