# Feature Specification: AI-disabled platform mode

**Feature Branch**: `008-ai-disabled-mode`

**Created**: 2026-08-26

**Status**: Draft

**Input**: feat(platform): "AI disabled" platform mode — no seeding, no schedulers, minimal UI that states the disabled condition

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A deployment where AI must not run at all (Priority: P1)

An operator installs XTM One somewhere AI is not permitted — LLM egress is blocked by policy, or AI use is awaiting internal approval, or the platform shipped ahead of the AI licence. They set the platform to its AI-disabled state and start it.

**Why this priority**: This is the feature. Everything else refines it. Today the only way to run without AI is to configure no provider, which is not an off state but a broken one: the platform seeds its whole AI catalogue, starts schedulers that fire the seeded work, and fails each run against a missing provider. An operator gets failed runs, noisy logs and background load for a capability they deliberately did not buy.

**Independent Test**: Start a fresh platform in the AI-disabled state. Confirm no built-in AI content was created, no AI scheduler dispatched anything, and no call was made to a model or embedding provider — then confirm the platform is otherwise usable: sign in, manage users, register a connected platform, read the audit log.

**Acceptance Scenarios**:

1. **Given** a fresh installation with AI disabled, **When** the platform boots, **Then** no built-in agents, assignments, templates, skills, knowledge bases, intent bindings, example flow or template containers are created, and the log states plainly what was skipped and why.
2. **Given** AI is disabled, **When** the schedulers tick, **Then** no assignment, evaluation, knowledge-base sync or rule sync is dispatched.
3. **Given** AI is disabled, **When** the platform runs for an extended period, **Then** it makes no request to any model or embedding provider.
4. **Given** AI is disabled, **When** an administrator manages users, groups, invitations, roles, branding, platform registration or the audit log, **Then** every one of those works exactly as it does with AI enabled.

---

### User Story 2 - Turning AI on or off without a restart (Priority: P1)

An administrator on a running platform is cleared to use AI — the licence arrives, or policy changes. They turn AI on from the settings screen and the platform provisions itself. Later, an incident requires AI to stop immediately; they turn it off without redeploying.

**Why this priority**: Equal to Story 1 because the issue settles it as a requirement rather than an enhancement, and because it constrains the design of Story 1: the disabled state cannot be a boot-time constant read once, and the built-in content cannot only be creatable at boot.

**Independent Test**: On a platform started with AI disabled, turn AI on from the settings screen without restarting. Confirm the built-in content appears as it would on a normal install, and that turning it on a second time changes nothing further. Turn it off again and confirm dispatch stops and executions are refused.

**Acceptance Scenarios**:

1. **Given** AI is disabled and no built-in content exists, **When** an administrator turns AI on, **Then** the built-in content is provisioned exactly as a normal boot would create it, without a restart.
2. **Given** AI has just been turned on, **When** an administrator turns it on again, **Then** nothing is duplicated and nothing fails.
3. **Given** AI is enabled with work in progress, **When** an administrator turns AI off, **Then** no new work is dispatched and new execution attempts are refused, while work already running is allowed to finish on its own rather than being cut off mid-way.
4. **Given** an administrator has never touched the toggle, **When** the deployment-level default changes, **Then** the platform follows the new default — an untouched installation is not pinned by a stale stored value.
5. **Given** an administrator has set the toggle explicitly, **When** they choose to reset it to the platform default, **Then** the platform follows the deployment-level default again.
6. **Given** any change to this setting, **When** it is made, **Then** it is attributable in the audit trail: who changed it, when, and to what.

---

### User Story 3 - The platform states the disabled condition instead of looking broken (Priority: P1)

Someone opens a platform with AI disabled — an evaluator, a new user, an administrator. They can see what the platform would do with AI enabled, understand that it is deliberately off, and find out how to turn it on. They never meet a blank page or a failure that reads like a bug.

**Why this priority**: Also P1, and deliberately so. An install that is silently missing half its function is the problem this feature exists to solve; a technically correct backend behind a confusing screen would not deliver it. The issue is explicit that making the missing capability *visible* is a requirement, not a side effect.

**Independent Test**: With AI disabled, visit every AI area of the product as a non-administrator. Confirm each one is reachable, states that AI is disabled, and offers no action that will fail. Then confirm the same on a fresh install where those areas are legitimately empty.

**Acceptance Scenarios**:

