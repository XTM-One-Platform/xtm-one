# Phase 0 Research: Minimal A2A Client (Outbound)

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-08-27

Supersedes the 2026-08-26 research, which assumed two outbound-only provider keys and no direction concept. Findings R1, R2, R4, R5, R8, R9 and R10 survived that change unaltered and are restated here in current terms; R3 was replaced, and R11–R14 are new. Every decision was checked against the code; three checks contradicted an assumption and are marked **⚠**.

---

## R1 — A second module may import the A2A SDK

**Decision**: `app/a2a/client/sdk.py` becomes a second permitted `a2a_sdk` import site; `protocol.py`'s docstring is amended to say there are two containment points and why.

**Rationale**: `protocol.py`'s "ONLY module" rule exists so there is exactly one *shaper* — two would disagree about what v0.3 means, and a partner would see a card contradicting the task surface. That argument is about version shaping, not network I/O. The SDK's client half (`A2ACardResolver` with a configurable `agent_card_path`, `client_factory`, `interceptors`, `utils/signing`) is a different concern. The invariant worth keeping — SDK types never leaving `app/a2a/` — is preserved.

**Alternatives**: Route client calls through `protocol.py` (makes a type module own an HTTP client and both directions). Reimplement card fetch and JWS verification (hand-rolling verification of a JCS-canonicalised document for a fail-closed control).

---

## R2 — Card signature verification

**Decision**: `a2a.utils.signing.create_signature_verifier`. `InvalidSignaturesError` → unusable, no override (FR-015). `NoSignatureError` → accepted, recorded unverified (FR-016).

**Rationale**: The SDK's error taxonomy already draws the line the clarification session landed on, so the two cases sit a few lines apart in one `except` chain instead of a bespoke module. `ProtectedHeader` carries `jku`, so key retrieval is the SDK's problem.

**Alternatives**: A verifier over `pyjwt` (verifies JWTs; a signed card is a detached JWS over a canonicalised document). Deferring verification because no target signs today (it is finding F7, and the path must exist before a partner starts signing).

---

## R3 — ⚠ replaced: catalog shape is three direction-bearing entries, not two new keys

**Decision**: The two shipped inbound keys (`oauth_a2a`, `entra_a2a`) gain an outbound mode, a third key `google_a2a` is added, and each integration carries exactly one direction. *(Revised 2026-08-31: `google_a2a` was decided outbound-only here and shipped serving both directions — spec Clarifications, 2026-08-31.)*

**Rationale**: The superseded plan added `a2a_vertex` and `a2a_foundry` alongside the two inbound keys — four catalog cards for what an operator experiences as two partner relationships. Folding Foundry outbound into `entra_a2a` (both are Entra credential flows, opposite directions) and OAuth outbound into `oauth_a2a` leaves one card per mechanism. Google needs its own because neither existing flow reaches it: outbound OAuth presents a client-credentials bearer, Vertex requires a service-account assertion at `cloud-platform` scope.

The third outbound target has a real name rather than being merely generic: a client-credentials bearer is what this platform's own inbound surface issues, so outbound OAuth covers XTM One → XTM One federation and any partner exposing OAuth the same way.

**Alternatives**: Two new outbound-only keys (four cards, and the inbound surface untouched — cheaper, and rejected on operator experience). One key with a platform field (the catalog is keyed by provider and cannot express a conditional credential form). Renaming all keys to platform names (renames provider keys on a shipped surface, needs a data migration for no functional gain).

---

## R4 — Protocol version negotiation

**Decision**: Reuse `app/a2a/protocol.py`. Intersect `SUPPORTED_VERSIONS` with the partner's card, take the highest, store it on the registration, declare it on every request via `VERSION_HEADER`.

**Rationale**: `protocol.py` already holds both versions, the state map, the error envelope and v1.0 ↔ v0.3 shaping in both directions, so FR-051's two-version support needs a caller rather than a second shaper. One inbound subtlety must **not** carry over and needs a rationale comment: `DEFAULT_VERSION` answers what to serve an *inbound* caller that declares nothing. Outbound we are the client, and FR-023 forbids relying on any default — so the outbound path must never read that constant, which is sitting right there and would look correct.

