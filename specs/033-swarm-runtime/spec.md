# Feature Specification: Swarms — the runtime view of agents working together

**Feature Branch**: `spec/033-swarm-runtime`

**Created**: 2026-11-01

**Status**: Draft (PRD only; no implementation program opened yet, decided by Samuel Hassine on 2026-11-01)

**Input**: User description: "A fleet is an organisation, a swarm is an execution. A fleet can launch a swarm, and so can an agent. We should have a 'Swarm' tab: a swarm is a runtime. We almost have everything already; we want the live visualisation of handovers, active delegations and results, rendered from persisted events, on the fleet graph and in a swarm monitor."

## Why

XTM One already runs swarms: an orchestrator spawns background copies of itself or delegates to its handover agents, those consult other agents, claim work items, call partner agents over A2A, and hand results back, five levels deep (`docs/docs/usage/agentic-flow.md`). What exists today is the **organisation** of that work (agents, handovers, fleets) and the **archive** of it (Work History: one run at a time, the `Activity` tab of a run showing its nested consults). What does not exist is the **runtime** view: one picture of everything one launch set in motion, live while it runs and replayable once it ended. A person who launched a program through the CEO Office fleet today reads twenty run cards and a shared-state blackboard to know where it stands; a person who asks "what is running right now in my fleet?" has no page to open.

The concepts, in one line each, so that the whole product uses the same words:

| Term | What it is | Lifetime | Where it lives today |
|---|---|---|---|
| **Agent** | A configured individual: persona, tools, memory, handovers | Permanent | Agents |
| **Fleet** | An owned organisation of agents: one orchestrator, desks, exposed members, one coordination workspace | Permanent | Agentic Flow > Fleets |
| **Swarm** | One execution: the tree of runs that one launch set in motion, across agents and fleets | From the launch to the last run's end | Nowhere as one object; scattered across runs and tasks |

A swarm is **not** a new execution model: every run in it is an assignment run, a chat turn or a background task of an agent (constitution, Principle I). A swarm is the name, the identity and the projection of a run tree.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Every launch is a swarm with an identity (Priority: P1)

A run that spawns other runs, or that is itself spawned, belongs to a swarm. The swarm has an id, a root (the run that started it), a launcher (the agent, the fleet, the assignment, the flow, the channel message or the API call that started the root), a status, a start and an end. Every run, background task, consult, transfer, A2A partner call and work item claim made inside it carries the swarm id. A run that spawns nothing is a swarm of one; it costs nothing to show it as such.

**Why this priority**: nothing else in this feature can be drawn or listed without the identity; it is also the hook every later feature (budgets per launch, cost attribution, replay) needs.

**Independent Test**: launch an orchestrator that delegates to two agents, one of which consults a third; read the swarm by its id and confirm it lists the four runs with their parent, their handover type and their status; confirm each run's detail names the swarm.

**Acceptance Scenarios**:

