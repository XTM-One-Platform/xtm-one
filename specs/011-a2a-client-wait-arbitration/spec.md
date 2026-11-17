# Feature Specification: How a calling agent waits for a partner's answer

**Feature Branch**: `011-a2a-client-wait-arbitration`

**Created**: 2026-09-09

**Status**: Draft

**Input**: User description: "update specs accordingly" — following the design discussion of how a source agent arbitrates between polling and callbacks when delegating to an A2A partner.

## Context

`009-a2a-client-outbound` built the outbound direction on polling alone, on the
reasoning that it was symmetric to an inbound surface that also only polled.
`010-a2a-handover-compliance` gave the inbound surface callbacks, and the
asymmetry that leaves costs real work: a partner task observed taking 631
seconds completed successfully while the calling side abandoned it at its
300-second ceiling, cancelled nothing, and reported a timeout to the calling
agent. The inbound surface supports tasks up to four hours.

This specification covers the calling side only. It settles two things `009`
left: what an exceeded ceiling means when the partner is still working
(`009` FR-024 made every one a timeout), and whether a partner may reach this
deployment at all. The second is the security-significant one — being called
back means being reachable — and is why callbacks come last here, gated on an
operator's decision rather than on a partner's card.

## Clarifications

### Session 2026-09-09

- Q: When a partner's callback arrives for a task whose calling run was an interactive chat turn, should the platform resume that chat turn too, or only assignment-triggered runs? → A: Both. Each run path resumes by the mechanism it already has — the durable execution behind an assignment run, and for a chat conversation the mechanism that already resumes an idle conversation when background work finishes. No new resumption machinery, and no divergence from `009` FR-021.
- Q: When a callback will report the outcome anyway, how long should a delegation wait before handing the task back to the calling agent? → A: A short, deployment-configurable grace period, necessarily shorter than the ceiling. This platform's own inbound surface answers every submission before the work is done, so with no grace every delegation against it — a three-second one included — would cost the calling agent two turns.
- Q: When a chat turn hands a still-running task back and then ends, should the platform cancel that task at the partner? → A: No. Cancel when an assignment run reaches its outcome, or when the user aborts the conversation — a partner task joins the conversation-level cancel cascade that background sub-agent tasks already use. A turn ending is not the conversation ending, and cancelling there would kill every handed-back task seconds after creating it.
- Q: How should the platform decide whether a partner can reach it for callbacks? → A: An explicit operator setting, falling back to derivation from the configured public base URL when the operator has not set one.
- Q: Should the number of partner tasks a conversation or run can leave outstanding at once be bounded, and if so should they share the existing budget for background work? → A: Unbounded. The calling run's existing budgets bound how many delegations it can make, and each partner enforces its own limits. The per-run background-work cap is sized to protect this platform's database connection pool, which a partner task does not consume, so borrowing it would borrow reasoning that does not apply.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A long partner task does not cost the calling agent its answer (Priority: P1)

An agent delegates work that takes longer than one attempt may wait. Instead of
being told the delegation timed out, the calling agent is told the work is still
running and given a way to collect it. It decides whether to wait, do something
else first, or report progress to the person who asked. The partner's work is
never thrown away for having taken too long.

**Why this priority**: It is the observed failure, and the only story that
changes how long a delegation may usefully take. The others make waiting more
informative or cheaper; this one makes a whole class of partner work reachable
at all. It also depends on nothing else here — it works over polling.

**Independent Test**: Delegate to a partner whose task outlasts the attempt.
The calling agent receives a still-running outcome naming the task, collects
the result on a later turn, and the partner task is never cancelled for having
outlived the attempt.

**Acceptance Scenarios**:

1. **Given** a partner task still running when the attempt ends — at the grace period where the platform will be told the outcome, at the ceiling where it will not — **When** the attempt ends, **Then** the calling agent receives a still-running outcome carrying the task's identity, and the partner is not asked to cancel
2. **Given** a still-running outcome from an earlier turn, **When** the calling agent collects that task, **Then** it receives the result if the task has finished, or a still-running outcome again if it has not
3. **Given** a task identifier that this run never created, **When** a calling agent tries to collect it, **Then** the collection is refused
4. **Given** an assignment run that ends with a still-running partner task outstanding, **When** the run reaches its outcome, **Then** the platform attempts to cancel that task at the partner
5. **Given** a chat delegation that handed a task back, **When** the turn ends, **Then** the task is left running and remains collectable — and is cancelled only when the user abandons the conversation
6. **Given** a calling agent that never collects an outstanding task, **When** the run's own budgets are exhausted, **Then** the run ends with a defined outcome and the partner task is not left running unattended

---

### User Story 2 - The platform knows what each partner supports (Priority: P2)

An operator inspecting a partner agent can see whether it offers callbacks,
alongside the skills and content types already shown. The platform
records this when it verifies the card, so a delegation reads it rather than
asking the partner again.

**Why this priority**: Nothing can arbitrate between mechanisms without it, so
US3 depends on it — but on its own it only informs an operator,
which is why it is not P1.

**Independent Test**: Register a partner whose card declares callbacks and one
whose card declares none. Both are shown accurately without any delegation
being made.

**Acceptance Scenarios**:

1. **Given** a card declaring callback support, **When** the card is verified, **Then** it is recorded against that partner agent and shown to the operator
2. **Given** a card declaring none, **When** the card is verified, **Then** the platform records the absence and never registers a callback with that partner
3. **Given** a partner agent whose card stops declaring callbacks, **When** the periodic re-verification runs, **Then** the recorded capability is updated and delegation continues rather than being suspended for acceptance

---

### User Story 3 - A partner tells us when it is done instead of being asked (Priority: P3)

For work measured in hours, the partner notifies the platform on its own when
the task changes state, and the calling run is resumed by that notification.
The platform holds nothing open while it waits.

**Why this priority**: The right mechanism at that duration and the only one
where waiting costs no held resource, but it has the narrowest reach — it
requires the partner to be able to reach this deployment — and it is worth
nothing until a run can be resumed by an incoming event, which US1 establishes
in its model-driven form first.

**Independent Test**: Delegate to a partner that supports callbacks from a
deployment the partner can reach. The calling run is resumed by the
notification, with no repeated asking in between beyond the safety floor.

**Acceptance Scenarios**:

1. **Given** a partner declaring callback support and a deployment the partner can reach, **When** a delegation is made, **Then** the platform registers a callback and is resumed by it when the task reaches a terminal state
2. **Given** a deployment the partner cannot reach, **When** a delegation is made, **Then** no callback is registered and the delegation proceeds by another mechanism
3. **Given** a registered callback that never arrives, **When** the safety floor next asks the partner directly, **Then** the delegation still reaches its outcome
4. **Given** a callback and a direct answer that both report the same terminal state, **When** both arrive, **Then** the calling run concludes once

---

### Edge Cases

- **A partner declares a capability and then refuses to honour it.** The
  mechanism degrades to the next one available; the delegation does not fail.
  A capability is a claim about what a partner offers, not a guarantee for a
  given call — the caller may be at an allowance the partner enforces per
  client.
- **This deployment cannot receive callbacks at all.** True of any deployment a
  partner cannot reach, which is a property of the calling side's own network
  rather than of the partner. Decided once, where the partner's health is
  already recorded, not per delegation.
- **A partner stops trying to deliver a callback.** Partners bound their
  delivery attempts and then give up; this platform's own inbound surface does.
  A caller that waits only for a callback would wait forever, so asking
  directly remains the floor under every other mechanism.
- **The work that would collect an outstanding task goes away.** Whether the
  task was left by a question, by a still-running outcome, or by a callback that
  never arrived, the partner is told to stop — when an assignment run ends, or
  when a user abandons the conversation. Not when a chat turn ends: handing back
  is what ends that turn, so cancelling there would cancel everything.
- **Two mechanisms report the same transition.** The first to arrive concludes
  the delegation; the second changes nothing.
