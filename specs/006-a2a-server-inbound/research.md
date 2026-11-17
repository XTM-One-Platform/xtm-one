# Phase 0 Research: Minimal A2A Server (Inbound)

**Date**: 2026-08-18 | **Spec**: [spec.md](./spec.md)

All findings verified against the working tree on 2026-08-18. Where the benchmark (`dev-docs/agent-interop-benchmark.md`, 2026-08-13) already established a fact, it is cited rather than re-derived; where this pass found something the benchmark did not, it is marked **NEW**.

---

## R1 — Protocol library: adopt `a2a-sdk`, contained to one module

**Decision**: Add `a2a-sdk==1.1.2` and confine every import of it to a single module, `app/a2a/protocol.py`. Nothing else in the codebase imports it.

**Rationale**: The v1.0↔v0.3 differences are exactly the fiddly surface worth not re-deriving by hand — the `Part` union is renamed (`text`/`raw`/`url`/`structuredData` in v1.0 vs `TextPart`/`FilePart`/`DataPart` in v0.3), and `extendedAgentCard` moves into `AgentCapabilities`. v0.3 compatibility is a hard requirement (FR-027), so both shapes must be produced correctly, and the SDK is the reference implementation the conformance suite is written against. The containment boundary answers the obvious objection: this is a young third-party dependency landing on a security surface, and a breaking SDK release must be absorbable by editing one file rather than auditing every call site.

**Verified**: `a2a-sdk` is **not** currently in `backend/requirements.txt` — this is a genuinely new dependency, not a re-declaration. It supports Python 3.10–3.14; the backend venv is 3.14.

**Alternatives considered**: Hand-roll the JSON-RPC and models, following the precedent of `app/api/v1/mcp_endpoint.py` which hand-rolls its JSON-RPC helpers. Rejected: MCP's tool schema is far smaller than A2A's dual-version data model, and maintaining two protocol shapes by hand is precisely where drift appears. Vendoring the JSON schema was also considered and rejected as the worst of both.

---

## R2 — Card hosting path collides with the SPA catch-all **NEW**

**Decision**: Serve cards under a dedicated `/a2a` prefix and **add `"a2a"` to `_API_NAMESPACES` in `app/main.py`**.

**Rationale**: `app/main.py:3501` lists the machine-facing namespaces that must never fall through to the SPA: `("api", "v1", "mcp", "xtm", ".well-known")`. An unknown path outside those returns HTTP 200 with `index.html`. FR-010 puts the card at a per-agent path (e.g. `/a2a/{token}/.well-known/agent-card.json`), whose first segment is `a2a`, not `.well-known` — so without this addition, a card fetch for a revoked or unknown exposure would return an HTML page instead of the uniform not-found FR-026 requires, and a partner's registration tooling would choke on parsing rather than degrade cleanly.

This is the one defect in this area that cannot be reproduced against a test double, because it only manifests when the SPA catch-all is installed. `_install_spa_catchall` is deliberately factored as a standalone function so tests can install it on a throwaway app — that is the hook to test against.

**Alternatives considered**: Hosting the card at the deployment-root `.well-known` path, which is already covered. Rejected by FR-013 — one card per agent, no index, and the root must not identify a default agent.

---

## R3 — OAuth client-credentials grant does not exist yet **NEW**

**Decision**: Extend the existing authorization server (`app/api/v1/mcp_oauth.py`) with an RFC 6749 §4.4 client-credentials grant, and bind each issued client to exactly one exposure per FR-040.

**Rationale**: The benchmark recorded "substantial OAuth reuse already exists" and it does — RFC 9728 protected-resource metadata, RFC 8414 AS metadata, RFC 7591 dynamic client registration, PKCE S256-only. But the token endpoint dispatches on `grant_type` at `mcp_oauth.py:1291` and handles only `authorization_code` and `refresh_token`, returning `unsupported_grant_type` for everything else; `grant_types_supported` in the AS metadata (`mcp_oauth.py:244`) says the same. **Client credentials is not implemented.** FR-020 requires it, so this is real work inside Phase 1 rather than the pure reuse the benchmark's cost table implied.

Registration is *not* via the existing DCR endpoint: FR-040 requires the credential to be minted as part of exposing an agent and revoked with it, which is a different lifecycle from partner-initiated dynamic registration. The client row carries an exposure foreign key; `grant_types` on that row is `["client_credentials"]` only.

**Alternatives considered**: Reusing DCR and adding a separate grant table. Rejected — it decouples credential lifetime from exposure lifetime, which FR-006 explicitly ties together.

---

## R4 — Asynchronous execution reuses `AssignmentRun`

**Decision**: An external task creates an `AssignmentRun` with `assignment_id = NULL`, `agent_id` set, and `user_id` set to the boundary holder. The `A2ATask` row references that run rather than duplicating its state.

