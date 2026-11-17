# Feature Specification: Minimal A2A Server (Inbound)

**Feature Branch**: `feat/implement-A2A-server`

**Created**: 2026-08-18

**Status**: Draft

**Input**: User description: "Build the minimal A2A server specification based on documentation. Use existing agent - user bounding respecting company managed and sharing groups to allow agent exposure."

## Context

`dev-docs/agent-interop-benchmark.md` and `dev-docs/agent-interop-literature-review.md` (2026-08-13) evaluated the cross-vendor agent-interoperability space and recommended adopting **A2A v1.0**, implemented **server (inbound) first**: external agents discover an XTM One agent and give it work. The named first targets are customer-operated agents on Azure Foundry and Vertex AI.

This specification covers the **minimal** viable inbound surface — the smallest thing that lets a customer's Foundry or Vertex agent discover and task an XTM One agent — and deliberately declines the larger Phase 1 scope that the research's cost table implies. The minimising decision is stated up front because it shapes every requirement below:

**Exposure authority and execution bounds reuse the platform's existing agent–user bounding model rather than introducing new ones.** Every shareable entity already carries a three-tier ownership model — personal owner, company-managed, and group-shared with a permission tier — and the platform already derives both *who may change a thing* and *what a given user may see* from it. External exposure is expressed in those same terms. No new permission table, no separately declared "external visibility scope", and no parallel identity concept.

Consequences of that choice, and what it means for the three pre-existing security findings the research flagged as gating any external exposure (F1–F3, benchmark §13), are recorded in **Assumptions** and **Out of Scope**.

## Clarifications

### Session 2026-08-18

- Q: Who may turn on external exposure for a personally-owned agent? → A: Full parity with the platform's existing management authority — the owner, an administrator, or a group member holding the configuration-level sharing tier. Exposure is treated as another management action rather than a new capability, which is the purest form of the reuse this specification is built on. The accepted consequence is that a configuration-tier group member can place an agent externally whose execution boundary belongs to its owner; that is mitigated by notifying the owner (FR-005) rather than by narrowing the authority.
- Q: A company-managed agent is visible to everyone but retains its original owner, and only an administrator may expose it. Whose boundary does external work execute inside? → A: A company-managed-only boundary. External work on a company-managed agent reaches company-managed resources and nothing else — no individual's personal or group-shared data, including the exposing administrator's. This matches what a company agent conceptually is, and it removes the administrator-visibility-bypass finding from this path entirely rather than fighting it with a filter. It is a narrowing of the platform's existing visibility rule, not a new concept.
- Q: When a partner's credentials are registered, may they task every agent the deployment has exposed, or only specific ones? → A: One registration per exposure. A credential is minted as part of exposing an agent, reaches only that agent, and is revoked with the exposure. A partner integrating with several agents holds several credentials — which is how both target platforms register third-party agents anyway, one card and one registration per agent. This keeps a leaked credential's blast radius to exactly one agent, and it needs a binding rather than a grant table, so it is also less machinery than the more flexible alternative.
- Q: When an agent's ownership tier or group sharing changes while it is exposed, should the exposure keep running? → A: No — the platform auto-revokes it. A change that alters the boundary external work runs inside, or the authority required to expose the agent, revokes the exposure and the credentials bound to it; re-exposing is a deliberate act by someone holding current authority. This is fail-closed per the constitution's Principle I, and it is the least machinery: a hook on tier and sharing changes, with no widening analysis to build. It is chosen knowing it breaks a partner integration on an uncommon but deliberate administrative action, which is preferred over a partner's reach silently widening when a company-managed agent is switched to personal.
- Q: When an agent is exposed again after being revoked, is its card URL the same one as before or newly minted? → A: Newly minted every time; a revoked URL never resolves again. The deciding argument is that revocation already kills the credentials bound to the exposure, so the partner must be contacted to receive new credentials on any re-exposure regardless — handing them a new URL in the same message is nearly free. That removes the only real argument for reuse and leaves the cleaner property that a leaked or stale URL can never resurrect.
- Q: When a partner re-sends the same message after a network failure, does the platform start a second agent run? → A: No, within a bounded window — task creation deduplicates on the message identifier the protocol already carries, so a repeat returns the existing task. This needs no new field and no partner-facing concept. It is explicitly not a guarantee: a client that mints a fresh identifier per attempt is not protected, so the requirement makes every submission individually attributable rather than promising perfect deduplication.
- Q: How is the promise that external traffic does not affect other users made measurable? → A: The performance half of the promise is withdrawn rather than measured. The criterion is narrowed to what this feature actually controls — zero usage allowance consumed from anyone but the agent's owner of record — and per-registration rate limiting is named as the sole control on load. No latency target is set and no concurrency cap on external runs is introduced, because either would mean this feature inventing the platform's first performance objective. The control is real rather than absent: with one registration per exposure, total external submission rate is bounded by the number of exposures times the per-registration limit. The residual risk is recorded in Assumptions.

### Session 2026-08-19 — corrections from implementation review

Two decisions recorded above were **reversed** after review found neither was
implemented on any live path. They are kept rather than deleted, because the
reasoning that produced them is the reason to be careful next time.

- Q: Does the company-managed-only boundary agreed on 2026-08-18 hold? → A: No,
  and it was withdrawn. It described an access mode this platform does not have.
  A company-managed agent has no data access of its own anywhere: interactive
  chat borrows the chatting user's identity, the existing MCP endpoint the
  caller's, and an assignment runs as `run_as_user_id or assignment.user_id`.
  Nothing implemented the invented scope either — the function written for it
  had no call site, so the pin it carried against administrator visibility
  bypass executed on zero requests while reading as a guarantee. The feature now
  follows the platform's own answer: one named user, recorded at exposure time,
  which an administrator must choose explicitly for a company-managed agent
  because its stored owner is merely whoever seeded it.
