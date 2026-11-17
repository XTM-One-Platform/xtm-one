# Feature Specification: A2A-Compliant Handover

**Feature Branch**: `feat/follow-up-A2A-handover`

**Created**: 2026-09-07

**Status**: Draft

**Input**: User description: "Implement A2A standard compliant support of handover."

An exposed agent may already hand work to its peers by consultation, transfer,
background delegation and mandatory transfer, all bounded by the run-as identity
(`006-a2a-server-inbound` FR-034). Three of the four are faithfully represented
on the wire. This feature closes the rest: a task whose work outlives its own run
currently reports a terminal state while that work continues, a transfer cannot
carry the conversation the protocol says it owns, and the card declares
unsupported the two capabilities the specification defines for exactly the
long-running case handover creates.

What the card declares about push notifications is settled here rather than by
`006-a2a-server-inbound` FR-016, which requires only that the declaration be
accurate.

## Clarifications

### Session 2026-09-07

- Q: Does this feature also cover the outbound direction — consuming a partner's
  push callbacks — or only the surface we serve? → A: Inbound only;
  the client direction stays with `009-a2a-client-outbound`.
- Q: Should a submission carrying a context identifier the platform never issued
  be accepted as a new context, or refused? → A: Refused; the platform mints
  every context identifier, and an unissued one is answered exactly as a foreign
  one so existence cannot be probed.
- Q: While a task waits on background work it delegated, which state should it
  report? → A: `working`; nothing is required of the partner, and
  `input-required` is reserved for a task blocked on the client.
- Q: Should an exposed agent be able to spawn background copies of itself for a
  partner's task? → A: Yes, with no external distinction; it shares the
  delegation path, so the task-lifetime requirements cover it and no separate
  control is warranted.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Delegated work finishes where the partner can see it (Priority: P1)

A partner submits work an agent chooses to run in the background. The task stays
in a working state until that work is done, its output arrives as part of the
task, and cancelling the task stops the work.

**Why this priority**: it is the one case where the surface currently reports
something untrue — a completed task whose work is still running, unreachable,
uncancellable and billed afterwards. Everything else here is a capability that is
merely absent.

**Independent Test**: submit to an agent that delegates, poll the task to
terminal, and confirm the reply contains the delegated output; separately cancel
mid-flight and confirm the work stopped.

**Acceptance Scenarios**:

1. **Given** an agent that delegates work in the background, **When** a partner's
   task is submitted, **Then** the task reports working until the delegated work
   is finished.
2. **Given** delegated work that has finished, **When** the partner reads the
   task, **Then** its output is among the task's artifacts.
3. **Given** a task with delegated work in flight, **When** the partner cancels
   it, **Then** the work stops and the task reports cancelled.
4. **Given** delegated work that fails, **When** the partner reads the task,
   **Then** the task reports a failure carrying a curated reason.

---

### User Story 2 - A transfer carries the conversation (Priority: P2)

A partner sends a follow-up message on a task whose work was transferred to
another agent, and the agent now responsible answers it.

**Why this priority**: transfer's contract in the protocol is that the target
handles all future messages; without continuation the platform offers a
one-shot hand-off under a name that promises more.

**Independent Test**: submit to an agent configured to transfer, then send a
second message referencing the returned context and confirm the responding agent
is the transfer target.

**Acceptance Scenarios**:

1. **Given** a submission carrying a context identifier from an earlier task,
   **When** it is accepted, **Then** it is grouped under that context rather than
   a new one.
2. **Given** a task whose work was transferred, **When** the partner submits a
   follow-up in that context, **Then** the transfer target answers it.
3. **Given** a context whose responsible agent is no longer reachable by that
   partner, **When** a follow-up arrives, **Then** it is refused with a
   structured error.
4. **Given** a context created by one partner, **When** another partner submits
   against it, **Then** the submission is treated as unknown.

---

### User Story 3 - A partner learns the outcome without polling (Priority: P3)

A partner registers a callback for a task and is notified when it changes state,
including hours later, without holding a connection or polling.

**Why this priority**: the protocol's own answer for long-running tasks, and User
Story 1 is what makes tasks long-running. Valuable but not a correctness fix —
a partner that polls is served today.

**Independent Test**: register a callback on a submitted task, disconnect, and
confirm the notification arrives on the terminal transition.

**Acceptance Scenarios**:

1. **Given** an agent card, **When** a partner reads its capabilities, **Then**
   push notifications are declared supported.
2. **Given** a registered callback, **When** the task reaches a terminal state,
   **Then** the callback receives a notification identifying the task.
3. **Given** a notification, **When** the partner inspects it, **Then** it
   carries no task content and the partner reads the result through the task.
4. **Given** a callback endpoint that is unreachable, **When** delivery is
   attempted, **Then** delivery is retried a bounded number of times and the
   task's own outcome is unaffected.

---

### Edge Cases

- A task's delegated work outlives the bounded lifetime of a non-terminal task:
  the task becomes terminal with a stated reason, and the work is stopped.
- Delegated work completes after its task was already cancelled: the output is
  not added, and the task stays cancelled.
- A transfer chain reaches the platform's delegation depth bound: the task fails
  rather than hanging.
- A partner registers a callback pointing at an address inside the deployment's
  own network: registration is refused.
