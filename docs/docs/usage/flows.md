# Flows

Flows are visual automations you compose on a canvas: nodes for triggers,
agents, conditions, actions and utilities, connected by edges that decide
what runs next. Unlike a chat conversation where the agent picks its own
route, a flow's graph is the plan — what you draw is what runs.

Open `Agentic Flow` in the navigation, then its `Flows` tab, to reach the
flow library; create a flow or open one to enter the builder.

## Compose on the canvas

Drag nodes from the left library onto the canvas, or double-click the
canvas to search. Connect the chevron pins to define execution order;
capability sockets along an agent node's edges extend that agent for this
flow's runs only (tools, knowledge bases, structured output, events).
Select a node to configure it in the right drawer — large fields open in a
bigger editor with the expand button next to their label.

A `Budget` node is the one capability worth knowing about before you
run anything expensive: wire it into several agent nodes and they share
one spending ceiling, or wire it into a `Flow` node and the ceiling
covers every agent of the flow you call. See
[Run budgets](agent-budgets.md).

A `Loop` repeats what hangs from its `Each item` pin once per item, then
fires `Done`; a `While` repeats its `Body` while its condition holds. Loops
nest: a loop placed in another loop's `Each item` branch runs all its passes
on every pass of the outer loop, before the outer loop moves to its next
item. Passes multiply: every node a run executes counts toward its limit
of 1000 steps, and a run that reaches it fails.

The builder validates as you type: errors block saving, warnings ride
along so unfinished drafts still save. A fresh platform seeds a pack of
educational examples — open any flow named `Example: …` and read its note
blocks.

## Read what an upstream node returns

A node reads the result of a node that runs before it with a reference
such as `{{step_1.output}}`. Type `{{` in a text field to pick one: the
list offers every value the upstream nodes publish, down to the fields of
a structured answer (`{{step_1.fields.severity}}`), with each value's type.

The `Input data` section at the top of a node's drawer shows the same
values as a tree, with the value each one had in the flow's last run.
Drag a field into a text field to insert its reference, or click it to
copy the reference.

The builder learns these fields from two places:

- the structure you declare: a `Structured output` node wired into an
  agent (or the agent's own JSON output schema), the parameters of a
  `Manual` trigger, the fields of an event type;
- the flow's last run, for any node it reached.

A field the declaration and the last run both know is marked as declared
and seen. Run the flow once to see the fields of a node that declares
nothing, such as a `Script` that prints JSON.

!!! tip "An agent's JSON answer"
    When an agent answers in JSON, read its fields with
    `{{agent_node.fields.KEY}}`. `{{agent_node.output}}` is the answer's
    text: a path below it, such as `{{agent_node.output.severity}}`, is
    always empty, and the builder warns about it.

!!! tip "An integration's data"
    An `Integration` node whose operation answers with data publishes it
    beside its text `result`. An object is `fields`, as for a `Script`: an
    OpenCTI container just created (`{{create.fields.id}}`), a GitHub
    GraphQL query, or a Notion database query with `output_format` set to
    `json`, which returns each row's properties as data (full date ranges,
    people and related pages by id) rather than text written for a reader.
    A list is `items`: the entities an OpenCTI listing found, each with its
    id, type and name. Give `{{list.items}}` to a `Loop`, or read one key
    of every item with `{{list.items.name}}`.

!!! example "One OpenCTI RFI from several reports"
    A `Manual` trigger `start` declares the run's input, such as a
    `question` parameter. An `Integration` node `list` (OpenCTI,
    `list_opencti_entities`) finds the reports, for example those carrying
    a label added in bulk in OpenCTI. An `Expression` node `names` writes
    their names as one line: `join(", ", {{list.items.name}})`. An
    `Integration` node `create` (`create_opencti_container`) creates a
    single `Case-Rfi`: `name` `RFI on {{names.value}}`, `description`
    `{{start.payload.question}}`, `object_ids` `{{list.items.id}}`,
    `case_types` the RFI's information types, and the markings and labels
    by id. A flow writes OpenCTI knowledge the way an assignment does: in a
    draft it opens, which someone validates in OpenCTI.

## Filter with a classifier model (CLM)

The `Classify (CLM)` condition hands a piece of content to a classifier
language model, such as TypeSafe's Jev or Cloudflare's Clef. The model does
not write an answer: it picks one of the labels you declare and says how sure
it is, in milliseconds and for a fraction of an agent run's cost. Put it in
front of an agent to keep the agent's tokens for what deserves them. For
example, sort the alerts a SIEM or an ITSM such as TheHive sends, so that
only the relevant ones reach the LLM.