- Q: Does the input-required continuation in FR-023 hold? → A: No. External runs
  execute through the chat path, which never registers the human-input tool and
  never parks a run awaiting input — that machinery belongs to the assignment
  engine — so the state was unreachable. The branch that claimed to serve it
  validated a state that cannot occur and then created a *different* task, so a
  partner's follow-up was answered with an unrelated id. Continuation is now
  refused explicitly.

The common thread, and the thing to guard against in the outbound phase: every
one of these was a function written, documented and unit-tested, but never
called. Passing tests around an uncalled function are worse than no tests, because
they read as assurance. A zero-call-site sweep is cheap and would have caught all
of them.

### Session 2026-08-25 — built-in and company-managed agents

Both questions arose from the same move: exposure stopped being a switch of its
own and became a consequence of binding an A2A integration. That relocated the
decision into `integration_bindings`, and two rules written for a switch stopped
fitting a binding.

- Q: May a built-in agent be exposed? → A: Yes, by an administrator. It was
  refused, on the reasoning that a built-in is resynced from seed data on every
  restart so anything configured on it drifts back. That reasoning does not
  reach exposure: the resync overwrites the agent's seeded fields and does not
  touch `integration_bindings`, so the decision holds across restarts like any
  other. The refusal also had a cost the switch-era reasoning did not price —
  in XTM One the built-ins *are* the product's agents, so refusing them left
  partners able to reach only agents an operator had hand-built, which is close
  to the opposite of what this feature is for. Authority is unchanged: a
  built-in is company-managed, so this is administrator-only, and its seeded
  definition stays read-only. What a platform upgrade may still move is the
  card's *advertised* surface (name, description, permitted built-in tools),
  which is a reviewed change to the product's own agents rather than drift, and
  cannot move the boundary — the run-as identity lives on the exposure, which
  the seed pass never touches.
- Q: Does FR-031's "an administrator MUST name the run-as user explicitly" hold
  for a company-managed agent? → A: No, and it is withdrawn. It presumed a
  moment at which to demand the choice, and binding an integration is not one —
  there is no exposure form to refuse. Enforcing it would have meant refusing
  every company-managed agent, which with the answer above now includes every
  built-in. The identity is therefore **inherited** from the agent's stored
  owner, and the trade-off the original rule existed to prevent is accepted
  rather than solved: the stored owner is usually whoever seeded the agent, so
  a partner sees that person's personal and group-shared data. It is mitigated
  by being *stated* — surfaced in the Integrations list on the agent's Tools tab, in the audit
  trail, and in the operator guide — instead of being left implicit, and an
  administrator who wants a different identity has no surface to nominate one
  from — see the following question.
- Q: The explicit expose and revoke endpoints kept working alongside the
  binding. Should they stay? → A: No, and they are removed. They were a second
  way to reach one state, and the two disagree the moment either is used: an
  exposure created without a binding is reachable with nothing to show for it,
  and invisible in a UI that derives "exposed" from bindings — the drift a
  one-time migration had already been written to clean up. Keeping them "for
  the management API" also meant keeping the only path that could nominate a
  run-as user or narrow an exposure's tool list, which read as a supported
  capability while no interface offered it and nothing exercised it. Two
  consequences are accepted rather than worked around. FR-031's nomination is
  withdrawn outright: the identity is always inherited now. And FR-005's
  notification moved onto the binding hook, where exposure actually happens; it
  can no longer name the actor, because a flush listener has no request context
  to read one from, so it says what happened and leaves who to the audit trail.
  What is NOT accepted is losing the notice itself — a mitigation wired to a
  path nobody takes protects nobody.
- Q: Should the built-in carve-out admit A2A bindings only? → A: No — it admits
  the agent's whole integration configuration. Confining it to A2A made
  exposure look like a special power when it is an ordinary binding, and left
  the product's own agents unable to reach the customer's OpenCTI while being
  reachable by a partner, which is the wrong way round. It also needed a
  guard of its own: with only A2A keys writable, a request could name a GitHub
  integration under an `oauth_a2a` key and smuggle real tools onto a read-only
  agent, so the gate had to verify each id's actual provider. Admitting every
  integration removes both the anomaly and the guard. The read-only rule is
  unchanged for what it was written to protect — persona, name, tools, model,
  memory — and one field is knowingly imperfect: `integration_template_bindings`
  is rewritten from seed data on every restart, but the Tools tab cannot avoid
  sending it (moving a provider from per-user to a fixed integration changes
  both), so it is admitted and the residual drift documented rather than
  blocking the common case.
- Q: An agent's connected tools can come from a fixed binding or from a
  per-user (template) binding. Does an external run get both? → A: Both. It got
  only the fixed half, because the session-scope argument the run is given
  *replaces* the resolved set rather than adding to it, so passing only fixed
  bindings silently discarded every per-user provider. That is a capability
  family going missing, not a narrowing anyone asked for, and it fell hardest on
  exactly the agents the first answer admits: every built-in binds its OpenCTI /
  OpenAEV / OpenCRQ tools per-user and nothing else, so an exposed built-in
  would have answered a partner with no product data at all. Per-user bindings
  are now resolved against the exposure's run-as user, through the same rule
  every internal path uses — which cannot widen the boundary, since that rule
  admits only the run-as user's own integrations and genuinely company-wide
  ones.