- A partner registers callbacks repeatedly for one task: the number retained is
  bounded.
- A follow-up arrives for a context whose task is still running: it is refused
  rather than forking a second task under the same context.

## Requirements *(mandatory)*

### Functional Requirements

**Task lifetime**

- **FR-001**: A task MUST report the working state while work its run delegated
  is still running.
- **FR-002**: A task's execution lease MUST continue to be renewed for as long
  as FR-001 keeps it non-terminal.
- **FR-003**: The output of delegated work MUST be readable through the task
  that caused it.
- **FR-004**: A cancellation MUST stop the work its task delegated.
- **FR-005**: A non-terminal task MUST have a bounded lifetime, after which it
  becomes terminal with a machine-readable reason.
- **FR-006**: Platform usage consumed by delegated work MUST be attributable to
  the external client that caused it, whenever it is charged.

**Conversation**

- **FR-007**: A submission carrying a context identifier the platform issued to
  that client MUST be grouped under that context.
- **FR-008**: A context identifier MUST be minted by the platform for a
  submission that carries none.
- **FR-009**: A submission carrying a context identifier the platform did not
  issue MUST receive the same not-found outcome a foreign context receives, so
  the existence of a context cannot be probed.
- **FR-010**: A context MUST be readable and continuable only by the client that
  created it.
- **FR-011**: A context MUST record the agent currently responsible for it, which changes only when an agent hands the conversation over by choice — never when an automatic hand-off routes one message through a pipeline stage.
- **FR-012**: A follow-up submission in a context MUST be executed by the agent
  that context records as responsible.
- **FR-013**: A follow-up MUST be refused with a structured error when the
  responsible agent is no longer reachable by that client.
- **FR-014**: A follow-up MUST be refused while the context's previous task is
  still non-terminal.

**Push notifications**

- **FR-015**: A card MUST declare push notifications supported.
- **FR-016**: The platform MUST serve the protocol's push-notification
  configuration methods for a task the calling client owns.
- **FR-017**: A notification MUST carry the task's identity and state and no task
  content.
- **FR-018**: A notification MUST present the credential the partner registered
  with its callback, so the partner can reject one it did not originate.
- **FR-019**: A callback address MUST be refused if it resolves inside the
  deployment's own network, before any request is sent to it.
- **FR-020**: Delivery MUST be retried a bounded number of times.
- **FR-021**: Delivery MUST NOT delay or alter the task's own outcome.
- **FR-022**: The number of callbacks retained per task MUST be bounded.

- **FR-031**: A callback supplied inline on a submission MUST be registered for
  the task that submission creates, so a partner need not learn the task's
  identifier before it can be notified about it.

### Key Entities

- **Context** *(new)*: the protocol's conversational grouping. Holds the client
  that owns it and the agent currently responsible, and groups the tasks
  submitted under it.
- **Push Notification Config** *(new)*: a partner-supplied callback for one task —
  its address, the credential form the partner will verify, and its delivery
  state.
- **A2A Task** *(existing)*: gains its context binding, and its state becomes
  derived from delegated work as well as from its own run.
- **Delegated Work** *(existing)*: the platform's background work item, which
  becomes addressable from the task that caused it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero tasks report a terminal state while work they delegated is
  still running, across the delegation scenarios in the review guide.
- **SC-002**: 100% of delegated output submitted by a partner is retrievable
  through the task the partner holds, with no second identifier required.
- **SC-003**: A cancelled task has no work still running one minute after the
  cancellation is accepted.
- **SC-004**: 100% of non-terminal tasks reach a terminal state within the
  configured bound, extending `006-a2a-server-inbound` SC-002 to tasks whose work
  outlives their run.
- **SC-005**: A partner following up in a context reaches the agent responsible
  for it in 100% of attempts, and reaches no other partner's context in any.
- **SC-006**: A partner that registers a callback and never polls learns every
  terminal outcome.
- **SC-008**: Consumption by delegated work is attributable to the submitting
  client in 100% of audit records covering it.

## Assumptions

- The compliance target is the protocol versions already served (0.3 and 1.0) over
  the JSON-RPC binding this platform serves; no new transport binding is added.
- Work an agent delegates to a copy of itself reaches the same background
  execution path as work delegated to a peer, so the task-lifetime requirements
  govern it without an external-only distinction; User Story 1 is therefore
  built before the protocol capabilities that assume it.
- Only the surface this platform serves is in scope. Consuming a partner's
  push notifications belongs to `009-a2a-client-outbound`, and our
  client continues to reach partners by polling.
- Delegated output is added to the task that caused it rather than surfacing as
  sibling tasks, because no method enumerating a context's tasks is served and a
  partner would have no way to discover them.
- A follow-up is executed under the card the partner originally addressed; the
  responsible agent answers within that card's declared contract rather than
  substituting its own.
- The exposure, credential and identity model is unchanged: a context and its
  tasks are reachable exactly as the tasks are today
  (`006-a2a-server-inbound` FR-029, FR-040).
- Webhook delivery reuses the outbound egress validation, retry and circuit
  breaking already built for the client direction rather than adding a second
  egress path.
- Consultation, mandatory transfer, transferred-answer attribution and
  timeout-as-failure are already implemented and are relied on, not re-specified.