- **Provider and model.** The model runs on one of the platform's LLM
  providers (Settings > AI Models). OpenRouter serves Jev
  (`typesafe/jev-1.13`) and OpenAI serves its own decision model. Left on
  the platform default, `Provider` is the one that lists the model, or the
  only provider available to you that serves decision models; when several
  qualify, pick one.
- **Labels.** Each label is a branch: the node grows one output per label,
  like a `Switch`. The description tells the model what belongs there. Two
  labels make a yes/no filter.
- **Confidence floor.** When the model is less sure than the floor, the
  content takes the `Uncertain` branch instead of its label's. Route that
  branch to the agent or to a person rather than drop it.
- **Errors.** A call that brings no decision back (provider unreachable,
  refusal, an answer outside your labels) takes the `On error` branch. With
  nothing wired there the run fails: nothing is filtered out silently.
- `{{node.label}}`, `{{node.confidence}}` and
  `{{node.probabilities.LABEL}}` are readable downstream.

The content is classified as data, but a crafted alert can still try to look
harmless. Keep a confidence floor and review what the filter drops.

!!! example "Example: TheHive alert intake and triage (CLM 1/2 and 2/2)"
    TheHive calls the intake flow's `Webhook` once per new alert (a TheHive
    notification on `AlertCreated` that calls a webhook). The intake only
    emits one `Example: TheHive alert received` event per alert, so its
    run lasts milliseconds even with `Concurrent runs` left at 1.
    Every event starts its own run of the triage flow, and those runs
    execute in parallel, so no dispatcher agent or script is needed. In each
    run, the classifier labels the alert. Alerts to investigate, and alerts
    it is unsure about, go to your TheHive Case Manager agent. Noise gets a
    comment in TheHive and spends no LLM token. To pull alerts on a
    `Schedule` instead, loop over them and `Emit event` for each one.

!!! note "How many runs a trigger starts at once"
    A `Schedule` or `Integration event` trigger never starts a run while the
    same flow has one active. A `Webhook` trigger lets deliveries start up
    to its `Concurrent runs` setting (1 by default, at most 50): runs of the
    flow started by any trigger count toward it, including a run paused on
    an `Ask user` question, and a delivery beyond it is answered with HTTP
    429 and a `Retry-After` header, so the sender retries.
    Raise it for a source that sends many deliveries at once, such as a SIEM
    or TheHive. `On event` triggers have no limit: every emission starts its
    own run.

## Run, observe, iterate

`Run` executes the flow immediately, drafts included — it is also the test
button. Each run keeps a step journal: per-node inputs, outputs, errors
and durations, with a live execution overlay on the canvas. The journal
streams while the run works — the node currently executing shows as
running in the trace and on the canvas the moment it starts, not after
the run ends — and stays available afterwards for step-by-step
troubleshooting of any past or failed run. `Activate` puts the flow's
triggers live (schedule, webhook, events, work items); deactivate
returns it to draft.

