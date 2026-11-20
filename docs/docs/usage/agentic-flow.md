# Agentic flow

`Agentic Flow`, opened from the main sidebar, shows how your agents cooperate: who hands work to whom, which agents work together as a fleet, and what is scheduled to run.

## What Agentic Flow is for

Use this view when you want to:

- understand how one agent hands work to another
- see the structure behind a shared workflow
- inspect the path a task can follow
- review or adjust that structure, if you are allowed to edit it

## Understand the handover types

The graph uses named connection types between agents.

- `Delegate` sends work to another agent in the background.
- `Consult` asks another agent for help and returns to the original agent.
- `Transfer` moves the conversation to another agent.
- `Mandatory transfer` forces the conversation to continue with another agent.

These labels matter because they change how the user experience behaves in chat and in automation.

A consulted agent can consult in turn. When the agent asked needs another agent's answer and has that agent as a `Consult` handover, it asks it before answering, so its answer carries what the other agent found: an orchestrator consulting a clerk that needs a figure from an analyst gets the clerk's answer with the analyst's figure. While it is being consulted, an agent only consults: it never transfers the conversation and never starts background work. A chain of consults stops at the platform's delegation depth (five levels), and an agent already in the chain is never asked again - a consult that would loop back to it is refused as a circular handover. Each nested consult shows in the `Activity` tab under the run of the agent that made it.

An agent running a background task can ask the agent that gave it the task a question. Handovers back to an agent already in the chain are refused, so that a task never loops back to its orchestrator; one exception: when the agent that spawned the task is also one of its `Consult` handovers, the task can consult it, up to three times per run by default (`DELEGATED_PARENT_CONSULT_LIMIT`, 0 turns it off). The orchestrator answers from what it knows and its own tools, without consulting or delegating to anyone in that answer. A background task back to the orchestrator stays refused.

An agent whose handover agents have no type yet (configured before handover types existed, or through the API without types) shows each of them as `All types`: it can delegate to, consult or transfer to each one. Types are set per agent, so giving one of them a type, on the agent's configuration page or on this graph, gives every one of them a type: the others become `Delegate`, and you are shown which ones and asked to confirm first. Saving the agent's handovers without picking a type keeps them `All types`, and so does adding a handover agent next to them. Once the handover agents have types, removing the only typed one, or deleting that agent, leaves the others `Delegate`: they never switch back to `All types` on their own.

An agent can also delegate to itself: faced with a large workload, it can split the work across background copies of itself that run in parallel, even with no handover agent configured. This is on by default. To keep an agent from spawning copies of itself, turn off `Parallel copies of itself` in the `Agent Handover` card of its configuration; it can still delegate to its handover agents, and copies already running finish their work. When an administrator has switched the capability off for the whole platform (`AGENT_SELF_FANOUT_ENABLED`), no agent spawns copies of itself, whatever this option says.

## The global Agentic Flow page

Choose `Agentic Flow` from the left sidebar. The page has up to three tabs:

- `Agent-to-agent map`, the tab it opens on: the agents in scope and the handovers between them
- `Fleets`: the fleets you can see, each with its own page (see [Fleets](#fleets))
- `Flows`: the flow library, when your platform has flows turned on

The map has two views, chosen with the switch above it:

- `Graph` (the default): your agents in one graph (`Mine`), or the agents of another segment, the handover links between them, a focus field to isolate one or more agents, on a graph you can pan and zoom
- `Schedule`: the assignment triggers and one-off scheduled items of the same agents, on a timeline of the next hour, the next 24 hours or the next 7 days. An agent's `Coming up` card opens this view narrowed to that agent, and `Show every agent` widens it back. An old link to `/flows/schedule` opens this view.

The tab and the view are kept in the address (`/flows/map`, `/flows/map/schedule`, `/fleets`), so a link reopens the same screen and the browser's back and forward move between them.

Scroll the mouse wheel or pinch on a trackpad to zoom in or out where the pointer is. Drag an empty area of the map to move it. Double-click an empty area to fit the whole map back in view. While the pointer is over the map, the page does not scroll.

## Choose whose agents the map draws

The toolbar above the graph starts with the same visibility filter as every list page: `Mine | Company | Shared | All`, each with the number of agents it holds. When neither the address (`?vis=`) nor your last pick names a segment, the map opens on the first one that holds an agent: `Mine`, else `Company`, else `Shared`, else `All`.

- `Mine` draws the agents you own and the handovers between them.
- `Company` draws the agents published to the whole company.
- `Shared` draws the agents shared with you directly or through a group.
- `All` draws every agent you can see.

A handover to an agent outside the segment is not drawn. The counts under the graph (agents, fleets, handovers, runs in flight, open work items) are those of the segment shown. When you own no agent but others are visible, one line under the toolbar says so, with a link to `All`.

The segment is kept in the address (`?vis=mine`, `?vis=all`...), so a link reopens the same map. A link that focuses an agent (`?focus=`) and names no segment opens on `All`. The `Filters` menu keeps the other filters (agent type, enabled, activity); they apply inside the segment.

## See fleets as one node

When some of the agents you can see belong to a fleet, the map draws each fleet as a single node by default: the fleet's name and icon in place of its members. A handover from an agent to a member is drawn to the fleet node, a handover from one fleet to another is drawn once between the two fleet nodes, and the handovers inside a fleet are left to the fleet page. The counts under the graph are those of what is drawn, fleets counted apart.

Click a fleet node to open its fleet page. A fleet node is never the start or the end of a new handover: handovers stay between agents, so turn off the `Fleets` switch in the toolbar to draw every agent and edit their handovers.

## Follow the map as it changes

When the map changes (another layout, other force settings, the `Fleets` switch, a focus, a filter, an agent added or moved), the agents slide from where they are to their new place in under a second. New agents fade in where they land and removed ones fade out, so you can follow each agent across the change. If your system asks for reduced motion, the map draws the change at once.

Hover an agent to dim every agent and handover that is not one handover away from it. Click `Fit to view` in the zoom controls to frame every agent.

Drag an agent to move it: it stays where you drop it, with its handovers, until you change the layout or the agents shown. From the keyboard, focus an agent with `Tab` and move it with the arrow keys (hold `Shift` for larger steps). Hold `Alt` with an arrow key to move the focus to the nearest agent in that direction instead. Dropped or moved onto another agent, it goes to the nearest free spot. A moved agent carries a small dot on its ring. Click `Unpin agents` in the zoom controls to put every moved agent back in its place. Agents with no handover sit with the other agents of their fleet (under the fleet's name when the map shows several fleets), apart from the other fleets and the agents of the playground. When the map is too dense to draw any name, a hint at the bottom left says to zoom in or hover an agent to read its name.

## Use the focus field

Large graphs are easier to read when you focus on a few agents.

Use the focus field to:

- highlight one agent
- compare a few related agents
- reduce visual noise in a large environment

You can add and remove focused agents directly from the filter area, or double-click an agent on the graph. A focused agent that is no longer on the graph (a deleted agent, an old link) is ignored: when none of the focused agents is on it, the whole graph is shown.

Once an agent is focused, choose how the graph shows it:

- `Show only` (the default) lays out the focused agents alone: each one, every agent it hands work to or receives work from, and the handovers between them. Double-click one of those agents to add it to the focus and bring its own handover agents in, one step at a time. The counts under the graph are those of what is shown. A handover is created between two agents on the graph: to connect an agent that is not shown, add it to the focus or switch to `Highlight`.
- `Highlight` keeps the whole graph and dims every agent outside the focus.

## Open an agent from the graph

You can open an agent directly from the global graph.

This is useful when you want to move from a visual connection view to the full agent detail page.

## Editing the global graph

If your role allows it, the global graph is not only a viewer. It can also be an editing surface.

Authorized users may be able to:

- create a new handover between two agents
- change the type of an existing handover
- remove a handover

Click an agent to select it and open its details panel. To create a handover, press the connection icon at the bottom right of an agent you can manage and drag it onto the target agent, then pick the handover type. Releasing the icon without moving starts the handover too: click the target agent to end it, or press `Esc` to cancel. From the keyboard, press `Space` on the focused agent to start a handover and `Space` or `Enter` on the target to end it; `Enter` at rest opens the details panel.

Because these are global relationships, a change here affects all users who rely on that agent behavior. When the platform refuses a handover, the message says why: a handover to a member of a fleet that does not expose it to other fleets, for example, names the fleet and points to its orchestrator.

## Fleets

The `Fleets` tab lists the fleets you can see: groups of agents that work together around an orchestrator and one workspace. Each card shows the fleet's orchestrator, how many home members and connected agents it has, and its workspace. A building icon marks a company-managed fleet, a shared icon a fleet another user shared with you.

### Create a fleet

The tab offers two ways, on the empty state and above the cards:

- `New fleet` opens a form: a name and a description, the workspace the fleet coordinates in (one you manage, or a new one named after the fleet), the orchestrator and the home members, the connected agents with their collaboration instruction and the context they receive, and which home members other fleets may reach (the orchestrator always can). Only agents you manage that have no home fleet yet can be home members, since an agent has one home fleet. `Create fleet` opens the new fleet's page.
- `Create with a prompt` opens the chat with a sentence to finish ("Create a fleet named ... in the workspace ... with ... as orchestrator and ... as members"), on an assistant that carries the `Fleets` built-in tool. The assistant creates the fleet and answers its link. The button is disabled when no assistant carries the tool: turn on `Fleets` in an agent's `Built-in Tools`, or in the General Assistant's.

The `Fleets` built-in tool lets an agent list, read, create, update and delete fleets, set their members and their exposure. It acts as you: it sees and changes only the fleets and agents you could from the tab, and company-managed fleets stay for administrators.

Click a card to open the fleet page:

- the header names the fleet, its workspace and its orchestrator, and its `Settings` button opens the fleet settings
- `Graph` draws the handovers between the fleet's agents (its home members and the agents it connects) with the same layouts and toolbar as the agent-to-agent map. An agent that is not a home member is drawn as a ghost node: a dashed outline, less emphasis and the name of its home fleet under its own (nothing for an agent in no fleet). That covers the agents the fleet connects and the agents at the far end of a handover that crosses the fleet's boundary. A ghost node only opens the agent when you click it. Only the handovers the fleet graph returns are drawn: a handover between two agents that are both outside the fleet is left out. A cross-fleet handover that no connected agent governs, or that reaches an agent its own fleet does not expose, is drawn with a dotted line, and its tooltip says which. An agent you cannot read is not drawn.
- `Data flow` shows where the fleet's work starts and where it goes, in three lanes: the sources that start its members (integration events, schedules, platform events), the members, and the integrations they write to. Hover a member to light its own sources and outputs; click a source to read its assignments and filters. Opening it reads the configuration only: it starts no run and uses no quota. The address `?view=flow` opens it, and so does an older link to the graph's former `Data flow` switch.
- `Schedule` shows the assignment triggers and one-off items of the fleet's home members (the agents it connects keep them in their own fleet)
- `Members` lists the home members you can see as the fleet's hierarchy (the orchestrator first, each member under the one it reports to) with their role and their health, what needs a look first: `Failing` (a run failed in the last 24 hours), `Stale` (open work items left waiting), `Disabled`, `Idle` (no run yet) and `OK`, with the last run and the runs and work items in flight. Click a member to open the agent. When the fleet has members but none is shared with you, the panel says so. Below them, `Connected agents and fleets` lists what the fleet calls without owning it.

A fleet lives in one workspace, where its agents coordinate: work items, shared state and knowledge. The workspace survives the fleet, not the reverse: deleting a fleet keeps its workspace with its work items and shared state, a workspace that hosts a fleet cannot be deleted, and one that hosts a live fleet cannot be archived. The workspace's card in `Work > Operations > Workspaces` names the fleets it hosts (see [Fleets and workspaces](workspaces.md#fleets-and-workspaces)).

### Edit a fleet

The owner of a fleet, the people granted configuration on it and administrators edit it from its page. Anyone else sees the same panels with every control disabled and the reason.

`Settings` opens a side panel: name, description, icon and colour; the workspace the fleet's agents coordinate in (one per fleet); the orchestrator, chosen among the home members; the members other fleets may reach besides the orchestrator; visibility and sharing, as for workspaces; archive or restore; delete. Deleting a fleet never deletes an agent: its members go back to the playground with their handovers and assignments, and the workspace is kept.

The panel's `Run defaults` section sets what one run of a home member may use when the agent sets nothing of its own: the four budget limits of the agent `Budget` tab (cost, tokens, tool calls, duration) and `Max iterations`. An agent's own value always wins, a connected agent keeps its own home fleet's defaults, and an empty field inherits the platform budget; clearing every field removes the fleet's defaults. You can lower a limit below the platform budget; a value above it is outlined with the reason and only an administrator can save it, as on the agent `Budget` tab.

Under the members panel, the people who manage the fleet see `Fleet cost`: the priced spend of the runs of its home members over the last 7, 30 or 90 days, in total and by agent. A connected agent's runs count for its own home fleet.

A run's units start unreviewed. Whoever may act on the run (an administrator for a shared run) marks its outcome with `PUT /api/v1/roi/records/{record_id}/outcome` and `{"outcome": "accepted" | "modified" | "rejected", "review_min": 5, "note": "..."}`, or `{"outcome": null}` to clear it. A rejected run counts no value, a modified one counts its value less the review minutes, an unreviewed one counts like an accepted one and is shown apart: each group of the report carries `reviewed`, `unreviewed`, `rejected` and `review_rate`. Administrators list the records to review with `GET /api/v1/roi/records?outcome=unreviewed`, and the runs list takes `review=unreviewed`.

In `Members`:

- `Add a member` offers the agents you manage that have no home fleet: an agent has one home fleet or none.
- Drag a member onto another to make it report to that one, or onto `Top level`; from the keyboard, the member's menu has `Change parent...`. A member never reports to itself or to one of its own reports.
- The member's menu also sets its role (a label shown beside its name, the agent does not change), exposes it to other fleets, moves it to another fleet you manage, or removes it from the fleet. A move happens in one step: the member leaves this fleet and joins the other at top level, not exposed and without a role. When the other fleet refuses it (archived, company-managed for a personal agent, already connected to that agent), nothing changes and the member stays where it was, role, parent, exposure and orchestrator included. Moving the orchestrator leaves this fleet without one until you choose another. The agent keeps its handovers and assignments. An agent with the `fleets` tools does the same with `move_fleet_member`.
- `Connect an agent` and `Connect a fleet` add a connected row; each says when the fleet calls it (the collaboration instruction) and what context it receives (none, a summary or the full conversation).

On the `Graph` view, the handovers of the home members are edited as on the agent-to-agent map: draw a handover from a member you can manage to another agent of the graph, or click a handover to change its type or remove it. Ghost nodes are never the start of a new handover here; edit them from their own fleet. A handover to a member of another fleet that its fleet does not expose is refused, and the message names the agent and its fleet. Nothing is editable on the graph of an archived fleet. Unlike membership, a handover is part of the source agent's configuration: changing it is an agent update.

Right-click a node of the fleet graph to open its menu:

- `Open the agent` opens its page.
- `Focus on its handovers` dims every agent except this one and the agents it hands over to or receives a handover from; `Clear the focus` (or a double click on the node) shows them all again.
- On a home member of a fleet you can manage, and only while the fleet is not archived: `Add a handover from it...` picks a target among the home members and a handover type (it needs the right to edit the agent, as drawing the edge does), `Change parent...` picks who it reports to in the fleet, and `Remove from the fleet` takes it out after a confirmation; the agent goes back to the playground, unchanged.

A ghost node's menu only opens the agent or focuses on it.

On the graph of a fleet you can manage, and only while it is not archived, drag a home member's node onto another home member to make it report to that member. While you drag, a line follows the pointer: solid over the member it would report to, which is ringed, and dashed grey over a node it cannot report to (itself, one of its own reports, its current parent, a ghost), where the pointer shows it is not allowed and dropping changes nothing. After a drop, the new parent stays ringed for a moment. The drag needs a mouse; from the keyboard, use `Change parent...` in the member's menu. Press `Escape` or release outside a member to cancel. A short click still starts a handover. To move a member back to the top level, use `Change parent...` or the members panel.

None of the membership changes edits an agent: membership never creates a configuration version or triggers an evaluation. An archived fleet keeps its settings editable, to restore it, and its members frozen: adding, changing, removing or exposing a member is refused until it is restored, in the panel, through the API and through the `fleets` tools alike. Changing the orchestrator never leaves the previous one exposed unless it was exposed on its own.

### Set a fleet's run defaults and read its cost

Run defaults and the fleet's cost are on the fleet page: `Run defaults` in the `Settings` panel, and the `Fleet cost` card under the members panel (see [Edit a fleet](#edit-a-fleet)). A member that sets its own value keeps it; a limit it leaves empty takes the fleet's; what neither sets is the platform's. A connected agent runs on the defaults of its own home fleet, never on those of the fleet that calls it, and its runs count in its own home fleet's cost.

### Export and apply a fleet spec

A fleet can be kept as code: its spec (`apiVersion: xtm-one/fleet/v1`, `kind: Fleet`) carries the fleet's name, description, icon, colour and workspace name, the card's skills and accepted handover types, the fleet's autonomy level (`autonomy_level`), the orchestrator, and each home member with its role label, whom it reports to, whether it is exposed, its handovers to other members and its agent in the agent export format. Secret values never travel, and an agent you can see but not manage travels by reference only. Connected agents and fleets travel by reference with their collaboration instruction and context inclusion.

- Export: ask an assistant that carries the `Fleets` built-in tool to export the fleet (`export_fleet_spec`).
- To export the structure only, ask for the spec without the agent bundles (`export_fleet_spec` with `bundles=false`). Every member is a reference to its agent (its slug) with its role label, parent, exposure and handovers, a few kilobytes where the agent bundles of a large fleet weigh megabytes. The tool writes the full spec, bundles included, to a file of the conversation and names it; it does the same on its own when the full spec is too long to answer inline. A structure-only spec is a valid spec: applied, each member is adopted by its slug.
- Apply with the `apply_fleet_spec` tool (the spec inline, or a file of the conversation holding it, such as the one `export_fleet_spec` wrote). It runs as a dry run by default: the answer is the plan (each object created, updated, added, removed or unchanged), and nothing is written until you ask the assistant to apply it for real.

Applying is idempotent. The fleet is found by its slug among your own fleets, and each member by its key, the agent's slug: an agent you manage under that slug joins the fleet as it is and is never edited (a difference with the spec is reported as a warning); otherwise the agent is created from its bundle, with its handovers to the other members. Home members the spec does not name leave the fleet, and their agents are kept. A new fleet is created at the spec's autonomy level, so it never runs a moment more autonomous than the spec says; on an existing fleet the spec can lower the level, and a raise is reported as a warning and left to an administrator on the fleet page. The `create_fleet` tool takes the same `autonomy_level`. A reference that cannot be resolved for you (an agent you cannot manage, one already home in another fleet, a connected agent you cannot see) is listed and blocks the apply, unless you ask for a partial apply. A spec applies to a personal fleet only: the export of a company-managed fleet applied under its slug is refused whole, dry run included, with the reason, rather than planned as a personal fleet without its company-managed members. Change a company-managed fleet on its fleet page or with the fleet tools, or apply its spec under another slug. A fleet exported, deleted with its agents, and applied again exports the same spec.

### Fork a fleet

Any fleet you can see can be forked into a personal copy that you can fully edit, a built-in fleet included. Built-in fleets are read-only and rewritten by the platform at every start; a fork is how you adapt one to your own needs. Fork through `POST /api/v1/fleets/{id}/fork` with an optional `name` (default `Copy of` the source's name), an optional `workspace_id` (default: a new personal workspace) and `members`:

- `copy` (the default): every member agent becomes an editable personal copy, as `Duplicate` does on an agent page. The hierarchy, exposure, role labels and orchestrator follow, and the handovers between members point at the copies.
- `keep`: the orchestrator is copied, and the other members are linked to the fork as connected agents (an agent has one home fleet), with their role label as the collaboration instruction.

The fork also carries the card's skills, the accepted handover types, the run defaults (lowered to your ceiling) and the connected agents and fleets you can see. Every copied automation is created switched off, whatever its state on the source, so a fork starts no run and uses no quota until you enable an automation yourself. The fork records where it came from (`forked_from`: the source fleet, whether it was built-in, and a fingerprint of its structure at fork time), so a later change to the source can be detected. `GET /api/v1/fleets/{id}/source-changes` answers whether the source changed since the fork and lists what moved: the fleet card fields, the members added, removed or changed (role, parent, exposure, persona, skills, handovers) and the connected agents and fleets. The platform never touches the fork again.

### Generate a fleet spec from a prompt

Describe the fleet you want ("an orchestrator that triages incoming alerts, with an analyst, a hunter and a reporter") and let the model write its spec:

- Ask an assistant that carries the `Fleets` built-in tool: it calls `generate_fleet_spec`, shows you the spec and its plan, and applies it with `apply_fleet_spec` once you agree. Ask it to change the spec before applying; it refines the same spec rather than starting over.

Generating never writes anything: the answer is the spec and the exact dry-run plan of applying it. The model sees the agents, fleets and workspaces you can see. An agent you manage that has no home fleet may be reused as a member; any other existing agent can only be connected. A new member is created with a name, a description and a persona, and nothing else: add its tools, model and integrations on its agent page after the apply. Generating needs AI enabled and the agentic AI licence, like applying.

### Keep a fleet's documentation current from an assistant

A fleet's About documentation is always generated by AI. An assistant that carries the `Fleets` built-in tool regenerates it with `regenerate_fleet_documentation` (the whole documentation, or one section), with the same permission as editing the fleet's configuration. After a change to a fleet's configuration (`update_fleet`, its members, their hierarchy, a move, an applied spec), the answer says `documentation_out_of_date: true` when the documentation no longer matches, so the orchestrator knows to regenerate it once. On a built-in fleet the result is a local version; the shipped one can be restored from the About page. Each regeneration is one model call counted on the run or conversation that makes it; nothing regenerates on its own.

A suggestion of the documentation dismissed from the About page or with `list_fleet_doc_findings(action="dismiss")` stays dismissed however a later regeneration words it: the same kind of point on the same member, about the same subject, keeps its id and stays hidden. A dismissal lapses only when the member it names is reconfigured or after 90 days, and `list_fleet_doc_findings` then says why next to the suggestion that is back. A change of the fleet's approval gates never lapses a dismissal: a gate that fixes the point removes the suggestion itself.

A home member with no role label is raised as a suggestion at every whole regeneration, one per member, whatever the size of the fleet; it goes as soon as the label is set. An assistant sets or clears a member's role label with `set_fleet_member_parent` and its `role_label` argument (an empty label clears it; passed alone, the member keeps its parent), on any fleet it may manage, company-managed included; the answer names the old and the new label, and the change is audited like a change from the member menu.

### Share a fleet as a one-page datasheet

Once a fleet has documentation, **Export PDF** at the top right of its About page downloads a one-page A4 datasheet: the fleet's purpose and outcome, its status, a small graph of its members, what starts it, its steps and approval gates, the integrations and tools each member uses, the objects it creates, the prerequisites as they stand for you, the automations on, the estimated monthly runs and the unit of work, with the date and version of the documentation. It is built from the stored documentation, so it calls no model, uses no quota and works with AI switched off. A part the documentation does not cover is printed as "Not stated in the documentation"; a long list ends with "+N more" so the sheet always fits on one page.

### Follow a fleet's operations

The fleet page has seven views: `Graph`, `Data flow`, `About`, `Members`, `Schedule`, `Operations` and, for the fleet's managers, `ROI`. `Operations` shows what the fleet's home members run, delegate and produce, its orchestrator included and its connected agents left out: `Runs` (the default), `Background Tasks`, `Work Items`, `Shared State` and `Worksets`. Each list is the Operations page's own, with its filters, pagination and details, locked on the fleet, so it has no `Fleet` filter; the same holds for the `Operations` tab of a workspace, locked on the workspace. The list is kept in the address (`?ops=work-items`), so a link reopens it. To compare fleets or look across several, use the `Fleet` filter of the Operations page (see [Work history](work-history.md)).

### See an agent's fleet from its page

The `Overview` of an agent carries a `Fleet` card:

- its home fleet, as a link to the fleet page, with who it reports to (`Leads the fleet` for the orchestrator, `Reports to the orchestrator` at the top level, `At the top level: the fleet has no orchestrator` when none is set, or the name of its parent) and its role label;
- the chips `Orchestrator`, `Reachable from other fleets` and `Archived` when they apply;
- `Connected to`: the other fleets that connect this agent, with the context each one receives (`No context`, `A summary`, `The full conversation`, the words of the members panel) and its collaboration instruction;
- an agent in no fleet says so, with a link to the `Fleets` tab; an agent whose home fleet you cannot see says `In a fleet you cannot see.` without naming it.

Without the licence the card is not drawn; any other failed read says `The fleet could not be loaded.` on the card. The card is read only: `Edit in the fleet` (or `Open the fleet` when you cannot manage it) opens the fleet page, where membership is edited. Only the fleets you can see are named.

Fleets are part of the agentic AI features of the Enterprise Edition: without that licence the tab says so instead of listing fleets, and the agent page shows no `Fleet` card.

### Good practices for a fleet

These are suggestions drawn from fleets that run in production. The platform refuses nothing that departs from them; adapt them to the fleet's purpose. Every fleet orchestrator carries them as the built-in skill **Fleet design and operations**, and the setup assistant proposes the first one when you build a fleet. The built-in fleets ship with it: each of their members holds a `Consult` handover to the fleet's orchestrator.

1. **Two-way coordination.** Give each home member a `Consult` handover to the orchestrator, so it can ask a route, scope or context question; the orchestrator holds the handover types it needs to its members; members report to the orchestrator in the layout. A delegated task may consult the agent that spawned it when that agent is one of its `Consult` handovers.
2. **One entry point.** People talk to the orchestrator; members run from their automations, chat off, unless a person needs one directly.
3. **Reactive first.** Wake a member on an event (its work item inbox, a platform event, an integration event); keep a schedule for a permanent cycle someone reads at a fixed time, plus at most one safety net with its exit criterion; temporary work is a timer or a one-off scheduled item.
4. **Work through work items.** One key prefix per member, the owner set, a precise ask and deliverable, dedupe by key, `not_before` to defer and `due_at` for a real deadline, files attached; something wakes every item; the orchestrator reads results back before using them.
5. **Closed loop.** An item whose completion changes what the fleet runs on carries a fleet impact to the orchestrator, which listens on `work_item.completed`.
6. **Self-healing.** Listeners on the orchestrator for `run.orphaned`, `run.failed` and frictions; `get_fleet_health` weekly with the **Fleet health advisory** skill.
7. **Decisions to people.** Park an item for a person with a structured ask (options and a recommendation); read who is waiting live from the items; the run that closes a loop also resolves its shared state and memory.
8. **Shared state hygiene.** One stable key per thing, its terminal condition named at creation, resolved when done, lists bounded.
9. **Knowledge and records.** Generic, stable personas; procedures in skills, each rule written once and dated; durable facts in a knowledge base; short-lived state in memory or shared state. A fact in the knowledge base is a lead: the system of record wins. In more detail:
    - One shared working knowledge base per fleet, named `<Fleet> - Working knowledge`: the orchestrator holds the write grant, every member is bound to it read-only, so there is one writer and every member reads the same facts.
    - Members search it at the start of every task and propose updates in their run result (a `KNOWLEDGE:` line); the orchestrator applies them.
    - Numbered documents, one per subject, rewritten whole in place with a "Last rewritten" date. A good default set: 00 How this knowledge base works, 10 Fleet runbook, 15 Integration routing, 20 Counterparts directory, 30 Members' working notes, 40 Owner's standing preferences, 50 Programs context, 60 to 89 domain documents, 90 Lessons log.
    - The runbook mirrors the live fleet (members, accounts, assignments and their triggers, skills, shared state and memory keys, temporary assignments with their exit criterion, a rules registry, known gaps, a 14-day change log), under a size bound.
    - Credentials, secrets, identity numbers, personal data, message content and confidential figures stay out.
    - A weekly inventory read corrects the runbook against the live fleet; a change of the fleet's shape goes into the change log the same day.
    - Long documents are written in one write from a draft, never assembled across several writes.
    - Consistent names (`[<Fleet>] <Member>`, skills prefixed by their member, one work item key prefix per member, shared state keys `<fleet>:<thing>`); a retired member, assignment or skill is deleted once its replacement is verified; members share one settings baseline unless a reason is recorded.
    - Every member ends its internal run summary with what it used and its frictions; these lines stay out of what a person reads.
10. **Smart notifications.** Act by default on safe work, ask only for a real decision with a recommendation, tell the affected person once, nothing for a non-event.
11. **Verify.** Read back every write, do not assume ids, names or statuses, remove a run's test artefacts in that run.
12. **Least tools.** Scope each member's built-in tool groups to what it does.

The fleet's About page reads several of these practices off the configuration and lists what departs from them under **Suggestions**, with the findings of its documentation. The page computes them each time it opens, with no model call, for the people who manage the fleet:

| Suggestion | When it shows | What you can do |
| --- | --- | --- |
| Member cannot consult the orchestrator | A home member holds no handover to the orchestrator that runs as `Consult`. | **Apply** adds that `Consult` handover after one confirmation; the member's other handovers keep their type and the change is versioned on the agent. |
| Member open to chat | A home member other than the orchestrator takes chats. | **Open** the agent and turn chat off, or dismiss it when people talk to that member directly. |
| Work items without an inbox | A home member owns open work items and has no enabled automation triggered by its work items. | **Open** the agent and add that automation. |
| Orchestrator without listeners | The orchestrator has no enabled platform event automation on `work_item.completed`, `run.orphaned` or `run.failed`. | **Open** the orchestrator and add the listeners. |
| No working knowledge base | No knowledge base is bound to the orchestrator and every home member, and none the orchestrator writes is read by a member. | Create `<Fleet> - Working knowledge`; the setup assistant (`get_fleet_setup`) proposes it with five skeleton documents and creates it on your yes. |
| Orchestrator cannot write the knowledge base | A knowledge base bound to the orchestrator and every home member has no write grant for the orchestrator. | **Apply** grants the orchestrator write on it after one confirmation, versioned on the agent. |
| Member without the working knowledge base | A home member is not bound to the knowledge base the orchestrator writes for the fleet. | **Apply** binds it to the member, read-only, after one confirmation and the same binding checks as an agent edit; dismiss it with a reason when the member is kept isolated on purpose. |
| Knowledge base with several writers | A home member other than the orchestrator also writes the working knowledge base. | **Open** the member and keep it read-only, or dismiss it with a reason. |

**Dismiss** and **Restore** work as for the other suggestions: a dismissal, with its optional reason, holds while the member it names is unchanged. A suggestion goes by itself once the configuration follows the practice. Nothing is refused because of a suggestion.

## Next step

The next chapter explains [`Work History`](work-history.md), where you review runs, waiting items, notifications, and execution details.