**Alternatives**: A second outbound shaper, for the reason the module's own docstring gives.

---

## R5 — Circuit breaker state lives in Postgres

**Decision**: Consecutive-failure counters, cooldown expiry, last failure kind and reason as columns on `a2a_integration_settings`. *(Revised 2026-08-31: these are per partner AGENT and shipped on `a2a_client_targets`, so one unreachable agent does not pause delegation to its siblings — FR-059.)*

**Rationale**: Two in-repo patterns exist. `tools/mcp_client.py` keeps circuit state in Redis with a TTL and is explicitly fail-open when Redis is down. `models/knowledge_base.py` and `nova_rule_source.py` keep counters in Postgres and already separate transient from permanent failures — which is the FR-053 split. The DB pattern wins on the requirement that made FR-053 exist: the health verdict must state *why* a registration is unusable, and a Redis key with a TTL cannot be surfaced as a durable reason or survive a restart. One row, one classification, both surfaces reading it.

**Alternatives**: Redis TTL (fail-open is fine for a load control but cannot carry the durable half; splitting across both stores recreates the two-sources-of-truth bug). A rolling failure rate (rejected in clarification as more machinery than a first client needs; the residual is in Edge Cases).

---

## R6 — ⚠ no deployment-wide egress policy exists for this code path

**Decision**: Read FR-054 as classify-and-surface, and add a narrow guard (`client/destinations.py`) refusing non-public, loopback, link-local and cloud-metadata destinations at registration and before every request.

**Rationale**: The repository's only egress allow-list is `opensandbox_network_allow`, which builds `NetworkRule`s for sandbox containers. Nothing constrains backend-initiated `httpx` calls, and the `is_private` / `ip_address` hits elsewhere are threat-intel providers *analysing* IPs, not guarding requests. A card URL is operator-supplied and fetched by the backend with stored credentials, so the absence of a guard is a live request-forgery surface. Enforcement of network policy stays in the deployment; classification is what the platform can honestly own. The spec was corrected accordingly (Clarifications, 2026-08-27).

