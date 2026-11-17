# Contract: REST API


> **Surface names** follow the `Approval Surface` entity in [spec.md](../spec.md):
> browser socket, REST, platform stream, chat channels, public embed. "Chat" alone
> spans the first four, and "chatbot" means the external `filigran-chatbot`
> package — nothing in this repository.

Endpoints added or changed. Existing paths keep their current auth dependencies (`require_run_access`, `get_current_user`) unless stated.

---

## 1. `GET /api/v1/assignments/runs/{run_id}/pending-approvals` — **NEW**

Returns the structured proposals for a paused run (FR-006).

**Auth**: `require_run_access(run, user, action="respond to")` — same rule as `/respond` (`assignments.py:1390`): owner-only for personal runs, admins for `is_company_managed`.

**200**
```json
{
  "run_id": "uuid",
  "status": "awaiting_human",
  "kind": "action_proposal",
  "proposals": [
    {
      "tool_call_id": "call_abc123",
      "tool_name": "opencti_delete_entity",
      "tool_description": "Permanently delete an entity from the platform.",
      "arguments": {"entity_id": "…", "cascade": true},
      "input_schema": {"type": "object", "properties": {"…": {}}},
      "source": "integration:opencti"
    }
  ]
}
```

`kind` is `"action_proposal"` for gated tool calls and `"free_text"` for a classic `request_human_input` pause (FR-011). Clients MUST branch on it rather than assuming one shape.

**409** if the run is not `awaiting_human`.

---

## 1b. `GET /api/v1/platform/chat/conversations/{conversation_id}/pending-approvals` — **NEW**

The embedded-assistant twin of the above. A chat prompt arrives as one event on a
stream, so a client whose page reloaded holds nothing — not even the
`tool_call_id`s a decision must name. This returns what the event carried:

```json
{ "conversation_id": "9f1c...", "proposals": [ /* as sent */ ] }
```

An empty `proposals` is the ordinary answer, not an error. Ownership is checked
as on the decision endpoint, and an unknown or foreign conversation is 404 rather
than 403 so the endpoint never confirms someone else's conversation exists.

## 2. `POST /api/v1/assignments/runs/{run_id}/respond` — **EXTENDED**

Today accepts only `{"response": "<non-empty text>"}` (`assignments.py:1367-1416`). Extended with an alternative body for approval decisions (FR-008). Exactly one of `response` / `decisions` MUST be present.

**Request — approval decisions**
```json
{
  "decisions": [
    {"tool_call_id": "call_abc123", "decision": "approve"},
    {"tool_call_id": "call_ghi789", "decision": "reject",
     "rejection_reason": "Wrong target environment."},
    {"tool_call_id": "call_jkl012", "decision": "approve_always"}
  ]
}
```

**Rules**
- Every `tool_call_id` in `pending_approvals` MUST appear exactly once. A partial or duplicated set → **422**. Resuming with an undecided call would leave an orphaned `tool_use` block (research D2).
- A decision carries no arguments. The reviewer answers the call as proposed; an agent that proposed the wrong one is corrected by a reject with a reason (FR-010).
- `approve_always` upserts a `ToolApprovalPreference` for the **calling user** and approves this call (FR-015).
- The existing `{"response": "..."}` body remains valid and unchanged for free-text pauses (FR-011) — but is **refused with 400 on a gated pause**. Such a pause fills `human_input_question` too ("Approval required before running: ..."), so a classic client sees something answerable and sends prose; taking it would resume down the free-text branch and leave the gated `tool_use` blocks unanswered.

**200** — decisions recorded, run transitions to `running`, resume dispatched.
**400** if the run is not `awaiting_human`.
**422** on partial or duplicated decision sets.

---

## 3. `GET /api/v1/tool-approvals` — **NEW**

The caller's own active standing exceptions (FR-017). **There is no cross-user variant** — an administrator inspecting another user's preferences does so by impersonating them (FR-017a), which makes this same endpoint return that user's rows.

**200**
```json
{
  "approve_all_tools": false,
  "tool_approvals_enabled": true,
  "approve_tool_calls_default": true,
  "preferences": [
    {
      "id": "uuid",
      "tool_key": "opencti_create_report",
      "tool_source": "integration:opencti",
      "granted_at": "2026-08-12T09:00:00Z",
      "granted_by_admin_id": null,
      "stale": false
    }
  ]
}
```