1. **Given** AI is disabled, **When** a user opens the navigation, **Then** every AI area is still listed and reachable, marked as unavailable rather than removed.
2. **Given** AI is disabled, **When** a user opens an AI area, **Then** actions that would need AI are visibly unavailable and explained, and no such action can be started.
3. **Given** AI is disabled, **When** a user lands on the home screen, **Then** it states that AI is disabled, why, and what is needed to enable it.
4. **Given** AI is disabled, **When** a user is on any screen, **Then** a persistent indication of the disabled state is visible, including to non-administrators.
5. **Given** AI is disabled, **When** someone reaches the sign-in screen, **Then** the disabled state is stated before they authenticate.
6. **Given** a fresh install with AI disabled and therefore no content at all, **When** a user browses the AI areas, **Then** the emptiness reads as intentional rather than as a failure to load.

---

### User Story 4 - Existing AI data stays browsable (Priority: P2)

A platform that has been running with AI has it turned off. Its agents, flows, knowledge bases, past runs, traces and conversations remain readable.

**Why this priority**: Depends on Stories 1–3 being in place, and is the settled answer to what happens to existing data — read-only rather than hidden or deleted. Separable because a first delivery could refuse reads without being wrong about anything else, and this story is what makes that not good enough.

**Independent Test**: On a platform with existing AI data, disable AI. Confirm every listing and detail view still reads, and that no attempt to change or run anything succeeds.

**Acceptance Scenarios**:

1. **Given** existing AI data and AI disabled, **When** a user lists or opens any of it, **Then** it is readable.
2. **Given** existing AI data and AI disabled, **When** anything tries to create, change, delete or execute it, **Then** the attempt is refused with a single consistent, machine-readable reason.
3. **Given** AI is disabled, **When** it is later enabled again, **Then** nothing that existed before was destroyed while it was off.

---

### User Story 5 - Deterministic automation keeps working (Priority: P2)

An operator relies on flows that do not involve AI — scheduled integration syncs, webhook-triggered HTTP calls, notifications. Those keep running with AI disabled. Flows that do need AI are held back with a stated reason rather than started and failed.

**Why this priority**: The settled decision that "AI off" does not mean "automation off". P2 because Stories 1–3 define the mode; this one prevents it from being uselessly broad.

**Independent Test**: With AI disabled, run a flow with no AI step and confirm it completes. Attempt one containing an AI step and confirm it is recorded as skipped, with a visible reason, and that no part of it executed.

**Acceptance Scenarios**:

1. **Given** AI is disabled, **When** a flow whose steps need no AI is triggered by schedule, webhook or event, **Then** it runs normally.
2. **Given** AI is disabled, **When** a flow containing at least one AI step is triggered, **Then** it does not start, and is recorded as skipped with a reason a user can read in the operations view.
3. **Given** AI is disabled, **When** a flow containing an AI step is triggered, **Then** no step of it executes — it never starts and fails part-way.
4. **Given** AI is disabled, **When** a user edits a flow, **Then** AI steps are visible but unavailable, and a flow that already contains one says why it will not run.

---

### Edge Cases