- **A still-running outcome is returned for a task that has in fact finished.**
  The collection reports the result; a still-running outcome is a statement
  about one moment, and the calling agent is never told a finished task is
  running for more than one turn.

## Requirements *(mandatory)*

### Functional Requirements

**Handing the wait back (US1)**

- **FR-001**: When a delegation attempt ends with the partner task not yet terminal, the platform MUST return a distinct non-failure outcome naming that task. A ceiling reached against a live task is not a failure, and the cancellation `009` FR-024 requires MUST NOT be sent in that case. Where the platform will be told of the outcome by other means (FR-016), the attempt MUST end at a short, deployment-configurable grace period, necessarily shorter than the ceiling; where it will not, the ceiling remains what ends the attempt.
- **FR-002**: A calling agent MUST be able to collect a task returned under FR-001, receiving either its result or a further still-running outcome.
- **FR-003**: A collection MUST be accepted only for a partner task the calling run itself created, on the same terms as a continuation (`009` FR-046).
- **FR-004**: A collection MUST be bounded by the same per-attempt ceiling as any other attempt, and the exchange as a whole MUST be bounded by the calling run's existing budgets and introduce no limit of its own (`009` FR-048).
- **FR-005**: The platform MUST attempt to cancel an outstanding task at the partner when the work that would collect it has gone: an assignment run reaching any outcome, or a user abandoning the conversation a chat delegation was made in. A chat turn ending MUST NOT cancel, because the conversation that will collect the result outlives it. This extends `009` FR-025 to tasks left outstanding by a still-running outcome, not only by a partner's question, and reuses the conversation-level cancellation that background work already goes through rather than adding a second rule.
- **FR-005a**: A user MUST be able to stop an outstanding task before that boundary is reached, by asking the calling agent in the conversation the delegation was made in. The hand-back MUST tell the agent to offer this. Cancelling MUST be scoped to the asking run or conversation — a task identifier is not a capability — and MUST NOT be something the agent decides alone: the work is real and already paid for.
- **FR-006**: A still-running outcome MUST carry no partner-authored text beyond what a partner's question already carries under `009` FR-047, and MUST be distinguishable by a calling agent from both a result and a failure.

**Recording what a partner supports (US2)**

- **FR-007**: The platform MUST record, per partner agent and at verification time, whether that partner's card declares support for callbacks, and MUST show it alongside the card facts `009` FR-018 already shows.
- **FR-008**: The platform MUST NOT attempt a mechanism a partner's card does not declare.
- **FR-009**: A change to a declared capability MUST NOT suspend delegation for acceptance under `009` FR-019: unlike a changed endpoint or content type, a capability that appears or disappears changes how the platform waits and not what it may send.
- **FR-010**: The platform MUST record whether this deployment can receive a partner callback, as a property of the deployment rather than of any partner, and MUST report it where a partner's health verdict is already reported (`009` FR-020). An explicit operator setting decides it, and a refusal MUST NOT be overruled by any other configuration: registering a callback tells a partner where this deployment lives, which that setting exists to let an operator refuse. Where no explicit setting is given, a configured partner-facing callback address MUST decide it, since naming one asserts reachability; where there is neither, the platform MUST derive it from the configured public address, treating an address it would refuse to reach under `009` FR-035 — loopback, private, link-local or cloud-metadata — as unreachable.

**Being told rather than asking (US3)**

- **FR-016**: Where a partner declares callbacks and FR-010 records this deployment as reachable, the platform MUST be able to register a callback for a delegated task, at the deployment's configured partner-facing address where it has one and at its configured public address otherwise, and resume the calling run when that callback reports a terminal state. Resumption MUST use the mechanism that run path already has for resuming on a background result, and MUST introduce none of its own — a run and a conversation are resumed differently, and neither difference is this feature's to invent.
- **FR-017**: The platform MUST authenticate an incoming callback and MUST accept it only against a task a calling run of this deployment created; an unauthenticated or unrecognised callback MUST be refused and MUST NOT resume anything.
- **FR-018**: A registered callback MUST NOT be the only mechanism by which a delegation can conclude: the platform MUST retain a bounded direct-asking floor beneath it.
- **FR-019**: A delegation MUST conclude exactly once regardless of how many mechanisms report its terminal state.