`approve_all_tools` is the caller's own allow-all (FR-024). `tool_approvals_enabled` is the platform switch (FR-023),
carried here **only so the screen can explain why nothing pauses** when the user's own mode is off; it is read-only on
this endpoint and set through §9a. `approve_tool_calls_default` is the install default (FR-014), carried so an agent
page can say what "follow the install default" means; read-only here, set through §9a. A missing settings row reads as
`true` for both.

`stale: true` marks a preference whose fingerprint no longer matches the live tool — it will be invalidated on next use (FR-019a). Surfacing it lets the user see *why* a tool started prompting again (FR-019b).

---

## 4. `DELETE /api/v1/tool-approvals/{preference_id}` — **NEW**

Revokes one of the caller's own preferences (FR-017). Sets `revoked_at` with `revoked_reason = "user_revoked"`; the tool pauses again on the caller's next affected call.

**Auth**: the row MUST belong to the calling user. A preference belonging to anyone else → **404** (not 403 — do not confirm existence of another user's preferences).

**204** on success. Writes an `approval_revoked` audit record including `impersonated_by` when applicable (FR-017b).

---

## 4a. `PUT /api/v1/tool-approvals/mode` — **NEW**

Turns the caller's own allow-all on or off (FR-024). Self only — there is no variant that sets it for another user, for
the same reason §3 has no cross-user read.

**Request**
```json
{ "approve_all_tools": true }
```

**200**
```json
{ "approve_all_tools": true }
```

The flag only — **not** the §3 shape. A screen that also renders the preference list or the platform switch re-reads §3
after this call.

Standing preferences are **not** deleted when this is enabled (FR-024) — turning it back off restores the exact previous
posture. Writes an audit record either way (FR-025).

---

## 5. `GET /api/v1/hitl-whitelist` — **NEW** (admin)

The effective whitelist with provenance per entry (FR-001b).

**Auth**: administrator only.

**200**
```json
{
  "shipped_default_version": 1,
  "entries": [
    {"tool_key": "search_knowledge", "source": "shipped_default", "recognised": true},
    {"tool_key": "search_opencti", "source": "platform_read", "provider": "opencti", "recognised": true},
    {"tool_key": "my_read_tool", "source": "admin_added", "created_by": "…", "created_at": "…", "reason": "internal reporting only"}
  ],
  "platform_reads": {
    "opencti": {"exempt": ["search_opencti"], "regated": ["list_opencti_drafts"]}
  },
  "platform_reads_available": true,
  "removed_from_default": [
    {"tool_key": "list_agents", "created_by": "…", "created_at": "…", "reason": "leaks agent inventory"}
  ]
}
```

`source` distinguishes the two halves of the product default — `shipped_default` for the curated list, `platform_read` for a registered platform's read-classified tools (FR-001g), attributed to the platform that contributes it — from administrator additions. `removed_from_default` lists default entries the administrator has explicitly re-gated, so a reader can see the whole picture rather than only what is currently exempt.

`platform_reads` groups the derived half per platform, and reports the re-gated names alongside the exempt ones: that is the state in which the caller most needs to offer the control that restores them.

`platform_reads_available` is `false` when the registrations could not be read. This listing degrades rather than failing — a read-only screen showing the curated half beats an error page — but the flag is mandatory in the response because an empty `platform_reads` otherwise reads as "this deployment has none", which an operator would take as "they were re-gated". The **write** endpoints below take the opposite branch and refuse.

---

## 6. `POST /api/v1/hitl-whitelist` — **NEW** (admin)

Adds a tool to the whitelist, or clears an existing `remove` override.

```json
{"tool_key": "my_read_tool", "reason": "internal reporting only"}
```

**Rules**
- Upserts on `tool_key` via `ON CONFLICT` — if a `remove` override exists it flips to `add` rather than inserting a second row, and concurrent administrators converge instead of one hitting the unique constraint (Constitution II).
- Any tool may be added, including a destructive one (FR-003). The API does not refuse; the warning and audit record are the safeguards.
- Writes a `whitelist_tool_added` audit record.

**201** on success. **403** for non-administrators. **503** when the derived platform-read layer cannot be determined — see below.

### Why the write paths refuse (503)

Whether this call *adds* a tool or *undoes a removal* depends on current default membership, and half of that default is derived from the registered platforms. When those cannot be read, the honest answer is "ask me again": nothing is written, and the client retries.

This is not hypothetical caution. Absorbing the failure into "no platform reads" made `DELETE` conclude a derived read was already gated, write nothing, and answer 204 — the operator was told a tool now asks for approval while it went on running unattended once the database recovered. The gate keeps the opposite behaviour on purpose (unknown ⇒ exempt nothing ⇒ ask a human), which is safe precisely because it errs toward asking.

---

## 7. `DELETE /api/v1/hitl-whitelist/{tool_key}` — **NEW** (admin)

Re-gates a tool: records a `remove` override if the tool is in the product default — curated or a derived platform read — or clears an `add` override.

**The removal persists across upgrades** even if a later shipped default contains the tool (FR-001e), and across the platform reconnecting. Writes a `whitelist_tool_removed` audit record.

**204** on success.

---

## 7a. `POST /api/v1/hitl-whitelist/platform-reads/{provider}/require-approval` — **NEW** (admin)

Re-gates every read tool one registered platform contributes (FR-001h), writing the same durable `remove` overrides that re-gating one tool by hand records.

**200**
```json
{"provider": "opencti", "changed": 19, "tool_keys": ["get_opencti_entity", "…"]}
```

`changed` counts what actually moved, so a repeat call reports `0` rather than re-asserting the whole set. **404** if `provider` is not a Suite platform — a third-party integration's read tools are not governed by this control. **503** when the derived layer cannot be read: a bulk action answering `changed: 0` would read as "already done".

---

## 7b. `POST /api/v1/hitl-whitelist/platform-reads/{provider}/restore-default` — **NEW** (admin)

Undoes the above, clearing the `remove` overrides rather than pinning `add` overrides — so a tool reclassified as a write in a later release starts gating again instead of being frozen exempt by the click that restored it.

Same response shape, and the same **404** / **503** rules.

---

## 8. `POST/PUT /api/v1/custom-tools`

**Unchanged.** Custom tools gain no approval fields — they are whitelisted by name like every other tool (FR-004). This is a deliberate difference from the earlier classification design, which added two columns here.

---

## 9. `PUT /api/v1/integrations/{id}` — **EXTENDED**

Adds `requires_approval` (default `true`) — one toggle for the whole integration (FR-021). Per-tool granularity for integrations is explicitly deferred.

**Not for a registered Suite platform.** A platform's approval is one decision made per platform (Settings → Parameters), and the gate reads that policy alone for the providers a registration governs — including OpenAEV under its legacy `openbas` identifier. Sending `requires_approval` for such an integration returns **409 Conflict** rather than storing a preference nothing enforces.

**Read side** (`GET /api/v1/integrations`, `GET /api/v1/integrations/{id}`, and every response that returns an integration): `requires_approval` answers one question — do this integration's tools ask before running — so for a governed provider it reports the *platform policy*, not the inert stored column. `approval_managed_by_platform` says which layer answered, so a client can send the reader to the control that decides instead of offering one that would be refused. A listing resolves the platform layer once for the page, not once per row.

---

## 9a. `PUT /api/v1/settings/platform` — **EXTENDED**

Accepts `tool_approvals_enabled` (bool), the platform-wide kill switch (FR-023), and `approve_tool_calls_default`
(bool), the install default agents with no switch of their own follow (FR-014). `GET` returns both.

**Auth**: administrator only.

Omitting the key leaves the stored value alone. Rejects anything that is not a boolean with **400** rather than coercing
— a switch that silently read `"false"` as on is the failure this endpoint exists to avoid. Writes an audit record
(FR-025).

While it is off, the whitelist screens of §5–§7b remain reachable and keep answering truthfully; it is the *interface*
that hides them (FR-023), because they would be controls deciding nothing.

---

## 9b. `PUT /api/v1/agents/{agent_id}` — **EXTENDED**

Accepts `approve_tool_calls`: `true`, `false`, or `null` (follow the install default) — the agent's approval switch
(FR-012). Returned on every agent response. On a built-in agent in XTM One mode it is, with `enabled`, the only field
an administrator may write (FR-012b); any other field, `human_in_the_loop` included, is refused with **403**. The audit
record carries the value set (FR-012e).

---

## Endpoints deliberately NOT added

| Not added | Why |
|---|---|
| Per-agent or per-assignment whitelist endpoints | FR-001c — the whitelist is platform-wide; gating must be answerable without inspecting the caller. |
| A "protected tools" endpoint | FR-003 — no set is permanently non-whitelistable. |
| `GET /admin/tool-approvals` (cross-user listing) | FR-017a — administrators get no cross-user view; impersonation is the single audited path. |
| `DELETE /admin/tool-approvals/{id}` | Same. Revoking another user's preference happens while impersonating them. |
| Any timeout / auto-resolve endpoint | FR-015e — pending approvals never expire or auto-resolve. |
