# Chat slash commands: `/context` and `/help` (PRD)

Status: validated 2026-10-26. Inspiration: Qwen Code's `/context` command
(`packages/cli/src/ui/commands/contextCommand.ts`, Apache-2.0, read for its
behaviour only; no code is copied).

## Problem

The composer's context gauge (#5018, #5023) tells a user how full the window
was at the end of the last run and splits it into six buckets. It cannot answer
the next questions a user (or an agent builder) asks when a conversation gets
heavy or the assistant starts forgetting:

- *Which* tools, MCP servers, skills or memory entries cost the most?
- How far is the conversation from the point where older turns get summarized?
- How much does this agent cost before anyone has typed anything?
- Is the number measured by the provider or estimated?

It also only exists in the web composer: the chat embedded in OpenCTI, OpenAEV
and OpenCRQ shows the gauge only, and Teams / Slack show nothing.

## Goals

1. A `/context` command that answers those questions on every chat surface the
   core serves: native web chat, the chat embedded in the three modules, Teams
   and Slack.
2. `/context detail` (alias `/context -d`) adds a per-item breakdown.
3. `/help` lists the commands available on the current surface.
4. A generic command registry, owned by the core, that later commands
   (`/compact`, `/clear`, ...) plug into without new surface work.

## Non-goals

- No action from the report (no "compact now", no "disable this MCP server").
- No exact provider token-counting call (`count_tokens` endpoints): no extra
  network call, no extra cost.
- No native commands on Telegram, Discord, Google Chat or WhatsApp in this
  delivery.
- No change to how or when the agent loop compacts.

## Behaviour

### Commands

| Command | Effect |
|---|---|
| `/context` | Summary: model, window, used (provider-measured when available), cached prefix, free space before compaction, compaction thresholds and current tier, usage by category |
| `/context detail`, `/context -d` | Summary plus one row per item inside each category, sorted by cost |
| `/help` | The commands available on this surface, with their description |

Parsing is shared by every surface: the trimmed message, case-insensitive,
must exactly match a registered command and one of its accepted arguments.
Anything else (`/etc/hosts`, `/contextual`, `/context foo`) is sent as an
ordinary message. A command is never sent to the model, never consumes agentic
quota, never appears in the model's history and never moves the gauge.

### Data: published by the loop, read by the command

The request is not assembled again for the report. Its assembly
(`agent_chat.run_agent_chat`) connects MCP servers and resolves skills, memory
and knowledge as it goes; replaying it for a read-only command would cost those
calls and drift from what the loop really sends. The agent loop is the one
place that sees the exact request on every path (web, embedded, channels,
mentions), so it publishes what each reading is made of, and the command reads
it back:

- each `thinking` event carries `context_detail` (`agent_loop._context_detail`):
  the system prompt split by the sections its assembler measured
  (`prompt_budget.section_chars`: persona, guardrails, attached prompts, user
  and agent memory, skills and skills catalog, workspace instructions and
  sources; the rest is `platform`), one row per offered tool (name, kind, MCP
  server or integration), tool results by tool name, and the compaction gate's
  own figures. Every split sums exactly to the bucket it details. It is
  server-only: `slim_frame` drops it from every published frame;
- the `llm_step` of the same call gives the provider's prompt and cached counts
  (`context_usage.provider_prompt_usage`, every provider's usage shape read by
  the tracing normalization);
- the run stores both on its reply (`ContextUsageTracker.command_meta()`, added
  where the reply is stored, never to a frame: a frame reaches every viewer of
  the conversation).

The report reads the conversation's latest reading (a reply an @mentioned agent
wrote measures that agent and is skipped: it carries a gauge reading but never
the command keys), adds as an estimate the answer the measured call wrote and
the messages written since, which the next request carries too, and flags an
agent edited after the reading. Its states:

| State | Used is |
|---|---|
| `measured` | the provider's prompt count of the latest call |
| `estimated` | the latest call's estimate (the provider reported no usage) |
| `previous_conversation` | no reading here yet: the system prompt and tools of the caller's latest reading with the same agent (the cost of the agent before the first message), plus the messages written here |
| `none` | nothing read yet: the report says to send a message first |