**Alternatives**: Reuse the sandbox allow-list (an operator's sandbox rules would silently decide which partners are reachable). Do nothing and cite the deployment firewall (leaves the requirement true on paper, absent in fact). Build a general egress policy (out of proportion; changes ~130 providers).

---

## R7 — ⚠ the inbound frontend hook documented endpoints that no longer exist

**Status**: fixed 2026-08-27. `useA2AApi.ts` claimed the management API could still expose an agent explicitly and nominate a run-as user; `a2a_admin.py` has only a `GET`. Recorded because the new outbound hook must not be modelled on that comment, and because SC-007's zero-call-site sweep must run against code rather than against prose describing it.

---

## R8 — Credential acquisition for three targets

**Decision**: All three mint a short-lived bearer from stored non-interactive credentials and reuse `integrations/http.py`'s `RefreshableHeaders` / `ensure_fresh_token` / `bearer_seconds_remaining`. Google: service-account key → signed assertion → `cloud-platform` token. Foundry: Entra client credentials against the pinned tenant. OAuth: client-credentials against the partner's token endpoint.

**Rationale**: `authlib`, `pyjwt` and `cryptography` are already dependencies and cover assertion signing and the client-credentials exchange, so neither `google-auth` nor `msal` is needed — each would add a transitive tree for one token exchange. The refresh problem is already solved for exactly this case: `http.py`'s docstring describes re-minting a short-lived bearer just before expiry across a long autonomous run, which is a delegation polling a partner for minutes.

FR-010's three-way distinction maps onto three points: token acquisition fails (durable), transport fails (transient), partner rejects a token we minted (durable).

**Alternatives**: `google-auth` + `msal`. Storing the partner's bearer directly (FR-010 requires refreshable credentials; a pasted bearer makes every registration a manual chore).

---

## R9 — Scheduling the sweep

**Decision**: Follow `a2a.tasks.reap_stale_tasks` — claim due rows with `FOR UPDATE SKIP LOCKED` in bounded batches, driven from the same place in `app/main.py`.

**Rationale**: The in-repo answer to Principle IV, already load-bearing inbound. `SKIP LOCKED` beats the advisory lock used by `core/migrations.py` because this is row-claiming work: two replicas sweeping different registrations concurrently is desirable. Idempotent for free — the sweep writes a verdict, not a transition — and a missed cycle is safe because FR-050's threshold is the binding control.

**Alternatives**: An advisory lock per cycle (serialises work that parallelises). An arq job (a queue hop for a scan of tens of rows).

---

## R10 — `input-required` through the agentic loop

**Decision**: The tool returns on a terminal state **or** `input-required`, carrying the partner's task id and question. A continuation is a second tool call naming that id, verified to belong to a delegation this run created.

**Rationale**: No new loop machinery. `tools/wait_policy.py` documents the delegate → pause → re-check idiom and the deliberate decision that a round which only waited does not consume a loop iteration — which is what makes a 5-minute ceiling survivable. `tools/run_budget.py` supplies FR-048's four axes with a hard block. So the continuation is reachable on both run paths through the ordinary loop, which is what FR-021 and FR-031 promise and what SC-014 checks. FR-046's ownership check is not incidental: the task id reaches the model inside partner-controlled content.

**Alternatives**: Park the run and ask a human (a different feature). Block until strictly terminal (turns a legitimate clarification into a guaranteed timeout).

---

## R11 — Where direction lives, and what the exposure hook must become

**Decision**: `direction` is a NOT NULL column on the new `a2a_integration_settings` side table, and the single answer to "is this integration A2A, and which way" lives in `app/a2a/providers.py`. `binds_a2a(agent)` — today a pure function over the agent's bindings — becomes a session-taking lookup that returns only **inbound** bindings.

**Rationale**: Checked the shipped path. `_sync_implicit_exposure` calls `binds_a2a(agent)`, which is `bool(bound_integration_ids(agent))` — pure provider-key membership, no notion of direction. Once a shipped key can go outbound, that test is wrong in the dangerous direction: binding an integration to obtain a delegation tool would create an exposure. The hook is a `before_flush` listener and already issues `select`s in the same session, so a lookup is feasible there.

Direction must be queryable, which rules out `extra_config_enc` (encrypted). Keeping it in `providers.py` rather than in `client/` matters because it governs the inbound hook as much as the outbound tool; a second module answering the same question is the two-sources-of-truth failure FR-053 exists to prevent.

**Fail-closed rule, and it is the important part**: an A2A integration with **no settings row** is treated as neither inbound nor outbound — not exposed, no tool. A missing row therefore degrades to a visible, recoverable "my agent is not exposed", never to an unintended exposure.

**Alternatives**: A column on `integrations` (an A2A-specific concept on the table all ~130 providers share). A separate direction table (three A2A tables, two lookups where the hook needs one). Deriving direction from which credential fields are populated (implicit, and ambiguous mid-edit).

---

## R12 — A direction change needs no new listener, because switching is refused while bound

**Decision**: Enforce FR-057 in the integration update path: refuse a direction change while the integration is bound to any agent, naming the bound agents. Clear the previous direction's credentials on a permitted change (FR-058).

**Rationale**: Checked the hook's scope: it filters `isinstance(obj, Agent)` over `session.new` and `session.dirty`, so it observes **agent** changes only. An integration-side direction change would be invisible to it. Under the chosen answer that is not a gap but a saving: if direction cannot change while bound, there is never a live exposure or a live tool to reconcile, so no `Integration` listener is needed and FR-041's revocation machinery is untouched. Had "apply immediately" been chosen, this feature would have had to add a second flush listener on `Integration` and a mass-revocation path.

`agents_binding(db, integration_id, provider)` already exists — the one spelling of "which agents bind this integration" — so the refusal reuses it rather than writing a second query.

**Alternatives**: Apply immediately (needs the new listener and a mass-revocation path). Apply to new bindings only (integration and bindings disagree until someone notices). Immutable direction (forces credential re-entry to fix a mistake).

---

## R13 — ⚠ the catalog cannot express per-direction fields today

**Decision**: Extend the catalog entry shape with per-direction field sets for A2A entries only, and have the catalog endpoint return the set matching the integration's direction.

**Rationale**: `integration_catalog.py` entries are static dicts with a flat `fields` list, and a search for conditional-field support (`fields_by`, `conditional`, `show_if`, `depends_on`) returns **zero** hits — no precedent exists. The directions genuinely need different fields: inbound `oauth_a2a` is `auth_type: "none"` with no operator-supplied credential because the platform mints the secret, while outbound needs a card location and partner-issued credentials; inbound `entra_a2a` stores verification *pins* (tenant, audience, allowed caller) while outbound stores *our* Entra client credentials.

**Alternatives**: One flat field list with everything (an operator sees fields that do nothing, and a required-field check cannot be direction-aware). Client-side filtering only (the field list still has to be tagged by direction, so it is the same change with the enforcement missing). Separate catalog entries per direction (the four-card problem this design exists to remove).

---

## R14 — ⚠ FR-014's "authenticated card retrieval" does not hold for the OAuth target

**Decision**: Implement credentialed card retrieval where the target requires it. **FR-014 was corrected accordingly on 2026-08-27** during the analyze remediation; it no longer asserts authentication as universal.

**Rationale**: Checked `a2a_server.py`: `GET /{card_token}/.well-known/agent-card.json` takes no user dependency — this platform serves its own cards **anonymously at a well-known path**, which is what the inbound spec requires (its FR-011) so that partner platforms can register from a URL alone. The third outbound target is that surface. So FR-014's "card retrieval MUST be authenticated with the registration's own credentials" is true of the two vendor targets and false of the third. Sending an unnecessary bearer would not break our own endpoint, but requiring it as a universal rule states something the feature does not do.

This is smaller than R6's correction — the behaviour is right either way — but it is the same class of defect: a requirement asserting a universal that the code cannot honour. Recorded for a spec follow-up rather than implemented around silently.

---

## R16 — Transport bindings: what Google's Agent Platform actually serves

**Decision** (2026-08-31): The outbound client consumes **JSON-RPC 2.0 and HTTP+JSON**, negotiated per partner agent at verification time from the card's declared interfaces, with JSON-RPC preferred where both are offered. The inbound surface keeps serving JSON-RPC only. gRPC is out of scope in both directions. (FR-065.)

**Rationale**: Vertex AI Agent Engine publishes an agent card and a REST task surface — `{card.url}/v1/card`, `/v1/message:send`, `/v1/tasks/{id}`, `/v1/tasks/{id}:cancel` — and **no JSON-RPC endpoint**. A2A defines three bindings and a client implementing one does not reach a partner serving another, so a JSON-RPC-only client shipped a `google_a2a` catalog entry that verified a partner's card and then 404'd every delegation. Consuming a second binding is a path through existing code; serving one would be a second public surface to secure, which is why the asymmetry is deliberate rather than an oversight.

Three adjacent defects surfaced while implementing it, each of which would have refused a genuine partner on its own, and none of which is REST-specific:

* **A v1.0 card was unreadable.** v1.0 moved the endpoint and the version onto `supportedInterfaces[]` (with `protocolBinding`, and a version *per interface*). Reading only the top-level `url` refused every conforming v1.0 card as `card_malformed`.
* **Outbound version matching was stricter than inbound.** `negotiate_version` accepted a patch level by comparing Major.Minor; `negotiate_outbound_version` compared the exact string. The reference SDK defaults `protocolVersion` to `0.3.0`, so both vendors' real cards were read as offering a version we do not speak.
* **A send answered with a Message was read as a Task.** Both bindings answer a send with Task *or* Message, and v1.0's default asks for the immediate answer. The oneof envelope (`{"task": …}` / `{"message": …}`) belongs to the proto bindings in **both** versions, not to v1.0 alone — keying the unwrapping on the version fails exactly one of the four combinations, which is the one Agent Engine uses.

**Alternatives**: JSON-RPC only, which is the status quo and reaches no Google-hosted agent. An operator-selected binding, which asks an operator to answer a question the card already answers and makes a wrong answer a silent 404. Serving HTTP+JSON inbound too — a second public surface for no demand. A vendor-specific Vertex client (`:query` / `:streamQuery`), which is the amortisation the benchmark's §7.2 rejected A2A's alternatives for.

**Verified against**: a partner shaped exactly like Agent Engine (`scripts/a2a_agent_engine_partner/verify.py` against a local stand-in), which is how the task-resource-name case was found — Agent Engine may return `tasks/{id}` where `Task.id` is documented as a bare identifier, and joining the resource name onto the collection path 404s every poll.

---

## R17 — A Google credential for a deployment that cannot create keys

**Decision** (2026-08-31): The Google mechanism accepts **either** a service-account key JSON **or** a service account to impersonate, exactly one, with both-at-once refused. Impersonation exchanges the platform's own ADC identity at `iamcredentials.googleapis.com/…:generateAccessToken` for a `cloud-platform` token as the target. (FR-066.)

**Rationale**: `constraints/iam.disableServiceAccountKeyCreation` is Google's own recommended baseline. Under it the key mode is not merely awkward, it is unconfigurable — so the catalog entry existed for customers who could never use it. Impersonation stores an address rather than a secret, mints per use, and is revoked by removing one IAM binding at the partner's end, which satisfies FR-010 more strictly than a key does: a key is the only credential in this feature that never expires.

Three implementation notes worth keeping:

* **`google-auth` becomes a direct dependency**, for one job: resolving the platform's own identity. It was already installed transitively, so the tree does not change. Reimplementing the ADC resolution order — metadata server, key file, external account, developer login — is the wheel not to reinvent, and its **external-account** branch is how a federated deployment would arrive here with no further code.
* **The exchange has its own response reader.** `generateAccessToken` answers proto-JSON: `accessToken` and an RFC 3339 `expireTime`, not `access_token` and `expires_in`. Reusing the OAuth reader finds no token and reports a misconfiguration that is not there.
* **The identity is resolved once per process**, not per delegation, because it is a fact about the deployment. `google-auth` refreshes it, so nothing local tracks its expiry.

**Alternatives**: An operator-run OAuth token broker against the shipped `oauth_a2a` mode. It works — prototyped end to end, and it needs no platform change at all, because transport is negotiated from the card rather than from the provider — but it relocates the Google identity into a component the customer runs, and its client secret is exactly as powerful as that identity. Recorded as a stopgap, not a design. Workload Identity Federation: the general answer where the platform does not run on GCP, and a larger piece — this platform would have to be an OIDC issuer with published keys, where its JWTs are HS256 with a single shared secret today. Building that issuer once would make Entra outbound keyless too, which is the argument for scoping it as its own feature rather than as a Google fix.

**Not solved by this**: a deployment that runs neither on GCP nor with any Google identity. Impersonation needs something to impersonate *from*; for those deployments the key remains the only mode, and federation is the eventual answer.

---

## Resolved unknowns

| Unknown | Resolution |
|---|---|
| Is an A2A client library available? | `a2a-sdk==1.1.2`, already installed, ships `client/` with a configurable card path (FR-014) |
| Can card signatures be verified? | Yes, with `NoSignatureError` / `InvalidSignaturesError` matching FR-016 / FR-015 |
| Cost of two protocol versions? | Low — `protocol.py` already shapes both directions |
| Does a circuit-breaker pattern exist? | Two; the Postgres-counter one is chosen (R5) |
| Egress policy for backend HTTP? | **No** — sandbox-only (R6) |
| Multi-replica scheduled work? | `reap_stale_tasks` with `FOR UPDATE SKIP LOCKED` |
| New auth SDKs needed? | One: `google-auth`, for resolving the platform's OWN identity when impersonating (R17). The three token exchanges still need none |
| How does the exposure hook decide today? | Provider-key membership alone; must become direction-aware (R11) |
| Does the hook see integration changes? | **No** — `Agent` instances only, which the refuse-while-bound rule makes moot (R12) |
| Can the catalog express per-direction fields? | **No** precedent; the entry shape is extended (R13) |
| Alembic head | `a2a2intmodel1`, single head; this branch adds `a2a3outbound1` and `a2a4transport1` on top |
| Which binding do the vendor targets serve? | Foundry JSON-RPC; **Agent Engine HTTP+JSON only** (R16) |
