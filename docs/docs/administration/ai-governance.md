# AI governance

This page describes what XTM One shares with AI providers, where that data is processed, and how to control it. Read it alongside [Data flow](../deployment/data-flow.md), which covers the request lifecycle and where data is stored.

## What is shared with an AI provider

When the platform calls an AI provider, the request contains only what the model needs to answer:

- the agent's persona and system instructions
- the current conversation or assignment prompt, including recent message history
- context the agent pulls in to answer — for example, snippets retrieved from a knowledge base, tool results, or an incoming trigger payload
- the definitions of the tools the agent may call (names and parameters, not their results until a tool runs)

What stays private and is never included in the request to the model:

- stored integration and channel credentials (encrypted at rest — see [Configuration](../deployment/configuration.md#security))
- user passwords and session tokens
- knowledge bases in full — when an agent answers, only the snippets it retrieves are sent (the full text is sent once to the embedding model when you first add the content, as described below)

!!! info "XTM One does not train on your data"
    XTM One never uses your prompts, documents, conversations, memories, or any other data to train, fine-tune, or improve an AI model. Your data is sent to the AI provider you configure **only** to answer your own requests, in real time. The platform itself trains nothing.

!!! warning "Your AI provider sees your prompts"
    Whatever an agent puts into a prompt — including retrieved documents and tool results — is sent to the AI provider you configured. The platform does **not** anonymize, tokenize, or redact prompt content before sending it, and prompts are stored unredacted in your own deployment (see [Data flow](../deployment/data-flow.md#where-data-is-stored)). Whether the provider retains or trains on the prompts it receives is governed by **its** terms, not by XTM One — review them, and scope agents' knowledge and tools accordingly. For maximum control, run a local model so no prompt data leaves your network.

### Adding documents to a knowledge base

To make a knowledge base searchable, XTM One first has to turn its documents into numbers a computer can compare — these numbers are called **embeddings**. Creating them means sending the document text to an **embedding model**, which is an AI provider just like the one that answers questions.

Two things are worth knowing:

- This happens **when you add the content**, not on every question. The same applies to an agent's saved memories.
- The embedding model can be a **separate provider** from the one that answers chats — and it can be a **local one** (for example Ollama), so the document text never leaves your network. You set it under `Settings` → `AI Models`.

## Where the AI provider runs

You choose the AI provider under `Settings` → `AI Models`, or pre-provision it (see the supported providers in [Configuration](../deployment/configuration.md#ai-providers)). The provider determines where prompt data is processed:

- **Hosted providers** (such as OpenAI, Anthropic, Gemini, and Mistral) process prompts on their own infrastructure, subject to their terms.
- **Self-hosted or local providers** (Ollama, or any OpenAI-compatible endpoint on your network) keep prompt data inside your perimeter. This is the recommended option for air-gapped deployments.

## Scope what agents can access

Because an agent only shares what it is configured to use, you control exposure by scoping the agent:

- limit which knowledge bases an agent can read
- limit which integrations and tools an agent can call
- use assignment-level scoping to further narrow tools and knowledge bases per automation

See the [User Guide](../usage/agents.md) for how agents, knowledge bases, and tools are configured.

## Guardrails and safety

XTM One adds several safety layers around every agent run. They lower the risk of misuse, but they are not a substitute for limiting what each agent can access and choosing your AI provider carefully.

- **Per-agent rules** — every agent has a separate "guardrails" box where you write hard rules it must always follow (for example, "never send external email" or "only answer questions about our products"). These rules are kept apart from the agent's persona, so an edit to its personality can't accidentally erase them.
- **Injection detection (alert only)** — messages coming into an agent are checked for known tricks that try to make the AI ignore its rules ("prompt injection" and "jailbreak" attempts). When one is spotted, XTM One records it as a security event for administrators to review — but it does **not** block the message. Treat this as an alert to check, not a wall that stops every attack.
- **Treating outside content as data, not orders** — anything an agent pulls in from the outside world (web pages it fetches, incoming webhook or integration messages) is wrapped so the AI treats it as information to read, never as instructions to obey. This helps stop a malicious web page or email from hijacking the agent.
- **Built-in security rules** — a fixed set of security instructions is added to every agent so it resists attempts to reveal its hidden instructions, override its rules, or pretend to be someone else.
- **Link checking** — when an agent's answer includes links to other XTM Suite products, XTM One checks and corrects them automatically, so the agent never hands out made-up links.

## Human approval of tool calls

When an agent is set to **ask before running tools**, it stops and asks a person before any tool call that is not known to be read-only: anything that could change something or reach outside the platform. The run stops, the reviewer sees exactly what the agent proposes, and nothing happens until they decide.

Tool approvals are **opt-in**. No agent asks until an administrator turns it on, for one agent or for every agent; until then every tool call runs as it always has, and no screen shows an approval state. See [Turning it on or off](#turning-it-on-or-off). That includes the short list of [tools that always ask](#tools-that-always-ask): they ask only on an agent that asks, so an agent nobody set to ask never stops for a person, whatever tool it calls.

This is not a suggestion the AI can talk itself out of. The platform checks every tool call before it runs, so the pause happens regardless of what the model decides to do.

Where everything lives:

| What | Where | Who |
|------|-------|-----|
| Whether approvals are in use, which agents ask, the default every agent follows, the per-tool rules, the connected platforms' policies, the emergency suspend switch | **Settings → Access and security → Tool approvals** | Administrators |
| Whether one agent asks | The agent → **Configuration → Tooling → Ask before running tools** | Whoever manages the agent |
| The tools you always allow, and **Allow every tool for me** | **Profile → Tool approvals** (shown once an agent asks, or while you hold a choice) | Each person, for themselves only |

### What asks first, and what doesn't

On an agent that asks, every tool **asks first unless a rule lets it run without asking**. That default matters: a tool nobody has considered yet — a newly connected integration, a custom tool somebody just wrote — asks first rather than acting first.

XTM One ships rules that let tools that only read data (searching, listing, fetching, looking up) run without asking. Threat-intelligence lookups such as `lookup_greynoise_ip` or `lookup_rf_domain` are reads; a tool named with a verb that reads for one service and changes data for another (enrich, triage, assess, identify, reverse, resolve, analyze) still asks. Ordinary read-heavy work is therefore unaffected. The same rule is applied to what you connect: the read-classified tools of your integrations and of company-managed, enabled MCP servers run without asking as well, scoped to what is actually connected — a provider you have not connected exempts nothing. The read tools of a connected Suite platform run without asking on the same basis — see [Tools from a connected platform](#tools-from-a-connected-platform). All of it is listed under **Tool rules** in **Settings → Access and security → Tool approvals**, grouped by where it came from, and any tool can be made to ask first.

An exemption covers the tool it was decided for, not every tool that carries the name. An MCP server or a custom tool that takes the name of a tool on the list still asks, and a company-managed MCP server's read covers that server's own tool only: a built-in of the same name, such as `web_fetch`, keeps asking. An entry an administrator adds covers every tool of that name. Tools that download a file (a Jira attachment, a Slack or Google Drive file, a social media post's image) keep asking even though they only read: they pull a document nobody has looked at into the agent's context.

**Chat bots are the exception.** A Slack, Teams or Discord bot's tools are never exempted automatically, including the ones that only read. Reading a channel means reading people's conversations rather than platform data, which is a decision for a person to make per tool rather than one to infer from a verb. Each configured bot carries its own switch under **Settings → Channels** instead. Turning it off means two things, and both are deliberate: conversations through that bot run uninterrupted whatever tools the agent reaches for, the [tools that always ask](#tools-that-always-ask) included, and the bot's own send/edit/delete tools stop pausing wherever they are called. Only that bot is affected — two bots of the same provider decide separately.

!!! warning "A bot cannot ask, so a gated tool asked for there stops"
    A chat conversation with a bot has no way to collect an approval today: the bot replies explaining that it stopped, and the turn ends. That is why the switch exists — without it, a gated tool requested through a bot is not delayed, it is unavailable. (An assignment run's approval can be answered from Teams through its pasted link, see [Where you answer](#where-you-answer).)

A few tools that act on nothing at all are exempt for the same reason: asking a person to approve them would only interrupt. `request_human_input` is the agent choosing to ask a question — gating it would mean pausing for approval to ask for approval — and `wait` is the agent's timer, bounded by the wait limits in **Settings → Parameters** and interruptible at any time, so gating it would replace a bounded pause with an unbounded one. `check_task_status` is the poll beside it, reading whether work the agent already started has finished, on a short fixed budget the model cannot change.

Administrators manage the rules under **Tool rules** in **Settings → Access and security → Tool approvals**. The rules can be prepared before any agent asks; the page says they change nothing until one does. You can:

- **Let it run without asking** — a tool you trust stops asking, for everyone. Any tool can be added, including destructive ones — the platform warns you rather than refusing. **Add a tool by name** does the same for a tool not connected yet.
- **Make it ask first** — a read-only tool we ship as running without asking starts asking. That decision survives upgrades: a later release that ships the tool exempt again will not silently undo it. **Back to the default** undoes it.
- **Make all its reads ask first** for a whole platform's read tools in one action, and put them back the same way. Ruling on dozens of entries one at a time is not a real choice, so it is offered as one.

A tool's own page, an integration's tool table and a bot's tool list show whether each tool asks first, only while approvals are in use, and link to the rule (for administrators) or to your own approvals (for everyone else). Your own approvals list the tools that ask first and the ones you always allow, so for anyone but an administrator a tool that runs without asking, and that you do not always allow, is shown without a link.

!!! warning "`code_interpreter` ships whitelisted"
    The code interpreter runs code the AI writes, and it is on the default whitelist so that routine calculations don't interrupt every run. It is sandboxed, but if your security posture requires a person to see generated code before it runs, this is the first entry to remove.

### Tools that always ask

On an agent that asks, a few tool calls wait for a person whatever else would let them run: a tool on the whitelist, "Yes, always" or **Allow every tool for me**. Only a decision on that very call lets it run. Approval stays opt-in, so these calls ask **only on an agent set to ask**: on an agent set to **Don't ask**, or that follows a platform default of not asking, they run like every other call, recorded with the agent's setting as the reason. The switch above everything is the platform's own: while [approvals are suspended for the whole platform](#suspending-approvals-for-the-whole-platform), they run unattended on every agent, recorded with that as the reason. They are calls whose effect a person should see before it happens:

- `act_on_page` with `submit`: a click that saves, creates, sends or deletes in the page the user is on.
- The Letsignit user writes, `create_letsignit_user`, `update_letsignit_user_variables` and `delete_letsignit_user`: the Letsignit API key reaches every employee, and what they change appears in every email that person sends afterwards. The Letsignit reads (`get_letsignit_signature`, `get_letsignit_user`) are not on this list: like the other directory reads (Microsoft Entra's `get_entra_user`, for instance), they change nothing, and an administrator who wants them asked makes them ask first under **Tool rules**.

On an agent that asks, while approvals are available, where nobody can answer (a chat bot, a client that does not support approvals, an action an agent scheduled from a conversation), such a call does not run: the agent replies that it stopped, and an exact scheduled action naming such a tool is refused. An assignment run pauses and waits for a person, like any run with a tool that asks.

### What a reviewer sees

When a run pauses, the reviewer gets the tool's name, what it does, and every argument with its description — so a value like `cascade: true` can be judged, not guessed at. They can then:

- **Yes** — run it as proposed.
- **No** — decline it. The agent is told it was declined and can propose something else.
- **Yes, always** — see below.

### Where you answer

Three places, with the same effect and the same record:

- **The run itself** — open it and answer there.
- **Operations → Executions** — approvals appear inline in the list, so you can answer without opening each run. Filter to **Awaiting Input** to see only what is paused.
- **The run's link pasted in Microsoft Teams** — the link turns into a card that shows each person what their own access allows: whoever may answer the run gets each tool with its arguments and an `Approve` / `Reject` choice, or the question with an answer box. The answer is recorded as given from Teams. See [Pasted XTM One links](channels/microsoft-teams.md#pasted-xtm-one-links).

Someone who sees the card but cannot answer the run can ask the people who can, with **Ask the owner to act** (or **Ask an admin to act** on a company-managed run). Those people get a notification in the bell, and the request is listed on the run page, with **Dismiss**, until the run is answered. It shares nothing: the person who asked still cannot see the run.

The bell tells you a run is waiting and links to it; the decision is made in one of the two places above, because judging a proposal means seeing every argument in full and that does not fit a notification.

### Answering several at once

Select several rows in **Operations → Executions** and answer them together. The selection is per run: a run that paused on more than one tool call is answered as a whole, because a half-answered pause cannot resume.

- **Select the N awaiting approval** picks exactly the paused rows, skipping notices you cannot act on.
- **Reject** asks for **one reason**, applied to every run in the selection. Give one — with nothing to go on, each agent can only come back and ask you what to do instead, so a reasonless bulk rejection costs more interruptions than it saves.
- **Approve always** over a selection lists **every tool** it would stop asking about before you confirm. Read that list: it is the same durable preference described below, applied to several tools at once.

Each run is recorded as its own decision. If one can no longer be answered — somebody got there first, or the run ended — the others still go through and you are told which did not.

#### Runs a standing approval already covers

Granting "always" (below) stops a tool asking on future calls, but the gate has already run for anything sitting paused — so those runs keep waiting for the tool you just trusted. Where that happens the executions list says how many, and offers to release them in one action.

It is an offer rather than something that happens on its own: a run restarting with no explicit action is surprising, and you may be part-way through deciding something else.

A run is released only when **every** one of its pending calls is covered. One covered and one uncovered call means it still needs answering by hand — releasing it would approve a call you never consented to. That rule is enforced where the release happens, not where it is offered, so a run that stops qualifying between the offer and the click is refused and named rather than let through.

Nothing is granted by releasing. The exceptions already exist; this only applies them to what was already waiting.

Turning on **Allow every tool for me** in your profile makes the same offer, for the same reason.

### "Yes, always"

This saves a preference for **you**, not for everyone. That tool then runs without asking you again.

Two things to understand before using it:

- It applies to **your scheduled runs too**, not just the one you were watching. The approval prompt says so before you confirm.
- Another user is still asked. Your preference never lowers the bar for anybody else.
- It does not cover the [tools that always ask](#tools-that-always-ask): "Yes, always" runs the call in front of you, and the next one asks again.

You can review and take back your saved approvals at any time under **Profile → Tool approvals**, where **Tools that ask first** also lets you **Always allow for me** a tool before it ever asks you. If a tool's parameters change after you approved it, the approval is dropped automatically and the tool asks again — your consent described the older version, so it no longer applies.

An approval belongs to the tool you approved, not to every tool carrying its name. A tool from an MCP server is covered only on the server you approved it on: another server advertising the same tool name still asks, and so does a built-in tool sharing the name of an MCP or custom tool you approved. Approving the name again on another server moves your approval to that server. A tool from an MCP server that you approved before the server was recorded asks you once more; answering **Yes, always** records its server.

### "Allow every tool for me"

If approving tools one at a time is not how you want to work, **Profile → Tool approvals** offers the wholesale form: **Allow every tool for me**. While it is on, none of your conversations or scheduled runs wait for your approval — including tools that delete data or act outside the platform, which is why turning it on asks you to confirm first.

It does not cover the [tools that always ask](#tools-that-always-ask). Like a per-tool approval, this is a preference for **you** alone; nobody else's runs are affected. Your saved per-tool approvals are kept rather than deleted, so turning the mode off restores exactly the posture you had before. Switching it either way is written to the audit log, and every call that runs under it is recorded with that as the reason.

### Scheduled runs

A scheduled run that hits a tool needing approval **pauses and waits**. It does not time out, and nothing is auto-approved. The assignment's owner is notified, and the notification names the tools waiting.

This is deliberate: unattended automation is where an unreviewed destructive action does the most damage. To let one agent's scheduled work run end to end, let the tools it uses run without asking, or set that agent to **Don't ask**.

### Turning it on or off

Each agent has an **Ask before running tools** setting under **Configuration → Tooling**: **Follow the platform default**, **Ask** or **Don't ask**. It applies to everything the agent does, runs and chats alike. An agent that follows the default takes the platform-wide **Every agent asks by default** switch under **Settings → Access and security → Tool approvals**.

That default is **off on a new install**, and an upgrade changes neither it nor any agent: an install that turned it on, or that a release before approvals were opt-in created with it on, keeps its setting. Otherwise nothing asks until an administrator turns approval on, for one agent or for every agent, agents created later included.

The top of **Settings → Access and security → Tool approvals** says whether approvals are **In use**, **Not in use** or **Suspended**, and lists the agents that ask (and the General Assistant when it does), each linking to its setting.

The agent's **Human in the loop** switch is a different thing: it lets the agent choose to ask a person through the `request_human_input` tool and adds the review prompt. It does not turn the tool approval gate on.

Every tool call is recorded either way, with why it ran: allowed by a rule, covered by a saved approval, approved by a person, or run because approval was off. Changing either setting is audited with the value set, who set it, and when.

### Suspending approvals for the whole platform

**Suspend all approvals**, at the bottom of **Settings → Access and security → Tool approvals**, is the emergency switch. While it is on, no tool call waits for anyone, the General Assistant included, whatever an agent or the platform default says, the [tools that always ask](#tools-that-always-ask) included: tool approval is opt-in at platform level, and no tool-level flag turns it back on. Every call that runs under the switch is recorded with that as the reason. The rules and the per-platform policies stay saved but stop applying; **Resume approvals** puts everything back as it was.

It asks for confirmation and is audited. An administrator's agent with the `Platform insights` tools reads both switches with `get_platform_ai_settings` (under `tool_approvals`: `tool_approvals_enabled`, `approve_tool_calls_default` and `env_seed`, the `TOOL_APPROVALS_ENABLED` value as deployed) and changes them with `update_platform_ai_settings` (`tool_approvals_enabled: false` suspends, `true` resumes; `approve_tool_calls_default` is **Every agent asks by default**), with the same administrator check, the same validation (true or false only) and the same audit entry as this page, marked as coming from the tool. `TOOL_APPROVALS_ENABLED=false` seeds it suspended: it is read once, when the settings row is created on a fresh install or during the upgrade that adds the switch, and ignored afterwards. Unset, or anything unrecognised, leaves approvals available (which still asks nothing until an agent is set to ask).

Two things to know:

- **A run that has already paused stays paused.** Clear those from **Operations → Executions**: answer them, answer by tool, or dismiss them.
- **The settings are read fail-closed.** If they cannot be read, tools gate rather than run.

### The General Assistant

The General Assistant has no agent behind it, so it follows the platform default wherever chat is available. Where it is not, the default reaches only the agents, and **Every agent asks by default** and its confirmation do not name the General Assistant. The suspend switch and a user's own **Allow every tool for me** also reach it; so do the tool rules, which are the narrower lever when it keeps asking about a tool you consider routine.

### Built-in agents

Agents that ship with XTM One are read-only: their persona, tools and settings are managed by the platform and re-synchronised on every restart. **Ask before running tools is the exception**: an administrator can set it on a built-in agent from the same screen as any other agent. Built-in agents are what an embedded assistant in OpenCTI, OpenAEV or OpenGRC drives, so without it the only lever would be the platform-wide default.

- **Administrators only**, as for everything on a company-managed agent.
- **Your choice survives restarts**: the boot-time re-synchronisation does not touch it.

Nothing else about a built-in agent or its assignments becomes editable.

### Tools from a connected platform

A Suite platform that registers with XTM One — OpenCTI, OpenAEV, OpenGRC — contributes its own tools, a few hundred of them across the three.

**Its read tools skip approval.** Every platform tool is classified as reading or changing data, and the ones that only read — searching, listing, fetching, querying — are exempt on the same footing as XTM One's own read tools. Anything that changes the platform still asks. So "require approval" for a connected platform means *ask before anything that changes it*, not *ask before anything from it*: an agent can look up a report unattended, and still stops before editing one.

Two things bound that:

- **The classification errs towards asking.** A tool whose purpose cannot be established from its name counts as changing data. In particular the consolidated `manage_…` tools — which can list *or* create *or* delete depending on how they are called — always ask, because no single answer about them would be right.
- **One read still asks anyway**, by deliberate choice rather than by classification: reading a file's contents into the AI's context, because that loads a third-party document nobody has looked at. Identity and RBAC reads — who the users are, which roles and groups exist, what a role can do — are **not** held back: they change nothing, and re-gating them where that matters is a deployment's decision, made on the whitelist screen. Half-holding them back would only have looked like a protection: the summary asked while every part of it stayed readable.

If you would rather no platform read ran unattended, **Tool rules** in **Settings → Access and security → Tool approvals** groups them per platform and makes the whole set ask first in one action. That decision survives upgrades and survives the platform reconnecting, and the same control puts them back.

Whether the platform's *other* tools ask is decided per platform under **Settings → Access and security → Tool approvals → Approval for connected platforms**, which lists whatever is actually connected. Each row shows two separate things:

- **Whether that platform can display an approval prompt.** A fact the platform reports about itself when it registers — only it knows. Most builds today have never said, which is not the same as saying they cannot.
- **What happens to its tools while no prompt can be shown.** A policy only you can set: gate them, so its agents explain themselves instead of acting, or let them run.

The row also says where the current setting came from — a default, this deployment's configuration, or an administrator — so you can tell whether it is protecting you or merely inherited from an install nobody remembers.

A platform that later ships approval support and re-registers starts gating again on its own, unless an administrator explicitly chose otherwise. In that one case the setting stands and the page offers to restore the safer posture in a click, rather than changing it silently behind you.

!!! tip "Setting the starting point at deploy time"
    `TOOL_APPROVALS_PLATFORM_DEFAULT` decides what a platform gets **the first time it registers**. Set it to `skip` when deploying beside a platform release that cannot prompt yet, so its agents keep working without anyone making an API call on first boot.

    It seeds a value rather than overriding one: whatever it sets is visible and changeable on the settings page, and it never overwrites a choice an administrator has made or applies to platforms already registered.

If you drive XTM One chat from another application, see [Chat approvals in client applications](../deployment/chat-approvals-in-clients.md).

## Data retention

All platform data lives in **your own deployment** (see [Data flow](../deployment/data-flow.md#where-data-is-stored)). XTM One applies these defaults:

| Data | Where it is stored | Default retention | How to control it |
|---|---|---|---|
| Conversations and messages | PostgreSQL | Kept until deleted | Delete the conversation, or the agent it belongs to |
| Assignment runs and results | PostgreSQL | Kept until deleted | Delete the run, or the assignment |
| Agent memory — recent interactions | Redis | 24 hours (auto-expires) | Automatic |
| Agent memory — rolling summary | Redis | 30 days (auto-expires) | Automatic |
| Agent memory — long-term facts | PostgreSQL | 90 days, max 200 per agent | Admins can clear an agent's memory at any time |
| LLM traces | PostgreSQL (or ClickHouse) | Purged after the retention period — **90 days by default** | Adjustable from **1 to 365 days** under `Settings` → `Observability`, where `Store full payloads` off keeps no prompt, answer or tool content and `Enable local trace storage` off keeps no trace (see [What a trace keeps](../deployment/monitoring.md#what-a-trace-keeps)); individual traces can be deleted |
| Daily observability aggregates | PostgreSQL | **Kept indefinitely** — they outlive the trace purge on purpose | Not covered by the trace retention setting; see [What survives the trace purge](#what-survives-the-trace-purge) |
| Audit logs | PostgreSQL | `Audit log retention (days)` in Settings > Observability, 90 days by default (each user's most recent sign-in is kept); the entries of a user's own actions are deleted with that user's account | Admin-only visibility; entries cannot be edited from the platform |
| Prompt, persona, agent setting and skill versions | PostgreSQL | Full history by default | `PROMPT_VERSION_RETENTION_COUNT` keeps only the latest N versions of each series (`0` = unlimited); a locked version is never pruned |
| Resolved shared state entries | PostgreSQL | 30 days after resolution | `SHARED_STATE_RESOLVED_RETENTION_DAYS` (`0` keeps them until an agent or admin deletes them); active entries are never purged |
| Work items (tasks agents hand to each other) | PostgreSQL | Done or cancelled items: 30 days. An open item with no activity for 14 days is cancelled, unless it has a start date or deadline still ahead or is a [scheduled work item](../usage/scheduled-work-items.md) | `WORK_ITEM_RESOLVED_RETENTION_DAYS` and `WORK_ITEM_ABANDONED_AFTER_DAYS` (`0` disables each) |
| Worksets (an agent's scoped document corpus) | PostgreSQL and object storage | Resolved worksets: 7 days. A workset with its own expiry goes once past it. The corpus the platform creates by itself for an agent's chat or an assignment expires 30 days after its last use | `WORKSET_RESOLVED_RETENTION_DAYS` (`0` disables the purge); `WORKSET_IMPLICIT_IDLE_DAYS` for the agent and assignment corpora (`0` keeps them until resolved); a workset a flow creates without an expiry is never purged |
| Flow events | PostgreSQL | 30 days | `FLOW_EVENT_RETENTION_DAYS` (`0` disables the purge); never shorter than the flow run timeout, so a waiting run keeps hearing its event |
| Relayed request records (host, method, status, size, duration) | PostgreSQL | 30 days | `RELAY_REQUEST_LOG_RETENTION_DAYS` (`0` = forever); see [Relays](../usage/resources/relays.md) |
| Public chat widget visitor uploads | Object storage | 7 days after the visitor's last message; a file never sent: 1 day | `PUBLIC_WIDGET_UPLOAD_RETENTION_DAYS` (at least `1`, cannot be switched off) |
| Sandbox workspaces (files the code interpreter keeps) | Object storage | Conversation workspace: 7 days unused, or at once when the conversation is archived or deleted. Assignment run workspace: when the run ends | `CODE_INTERPRETER_WORKSPACE_IDLE_TTL_HOURS` (168) and `CODE_INTERPRETER_RUN_WORKSPACE_IDLE_TTL_HOURS` (24, for a run that has not ended) |
| WhatsApp messages received by a WhatsApp integration | PostgreSQL | 30 days | The integration's `inbound_retention_days` field, from 1 to 365 days |

### What survives the trace purge

Deleting an expired trace does **not** delete the KPI it contributed to. Once a day is complete, XTM One rolls its traces up into a small daily aggregate and keeps that aggregate indefinitely, so the Analytics dashboard can still answer "what did last quarter cost?" long after the individual traces are gone. This is why a 30-day or 90-day chart still has data on a platform with 7-day trace retention.

What you keep, and for how long:

| | Individual traces | Daily aggregates |
|---|---|---|
| Retained for | The retention period (1–365 days, default 90) | Indefinitely |
| Granularity | Every request, with prompts, tool calls and errors (the prompts and tool content only while `Store full payloads` is on) | One row per day, agent, model, source, user, the person the usage counts for and their [department](../deployment/monitoring.md#departments) |
| Answers | "What exactly happened in this conversation?" | "How much did this agent, model or person cost, and how fast was it?" |
| Where in the UI | Trace explorer, individual trace detail, top errors | Cost, volume and latency charts on Analytics |

Consequences worth knowing before you choose a retention period:

- **Prompt and response content is *not* aggregated.** Only counts, token totals, cost, latency distributions and feedback averages survive. Nothing a user typed, and no tool result, outlives the retention period.
- **Aggregates are per user.** A day's row is keyed by agent, model, source *and* the user who made the request - and, for a request with no user of its own (an assignment run, a sub-agent, a webhook), the person it counts for - with their department at the time, so per-person and per-department usage and cost stay answerable after the traces are gone. If your retention policy requires per-user usage records to expire as well, treat these aggregates as in scope — the trace retention setting does not clear them.
- **Error details and per-request diagnostics do not survive.** The "Top errors" table and trace detail are served from raw traces only, so they are always limited to the retention window.
- **Latency beyond the window is a distribution, not the raw timings.** Each day stores a bucketed histogram of its durations, which is what lets percentiles be merged correctly across a long window. Expect percentile values to be accurate to within a bucket rather than exact.
- **A day is never purged before it is aggregated.** The maintenance job aggregates first and refuses to delete a day that has no aggregate yet, so lowering the retention period (or a restart longer than it) cannot silently drop a day's KPIs. If a window you select reaches back further than any surviving data, Analytics says so instead of showing zeroes.
- **ClickHouse follows the same retention period.** With `tracing_store_provider=clickhouse`, the maintenance job deletes the expired ClickHouse traces and spans, with their offloaded payloads, on the same window as PostgreSQL, about once an hour. It also declares a table TTL at the retention period plus 7 days on the trace, span and score tables, as a backstop: if maintenance stops, ClickHouse still expires the rows on its own, but their offloaded payload objects stay in the bucket. Long-window dashboards read the PostgreSQL aggregates either way.

To remove the aggregates as well, delete the `llm_daily_stats` rows (or the deployment's PostgreSQL volume, as below).

Two categories are **not** retained by XTM One and are governed entirely by your own infrastructure:

- **Application logs** are written to standard output. Your container runtime or log aggregator decides how long they are kept and how they are rotated — see [Monitoring](../deployment/monitoring.md#logs).
- **Metrics** are exposed on the `/metrics` endpoint and scraped by your Prometheus. The platform holds only current values; historical retention lives in your monitoring stack.

To remove all data, delete the deployment's PostgreSQL, Redis, and object-storage volumes. For selective deletion, delete the specific agents, conversations, or knowledge bases from the application.

## Next step

Review [Monitoring](../deployment/monitoring.md) to see how AI requests are traced and observed, and [Authentication](../deployment/authentication.md) to control who can send them.