No model, embedding or MCP call is made. The General Assistant's own path
(`general_chat`) measures no prompt sections: its system prompt is one line.

Categories are the loop's char-derived estimates, as the gauge's. A measured
report reconciles them with the provider's count: when they exceed it every
line is scaled down to it, items with their line; when they fall short, the gap
is shown as **Unattributed** rather than folded into another line. The cached
prefix is an annotation, never subtracted (a cache hit spans several
categories). A stored detail that does not add up to its bucket is ignored and
the bucket is shown whole.

### Categories

The six gauge buckets are refined; each line lists only what costs tokens.

| Category | Items (`/context detail`) |
|---|---|
| System prompt | persona, guardrails, attached prompts, platform instructions |
| Memory | your memory, agent memory |
| Workspace knowledge | workspace instructions, workspace sources |
| Skills | loaded skills, skills catalog |
| Built-in tools | one per tool |
| MCP tools | one per tool, with its server |
| Integration tools | one per tool, with its integration or channel bot |
| Custom tools | one per tool |
| MCP, integration and custom tools | a reading stored without its detail |
| Other tools | the tools past the 256 rows a reading keeps |
| Conversation summary (compaction digest) | - |
| Tool results | one per tool name, summed (knowledge-base searches included) |
| Messages | - |
| Unattributed | - |

The gauge keeps its six buckets; the refined split is the report's only.

### Compaction

The report shows the compaction gate's own figures as the loop published them:
the conversation's current measure (plus the messages written since), the
"approaching" warning (85 % of the trigger), the trigger and the target, the
free space before compaction, and the level: `normal`, `approaching`,
`overflowed`, the loop's own verdict (#5023), raised to `approaching` when the
messages written since cross the warning. It states that the gate measures the
messages and the system prompt, not the tool definitions, so the level and the
used share do not line up one to one.

### Access

Who can send a message in the conversation can run a command there, on every
surface: its owner, checked on the conversation row as sending a message is,
with the conversation's agent still visible to them on the agent row (a share
revoked since the conversation started stops the report at once). Someone the
conversation is shared with, or an administrator reading another person's
conversation, cannot send, so cannot run a command (404, as an unknown id).
Before a conversation, the agent must be visible to the caller: own, user or
group grant (`read_only` or `config`), company-managed.

The report carries names and sizes only, never the persona, a memory entry, a
tool description or a knowledge excerpt.

## Surfaces

### Web chat (native)

- Typing `/` at the start of an empty composer opens a command menu built like
  the `@` menu (keyboard navigation, filtering, Escape), fed by the registry
  (`GET` of the commands available to the web surface).
- Running a command adds a rich, **ephemeral** message to the thread: a stacked
  bar of the window in the gauge's colours, the summary block, the category
  table and, in detail mode, collapsible sections per category. `/help` renders
  the command list. The message can be dismissed and is gone on reload: nothing
  is stored.
- Running a command while a turn streams is allowed (read-only).
- Every label goes through `t_i18n`.

### Embedded chat (OpenCTI, OpenAEV, OpenCRQ)

- The platform proxy exposes the same registry and execution contract
  (`/api/v1/platform/chat/commands`), returning the structured report plus a
  markdown rendering of it.
- One PR per module repository (OpenCTI, OpenAEV, OpenCRQ) adds the `/` menu and
  renders the structured report like the web chat (ephemeral), each delivered,
  reviewed and tested on its own.
- A module that has not shipped its PR yet is unchanged: it does not call the
  command route, so `/context` typed there remains an ordinary message.

### Teams

- `context`, `context detail` and `help` join the bot's `commandLists` in the
  manifest (`apps/microsoft-teams/manifest/`), for the personal, team and group
  chat scopes. The existing `help` **prompt** command (which asked the model) is
  replaced by the registry's `help`; `status` stays a prompt command.
- The channel router recognizes the command text after the bot mention is
  stripped and answers with an Adaptive Card rendering of the report, not
  stored in the conversation's history. When the bot may send a targeted
  message (`supportsTargetedMessages`), the answer is visible to the caller
  only.