**Across all of them**

- **FR-020**: The mechanism a delegation used MUST be recorded on that delegation, so an operator can tell why one waited the way it did.
- **FR-021**: No difference between mechanisms may be visible to a calling agent beyond the still-running outcome — a delegation's result, failures and reasons MUST be identical whichever way the platform waited (`009` FR-021, FR-029).
- **FR-022**: This feature MUST NOT bound how many partner tasks a calling run or conversation may leave outstanding. The calling run's existing budgets already bound how many delegations it can make (`009` FR-048), and each partner enforces its own limits. The per-run cap on background work is sized to protect this platform's database connection pool, which a task executing on a partner's infrastructure does not consume.

### Key Entities

- **Declared capability**: whether a partner's card says it will call back, recorded per partner agent at verification, refreshed by the existing sweep, and never inferred from a partner's behaviour.
- **Callback reachability**: whether a partner can reach this deployment. One value for the deployment, recorded where health is recorded.
- **Outstanding task**: a partner task a calling run created and has not concluded, whether left by a question or by a still-running outcome. It outlives the turn that created it and is cancelled when nothing is left to collect it.
- **Wait mechanism**: whether a delegation waited by asking directly or by being called back, recorded on the delegation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A partner task lasting up to the inbound surface's own maximum task lifetime returns its result to the calling agent. Zero delegations are abandoned solely for outlasting a single attempt.
- **SC-002**: Zero partner tasks are left running with no caller — measured by cancelling at the partner for every task a run leaves outstanding, and confirmed against the partner's own record of what it was asked to stop.
- **SC-003**: 100% of delegations reach a defined outcome when the preferred mechanism is unavailable or silent — measured with a partner that declares callbacks and never sends one, and a deployment no partner can reach.
- **SC-004**: For a partner task that hands off internally, the calling agent can name the stage in progress before the task finishes — impossible by asking repeatedly, which shows only that the task is still working.
- **SC-005**: A delegation concludes once and only once, with zero double-conclusions across two mechanisms reporting the same transition.
- **SC-006**: An operator can tell, for any completed delegation, which mechanism it waited by and why that one — with zero delegations recording a mechanism the partner does not declare.
- **SC-007**: The result a calling agent receives is identical whichever mechanism was used, verified by running the same delegation under each and comparing outcome, result and failure reason.
- **SC-008**: A delegation whose partner task finishes within the grace period returns its result in the calling agent's same turn. Zero short delegations are turned into a two-turn exchange by the hand-back — measured against a partner that answers every submission before the work is done, as this platform's own inbound surface does.

## Assumptions

- A partner's card is the only source of truth for what that partner supports. The platform does not probe for a capability, and does not infer one from a successful call.
- The per-attempt ceiling stays as `009` FR-024 defines it, including its default. This specification changes what happens when it is reached against a live task, not how long an attempt may run.
- A calling agent decides when to collect an outstanding task. The platform does not schedule the collection, and US1 therefore needs no mechanism for resuming a run on an external event — that is US3's, and is why US3 follows it.
- The existing verification sweep and its staleness threshold (`009` FR-019, FR-050) carry the capability facts; no second sweep is introduced.
- Callback reachability is decided once per deployment rather than negotiated per partner, and a deployment that cannot receive callbacks remains fully functional on the other mechanisms. Deriving it from the configured public address is a default, not evidence: a deployment can advertise an address that no partner can actually reach, and the cost of that mistake is a partner exhausting its delivery attempts while the polling floor concludes the delegation regardless.
- The inbound surface's behaviour is settled by `010` and is not revisited here, including its own bound on delivery attempts — which this specification treats as the behaviour any partner may exhibit.