An integration-event trigger reads the events of the integration its node
names for the flow's owner: when the owner cannot see that integration, its
events are still delivered, the worker log names the flow once an hour and the
security audit lists it (see
[Who receives an integration's events](resources/integrations.md#who-receives-an-integrations-events)).

!!! tip "Ask a human mid-run"
    The `Ask user` node pauses the run and rings the bell with your
    question; the reply resumes the run and is readable downstream.

### When a flow's owner is deactivated

While an administrator has disabled the account that owns a flow, nothing
starts that flow unless it is company-managed - a flow shared with a group
or a person pauses too: not its schedule, webhooks, integration events,
work items or flow events, not a sub-flow call from another flow, and not a
manual run, whoever asks - an administrator included. The flow list and the
builder show it as paused because its owner's account is deactivated.

- nothing about the flow itself changes: it stays active or draft as it
  was, so re-enabling the account resumes it exactly as it was, and a
  schedule that came due meanwhile runs once
- a run that was waiting to start is marked `skipped` with the reason when
  it would have started, and its work item goes back to the queue; a run
  in progress finishes, but a question it asked or a breakpoint it stopped
  on waits until the account is back (cancelling still works)
- a webhook delivery to it is answered `ignored` with the reason, and the
  same delivery sent again once the account is back runs
- company-managed flows are not affected
- to keep one running while the account stays disabled - a shared flow
  other people rely on, for example - an administrator transfers the flow
  to an active owner or makes it company-managed

## React to what an agent remembers

Agents record task state, verification results and handover context in
their persistent memory. Every durable write also fires the built-in
`Memory persisted` event type, so an `On event` node bound to it starts a
flow within seconds of the write instead of waiting for the next scheduled
run. The event carries the memory's id, the agent and user it belongs to,
its type and source, a short preview, and — when the memory contains a
line of the form `Status: remediation_needed` — that status token. The
memory itself is not copied into the event: a flow that needs the whole
entry reads it back by id.

Two settings on the `On event` node keep a busy agent from flooding a
listener:

- **Field filter** is a JSON predicate over the event's fields, and a list
  means "any of":
  `{"status": ["remediation_needed", "failed_verification"], "source": ["assignment"]}`
  subscribes to just the actionable writes. A field the event did not carry
  never matches, so a memory with no status line is not picked up by a
  status filter.
- **Debounce (seconds)** starts the flow at most once per window. Events
  inside the window are still recorded — they simply do not start a run —
  which matters when memory maintenance persists in a loop.

A flow never re-triggers itself on memory its own run wrote, and a cycle
between two flows stops at the event chain depth limit, so a listener that
writes memory of its own is safe to build.

Emissions are kept only as long as they are useful. Rewriting a memory
replaces its previous event rather than stacking one up per write, and the
platform purges emissions past a retention window (30 days by default) on its
hourly maintenance pass. Neither can remove an event a `Wait for event` node
is still waiting for.

## React to platform events

An `On platform event` trigger starts the flow when the platform reports
that something happened: a Video Studio render or AI shot ended, a work
item got a note, changed status or was completed, an assignment run or a
background task ended. These are the events an assignment can subscribe
to, and they carry the same payload, read as `{{trigger.payload.status}}`
or `{{trigger.payload.file_id}}`.

- **Events** lists the events that start a run; one event starts one run,
  however often the platform reports it.
- **Field filter** is a JSON predicate over the payload (dotted paths, a
  list means "any of"): `{"status": "ready"}`.
- **Scope** decides whose objects the flow hears. By default, an object of
  the flow's owner (their video, their personal work item), or one an agent
  placed on the flow takes part in when the owner can edit that agent: an
  agent they can only see (shared with them read-only, or company-managed for
  a non-administrator) does not count, and a share taken back stops counting
  at once. **Every object** is honoured only for a flow an administrator
  owns.

A flow never wakes on its own work: the end of an assignment run it
triggered, or a work item one of its nodes noted, re-statused or
completed. An event about a workspace the owner cannot read never
reaches it. The payload is text other people and agents wrote, so it is
quarantined like a webhook's when it reaches an agent.

## Act on platform features

The library's **Actions** section covers the platform's features
directly, without an agent in between. Every node acts as the flow's
owner, with exactly their access.

| Node | What it does |
| --- | --- |
| `Update work item` | Adds a progress note to a work item, changes its status, or completes it with a result. |
| `Hand to fleet` | Creates a work item for a fleet's orchestrator in the fleet's workspace, and optionally waits until the fleet completes it. |
| `Add workspace source` | Adds a text or a web page to a workspace's Sources (the owner must be able to edit the workspace). |
| `Create report` | Writes a structured report from a header and blocks, optionally dressed in a report template, or revises one by id. |
| `Export report` | Renders one of the owner's reports to a PDF file. |
| `Export presentation` | Renders a deck or an A4 / letter page to PDF or PowerPoint. |
| `Render video` | Renders a Video Studio video to MP4 and waits for it. A refused or failed render takes the **Fail** pin with its reason. |
| `Deep investigation` | Investigates a subject across its source packs and publishes the cited report. |

`Render video` appears only when the Video Studio is turned on, and
`Deep investigation` only when Deep Investigation is. `Add workspace
source`, `Render video` and `Deep investigation` use AI (an embedding
model, a speech model, a writing model), so a flow that contains one is
held back while AI is disabled.

The exports publish `{{node.file_id}}`, which a later step can attach, send
or share. A report or a presentation is usually produced upstream: by a
`Create report` node, or by an agent node wired to the `Generate
presentation` tool.

## Owner and access

A flow runs as its owner. An administrator changes who that is from the
builder: open the flow, click **Owner and access** (the user icon in the
toolbar), then either pick another active user in the **Owner** card or
switch on **Company-managed flow** to share it with every user, editable by
administrators only. Both changes save immediately and leave the canvas
untouched, so an unsaved edit stays where it is. A link to
`/flows/library/<id>?panel=owner-access` opens the builder with that drawer
already open (a paused flow's notice uses it). The button is not shown to
other users; the same changes are available through `PUT /api/v1/flows/{id}`
(`user_id`, `is_company_managed`) and are refused for a non-administrator.

A flow runs the agents, assignments, custom tools, knowledge bases, skills,
sub-flows, machines and platform variables its nodes name as they are
stored, whoever runs it. So saving a flow refuses a node that newly names one
its writer cannot see themselves (the save names the node and the field),
exactly as it does for an integration; what the flow already held saves
unchanged, and administrators are not checked. Restoring a version is held
to the same rule, the half a split detaches is checked as a new flow of
yours, and a duplicate leaves out, with a warning per node, what you cannot
use.

Trace inspection (`Inspect my traces`, `Inspect agent traces`) wired into an
agent node reads that agent's traces, which hold every user's conversations
with it. Saving a flow refuses it on an agent whose chats you cannot read
(an administrator, the agent's owner or someone it is shared with as
`Full access (incl. chats)` can), unless the agent carries it itself, and a
run only wires it, the trace inspection the agent carries or the agent
builder brings included, for a person running the flow who can read them.

A `Call tool` node runs the tool picked in its drawer. For a custom tool the
node keeps both its name and the tool itself, so the step runs that tool,
whoever runs the flow, as long as it is still an enabled tool of that name
that the flow's owner can use (every custom tool for a company-managed flow
or an administrator's flow). A tool created or shared later under the same
name changes nothing; the drawer lists every custom tool you can use, and
says whose each one is when several share a name.

A node saved before tools were kept this way, or whose tool was deleted,
disabled, renamed or is no longer shared, is resolved by its name: the
owner's own tool of that name first, then a company-managed one, then an
administrator's, then anyone else's. The step fails with the reason when none
of those tools carries the name, or when two of them carry it at the first of
those levels that has one - give them distinct names. With an LLM wired in,
each tool the model may choose from is read the same way, and offered only
when the person running the flow can use it too; the tool it chose is the one
that runs. Saving the flow warns about a name that designates no tool, or
several, and about a picked tool that is no longer available. Someone who
edits a flow they do not own may only add a tool they can use themselves,
since the step runs in the owner's reach and its output lands in the run;
the tool's name is held to the same rule, since it decides once the tool is
gone.

## Import a flow

`Import` in the flow library (or in the builder's toolbar) takes a JSON file
and always creates a new draft flow. Three formats are read:

- an **XTM One** export (`Export` on any flow), which round-trips exactly;
- a **LangFlow** export, best effort;
- an **n8n** workflow: the file `Download` produces, nodes copied from the
  n8n canvas, or a workflow from the n8n template library.

An import is never silently partial. The message shown when it lands counts
the nodes that need remapping, the nodes that need a review and the
connections that could not be kept, and the drawer of a node imported from
n8n or left unresolved shows the definition it came from.

### n8n compatibility

Each n8n node lands in one of three states:

| State | What it means | What you do |
|---|---|---|
| Mapped | The node has a local equivalent; its parameters and its plain `{{ $json... }}` references are translated. | Bind what n8n defined inline: the XTM One agent behind an AI Agent node, the flow a sub-workflow call starts. The builder flags those fields as required. |
| To review | The node type maps, but part of it could not be carried over faithfully: a JavaScript expression, credentials, an operator with no local equivalent. Its drawer lists each point under `Review before this node can run`. | Fix each point, then `Mark as reviewed`. Until then the node does not run: a run that reaches it fails, even when its error branch is wired, and the flow cannot be activated. |
| Unresolved | No local equivalent: an app XTM One has no integration for (Airtable, Spotify, ...), a message-bus trigger, a binary step such as image editing. The node keeps its position, its connections and its original parameters, and its drawer says why. | Replace it with an XTM One node: an integration, an HTTP request, an agent with the matching tools. |

What maps:

| n8n node | XTM One node |
|---|---|
| Manual Trigger, Execute Workflow Trigger | Manual trigger |
| Webhook | Webhook trigger (POST, secured by the secret minted at activation) |
| Schedule Trigger, Cron, Interval | Schedule trigger (cron; n8n's timezone setting is kept) |
| Chat Trigger | Chat start point |
| If, Filter | If / Else; several conditions become a chain of If nodes |
| Switch | Switch when every rule compares one value, a chain of If nodes otherwise |
| Merge, Wait, Loop Over Items, No Operation | Merge, Delay, Loop, Reroute |
| Edit Fields (Set) | Code node (Python) building the same fields |
| Aggregate, Limit, Sort, Remove Duplicates, Summarize, Rename Keys, Item Lists, Date & Time, Crypto, HTML, HTML Extract, Markdown, XML, Compare Datasets, TOTP | Code node running generated Python that does what the n8n node did; its options are the `settings` input. A `{{ $json.field }}` parameter is read in each item, as in n8n |
| Extract from File, Convert to File, Spreadsheet File, Move Binary Data | Code node reading or writing the text of CSV, JSON, text, XML, HTML or iCalendar content; a binary format (PDF, Excel, ...) passes through, to review: agents read files directly |
| Split Out | Loop over the field; the next nodes hang from `Each item` |
| Code (Python) | Code node, to review |
| Code (JavaScript), Function, Function Item, AI Transform | Code node holding the JavaScript as comments, to review: rewrite it in Python |
| HTTP Request, GraphQL | HTTP request (query, headers and body included; GraphQL as its JSON envelope) |
| Execute Command, SSH, Read/Write Files from Disk, Git | Runner command on an enrolled XTM One Desktop, to review |
| Gmail / Send Email, Slack, Microsoft Teams | Send message |
| Telegram, Discord, WhatsApp, Google Chat | Send message, to review: flow messages go out by email, Slack or Teams |
| Any node's *Send and wait for response*, Form, Wait for a form | Ask user: the question lands in the notification bell |
| Form Trigger | Manual trigger, to review: XTM One has no public form |
| Error Trigger | On event trigger, to review: emit the event from the error paths to watch |
| Wait for a call | Wait for event, to review |
| Execute Workflow | Flow call |
| Respond to Webhook, Stop and Error | Stop, to review |
| An app node XTM One has an integration for (GitHub, GitLab, Jira, Notion, HubSpot, Gmail, Google Drive / Calendar / Docs / Sheets / Tasks, Outlook, OneDrive, To Do, Slack, Mattermost, TheHive, ServiceNow, Zendesk, PagerDuty, MISP, Splunk, Elastic, SecurityScorecard, urlscan.io, Supabase, n8n, RSS, LinkedIn, X, ...) | Integration node with the matching operation picked, to review: its parameters keep their n8n names until you rename them to the operation's arguments |
| That app's trigger | Integration event when the integration emits one (new email, new Slack or Teams message); a schedule, to review, when n8n polled the app; a Webhook trigger, to review, when the app called n8n |
| That app's tool, given to an AI Agent | Integration capability wired into the agent |
| AI Agent, Basic LLM Chain, OpenAI message or image analysis, OpenAI assistant, Information Extractor, Summarization Chain, Question and Answer Chain | Agent node; chat model settings, memory, tools, retrievers and output parser become capability nodes wired into it, and an answer shaped as a JSON object gets a Break node so its fields stay readable |
| Text Classifier, Sentiment Analysis | Switch in LLM mode, with the chat model wired in |
| Sticky Note | Note |
| Execution Data, Debug Helper | Reroute (Debug Helper to review) |

An app node XTM One has no integration for stays unresolved, and its drawer
gives one of two verdicts. Apps an exposure-management platform has a use for
(SQL and document databases, Linear, Zammad, Freshdesk, SIGNL4, Rocket.Chat,
Cortex, Venafi, Kafka, ...)
are integration candidates; every other app (marketing, commerce, consumer
services) is outside the catalog. Either way, an HTTP request node calls the
app's API when the step matters.

Expressions: a reference such as `{{ $json.body.id }}`,
`{{ $('Webhook').item.json.body.id }}` or `{{ $node["Webhook"].json.body.id }}`
becomes `{{webhook.payload.id}}`. Anything else (arithmetic, method calls,
`$now`) stays visible between `⟨n8n: …⟩` markers and puts the node to review.

What never crosses over:

- **Credentials.** Configure the matching integration, channel or secret
  variable instead. A literal secret typed into a parameter, such as an
  `Authorization` header, is removed too.
- **Pinned test data**, the error workflow setting, per-node retries.
- **Item lists.** n8n runs each node once per item; XTM One passes one value
  from step to step. Where the workflow handled lists, iterate them with a
  `Loop` node.

## Build flows from a conversation

Agents can author flows too. Give an agent the `Flow builder` built-in
tool and it can discover the node catalog, compose a graph from your
prompt, validate it, save it (the server lays out nodes automatically),
run it and read the step journal to correct itself. See
[the flow builder API](flows-api.md) for the surface it uses.

## Next step

Continue with [the flow builder API](flows-api.md) to automate flows from
scripts or agents, or read [Agentic flow](agentic-flow.md) for the
agent-to-agent handover map.
