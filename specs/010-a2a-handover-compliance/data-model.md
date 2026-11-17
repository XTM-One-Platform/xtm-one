# Phase 1 Data Model: A2A-Compliant Handover

**Date**: 2026-09-07 | **Plan**: [plan.md](./plan.md) | **Spec**: [spec.md](./spec.md)

Two new tables, a foreign key over an existing column, and one partial index.
No existing column changes meaning.

---

## New: `a2a_contexts`

The protocol's conversational grouping, promoted from a per-task random value to
an entity that owns who may continue it and who answers.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | UUID | no | PK — the `contextId` partners see. Minted here and never accepted from a partner (FR-008, FR-009) |
| `client_id` | String(255) | no | indexed. The registration that created it; the only one that may read or continue it (FR-010) |
| `responsible_agent_id` | UUID FK → `agents.id` | no | `ondelete="CASCADE"`. The agent answering follow-ups, moved by a transfer (FR-011) |
| `exposure_id` | UUID FK → `a2a_exposures.id` | no | `ondelete="CASCADE"`. The card the partner addressed, which governs a follow-up's contract |
| `created_at` / `updated_at` | DateTime(tz) | no | platform convention |

**Indexes**: `(client_id, id)` — every read is ownership-scoped, mirroring the
composite already on `a2a_tasks`.

**Invariants**

1. A context is created only by a submission carrying no `contextId`. A submission
   carrying one that does not resolve for that `client_id` is answered as
   not-found, indistinguishably from a foreign one (FR-009).
2. `responsible_agent_id` starts as the addressed agent and follows a transfer.
   Consultation and delegation do not move it — neither claims future messages.
3. `exposure_id` is frozen at creation. A transfer changes who answers, not which
   card the partner validated against.

**Lifecycle**

```
(none) --submission without contextId--> live --exposure revoked / agent deleted--> gone (cascade)
                                          |
                                          +--transfer--> live, responsible_agent_id moved
```

No terminal state and no independent purge: a context lives as long as the
exposure it was created under, matching `006-a2a-server-inbound` FR-038's rule
for the tasks it groups.

---

## New: `a2a_push_configs`

A partner's callback for one task.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | UUID | no | PK — the `pushNotificationConfig` id the protocol addresses |
| `task_id` | UUID FK → `a2a_tasks.id` | no | indexed, `ondelete="CASCADE"`. Configs die with their task |
| `client_id` | String(255) | no | The owning registration; a config is invisible to any other (FR-010's rule, applied here) |
| `url` | Text | no | The callback address, validated before storage and again before each send (FR-019) |
| `token` | Text | yes | Fernet-encrypted. The opaque value the partner asks to have echoed back, when it supplies one |
| `auth_scheme` | String(40) | yes | The scheme the partner's endpoint expects, when it registered one instead of a bare token |
| `auth_credentials` | Text | yes | Fernet-encrypted. Presented under `auth_scheme` |
| `delivery_state` | String(20) | no | `pending` \| `delivered` \| `failed` — what the last attempt achieved |
| `attempts` | Integer | no | default 0. Bounded by the retry setting (FR-020) |
| `created_at` / `updated_at` | DateTime(tz) | no | platform convention |

**Indexes**: `task_id`; partial index on `delivery_state = 'pending'` for the
delivery sweep.

**Invariants**

1. The number of rows per `task_id` is bounded (FR-022); a registration beyond
   the bound is refused, not silently dropped.
2. `token` and `auth_credentials` are credential material, encrypted at rest like
   every other secret (Principle III), presented to the partner and never logged.
   The platform holds no key of its own for this (research [R2](./research.md)).
3. A row is never consulted to decide the task's own outcome (FR-021).

---

## Modified: `a2a_tasks`

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `context_id` | UUID FK → `a2a_contexts.id` | no | `ondelete="CASCADE"`. A row rather than a free-standing per-task UUID, so several tasks can belong to one context |

The existing `context_id` column already carries the right name and type, so this
is a foreign key added over populated data. Existing rows hold values that
correspond to no context row, so the migration creates one context per distinct
existing value — preserving the identifier a partner may already hold — before
adding the constraint.

**Derived, not stored**: the task's state now folds in its delegated work
(FR-001). Nothing about that is persisted on the task; `resolve_state` gains a
second input, keeping one source of truth per
`006-a2a-server-inbound`'s data model.

---

## Modified: `sub_agent_tasks`

No column change. One partial index, on the expression the run→children lookup
already matches:

```
metadata_json->>'parent_run_id'  WHERE status IN ('pending','running')
```

Rationale in research [R1](./research.md). It serves the FR-001 read on the
polling path and the FR-004 cancel cascade equally.

## Read-only dependencies

| Model | Used for |
| --- | --- |
| `A2AExposure` | the card a context was created under; revocation cascades |
| `Agent` | the responsible agent's identity and configuration |
| `AssignmentRun` | the run whose children FR-001 reads |
| `SubAgentTask` | the delegated work itself — status, result, error |