- **AI turned off while runs are in flight.** New dispatch stops at once; work already running finishes under its own existing time limit rather than being cut off, so no run is left half-written. The interface says this is what will happen before the administrator confirms.
- **AI turned on for the first time on a platform that has been running without it.** Provisioning must behave as a first boot would, and must be safe to repeat.
- **AI turned on, then off, then on again.** No duplicated built-in content, no orphaned schedules.
- **A stored setting that disagrees with the deployment default.** An explicit administrator choice wins; an untouched platform follows the deployment default. Only these two states exist — there is no third, ambiguous one.
- **A flow whose graph changes** so that it gains or loses an AI step while AI is disabled: whether it runs is decided from its current graph at trigger time, not from a stored verdict.
- **An execution requested through a non-interactive path** — an API token, an integration, a connected platform — is refused with the same reason as one requested through the interface, not with a different or vaguer error.
- **A fresh AI-disabled install seeds nothing**, so lists are empty for a reason unrelated to filtering. Empty-because-nothing-was-created and empty-because-AI-is-off must not be presented as the same thing.
- **Two administrators changing the setting at once** must converge on one value, both attempts attributable.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The platform MUST support a deployment-level configuration that runs it with AI off by construction, alongside its existing modes.
- **FR-002**: An administrator MUST be able to turn AI off and on at runtime, on a running platform, without a restart.
- **FR-003**: The effective state MUST resolve from exactly two inputs: an explicit administrator choice if one has been made, otherwise the deployment-level default. An administrator MUST be able to clear their choice and return to following the default.
- **FR-003a**: A change to the state MUST take effect on every replica within a few seconds, without a restart. It need not be instantaneous: the resolved answer MAY be reused briefly so that gating a request costs no database read, and the bound MUST be short enough that an administrator flipping the switch by hand does not notice it. Rationale: the gate runs on every AI request, and a per-request read would be paid by every user forever to make a setting changed a handful of times per deployment feel instant.
- **FR-004**: Every part of the system MUST decide behaviour from the single resolved state, never by inspecting the deployment-level setting directly, so that the runtime override cannot be honoured in one place and ignored in another.
- **FR-005**: With AI disabled, boot MUST NOT create any built-in AI content, and MUST record what it skipped and why. Skipping MUST NOT be silent.
- **FR-006**: With AI disabled, the system MUST NOT dispatch AI-dependent scheduled work.
- **FR-007**: With AI disabled, the system MUST make no request to any model or embedding provider, and MUST NOT require provider-dependent infrastructure in order to start.
- **FR-008**: With AI disabled, reading AI resources MUST continue to work.
- **FR-009**: With AI disabled, creating, changing, deleting or executing AI resources MUST be refused with a single consistent machine-readable reason, used by every surface, so a client can react to the state rather than infer it from an error message.
- **FR-010**: The refusal in FR-009 MUST apply to every entrypoint that would start AI work, including interactive use, scheduled work, integration-driven calls and API tokens. No entrypoint may bypass it and none may fail obscurely instead.
- **FR-011**: The disabled state MUST be discoverable before authentication, so the sign-in screen can state it.
- **FR-012**: Turning AI on at runtime MUST provision the built-in content that a normal boot would have created, through one path shared with boot, and MUST be safe to repeat without duplicating or failing.
- **FR-013**: Turning AI off at runtime MUST stop new dispatch and refuse new executions promptly. Work already in flight MUST be allowed to complete under its existing time limit rather than being cancelled, and the interface MUST say so before the administrator confirms.
- **FR-014**: Deterministic automation — flows with no AI step, and their schedule, webhook and event triggers, and integration syncs — MUST keep running with AI disabled.
- **FR-015**: Whether a flow needs AI MUST be derived from a single declared property of each step type, so that one list governs the runner, the triggers and the editor and they cannot disagree.
- **FR-016**: With AI disabled, a flow containing at least one AI step MUST NOT start. It MUST be recorded as skipped with a reason visible where runs are reviewed, and no part of it may execute.
- **FR-017**: With AI disabled, AI areas MUST remain listed and reachable in navigation, marked unavailable rather than removed.
- **FR-018**: With AI disabled, actions requiring AI MUST be visibly unavailable with a consistent explanation, and MUST NOT be startable.
- **FR-019**: With AI disabled, the platform MUST present a screen stating that AI is disabled, why, and what is required to enable it.
- **FR-020**: With AI disabled, a persistent indication of the state MUST be visible on every screen, to all users including non-administrators.
- **FR-021**: With AI disabled, administration unrelated to AI — authentication and single sign-on, users, groups, roles, invitations, profiles, platform settings and branding, audit logs, connected-platform registration, health and metrics — MUST be unaffected.
- **FR-022**: Settings sections that configure AI MUST render read-only rather than disappearing, beside the toggle itself.
- **FR-023**: Changing this setting MUST be recorded in the audit trail with who changed it, when, and to what.
- **FR-024**: Changing this setting MUST require administrator authorisation.
- **FR-025**: No data MUST be destroyed while AI is disabled, and re-enabling MUST require no manual repair.
- **FR-026**: Concurrent changes to the setting MUST converge on a single value, with each attempt attributable.
- **FR-027**: An empty AI area on a platform that never provisioned content MUST be distinguishable from one emptied by filtering, so neither reads as a failure.
- **FR-028**: The mode and its runtime toggle MUST be documented for operators, including how to enable AI later.
- **FR-029**: With AI disabled, the MCP endpoint MUST refuse every request with the same machine-readable reason as any other AI entrypoint — not partially, and not by disappearing. Rationale: the endpoint exists to be consumed by a model, so on a deployment chosen because model access is prohibited, any part of it left live keeps open the path the mode exists to close. It stays discoverable and states why it refuses, so a client that worked previously is told rather than left guessing.
- **FR-030**: A flow step type whose AI dependence is not clearly *no* MUST be treated as requiring AI. A step type introduced without an explicit classification MUST therefore be held back rather than run. This covers the currently ambiguous cases — a tool call with model-chosen arguments, guardrails, structured output — and every step type added later. Rationale: the two ways to be wrong are not symmetric. Wrongly holding a flow back produces a visible, reversible skip with a stated reason; wrongly running one invokes a model on a platform whose entire purpose is that none is invoked.
- **FR-031**: An AI-disabled platform MUST NOT require a licence in order to run, because nothing licensable runs on it — requiring one would block the case of shipping the platform before the AI capability is bought. The mode MUST be reported in telemetry, so the number of deployments sitting AI-off is visible.