- Q: Two A2A integrations bound to one agent showed each other's activity. Is
  filtering by bound agents fixable? → A: No — it answers a different question.
  An integration's activity is what *its* partner submitted, and the calling
  registration recorded on every run and task already says which partner that
  was; the last-activity figure had used it correctly all along while the
  activity list had not. Both now resolve it through one function, so they
  cannot disagree. A run also carries a badge naming its A2A provider wherever
  runs are listed, since an external submission otherwise looks identical to a
  manually triggered run on an agent whose whole purpose is to be tasked by
  partners. One case resisted this: a partner federating against an exposure
  created by the old explicit toggle calls under that exposure's OAuth-shaped
  registration deliberately — one partner, one rate budget — so the id reported
  its Foundry submissions as OAuth ones. The credential form that verified is
  now recorded at submission, which is the only point at which it is known;
  runs predating that stay ambiguous, because nothing captured it, and are kept
  visible rather than hidden from the partner they belong to.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Expose an agent under existing ownership rules (Priority: P1)

A user who already has management authority over an agent turns on external exposure for it. Authority comes from the model the platform already uses: they own the agent, they are an administrator, or the agent is shared with one of their groups at a configuration-level permission tier. Company-managed agents follow the existing rule that only administrators may change them. Nobody gains a new capability they did not already have over that agent, and no agent is ever exposed by default.

While exposing, the user may narrow which of that agent's tools external work may use; leaving that unset means the agent's own configured tools. On turning exposure on, the user receives that agent's own card URL and the credentials the partner will authenticate with, to pass to the partner platform out of band. Turning exposure off makes the URL stop resolving immediately and stops the credentials authenticating. Exposing the same agent again mints a fresh URL and fresh credentials, so both must be conveyed to the partner anew.

**Why this priority**: This is the control plane for the entire feature and the only part that changes existing platform behaviour. It carries standalone value — an auditable, revocable per-agent exposure register — and everything else is unreachable without it. Reusing the existing bounding model is what makes the feature minimal rather than a security-model rewrite.

**Independent Test**: Can be fully tested with no external protocol surface in existence, by verifying that exposure can be turned on and off exactly by those who can already manage the agent, that it is off for every agent initially, and that each change is audit-logged.

**Acceptance Scenarios**:

1. **Given** a user who owns an agent, **When** they turn on external exposure, **Then** it succeeds, they are shown that agent's card URL and the partner's credentials, and the change is audit-logged with actor, agent and timestamp.
2. **Given** a user with no management authority over an agent, **When** they attempt to expose it, **Then** the attempt is denied and the denial is audit-logged.
3. **Given** an agent shared with a group at the configuration-level tier, **When** a member of that group exposes it, **Then** it succeeds and the agent's owner is notified that their agent is now externally reachable.
4. **Given** a company-managed agent, **When** a non-administrator who can see it attempts to expose it, **Then** the attempt is denied; **When** an administrator does so, **Then** it succeeds.
5. **Given** a user exposing an agent, **When** they narrow the tools external work may use, **Then** only those tools are available to external tasks; **When** they leave it unset, **Then** the agent's own configured tools apply and nothing beyond them.
6. **Given** an exposed agent, **When** its exposure is revoked, **Then** the card URL and the task surface stop responding immediately, and in-flight external tasks accept no further operations.
7. **Given** an exposed agent, **When** it is switched between personal and company-managed, or unshared from the group whose member exposed it, **Then** the exposure is revoked, the revocation is audit-logged naming the triggering change, and the owner is notified.
8. **Given** a fresh deployment or an upgrade of an existing one, **When** no explicit exposure has been granted, **Then** no agent is externally reachable.
9. **Given** the deployment-level switch for this capability is off, **When** any external surface is requested, **Then** nothing responds, regardless of per-agent exposure state.

---

### User Story 2 - A partner platform discovers the agent (Priority: P1)

A customer registers an XTM One agent on their own agent platform by supplying only the card URL they were given. Their platform fetches the agent's card without credentials, reads the agent's name, description, skills, the content types it accepts and returns, and how to authenticate, and configures itself from that. No XTM One-specific code and no hand-entered field values are required on their side.

**Why this priority**: Discovery is what distinguishes this from the free-text tool the platform already exposes over its existing external endpoint. Both target platforms register a third-party agent by fetching its card, and Foundry does so anonymously by default — so an unauthenticated, self-describing card is the integration, not a convenience.

**Independent Test**: Can be fully tested by exposing one agent and pointing a standard A2A client at the card URL, verifying the returned card is well-formed and complete enough to configure a client, with no task surface exercised.

**Acceptance Scenarios**:

1. **Given** an exposed agent and its card URL, **When** an anonymous client fetches it, **Then** a well-formed Agent Card is returned describing that agent's name, description, skills, accepted input content types, produced output content types, authentication scheme, and interface binding.
2. **Given** an exposed agent, **When** its card is fetched with no protocol version declared, **Then** the card is rendered in the older compatible protocol shape, matching what the primary target platform assumes when it registers an agent.
3. **Given** an agent that is not exposed, or a card identifier that does not correspond to any exposure, **When** either card URL is fetched, **Then** the response is indistinguishable from the other — neither reveals whether an agent exists.
4. **Given** any attempt to discover what an organisation has exposed, **When** the deployment root or any listing location is probed, **Then** no index of exposed agents is served anywhere.
5. **Given** a card request arriving over a connection the platform cannot confirm is encrypted, **When** it is handled, **Then** it is refused with a terse `403 https required`, and the remediation is recorded for the operator rather than told to the caller.
6. **Given** an exposed agent whose card is fetched, **When** the client reads its declared capabilities, **Then** streaming and push notifications are declared unsupported, so no client attempts them.
7. **Given** an exposure whose tool list has been narrowed, **When** the card is fetched, **Then** the skills it advertises reflect that narrowing rather than the agent's unrestricted configuration.

