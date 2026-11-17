# Contract: Internal management surface

**Feature**: [spec.md](../spec.md) | Serves FR-001–FR-008, FR-017–FR-020, FR-039–FR-044

How an operator creates, inspects and repairs a partner registration. The governing principle is that **most of this surface already exists**: a partner is an integration, so creation, editing, sharing, deletion and authority are the ordinary integration endpoints, and binding is the ordinary agent update.

What is genuinely new is a small read surface plus two repair actions.

## Reused, not rebuilt

| Operation | Existing surface |
|---|---|
| Create / edit / share / delete a registration | The integrations endpoints. Authority is the existing integration authority; a denial is audit-logged by the existing path (FR-002, FR-003) |
| Store credentials | Existing Fernet encryption on the `integrations` row. Never returned readable, never logged (FR-004) |
| Bind a partner to an agent | The ordinary agent update that edits `integration_bindings` / `integration_template_bindings` — the whole opt-in. What it confers follows the integration's direction (FR-005, FR-006, FR-055) |
| Test connection | The existing per-provider connectivity probe, registered via `register_connectivity`. The probe **is** the usability verdict (FR-020) |
| First verification on connect | The existing `register_lifecycle` `on_connect` hook |
| Catalog entry, credential fields, setup guide | Data in `integration_catalog.py`: the two shipped entries gain an outbound variant (fields and setup guide), and one entry is added for Google Agent Platform outbound (FR-056) |
| Change an integration's direction | The ordinary integration update, with one new refusal: denied while bound to any agent, naming them (FR-057). On success the previous direction's credentials are cleared (FR-058) |

**One shipped behaviour changes.** The exposure hook creates an exposure for any A2A binding, with no notion of direction. It must consult direction instead: an outbound binding exposes nothing. Without that, binding an integration to obtain a delegation tool would silently expose the agent — a fail-open regression, not a cosmetic gap. An A2A integration with no settings row is treated as neither direction.

**No new expose/revoke pair.** This is the inbound phase's most expensive lesson, applied pre-emptively: it shipped explicit expose and revoke endpoints alongside the binding hook, then removed them because two paths to one state disagree the moment either is used, and the endpoint-created state was invisible in a UI that derived truth from bindings. There is one way for an agent to gain outbound reach — a binding — and SC-007's zero-call-site sweep is what keeps it that way.

## New: `/api/v1/a2a/client`

Four operations. Each requires JWT auth and management authority over the registration; the mutating ones are audit-logged.

### `GET /{integration_id}` — verification state

Read-only. Returns what the verified card declares and the current verdict, so an operator sees what a partner is before binding it (FR-018) and why it is refusing work when it is:

- what the card declares: name, description, skills, accepted and produced content types
- `signature_state` — `verified` or `unsigned` (FR-017)
- `agreed_protocol_version`
- `usability`, `unusable_reason`, `last_verified_at`, `last_verification_error`
- circuit state: whether open, and until when
- whether a card change is awaiting acceptance

Contains no credential, no token, and no fragment of either — in the payload or in any error it can return (FR-004).

### `POST /{integration_id}/verify` — verify now

On-demand re-verification (FR-019). Also the operator's escape from a cooldown: a successful verification closes the breaker, so someone who has just fixed a credential is not made to wait it out (FR-030).

### `GET /{integration_id}/delegations` — what this partner was asked to do

Read-only, newest first, bounded server-side. Scoped by the `integration_id` recorded on each delegation at submission time — never by the agents the integration is bound to (FR-042).

### `POST /{integration_id}/accept-card` — accept a changed card

Accepts the currently observed card, setting `accepted_card_fingerprint` and resuming delegation (FR-019). Audit-logged with actor, registration and the fingerprint accepted.

Two refusals are deliberate:
- accepting a card whose signature is present and invalid — that state is unusable with no override (FR-015)
- accepting a fingerprint other than the one currently observed — a trust decision applies to one card, not to whichever arrives next (FR-017)

## Governing switch

A deployment-level switch, **off by default** and independent of the inbound switch (FR-007). While off: no outbound request is attempted, no sweep runs, and the delegation tool is not contributed to any run — regardless of registration or binding state.

## Audit and telemetry

**Audit-logged** (FR-039, FR-040), through the existing `a2a/audit.py`: registration creation, credential update, binding change, card-change acceptance, suspension (including a signature or protocol downgrade), deletion, and every delegation with its calling agent, calling user, registration, time and outcome. Where a change is applied by a hook with no request context, the record states what happened and leaves who to the surrounding trail — the same concession the inbound phase's FR-005 makes, for the same reason.

**Attribution** (FR-042): a registration's activity shows only its own delegations, keyed on the `integration_id` recorded on each delegation at submission time — never inferred from an agent's current bindings, since two registrations may be bound to one agent and would otherwise each report the other's traffic. A run identifies the partner it delegated to wherever runs are listed.

**Telemetry** (FR-043): outbound counters are distinct from inbound and from internal, extending `a2a/metrics.py`. Each is incremented from the single place its event occurs, so a counter reading zero means nothing happened rather than nothing instrumented.

**Retry honesty** (FR-041): the platform performs no automatic retry after a network failure — a failed send is recorded once, as unreachable — so no delegation can hide a second submission.

## Frontend

One new TanStack Query hook (`useA2AClientApi.ts`) reading the surface above; no `useState` for server data, intervals from `lib/constants.ts`. Surfaces: the verification panel on the integration, an unverified-card indicator wherever provenance matters, and a partner badge on runs that delegated. New strings land in `en`, `fr` and `ja`. The registration form itself is catalog-driven, so it is data rather than components.
