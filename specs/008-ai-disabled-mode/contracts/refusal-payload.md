# Contract: what a refusal looks like

> **Surface names** follow [spec.md](../spec.md).

One shape for every write or execution refused because AI is disabled, so a client
branches on a value rather than matching prose (FR-009).

## The response

```
403 Forbidden
```

```json
{
  "detail": {
    "code": "ai_disabled",
    "message": "AI is disabled on this deployment. …"
  }
}
```

Nested under `detail` because that is what FastAPI emits, and the repo already
uses this shape for structured errors (`api/v1/flow_runs.py:313`). An earlier
draft of this contract showed `code` at the top level, which no handler in this
application produces.

`detail.code` is the part to branch on. `detail.message` stays human-readable:
no consumer in this repository parses the message today — the frontend reads
`ai_enabled` from the platform config instead — but an external client may be
displaying it, so it is added beside the prose rather than replacing it.

## Where it comes from

The two existing gating dependencies, `require_agentic_ai` and
`require_chat_enabled` (`backend/app/api/deps.py:399`, `:420`), which are already
attached at **83 route call sites**. Extending them reaches every gated route
without editing any, and means a route gated for licensing is gated for this mode
automatically — which is what FR-010 needs, since a route that forgot to opt in is
exactly how an execution path stays open.

Do not add a third gating mechanism. `platform.py:320-345` maintains a documented
map of feature keys to these gates; it is updated alongside, and it is the artefact
that would otherwise go stale.

## What must be refused

Creating, changing, deleting or executing anything requiring AI, from **every**
entrypoint — interactive use, scheduled work, integration-driven calls, API tokens
(FR-010). Also the MCP endpoint, entirely (FR-029): it exists to be consumed by a
model, so on a deployment chosen because model access is prohibited, any part left
live keeps open the path the mode exists to close. It keeps refusing with this
payload rather than disappearing, so a client that worked yesterday is told why.

## What must not be refused

Reading AI resources (FR-008). Listings and detail views stay `200` — that is the
browsable surface the mode is built around, and refusing reads would deliver the
blank, broken-looking install the feature exists to prevent.

Everything unrelated to AI (FR-021): authentication and single sign-on, users,
groups, roles, invitations, profiles, platform settings and branding, audit logs,
connected-platform registration, health and metrics. Plus deterministic
automation — flows with no AI step, their triggers, integration syncs (FR-014).

## Two failure modes this contract exists to prevent

- **A route that 500s** because a provider is missing rather than refusing
  cleanly. SC-004 admits none.
- **A route that silently succeeds** while doing nothing, which reads as working
  and is worse than a refusal.

## Flows are recorded, not refused

A flow containing an AI step is not "refused" — nobody is waiting on a response.
It is **not dispatched**, and recorded as skipped with a reason readable where
runs are reviewed (FR-016). No step of it executes: it must never start and fail
part-way, which is the whole reason the verdict is computed before dispatch rather
than discovered during it.