---

### User Story 3 - A partner agent gives the agent work (Priority: P1)

An external agent authenticates with credentials issued for it, sends the XTM One agent a message, and receives a task it can poll to completion. It can read that task's state and result, and cancel it. It sees only the tasks it created, on the one agent its credentials were issued for.

**Why this priority**: This is the deliverable. Stories 1 and 2 are prerequisites for it; without this the feature advertises an agent it cannot actually task.

**Independent Test**: Can be fully tested by authenticating a test client against one exposed agent, submitting a text message, polling the resulting task to a terminal state, retrieving its result, and confirming a second client cannot read that task.

**Acceptance Scenarios**:

1. **Given** an authenticated external client, **When** it sends a message to an exposed agent, **Then** a task is created and returned with an identifier and an initial state.
2. **Given** a task in progress, **When** the client reads it, **Then** its current state and, once terminal, its result artifacts are returned.
3. **Given** a task in progress, **When** the client cancels it, **Then** the underlying work stops and the task reaches a cancelled terminal state.
4. **Given** a task created by one external client, **When** a different external client requests it by identifier, **Then** the response is the same not-found outcome returned for an identifier that never existed.
5. **Given** a request bearing one of the platform's general-purpose, unscoped, non-expiring API keys, **When** it is presented to this surface, **Then** it is rejected and the rejection is audit-logged.
6. **Given** a running task whose agent needs input to continue, **When** the client reads it, **Then** it reports the state that means input is required; **When** the client sends a follow-up message on that task, **Then** the run resumes.
7. **Given** any task operation, **When** it fails, **Then** the failure is returned in the protocol's structured error form with a machine-readable code, not as free text.
8. **Given** an external client exceeding its allowed request rate, **When** it continues, **Then** further requests are refused with the protocol's structured error, and task creation is limited more tightly than reads and cancellations.
9. **Given** credentials issued for one exposure, **When** they are presented against a different exposed agent, **Then** the request produces the same not-found outcome as an agent that does not exist.
10. **Given** a submitted message, **When** the same message identifier is submitted again within the deduplication window, **Then** the task created by the first submission is returned and no second agent run starts.

---

### User Story 4 - Externally submitted work runs as one named user (Priority: P1)

Work submitted by a partner executes as one named user, decided when the agent is exposed and recorded on the exposure. For a personally-owned agent that is its owner, and the choice is not offered. For a company-managed agent — including every built-in — it is the agent's stored owner. That inheritance is an accepted trade-off rather than a good answer: the stored owner is usually whoever seeded the agent, so the identity is stated plainly wherever the exposure is shown rather than left to be discovered. It is never the actor who bound the integration, when the two differ — structurally so, since exposure is created by a flush hook that has no actor in scope at all. Every submission is attributable to the external client that made it.

**Why this priority**: Equal to Story 3, and inseparable from it — an inbound surface that cannot state what the incoming work may touch is not shippable on a security platform. This story is where the "reuse the existing bounding model" decision is verified rather than merely asserted.

**Independent Test**: Can be fully tested by binding an A2A integration to a personally-owned agent and confirming the recorded identity is its owner rather than the acting user, and by confirming an agent with no owner to inherit does not become exposed at all.

**Acceptance Scenarios**:

1. **Given** an exposed personally-owned agent, **When** an external task runs, **Then** every resource it reaches is one the agent's owner can see under the existing three-tier visibility rules.
2. **Given** an agent with no owner to inherit, **When** an A2A integration is bound to it, **Then** it does not become exposed — no identity is invented to stand in.
3. **Given** an agent exposed by someone other than its owner, **When** an external task runs, **Then** it runs as the owner and not the exposing actor, whether the exposing actor's own access is wider or narrower.
4. **Given** an exposure owned by a user holding administrator status, **When** an external task runs, **Then** its access is still filtered to that user's explicit visibility — administrator status confers no additional reach on this surface.
5. **Given** an external task, **When** it resolves any resource, **Then** it sees exactly what the user it runs as would see — the platform's ordinary visibility rules, with no A2A-specific widening or narrowing.
6. **Given** any point where a visibility decision cannot be evaluated, **When** an external task reaches it, **Then** access is denied rather than granted.
7. **Given** an external task, **When** it attempts to change exposure state, credentials, or permissions, **Then** the attempt is refused regardless of what the execution identity could do internally.
8. **Given** an external task that consumes platform usage, **When** it completes, **Then** the consumption is attributed to the user it ran as and audit-logged naming the external client responsible.

---

### Edge Cases