### Slack

- One namespaced slash command, `/xtm`, joins the Slack app manifest
  (`/xtm context`, `/xtm context detail`, `/xtm help`): slash commands are
  workspace-wide, so a bare `/context` or `/help` could collide with another
  app's. It is served
  by a new signed command endpoint per channel bot (signature and timestamp
  checked as the events endpoint does, answered within Slack's 3 s by an
  acknowledgement, the report following on `response_url`).
- The answer is ephemeral (`response_type: ephemeral`): only the caller sees it.
- A slash-command payload names the channel but no thread, so the command
  reads the caller's latest conversation in that channel; none yet gives the
  pre-conversation estimate for the bot's agent.

## Security

- Every name in the report (MCP server and tool names come from a third party)
  is escaped for each destination: markdown table cells (pipes, backticks,
  brackets, newlines), Slack mrkdwn, Adaptive Card text, HTML. Names are
  truncated. One test table covers each variant per destination.
- Errors returned to the caller are generic; the reason goes to the logs only.
- The command route is authenticated (JWT, platform token, or channel
  signature), behind the chat gate, and rate-limited per user like sending a
  message (30 per minute).
- The Slack command endpoint rejects a bad or stale signature before any work.
- No frame carries a reading's detail: `slim_frame` drops it on every relay,
  the public chat widget's included. That relay forwarded raw loop events to
  anonymous visitors, the system prompt copy every `tool_start` carries for
  the evaluation wrapper among them; it slims its frames since level 1.

## API

```
GET  /api/v1/chat/commands
  -> [{name, description, usage, arguments: [{name, aliases, description}]}]
POST /api/v1/chat/commands/run
  {text: "/context detail", conversation_id? | agent_id?}
  -> {command, argument, result: {kind: "context" | "help", ...}, markdown}
```

The text is parsed by the registry (`parse_command`): a text the composer would
send as a message, both ids, a malformed id, an empty or overlong text is a
422, never a 500.
Without `conversation_id` the report is about a conversation not yet started
with `agent_id`, the General Assistant when both are absent. The platform proxy
and the channel adapters will call the same services, not the HTTP route.

## Delivery

A stack of xtm-one PRs, each green on its own before the next opens:

1. Command registry, context report service and route, provider token count
   kept on the context reading, `/help`; backend tests.
2. Web composer: `/` menu, ephemeral command messages, report rendering.
3. Platform proxy contract for the embedded chat.
4. Teams and Slack native commands.

Then one PR per module (OpenCTI, OpenAEV, OpenCRQ), once level 3 is merged.

All PRs reference one GitHub issue created from this document.

## Tests (minimum)

- Report: pre-conversation, after a run with and without provider usage,
  overhead larger than the provider count (scaling), shortfall
  (unattributed), compaction summary present, overflowed run, agent changed
  since the last run, rows summing to the headline.
- Parsing: each accepted form, case, whitespace, near misses sent as messages.
- Access on the real routes: owner, non-admin, `read_only` refused, `config`
  share, group grant, revoked share, company-managed agent; same matrix through
  the platform proxy and a channel.
- Escaping table per destination.
- Slack: signature, stale timestamp, ack-then-`response_url`, ephemeral.
- Teams: manifest parsed and asserted on its structure; router intercept.
- Frontend: menu filtering and keyboard, rendering of each report state.

## Known limits

- A run whose last call ran with its tools stripped (the step-budget
  wind-down, the token budget, the forced answer) stores a reading with no
  tool definitions: the report shows the tools at 0 until the next turn's
  reading, although that turn sends them again.
- `ContextUsageTracker.command_meta()` is added by hand where a reply is
  stored (REST, WebSocket, platform proxy; cut turns included). A new storage
  path must add it, or `/context` on its conversations falls back to the
  gauge's buckets with no provider count.

## Decisions

1. Teams: the manifest's `help` prompt command becomes the registry's `help`;
   `status` stays a prompt command.
2. Slack: one namespaced command, `/xtm <command>`.
3. Slack: the command reads the caller's latest conversation in the channel.