### Key Entities

- **Resolved AI state**: The single boolean every decision reads. Derived from an explicit administrator choice when one exists, otherwise from the deployment-level default. The absence of a choice is meaningful and distinct from a stored "on".
- **Built-in content provisioning**: The set of AI content a normal platform creates for itself — agents, assignments, templates, skills, integrations, knowledge bases, intent bindings, the example flow, template containers. One repeatable operation, invoked at boot and when AI is turned on.
- **Flow step type**: A kind of step a flow may contain, carrying a declared property for whether it requires AI. The one place that property is stated.
- **Refusal reason**: The single machine-readable value returned by every refused write or execution, which clients use to render the disabled state rather than parsing prose.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A fresh AI-disabled platform creates zero built-in AI records, dispatches zero AI scheduled jobs, and makes zero model or embedding provider requests — all three counted, not inspected by eye.
- **SC-002**: An administrator can turn AI on and reach a fully provisioned platform without a restart, and repeating the action changes nothing.
- **SC-003**: After turning AI off, no new AI work is dispatched and no new execution is accepted, within one scheduler interval.
- **SC-004**: With AI disabled, 100% of AI read requests succeed and 100% of AI write and execution requests are refused with the same reason. No such request returns an unexpected server error, and none succeeds.
- **SC-005**: With AI disabled, a flow with no AI step completes at its normal success rate, and 100% of flows containing an AI step are recorded as skipped with a stated reason and zero steps executed.
- **SC-006**: Every AI area of the interface is reachable with AI disabled, and none presents an action that fails when used.
- **SC-007**: The disabled state is visible on the sign-in screen before authentication, and on every screen after it.
- **SC-008**: Non-AI administration tasks succeed at the same rate with AI disabled as with AI enabled.
- **SC-009**: Every change to the setting is attributable in the audit trail to a person, a time and a value, with no gaps.
- **SC-010**: Turning AI off and on repeatedly leaves no duplicated built-in content and no orphaned schedule.
- **SC-011**: A person unfamiliar with the deployment can tell, from the interface alone and without reading logs or documentation, that AI is deliberately disabled rather than broken.

## Assumptions

Defaults taken where the issue left a question open but a defensible answer exists. Each is a decision that can be reversed cheaply; the three that could not be defaulted are the clarification markers above.

- **The CTEM dashboard and connected-platform registration stay live** (issue question 1). They involve no AI, they are useful on their own, and leaving them working strengthens the contrast the mode is meant to create. "Minimal UI" is read as *no AI runtime*, not *fewer non-AI features*.
- **The public catalogue stays readable** (question 2). It is the natural place for someone to see what the platform would do with AI enabled, which is the stated purpose of keeping AI areas browsable at all.
- **Provider-dependent infrastructure becomes optional to run, never impossible to add** (question 3). Because AI can be enabled at runtime, a platform started with AI off must be able to gain those components without being rebuilt. Whether they are packaged as an optional deployment profile is an implementation decision, not a requirement.
- **In-flight work finishes rather than being cancelled** (question 5) — the issue's own proposal. Cancelling mid-run risks half-written state, which is worse than a bounded delay, and the delay is already bounded by the existing time limit.
- **Copy is owned by product and marketing, not engineering** (question 8). The specification requires the disabled state be stated and explained; it does not fix the words. Translation is expected for the same locales the product already supports.
- **Existing tiering and gating seams are extended rather than duplicated.** The issue notes an existing gating mechanism and an existing informational screen; this is the deterministic end of the same axis. Whether that is literally reused is a design decision, but a second parallel mechanism is not intended.
- **The three questions above were answered on 2026-08-26** and are recorded as FR-029, FR-030 and FR-031 rather than as assumptions: the MCP endpoint is off entirely, an ambiguous flow step counts as requiring AI, and the mode needs no licence but is reported in telemetry. The second is the one with the widest reach — it makes "requires AI" the default for any step type nobody has classified.
- **An AI-disabled deployment is an XTM One platform with AI switched off**, so when AI is enabled it provisions the XTM One content set. Nothing in the issue suggests an AI-disabled *Copilot* deployment, and the use cases given — bundled beside OpenCTI, OpenAEV or OpenCRQ — are all XTM One. If that is wrong, the content set is the thing to change.
- **"AI" means anything that needs a model or embedding provider**, directly or transitively. A feature that merely stores or displays data produced earlier by AI is not itself AI for this purpose — which is what makes the read surface safe to keep.