- **An exposed agent is disabled or deleted.** Its card and task surface stop responding; existing tasks report a terminal outcome rather than remaining pollable forever.
- **The user an exposure runs as is deactivated or deleted.** Exposure ceases to resolve — the platform must not fall back to any other identity.
- **The exposing actor's authority narrows after exposure** — the agent is unshared from their group, or their group permission tier is lowered. The exposure is revoked rather than left running on authority nobody holds.
- **An agent is switched between personal and company-managed while exposed.** The exposure is revoked in both directions of the switch, not only the widening one, so that no live exposure ever changes what a partner can reach.
- **A group member exposes another user's agent and is later removed from the group.** The exposure is revoked, and re-establishing it requires someone who currently holds authority over the agent.
- **A company-managed agent's stored owner is deactivated.** The exposure ceases to resolve — the ordinary deactivation rule (FR-007), not a special case, since the identity was inherited from that owner. A legacy exposure carrying an administrator's earlier explicit nomination is unaffected, because its recorded identity is somebody else.
- **An agent is re-saved without its group sharing changing.** The exposure survives: assignment of an unchanged value is not a change.
- **An exposure toggle is double-clicked.** One exposure, one credential; the second call must not mint a second usable secret.
- **An agent is exposed, revoked, and exposed again.** The partner receives a new URL and new credentials; every value from the previous exposure stays permanently dead.
- **A partner is given access to a second agent.** They receive a second set of credentials rather than a widening of the first, and revoking either exposure leaves the other working.
- **A partner polls a task while its exposure is being revoked.** The revocation wins; the partner receives a defined outcome rather than a partially-served response.
- **A partner declares an unrecognised protocol version.** The request is refused with an error naming the versions that are supported.
- **A run outlives its execution lease.** Its lease is refreshed while it is genuinely running, so only an abandoned run is driven terminal — and once terminal it must never re-enter a non-terminal state, nor be overwritten by the executor that was reaped.
- **A partner sends a message referencing an existing task.** Refused explicitly; the platform must not answer by creating a different task.
- **A partner sends content in a form the agent does not accept**, or expects structured output from an agent not configured to produce it. Both are refused before the run starts, with the mismatch named.
- **A run exceeds the platform's execution time bound.** The task reaches a terminal failed state carrying a machine-readable reason, rather than hanging.
- **The same message is submitted twice** by a partner retrying after a network failure. A repeat carrying the same message identifier returns the task the first submission created; a retry that mints a new identifier does start a second run, and both remain individually attributable.

## Requirements *(mandatory)*

### Functional Requirements

Requirement ids are stable and assigned in creation order. They are never reused or renumbered, so ids added by later clarification sessions appear at the end of the subsection they belong to rather than in numeric sequence.

#### Exposure control

- **FR-001**: Every agent MUST be unexposed by default; external reachability MUST require an explicit, per-agent opt-in.
- **FR-002**: The platform MUST restrict changing an agent's exposure state to actors who already hold management authority over that agent under its existing ownership model — the agent's owner, an administrator, or, for personally-owned agents, a member of a group the agent is shared with at a configuration-level permission tier. Company-managed agents MUST remain administrator-only, matching the existing rule. No separate exposure permission is introduced.
- **FR-044**: A built-in agent MUST be exposable by an administrator on the same terms as any other company-managed agent. Exposure MUST NOT be a special case of its own: it is an integration binding, and a built-in's integration configuration is administrator-editable in general — those are the deployment's own accounts rather than part of the agent's seeded definition. The read-only rule MUST still hold for that definition: any request that would also change a seeded field MUST be refused, whatever else it carries.
- **FR-003**: The platform MUST deny any exposure change by an actor lacking that authority, and MUST audit-log the denial.
- **FR-004**: The platform MUST audit-log every exposure grant and revocation with agent and timestamp. Exposure follows an integration binding, so the actor is the one recorded against that binding change; an automatic revocation names the triggering change instead, having no human actor.
- **FR-005**: When an agent becomes externally reachable, the platform MUST notify its owner and the user its external work runs as, wherever those differ, because the exposure places that person's own access on an external surface — potentially on someone else's initiative. The notice MUST be raised wherever exposure is created rather than on one request path, and need not name the actor: it is created by a flush listener with no request context, and who made the binding change is recoverable from the audit trail.
- **FR-006**: Revoking exposure MUST take effect immediately — the agent's card and task surface stop responding, in-flight tasks accept no further operations, and the credentials issued for that exposure stop authenticating.
- **FR-007**: The platform MUST cease resolving an exposure when it becomes ineligible — the agent disabled or deleted, or the user it executes as deactivated or deleted — without falling back to any other identity.
- **FR-008**: The capability MUST be governed by a deployment-level switch that is off by default; while it is off, no external surface responds regardless of per-agent state.
- **FR-009**: On exposing an agent, the platform MUST give the actor that agent's card URL to convey to the partner out of band.
- **FR-041**: The platform MUST revoke an exposure automatically when the agent's ownership tier or group sharing genuinely changes — a re-save that assigns an unchanged value MUST NOT revoke — in a way that would alter the user its external work runs as, or the authority required to expose it. The revocation MUST be audit-logged naming the triggering change, and the agent's owner MUST be notified. The platform MUST NOT apply such a change to a live exposure, in either the widening or the narrowing direction.

#### Discovery