**Rationale**: The spec's own Key Entities section says External Task "mirrors an internal run rather than duplicating one", and FR-022 maps "its existing agent run lifecycle" onto A2A states — that lifecycle is `AssignmentRun`'s. Critically, `assignment_id` is **nullable** (`app/models/assignment.py:362`), and the codebase already relies on this: `run_agent_chat`'s docstring describes a parent `AssignmentRun` being lazily created on first handover from a chat context, with no assignment involved. So a run without an assignment is an established shape, not a workaround.

This also inherits the engine's existing safety properties rather than re-implementing them: `execute_assignment_run` (`app/services/assignment_engine.py:1208`) has a terminal-state guard that refuses to re-execute a finished run, which is exactly the protection needed when a partner retry and a lease reclaim could otherwise both dispatch.

**State mapping** (FR-022):

| `AssignmentRun.status` | A2A v1.0 state | Note |
| --- | --- | --- |
| `pending`, `delayed` | `SUBMITTED` | |
| `running` | `WORKING` | |
| `awaiting_human` | `INPUT_REQUIRED` | the structural match the benchmark identified |
| `completed` | `COMPLETED` | |
| `failed` | `FAILED` | |
| `cancelled` | `CANCELED` | platform-internal spelling only; BOTH protocol versions put `canceled` on the wire (`a2a.compat.v0_3.types.TaskState`) |
| — | `REJECTED` | submission refused before a run starts (content-type or tool mismatch) |
| — | `AUTH_REQUIRED` | not used this phase |

`cancelled` already exists in `TERMINAL_RUN_STATUSES` (`assignment_engine.py:114`) with a comment reserving it "for a future explicit-cancellation feature". FR-019's `tasks/cancel` is that feature — the status is being claimed, not invented.

**Alternatives considered**: A standalone task table executed via bare `asyncio.create_task`. Rejected under Principle II — a run owned by one replica's event loop is orphaned in `running` forever if that replica dies. Full `OrchestrationExecution` + outbox was rejected as disproportionate for three methods.

---

## R5 — Tool bounding is an identity, not a parameter

**Decision**: Pass none of `run_agent_chat`'s `session_*_ids` parameters, so an
external task resolves the agent's own configuration (FR-034).

**Rationale**: those parameters *replace* what `run_agent_chat` resolves rather
than intersecting with it, so passing any of them narrows — and passing
`session_integration_ids` in particular would drop the per-user half of the
agent's integration bindings, which is every product tool the built-ins carry
(FR-045). External work is bounded by the identity it executes as (FR-029), and
that identity is what the operator commits to when binding the integration.

**Alternatives considered**: a per-exposure allow-list intersected with a fixed
set of externally-permitted built-ins. Rejected: it answers "how much of this
agent may a partner reach" with a smaller number than the agent itself, which
needs a second bounding model to maintain alongside the identity one, and left
the task path reassembling by hand what `run_agent_chat` already resolves.

---

## R6 — Cancellation and quota metering are already parameters

**Decision**: Use `run_agent_chat`'s `cancel_event: asyncio.Event` for FR-019's cancel, and leave `meter_quota=True` (its default) so FR-036's attribution follows the run's `user_id`.

**Rationale**: Both are existing parameters on the same call. Cancellation needs a process-local event plus a database signal, since the cancelling request may land on a different replica from the one running the task — the poller checks the `A2ATask` cancel flag between iterations.

---

## R7 — Visibility bounding and the company-managed variant

**Decision**: Force `user_visible_filter(..., is_admin=False)` on every external path (FR-032), and add a company-managed-only variant for FR-031.

**Rationale**: `user_visible_filter` (`app/api/v1/helpers.py:1440`) returns `sa_true()` — no filtering at all — when `is_admin=True`. FR-032 requires that administrator status never widens external reach, so the flag must be pinned false rather than derived from the executing user. FR-031's company-managed boundary is a strict narrowing of the same predicate to its `is_company_managed.is_(True)` arm alone, dropping both the `user_id` and group-overlap arms. This is a new predicate but not a new concept — it is one of three existing conditions used alone.

Note `run_visible_filter` (`helpers.py:249`) is already narrower than agent visibility (own + company-managed only, no group arm), which is the right precedent: run visibility being tighter than config visibility is established here.

---

## R8 — Rate limiting must be forced fail-closed on this surface

**Decision**: Call the existing `enforce_rate_limit` (`app/core/rate_limit.py:81`) with the registration id as the bucket, and force fail-closed behaviour on this surface regardless of the global setting.