1. **Given** an agent run that delegates to another agent, **When** the delegation starts, **Then** both runs carry the same swarm id and the second names the first as its parent with handover type `delegate`.
2. **Given** a consult, a transfer, a self fan-out copy, an A2A partner call or a work item claimed by an agent woken by a `work_item` trigger as a consequence of the root, **When** it starts, **Then** it belongs to the root's swarm with its own kind.
3. **Given** every run of a swarm ended, **When** the last one ends, **Then** the swarm's status is `completed`, `failed` (at least one run failed and nothing retried it) or `cancelled`, with its end time.
4. **Given** a run that spawns nothing, **When** it ends, **Then** it is a swarm of one with the same fields.
5. **Given** a run resumed after a rollout (the platform's resume and replay of interrupted runs), **When** it resumes, **Then** it keeps its swarm id and its place in the tree.

---

### User Story 2 - The Swarms tab lists what runs and what ran (Priority: P1)

A third tab, `Swarms`, next to `Agent-to-agent map` and `Fleets` on the Agentic Flow page, lists the swarms the person can see: live ones first, then finished ones. Each row or card shows the launcher, the root agent, the fleet when the root agent has a home fleet, the status, the counts of runs by status, active delegations, when it started and how long it has run, and the cost so far when the platform shows costs to that person. Search, filters (status, fleet, launcher kind, agent, period) and pagination follow the Agents list, like the Fleets tab.

**Why this priority**: the answer to "what is running right now?" in one page is the smallest valuable slice once swarms have an identity.

**Independent Test**: with two live swarms and ten finished ones across two fleets, open the tab, filter by one fleet and `running`, and confirm only that fleet's live swarm is listed with correct counts; open a finished one and confirm its final status and duration.

**Acceptance Scenarios**:

1. **Given** swarms in several states, **When** the tab opens, **Then** live swarms are listed first with their counts refreshing without a reload.
2. **Given** a person who can see only some agents, **When** the tab opens, **Then** a swarm is listed only when the person can see its root run (the existing run visibility rule), and the nodes they cannot see are drawn anonymised inside it.
3. **Given** a filter by fleet, **When** applied, **Then** only swarms whose root agent is a home member of that fleet are listed.
4. **Given** more swarms than one page, **When** the person pages, **Then** the pagination behaves as the Agents list's (same component, same page sizes, same URL parameters).

---

### User Story 3 - The swarm monitor: a live projection rendered from persisted events (Priority: P1)

Opening a swarm shows its monitor: the graph of the agents involved, laid out as the hierarchy of the run tree (the root on top, or the fleet's own hierarchical layout when the swarm runs inside one fleet), with the state of each node drawn on it: idle, running, waiting for a person (approval, human in the loop), waiting for a sub-run, done, failed. A delegation or a consult in flight is drawn on its edge as a moving marker from the caller to the callee, a result coming back as a marker the other way; the edge stays lit while the sub-run runs. A header bar counts events, active delegations and the latest handover (`HANDOFF  Orchestrator -> Analyst  numbers:okr-sync`). A right rail is bound to the selected node: its current tasks (its runs in this swarm, newest first, with their status and result preview), its recent events, the work items it claimed or created and the files it produced. The monitor is rendered from persisted events, so it draws the same picture live and after the fact.

**Why this priority**: it is the visualisation Samuel Hassine asked for; the list alone does not show how the work moves.

**Independent Test**: launch a swarm that delegates twice and consults once; watch the monitor while it runs and confirm each handover appears on its edge within two seconds of the event, the node states change with the runs, and the right rail of the orchestrator lists its three sub-runs; reload the page after the swarm ended and confirm the final picture is identical to the last live frame.

**Acceptance Scenarios**:

1. **Given** a live swarm, **When** a delegation starts, **Then** its edge lights and a marker travels from the caller to the callee within 2 seconds of the persisted event, without a page reload.
2. **Given** a running node, **When** it is selected, **Then** the right rail shows its runs in this swarm, its recent events and its work items, each opening the existing run, work item or file page.
3. **Given** a swarm that ended, **When** its monitor opens, **Then** every node and edge shows its final state and the header shows the totals, built only from the persisted events.
4. **Given** a node the person cannot see, **When** the monitor draws it, **Then** it is drawn as an anonymised node (`An agent you cannot see`) with its edges, and its rail shows nothing.
5. **Given** a swarm inside one fleet, **When** its monitor opens, **Then** the layout is the fleet's hierarchy and an agent outside the fleet is drawn as a ghost node, like the fleet graph.
6. **Given** a swarm whose root run waits for a person (approval, human input), **When** the monitor draws it, **Then** the node shows the waiting state and the rail offers the existing approval or answer action.

---

### User Story 4 - Replay a swarm event by event (Priority: P2)

The monitor has a timeline: a scrubber over the swarm's events (`12 / 411 events`), play and pause at a chosen speed, step forward and back, a jump to the next handover, failure or result. Scrubbing redraws the graph and the rail as they were at that event. The current event is named in the header with its kind, its agent and its subject.

**Why this priority**: the live picture is the first need; replay turns the same events into a review and a debugging tool, and into a demo of agents at work that needs no live system.

**Independent Test**: on a finished swarm of at least fifty events, scrub to the middle and confirm the graph shows only what had happened by then; play at 4x and confirm the picture ends identical to the live final frame.

**Acceptance Scenarios**:

1. **Given** a finished swarm, **When** the person scrubs to event N, **Then** the graph and the rail show the state after exactly the first N events.
2. **Given** a live swarm, **When** the person scrubs back, **Then** the live feed pauses and a `Live` button returns to the head.
3. **Given** a jump to the next failure, **When** pressed, **Then** the timeline lands on the first `run.failed` event after the current one, or says there is none.

---

### User Story 5 - The fleet graph shows its live swarms (Priority: P2)

On a fleet page, when at least one swarm runs inside the fleet, the fleet graph switches to a live mode: the same node states, lit edges and travelling markers as the monitor, superimposed on the fleet's hierarchy, with a chip per live swarm to isolate one or show all. The fleet card on the Fleets tab shows the count of live swarms. This is the "phase 5" of the Fleets program, delivered after fleet edition (phase 3d) and reusing the monitor's projection.

**Why this priority**: the fleet page is where a fleet owner already looks; it must not become a second, diverging implementation of the monitor.

**Independent Test**: with one live swarm in the CEO Office fleet, open the fleet page and confirm the live mode draws the handovers in flight; end the swarm and confirm the graph returns to the static hierarchy.

**Acceptance Scenarios**:

1. **Given** a fleet with a live swarm, **When** its page opens, **Then** the graph is in live mode and draws that swarm's state on the fleet's hierarchy.
2. **Given** two live swarms in the fleet, **When** one chip is selected, **Then** only that swarm's activity is drawn; **When** none is, **Then** all are, each in its own hue from the chart palette.
3. **Given** no live swarm, **When** the page opens, **Then** the graph is the static hierarchy with no live chrome.

---

### User Story 6 - Launch a swarm from the Swarms tab (Priority: P2)

A person starts a swarm by choosing a launcher (an agent they can use, or a fleet, whose orchestrator is the root) and writing the task; the tab opens the new swarm's monitor. The launch is an ordinary run of the launcher (a chat turn or a manual assignment run), never a new execution path. `Create with a prompt` on the tab seeds a chat on an assistant that carries a `swarms` built-in tool, like the Fleets tab does with the `fleets` tool; the tool lets an agent list, read, launch, pause, resume and cancel swarms it may see, acting as the person.

**Why this priority**: today a swarm is launched only by talking to the orchestrator or triggering an assignment; a visible launch closes the loop between the organisation (fleet) and the execution (swarm).

**Independent Test**: launch a swarm on the CEO Office fleet with a task, confirm the monitor opens on it with the orchestrator as root and the task as subject; cancel it from the monitor and confirm every run under it ends `cancelled`.

**Acceptance Scenarios**:

1. **Given** a fleet the person can use, **When** they launch a swarm with a task, **Then** the root run is a run of the fleet's orchestrator carrying the task, and the monitor opens on it.
2. **Given** a live swarm, **When** the person cancels it, **Then** every run and background task of the swarm is cancelled through the existing cancellation, work items it claimed are released, and the swarm ends `cancelled`.
3. **Given** a platform where no assistant carries the `swarms` tool, **When** the tab opens, **Then** `Create with a prompt` is disabled with the same guidance as the Fleets tab.

---

### User Story 7 - Budgets and the emergency stop apply per swarm (Priority: P3)

An administrator or a fleet owner sets a budget for a swarm at launch (tokens, cost, wall-clock, maximum runs, maximum depth), defaulted from the fleet's budget defaults when it has them and from the platform's agent budgets otherwise (`docs/docs/usage/agent-budgets.md`). Reaching a budget pauses the swarm (no new run starts; running ones finish their step) and notifies the launcher. The platform emergency stop (`docs/docs/administration/emergency-stop.md`) stops every swarm; a single swarm has its own stop.

**Why this priority**: the first value is seeing; controlling what a swarm may spend comes once there is a picture to read it on, and the budget primitives already exist per agent.

**Independent Test**: launch a swarm with a maximum of three runs; confirm the fourth delegation is refused with a reason visible in the monitor and the swarm is `paused`; raise the budget and resume; confirm it completes.

**Acceptance Scenarios**:

1. **Given** a swarm with a budget, **When** the budget is reached, **Then** no new run starts, the swarm is `paused`, the monitor says which limit and the launcher is notified.
2. **Given** a paused swarm, **When** a person with the right to do so raises the budget or resumes, **Then** the next pending delegation starts.
3. **Given** the platform emergency stop engaged, **When** a swarm is live, **Then** it ends `cancelled` like any run, and no swarm can be launched until the stop is lifted.

### Edge Cases

- A sub-run started by a `work_item` trigger on another agent: it belongs to the swarm of the run that created the work item when that run is in a swarm; a work item created by a person starts a new swarm when claimed.
- A run that joins two swarms (a consult back to an orchestrator already in the chain is refused by the existing circular-handover rule, so this cannot happen; a work item created by swarm A and claimed by a run of swarm B is drawn in both, attributed to A).
- An A2A partner call: a node of kind `external` drawn with the partner's agent card name; its internal events are unknown, only its start, its result and its failure are events.
- A transfer: the conversation moves to another agent; the swarm continues with the new agent as a node of the same tree, the former node marked `transferred`.
- A run killed by a rollout and resumed: one node, two attempts, drawn once with its attempt count; the gap is an event (`run.resumed`).
- A swarm older than the run retention: its events are purged with its runs (same retention), and the list says `events purged` on it.
- An event stream longer than the browser can animate: the live feed coalesces events older than the viewport, the replay still steps through all of them.
- A person loses access to the root agent while watching: the monitor closes with the existing access-lost message.
- The launcher is a flow (feature-flagged, owned by Romain D.): the flow run is the root; the flow's own view stays as it is; the swarm shows the agent nodes the flow ran.
- Two replicas write events of the same swarm: ordering is by a per-swarm sequence taken under the existing advisory-lock pattern (Principle IV), never by wall clock.

## Requirements *(mandatory)*

### Functional Requirements

**Identity**

- **FR-001**: The system MUST assign a swarm id to every root run and propagate it to every run, background task, consult, transfer, self fan-out copy, A2A partner call and work item claim that descends from it.
- **FR-002**: The system MUST record on each run its parent run in the swarm and the kind of the link (`delegate`, `consult`, `transfer`, `self`, `a2a`, `work_item`, `flow`).
- **FR-003**: The system MUST record the swarm's launcher: kind (`agent`, `fleet`, `assignment`, `flow`, `channel`, `api`, `swarm_tab`), id and display name, and the person it acts for.
- **FR-004**: The system MUST derive the swarm's status from its runs: `paused` whenever a budget holds it, including while live runs finish their current step; otherwise `running` while any run or background task is alive, `waiting` when every live run waits for a person, then `completed`, `failed` or `cancelled` once the last run ends; after a budget is raised and the swarm is resumed, it MUST derive `running` or `waiting` from the live runs; it MUST set the end time when the last run ends.
- **FR-005**: A run that spawns nothing MUST be a swarm of one with the same fields.

**Events**

- **FR-006**: The system MUST persist an ordered event per state change of a swarm: run started, resumed, ended (with status), handover started and returned (with kind, caller, callee, subject), person awaited and answered, work item claimed, completed or released, file produced, budget reached, swarm paused, resumed, cancelled, completed.
- **FR-007**: Events MUST carry a per-swarm monotonic sequence number written safely by several replicas (Principle IV), the agent and run they concern, and a subject of at most 200 characters.
- **FR-008**: Events MUST NOT carry tool arguments, tool results, prompts or message bodies; they carry references (run id, work item id, file id) the reader resolves with the existing access checks.
- **FR-009**: The system MUST serve a swarm's events as a page by sequence and as a live stream of new events, to anyone who can see the swarm's root run.
- **FR-010**: Events MUST follow the retention of the runs they belong to and be purged with them.

**Swarms tab**

- **FR-011**: The Agentic Flow page MUST offer a `Swarms` tab after `Fleets`, listing the swarms the person can see, live first, with launcher, root agent, fleet, status, run counts by status, active delegations, start, duration and cost (when costs are shown to that person).
- **FR-012**: The tab MUST offer search, filters (status, fleet, launcher kind, agent, period) and pagination with the same components, page sizes and URL parameters as the Agents list.
- **FR-013**: Live rows MUST refresh without a reload at the platform's standard refetch interval or faster when the stream delivers an event.

**Monitor**

- **FR-014**: Opening a swarm MUST show its graph: one node per agent involved (and per external partner), laid out as the run tree's hierarchy, or as the fleet's hierarchy when every internal node is a home member of one fleet (other agents as ghost nodes, as on the fleet graph).
- **FR-015**: Each node MUST show its state (`idle`, `running`, `waiting for a person`, `waiting for a sub-run`, `done`, `failed`, `cancelled`, `transferred`), its live run count and its attempt count when resumed.
- **FR-016**: Each handover in flight MUST be drawn on its edge as a marker travelling from the caller to the callee, the edge lit until the sub-run ends, and a return marker when a result comes back; reciprocal edges are drawn as arcs, as the agent map does.
- **FR-017**: A header MUST show the event count, the active delegation count, the latest handover (kind, caller, callee, subject) and the swarm's status, start and duration.
- **FR-018**: Selecting a node MUST bind a right rail to it: its runs in this swarm (status, result preview, link), its recent events, the work items it claimed or created, the files it produced, and, when it waits for a person, the existing approval or answer control.
- **FR-019**: The monitor MUST be rendered from the persisted events only, so that the live picture and the picture after the fact are the same function of the same data.
- **FR-020**: The monitor MUST reuse the agent map's canvas, layouts, toolbar, hover card, zoom and keyboard navigation rather than a second graph implementation.

**Replay**

- **FR-021**: The monitor MUST offer a timeline over the events: scrub, play and pause at 1x, 2x, 4x and 8x, step by one event, jump to the next or previous handover, failure or result; the header MUST name the current event.
- **FR-022**: Scrubbing a live swarm MUST pause the live feed and offer `Live` to return to the head.

**Fleet graph**

- **FR-023**: A fleet page whose fleet has at least one live swarm MUST draw the swarm's state on its graph (live mode), with a chip per live swarm to isolate one; the Fleets tab card MUST show the live swarm count.

**Launch and control**

- **FR-024**: A person MUST be able to launch a swarm from the tab on an agent they can use or on a fleet (its orchestrator as root) with a task; the root MUST be an ordinary run of that agent, and the monitor MUST open on it.
- **FR-025**: A person who can see a swarm and manage its root agent MUST be able to cancel it; cancellation MUST end every run and background task of the swarm through the existing cancellation and release the work items it holds.
- **FR-026**: A `swarms` built-in tool MUST let an agent list, read, launch, pause, resume and cancel the swarms the person it acts for could from the tab; it MUST be granted like the `fleets` tool (company-managed, seeded on the General Assistant and the CTEM Assistant alike).
- **FR-027**: `Create with a prompt` on the tab MUST seed a chat on an assistant carrying the `swarms` tool, and be disabled with guidance when none does.

**Budgets and stop**

- **FR-028**: A swarm MUST accept at launch a budget (tokens, cost, wall-clock, maximum runs, maximum depth), defaulted from the fleet's defaults when the root agent has a home fleet with them, else from the platform's agent budgets.
- **FR-029**: Reaching a budget MUST pause the swarm: no new run starts, running runs finish their current step, the monitor names the limit, the launcher is notified; a person with the right MUST be able to raise the budget and resume.
- **FR-030**: The platform emergency stop MUST cancel every live swarm and refuse new launches while engaged.

**Access and visibility**

- **FR-031**: A swarm MUST be visible to whoever can see its root run; inside it, a node whose agent the person cannot see MUST be drawn anonymised with no rail content; events of that node MUST expose nothing beyond kind and time.
- **FR-032**: Launching on a fleet MUST require the right to chat with its orchestrator; cancelling, pausing and changing a budget MUST require the right to manage the root agent, or administrator rights.

**Documentation and editions**

- **FR-033**: `docs/docs/usage/agentic-flow.md` MUST gain a `Swarms` chapter and `work-history.md` a pointer from a run to its swarm; the glossary above (agent, fleet, swarm) MUST appear in the documentation once.
- **FR-034**: Swarms are part of the agentic AI features of the Enterprise Edition, like fleets: without the licence the tab MUST say so instead of listing swarms; identity and events (FR-001 to FR-010) MUST still be recorded, since Work History relies on them.

### Key Entities *(include if feature involves data)*

- **Swarm**: one execution. Id, root run, launcher (kind, id, name, acting person), fleet (optional, the root agent's home fleet at launch), status, started at, ended at, budget (optional), counts (runs by status, active delegations, events), cost so far, retention bound to its runs.
- **Swarm node**: an agent's participation in a swarm. Agent (or external partner), kind (`internal`, `external`, `ghost`), state, runs in the swarm, attempt count. Derived from the events, never stored on the agent.
- **Swarm link**: a run's place in the tree. Child run, parent run, kind (`delegate`, `consult`, `transfer`, `self`, `a2a`, `work_item`, `flow`), subject, started at, ended at, outcome.
- **Swarm event**: an ordered, persisted fact. Swarm id, sequence, time, kind, agent, run, references (work item, file, person), subject. Never a payload.
- **Swarm budget**: the limits of one swarm and what has been consumed against each.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A person opens the monitor of a live swarm and sees a handover drawn within 2 seconds of its event, on a swarm of up to 200 runs and 5,000 events, at 60 frames per second on the reference laptop used for the agent map.
- **SC-002**: The picture of a finished swarm reloaded from events is pixel-identical (same node states, same edge states, same counts) to its last live frame, on every e2e scenario of the feature.
- **SC-003**: 100% of runs created after the feature ships carry a swarm id and, when spawned, a parent run and a link kind (checked by a CI test over every spawning path: delegate, consult, transfer, self fan-out, A2A, work item, flow).
- **SC-004**: On the CEO Office fleet, the time for its owner to answer "what is running now and where is it stuck?" drops from reading the run list and shared state (measured at launch, baseline recorded in the issue) to opening one page, verified on three real programs within the first month.
- **SC-005**: Zero events carry a prompt, a message body, a tool argument or a tool result (CI test on the event writer).
- **SC-006**: The swarm monitor and the fleet live mode share one projection module and one canvas: no second graph implementation (code review gate, named in the plan).

## Assumptions

- A run tree is the right unit of a swarm: every run of XTM One already knows the run that spawned it (delegation, consult, self fan-out) or the work item that woke it; the feature names and persists what is implicit today. Where a spawning path does not record its parent (to be audited in the plan: channel-driven turns, A2A inbound), the plan adds it.
- The event store is append-only and bounded by the run retention; no new infrastructure is assumed (Postgres with the existing advisory-lock pattern, the existing SSE or WebSocket channel used by chat and Work History for the live stream).
- The agent map and fleet graph ship first; the monitor is a projection drawn on that canvas, not a new one.
- Fleet edition (Fleets program phase 3d) ships before User Story 5; User Stories 1 to 4 do not depend on it.
- Flows keep their own view and owner; a flow run is a launcher, nothing more.
- Costs are shown per swarm only where the platform already shows costs to that person (Platform Insights rules).
- Retention, the default budget values and whether a swarm of one is listed by default on the tab (or only when filtered) are decided when the implementation program opens; they do not change the shape above.

## Out of scope

- A new execution engine, scheduler or worker pool: a swarm runs on assignments and agent runs (Principle I).
- A new agent kind ("worker", "specialist"): the nodes are the existing agents.
- Replacing Work History or the run detail page: the monitor links to them.
- Swarm templates (a saved launcher plus task to run again), swarm-to-swarm links, cross-platform swarms over A2A beyond drawing the partner node: candidates for a later PRD.
- Any change to the flows feature flag or to the flows views.

## Relationship to the Fleets program

- Phase 5 of the Fleets program ("live swarms on the fleet graph") is User Story 5 of this PRD; it is delivered under this PRD, after fleet edition, reusing the monitor's projection.
- The `swarms` built-in tool mirrors the `fleets` tool: same granting, same seeding on the default assistants of both deployments (Copilot mode and XTM One mode).
- The Swarms tab is the third and last tab of the Agentic Flow page (Agent-to-agent map, Fleets, Swarms), as Samuel Hassine set for the page on 2026-10-31 (three tabs, the agent-to-agent map first).

## Clarifications to settle when the program opens

- Q1: Should a swarm of one be listed on the tab by default, or only under a filter? Recommendation: hidden by default, shown with the filter `include single runs`, so that the tab reads as "what is orchestrated" rather than "every chat turn".
- Q2: Retention of events: the runs' retention (recommended) or a longer one for swarms a person pinned?
- Q3: Pause semantics on a budget: refuse new runs only (recommended, no mid-step interruption), or also suspend running runs at their next tool call?
- Q4: Which spawning paths do not record their parent today? To be audited in the plan before FR-001 is sized.