- **FR-010**: The platform MUST publish one Agent Card per exposed agent at a stable, per-agent URL that follows the standard well-known convention relative to that agent's own base path.
- **FR-011**: A card MUST be fetchable without credentials, because the target platforms fetch it anonymously when registering a third-party agent.
- **FR-012**: A card URL MUST identify the agent by an opaque value, never by the agent's internal identifier.
- **FR-013**: The platform MUST NOT serve an index or listing of exposed agents at any location, including the deployment root.
- **FR-014**: A card MUST declare the agent's name, description, skills, accepted input content types, produced output content types, authentication scheme, and interface binding, all derived from that agent's existing configuration rather than separately authored.
- **FR-015**: A card MUST declare output content types separately from input content types, and MUST declare structured output only for an agent already configured to produce it.
- **FR-016**: A card MUST declare its streaming and push-notification support accurately, whatever that support is. What the surface actually serves is settled by `010-a2a-handover-compliance` FR-023 and FR-014.
- **FR-017**: The platform MUST refuse a request to the partner-facing surface (card, discovery, task surface, callbacks) over a connection it cannot confirm is encrypted: encrypted transport plus the out-of-band card URL is the basis on which a partner trusts the card. A deployment behind a TLS-terminating proxy confirms encryption by trusting that proxy with `FORWARDED_ALLOW_IPS`. An exemption by address (loopback in a dev build or with `A2A_ALLOW_INSECURE_LOOPBACK`, the end-to-end suite's `TEST_ONLY_INSECURE_TRANSPORT_PEERS`) reads the real socket peer only and never applies to a request carrying `X-Forwarded-For`.
- **FR-018**: The card MUST be emitted from an explicit field projection rather than a serialisation of an internal object, so that a signature field can later be added without changing any other field's name, position or type, and without registered partners re-registering.
- **FR-042**: The opaque identifier in a card URL MUST be minted fresh each time an agent is exposed and MUST NOT be reused. The URL of a revoked exposure MUST never resolve again, producing the same not-found outcome as an identifier that never existed.

#### Task surface

- **FR-019**: Authenticated external clients MUST be able to send a message that creates a task, read a task by identifier, and cancel a task.
- **FR-020**: Task operations MUST require credentials issued through the platform's existing authorization server using the client-credentials grant, and MUST reject the platform's general-purpose, unscoped, non-expiring API keys, audit-logging each such rejection.
- **FR-021**: An external client MUST be able to read and act on only the tasks it created.
- **FR-022**: The platform MUST map its existing agent run lifecycle onto the protocol's task states.
- **FR-023**: The platform MUST NOT answer a message that references an existing task by creating a different task. Where that task is waiting on its caller it MUST be continued (`012-a2a-caller-clarification` FR-003); otherwise the message MUST be refused with a machine-readable error, and a task the caller does not own MUST be answered as not found.
- **FR-024**: The platform MUST accept inbound text and structured-data content, and MUST return results as text and file artifacts, plus structured data only for an agent configured to produce it.
- **FR-025**: The platform MUST return failures in the protocol's structured error form with a machine-readable code, never as free text.
- **FR-026**: An unknown task identifier, a task belonging to another client, an unexposed agent, and an unknown card identifier MUST all produce the same not-found outcome, so that no negative response discloses whether something exists.
- **FR-027**: The platform MUST honour a declared protocol version and MUST treat an absent version as the newest supported version, because both target platforms speak it and neither declares a version when fetching a card. A partner on an older version MUST still reach it by declaring it. An unrecognised version MUST be refused with an error naming the supported versions.
- **FR-028**: The platform MUST rate-limit each external client registration, limiting task creation more tightly than reads and cancellations, because a submission starts a full agent run while a read is a single lookup.
- **FR-040**: Each external client registration MUST be bound to exactly one exposure, MUST be created as part of exposing that agent, and MUST be able to reach only that agent. A credential presented against any other exposed agent MUST produce the uniform not-found outcome, and revoking an exposure MUST revoke the registrations bound to it.
- **FR-043**: Task creation MUST deduplicate on the message identifier the protocol carries: a repeat received within a bounded window MUST return the task the first submission created rather than starting a second run. Deduplication MUST NOT be presented as a guarantee — a client that mints a fresh identifier per attempt is not protected — and every submission MUST remain individually attributable in the audit record whether or not it was deduplicated.

#### Execution bounding

- **FR-029**: An externally submitted task MUST execute as a single named user, recorded on the exposure when the agent becomes reachable and never inferred at run time. It MUST NOT be the actor whose edit exposed the agent, when the two differ.
- **FR-030**: For a personally-owned agent, that user MUST be its owner, and MUST NOT be choosable — exposing an agent must never become a way to run it as somebody else.
- **FR-031**: For a company-managed agent — which includes every built-in agent — that user MUST be the agent's stored owner. It is always inherited and never nominated: exposure follows an integration binding, so there is no moment at which to ask. The inheritance MUST be surfaced to the operator and recorded in the audit trail, because the stored owner is whoever seeded the agent rather than a deliberate choice. Where there is no owner to inherit, the agent MUST NOT become exposed.
- **FR-032**: Visibility filtering MUST be applied to externally submitted work regardless of whether the execution identity holds administrator status; administrator status MUST NOT widen reach on this surface.
- **FR-033**: Where a visibility decision cannot be evaluated, access MUST be denied rather than granted.
- **FR-034**: External work MUST be bounded by the identity it executes as (FR-029) rather than by a tool list. A submitted task MUST run the agent's own configuration in full — every capability family the run resolves, management-capable built-ins included — and MUST be able to hand over to every peer that agent configures, whether or not the peer is itself exposed to the same partner. No handover type MUST be withheld, `mandatory_transfer` included: an exposed agent that is the entry to a pipeline answers with its terminal stage's output, which the result artifact MUST attribute (FR-024). Nothing outside the agent's own configuration MUST ever be reachable; the agent's tool configuration is what guarantees that, so no A2A-specific narrowing exists.
- **FR-045**: An externally submitted task MUST resolve the agent's per-user (template) integration bindings as well as its fixed ones, against the user the task executes as (FR-029) and under the platform's ordinary eligibility rule for per-user resolution. A provider with no eligible integration for that user MUST simply be absent, never satisfied from another user's account.
- **FR-035**: An externally submitted task MUST NOT be able to change exposure state, credentials, or permissions, regardless of what its execution identity could do internally.
- **FR-036**: Platform usage consumed by an external task MUST be attributed to the user the task executes as (FR-029), and exhaustion MUST surface as a structured protocol error and be audit-logged naming the external client responsible.

#### Records and observability

- **FR-037**: The platform MUST audit-log every external task submission with the external client, the agent, the user it ran as, and the time.
- **FR-038**: External task records MUST share the lifetime of the internal runs they mirror, and MUST NOT be purged on an independent schedule that would delete a partner's task while its run persists.
- **FR-039**: The platform MUST expose operational telemetry distinguishing external task traffic from internal traffic, so that load originating outside the organisation is separately observable. Exposure changes MUST be counted with the direction and the cause, from the single place either is written, so that a counter reading zero means "nothing changed" rather than "nothing instrumented".
- **FR-046**: Externally submitted work MUST be attributable to the specific partner that submitted it, and MUST be presented that way: an A2A integration's activity MUST show only its own partner's submissions, and a run listed anywhere MUST identify the A2A provider that submitted it. Attribution MUST key on what was recorded at submission time — the calling registration and the credential form that verified — and MUST NOT be inferred from the agents an integration is bound to, since two A2A integrations may be bound to one agent and would otherwise each report the other's traffic.

### Key Entities

- **Agent Exposure**: The per-agent opt-in record. Holds whether the agent is externally reachable, the opaque identifier its card URL is built from, and the single user external work executes as — the agent's owner for a personally-owned agent, an administrator's explicit choice for a company-managed one. Created and revoked only by an actor with management authority over the agent. Follows the pattern the platform already uses for per-agent public-web exposure — an enable flag plus an opaque token keeping the internal identifier out of public URLs —.
- **Agent Card**: The published, self-describing projection of an exposed agent, derived from that agent's existing configuration in full (FR-034). Read anonymously by partner platforms to configure themselves. Not separately authored and not independently editable.
- **External Client Registration**: The partner identity that authenticates to the task surface, established through the platform's existing authorization server and bound to exactly one exposure. Scopes task ownership — a client sees only the tasks it created, on the one agent it was issued for — and is the unit rate limits and audit records are attributed to. Created when the agent is exposed and revoked with it, so its lifetime never outlives the exposure it belongs to.
- **External Task**: The protocol-shaped view of an agent run started by a partner. Carries its identifier, conversational grouping, current state, result artifacts, the external client that created it, and the exposure it was submitted against. Mirrors an internal run rather than duplicating one.
- **Agent** *(existing)*: Already carries the owner, the company-managed flag, and the group-sharing lists and permission tiers that this feature derives all exposure authority and execution bounds from.
- **User, Group** *(existing)*: The identity and sharing primitives the bounding model is expressed in. Unchanged by this feature.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A customer registers an exposed XTM One agent on their own agent platform using only the card URL, with no XTM One-specific code and no hand-entered field values, in under 10 minutes.
- **SC-002**: 100% of externally submitted tasks reach a terminal state — completed, failed, or cancelled — with no task remaining indefinitely pollable.
- **SC-003**: 100% of exposures execute as the user recorded on them and no other, measured against a personally-owned agent, an agent exposed by someone other than its owner, a company-managed agent and a built-in — and zero agents become exposed with no identity recorded at all.
- **SC-013**: Exactly one surface can make an agent externally reachable. Measured by a zero-call-site sweep: no endpoint, task or tool creates an exposure other than the binding hook.
- **SC-004**: On a fresh deployment and on an upgrade of an existing deployment, zero agents are externally reachable until an explicit opt-in is recorded.
- **SC-005**: Revoking an exposure stops the card and task surface responding for that agent within one request cycle, with zero successful operations afterwards.
- **SC-006**: An anonymous caller with no out-of-band URL discovers zero exposed agents, measured by probing the deployment root, listing locations, and identifier enumeration.
- **SC-007**: 100% of exposure changes, external task submissions, and access denials are attributable to an actor or external client, an agent, and a time.
- **SC-008**: Every negative outcome on the task and card surfaces is indistinguishable between "does not exist", "not exposed", and "belongs to someone else" — zero responses that differentiate the three.
- **SC-009**: 100% of failures returned to partners carry a machine-readable code; zero free-text-only failures.
- **SC-010**: Zero tools outside an agent's own configuration are reachable by external work, across every capability family the run resolves — built-ins, custom tools, MCP servers, knowledge bases and integrations, the last counted in both their fixed and per-user forms (FR-045).
- **SC-011**: Zero usage allowance is consumed from any user other than the one the task executes as, across every external task.
- **SC-014**: Zero external tasks leave a terminal state once they reach one, and zero cancelled tasks subsequently report completion.
- **SC-012**: Zero live exposures survive a change to their agent's ownership tier or group sharing, measured across a flip in each direction and a group removal; and zero credentials remain usable after the exposure they were issued for is revoked.
- **SC-013**: Zero card URLs from revoked exposures resolve after re-exposure, measured by replaying every previously issued URL for an agent that has been exposed, revoked and exposed again.

## Assumptions

- **The existing ownership model decides who may expose.** Authority is `can_manage()` — owner, administrator, or configuration-tier group member — with company-managed agents, built-ins included, administrator-only. No new permission is introduced. The one rule this feature sets aside is the read-only treatment of built-in agents, and only for their integration configuration (FR-044): that rule guards fields the startup resync overwrites, and the exposure opt-in is not one of them. What external work can *see* is a separate question, answered by the user it runs as (FR-029) rather than by any A2A-specific rule.
- **The run-as identity comes from the agent, never from the exposer.** Personally-owned or company-managed, an agent runs as its stored owner. Exposing an agent therefore cannot lend the exposer's own access to a partner — and since exposure is created by a flush listener, there is no actor in scope for it to be taken from even by mistake.
- **A delegated exposure placing an owner's own access externally is an accepted risk, mitigated by notification.** Full parity with existing management authority was chosen deliberately over narrowing exposure to owners; FR-005 tells the owner and the run-as user rather than blocking the group member. The notice is raised where exposure is created — the binding hook — so it holds for every path that edits bindings rather than for one endpoint.
- **The agent's own tool configuration is the allow-list.** The constitution's rule that agent and tool exposure defaults to an allow-list is satisfied without a new declaration, and without a second narrowing: what an exposed agent is configured with is what a partner reaches, as its run-as user (FR-034).
- **The research's F2 finding is answered by reuse rather than by new machinery.** The benchmark records that no permission can express "may expose an agent externally". Under this specification, management authority over the agent is that expression — it already distinguishes owner, administrator, configuration-tier group member, and company-managed. No new permission and no new administrative UI is introduced.
- **The research's F1 finding is answered on this surface only.** Rather than rebuilding the platform's general-purpose API key model, this surface accepts only credentials from the existing authorization server and rejects unscoped keys outright. The existing external endpoint's own use of unscoped keys is untouched here and remains a live pre-existing finding.
- **The research's F3 finding (administrator status bypassing visibility) does not reach this path, for a reason that predates this feature.** The execution path resolves visibility through `user_visible_filter` with `is_admin` left at its default of False — pinned explicitly where runners are resolved — so naming an administrator as the run-as user yields ordinary user visibility, not a bypass. An earlier revision of this document credited an invented company-managed boundary for this; that boundary never existed. Declining a paginated external task-listing surface (see Out of Scope) removes the other place the bypass would have mattered.
- **Usage attribution for company-managed agents is imperfect and deliberately not solved here.** Consumption is attributed to the user the exposure runs as, which for a company-managed agent — a built-in included — is usually whoever seeded it. A company-level allowance would be the better end-state; introducing one is the kind of new accounting concept this specification exists to avoid, and it can be added later without breaking a registered partner.
- **The protocol target is A2A v1.0 over its JSON-RPC binding, with compatibility for the preceding version.** This is a partner-compatibility constraint, not an internal choice: the primary target platform supports only that binding for v1.0 and assumes the older version when none is declared, so both are required to reach it at all.
- **Cards are unsigned.** Neither target platform verifies card signatures, and the operator-delivered URL over transport the deployment is configured to terminate is the basis on which a partner trusts a card. Signing belongs with the outbound phase, where the verification side must be built anyway; FR-018 keeps that addition non-breaking.
- **One deployment is one company.** The platform has no tenant or organisation entity, and the protocol's tenant field is an optional opaque routing hint rather than a tenancy model. It is not mapped to the company-managed flag, which is a visibility flag and would create a divergent second tenancy concept.
- **Partner URLs are conveyed out of band.** The platform gives the exposing user a URL; how it reaches the partner is the customer's business process, and both target platforms consume third-party agents that way.
- **Both target platforms' A2A surfaces are Preview.** Breaking changes on their side during implementation are permitted and likely; this specification targets the standard, not either vendor's current subset.
- **Agent runs keep their existing execution time bound and existing usage accounting.** No new limits, ceilings or accounting concepts are introduced for external traffic.
- **Internal responsiveness under external load is deliberately not bounded, and this is an accepted risk.** Per-registration rate limiting is the only control, which does bound total external submission rate to the number of exposures times the per-registration limit — but no latency target is set and no cap on concurrent external runs is introduced. Setting either would mean this feature inventing the platform's first performance objective, which is not its mandate. If external load is later observed to affect internal users, a concurrency cap is the cheapest addition and can be introduced without breaking a registered partner, since it only narrows.

## Out of Scope

Named explicitly, because "minimal" is the defining constraint of this specification and each omission below appears in the research's cost table.

- **The outbound direction** — XTM One tasking a partner's agent. Deferred to a second phase, as recommended.
- **Streaming and push notifications.** Out of scope for this feature — partners poll here. Both are settled by `010-a2a-handover-compliance`. See FR-016.
- **Human-in-the-loop on an external task.** External runs execute through the chat path, and nothing on this surface parks a run for a person to answer — that belongs to the assignment engine. `input-required` here means a task blocked on its calling AGENT, which answers in its own turn (`012-a2a-caller-clarification`); a question needing a human is not something an exposed agent can ask.
- **Any A2A-specific data scope.** External work sees exactly what the user it runs as sees. The platform has no company-scoped access mode, and this feature does not invent one.
- **A paginated external task-listing operation.** Partners hold the identifiers of tasks they created and read them individually. Declining this removes the second place administrator visibility bypass would have mattered.
- **Card signing and signature verification.** Deferred with the outbound phase.
- **Protocol bindings other than JSON-RPC.**
- **A platform-wide rebuild of the general-purpose API key model**, a new granular permission system, and a general fix to administrator visibility bypass. The pre-existing findings behind each are addressed on this surface only, as recorded in Assumptions, and remain open platform findings elsewhere.
- **A company-level or per-exposure usage allowance.** External consumption is attributed to the agent's owner of record.
- **Any guaranteed bound on internal responsiveness while the external surface is under load**, and any cap on concurrent external runs. Per-registration rate limiting is the sole control; the residual risk is recorded in Assumptions.
- **Device-code authorization**, which no part of this surface needs.
- **Any change to the platform's existing external agent endpoint**, its protocol revision, or its credential handling.
- **Data-residency guarantees for work crossing vendor boundaries**, which the research flags as unresolved and which is not resolvable inside this feature.
