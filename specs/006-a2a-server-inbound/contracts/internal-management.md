# Contract: Exposure Management (internal, session-authenticated)

**Spec**: FR-001..FR-009, FR-034, FR-041, FR-044 | **Plan**: [../plan.md](../plan.md)

Authenticated with the platform's normal session identity. No new permission is introduced — authority is `can_manage(user, agent, group_ids)`, unchanged (FR-002).

**Exposure is not an operation of its own.** An agent is reachable exactly while it binds an A2A integration (`oauth_a2a` or `entra_a2a`), so exposing and un-exposing happen through the ordinary agent-update call that edits `integration_bindings` — the agent configuration's Tools tab — and a session flush hook keeps the exposure row in step wherever that edit comes from. This surface therefore has one endpoint: a **read**. There is deliberately no expose/revoke pair beside it, because two ways to reach one state disagree the moment either is used — an exposure created without a binding is reachable with nothing to show for it in the UI, which is the drift a one-time migration (`a2a2intmodel1`) had to clean up once already.

## Operations

| Operation | Authority | Notes |
| --- | --- | --- |
| Read an agent's exposure state | `can_manage` | Card URL and run-as identity; `404` when the caller may not manage the agent, so "not exposed" / "not yours" / "does not exist" stay indistinguishable (SC-008) |
| Expose an agent | `can_manage` | **Not an endpoint.** Bind an A2A integration through the agent-update API |
| Revoke | `can_manage` | **Not an endpoint.** Unbind the last A2A integration, or let automatic revocation fire |

Company-managed agents are administrator-only, inherited from `can_manage` rather than special-cased. Built-in agents are company-managed and therefore administrator-only too; the read-only rule that otherwise applies to them is set aside for their integration configuration (FR-044), of which an A2A binding is one — that rule guards the seeded definition the startup resync rewrites, and integrations are not part of it.

## Responses

| Condition | Status |
| --- | --- |
| Success | `200` |
| Agent not visible to the actor, or the actor lacks `can_manage` | `404` |
| Feature flag off | `404` — the capability does not exist for this deployment |

A denied *change* is still a `403` and still audit-logged (FR-003) — it is now raised by the agent-update API, which is where the change is attempted.

## Side effects

Both directions are written by the flush hook (`a2a.hooks`), not by a request handler, so they hold for every path that edits bindings — the REST update handler, an import, a flow.

- **Binding gained**: mints a fresh `card_token`, resolves and records the run-as identity (FR-029), and notifies the agent's owner **and** the run-as user when they differ (FR-005). `exposed_by_user_id` is left null: a `before_flush` listener has no request context to read an actor from, and who made the binding edit is recorded in the agent's own audit trail instead. Counted as `a2a_exposure_changes_total{action="expose",reason="bound"}`.
- **Last binding removed**: revokes with reason `unbound` — sets `revoked_at`, deletes any legacy per-exposure OAuth client, stops the card URL resolving, notifies owner and run-as user. Idempotent, which matters because automatic and implicit revocation can race across replicas (Principle II).
- **Automatic revocation** (FR-041): fires when the agent's `is_company_managed` flips, its group or per-user sharing changes, its owner is transferred, or it is disabled or deleted. Audit-logs the triggering change and notifies. **Applies in both directions** — narrowing revokes as surely as widening, so no live exposure ever changes what a partner can reach.
- **Run-as user deactivated**: not a revocation. Enforced as a live filter on every resolution (FR-007), because deactivation is reversible and revocation is not.

## Credential and URL delivery

The **card URL** is per-agent and re-displayable: it comes back on every status read, because it is an identifier rather than a credential. Re-binding after any revocation mints a new one and every previous value stays permanently dead (FR-042), so the partner must be sent the new URL.

The **partner credential** is not per-agent and is not delivered here. An `oauth_a2a` integration *is* the credential: creating one mints a client id and secret, the secret is shown once on the integration, and it is valid on every agent that integration is bound to. An `entra_a2a` integration carries no credential at all — only the pins that admit the partner's own Entra token. Legacy per-exposure credentials minted by the old explicit expose endpoint keep working and their client id still comes back on a status read; nothing mints new ones. There is no re-display of any secret and no rotation-in-place this phase, beyond regenerating an `oauth_a2a` integration's credential (which re-keys every agent it is bound to at once).