**Rationale**: `settings.rate_limit_fail_mode` defaults to `"open"` (`app/core/config.py:940`), meaning a Redis outage skips the check entirely. On an internal surface that is a deliberate availability tradeoff; on the external surface it would make rate limiting — which the spec names as the *sole* control on external load (Out of Scope) — vanish exactly when the platform is already degraded. Principle I requires the refusal. This is a local override, not a change to the global default, so internal behaviour is untouched.

**Values** (pinned here, as `/speckit-clarify` deferred them): task creation **10 per minute** per registration; reads and cancellations **1800 per minute** per registration. The ratio reflects the cost difference FR-028 names — a submission starts a full agent run, a read is a single indexed lookup. The read value was originally pinned at 120 ("accommodates a partner polling a long task"); testing against a live Foundry agent (2026-08-19/20) measured Foundry polling `tasks/get` in a zero-backoff loop at **8–10 requests per second**, surfacing any 429 to its user as a failed delegation rather than honouring `Retry-After`. 600/min sat exactly at that sustained rate and clipped on bursts, so the default gives the measured client 3× margin.

---

## R9 — Deduplication window

**Decision**: Deduplicate on the protocol's message identifier using a Redis key with a **24-hour** TTL, scoped to the registration.

**Rationale**: FR-043 says "bounded window" without a value. A long window is safe here precisely because the key is a client-minted unique identifier: a client re-sending the same message id a day later is by definition referring to the same message, not asking a new question. Scoping the key to the registration prevents one partner's identifiers from colliding with another's. Redis `SET NX` gives the atomicity needed so two concurrent retries cannot both start a run.

---

## R10 — Transport security check

**Decision**: Enforce FR-017 in the application, reading the forwarded protocol when `settings.trust_proxy_headers` is true and the request's own scheme otherwise; refuse rather than redirect.

**Rationale**: `trust_proxy_headers` already exists (`app/core/config.py:928`), defaults to `False`, and its comment already documents that it must only be enabled behind a proxy known to rewrite the header. That is exactly the semantics FR-017 needs, so no new configuration is introduced — the operator documentation gains one line stating that proxy-header trust must be on behind a reverse proxy or card fetches will be refused.

---

## R11 — Feature flag

**Decision**: Add `A2A_SERVER` to `FeatureFlag` and `KNOWN_FEATURE_FLAGS` in `app/core/feature_flags.py`.

**Rationale**: FR-008 requires a deployment-level switch that is off by default. The existing flag system is env-var driven, resolved once at startup, and already fail-closed — an unset `ENABLED_FEATURES` enables nothing, and an unknown id is logged and ignored rather than enabling anything. Both properties are what FR-008 asks for. The registry requires the `Literal` and the frozenset to be kept in sync by hand; both entries land in the same change.

---

## R12 — Audit and notification surfaces

**Decision**: Use `log_audit()` (`app/services/audit.py:48`) for FR-004, FR-037 and every denial. For FR-005 and FR-041's owner notification, reuse the in-app notification path.

**Rationale**: `log_audit` already takes actor, action verb, entity type and id, a human summary, structured details and the request for IP extraction — every field FR-037 names. Entity type `a2a_exposure` joins the existing open vocabulary (`agent`, `assignment`, `api_key`, …).

**Caveat**: `app/services/notify.py`'s `deliver_notification` is run-oriented — its helpers load an `AssignmentRun` and fan out to web/Teams/Slack/WhatsApp/email. FR-005 and FR-041 notify an agent *owner* about an exposure event, which is not a run. This needs either a small non-run notification path or an explicit decision to record the event in the activity feed only. Flagged as the one place the plan adds a surface rather than reusing one.

---

## R13 — Migration

**Decision**: One additive Alembic revision under `backend/alembic/versions/` creating `a2a_exposures` and `a2a_tasks`, plus the OAuth client's exposure foreign key.

**Rationale**: Additive-only, so the idempotent-re-run and downgrade-round-trip CI checks are satisfiable without data backfill. No existing column changes type or nullability, so a rolling deploy sees old replicas ignoring two tables they do not know about. Principle V's "documented behavior for unrecognized prior state" is trivially met — there is no prior state.

---

## Deferred quantities now pinned

| Quantity | Value | Source |
| --- | --- | --- |
| Task creation rate limit | 10/min per registration | R8 |
| Read + cancel rate limit | 1800/min per registration | R8 |
| Deduplication window | 24 h, per registration | R9 |
| Task state mapping | table in R4 | R4 |

## Open items carried into implementation

1. **Owner notification path** (R12) — the only place a new surface is added rather than reused.
2. **Stale-run reaper** — R4 inherits the engine's terminal guard but not a reaper for a run whose replica died mid-execution; the spec's SC-002 ("no task remains indefinitely pollable") requires one. Scope it to external runs.
3. `a2a-sdk` supply-chain review before the dependency lands, per the constitution's security posture.
