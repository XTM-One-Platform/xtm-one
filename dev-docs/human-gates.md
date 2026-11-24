# Human gates: one decision system

Status: accepted, choices F3, G2 and H widened (Sam, 21 Nov 2026); spec only,
no code. Tracking issue: #842.

A person meets one kind of human gate anywhere in XTM One: the same card, the
same inbox, the same bell entry, the same answer flow, whatever asked (an agent
run, an assignment run, a per-tool approval, a flow, a fleet work item). The
card is readable on its own: nobody has to open a run to understand the
decision or to answer it. The run, flow or item stays one link under the card.

This document is the contract of that system and the plan to reach it from the
five gates main ships today. The three open choices were decided by the
reviewer (Sam, 21 Nov 2026): **F3** (long waits stay paused, release the
assignment queue, expire at the deadline, section 7), **G2** (one `decisions`
table with a reversible data migration, section 8) and **H widened** (a
decision card is answerable on every send path, bot or not, Microsoft, Google
and the other discussion integrations, section 9, I.4). Every link a card or a
message carries resolves (section 9, I.7).

## 1. What exists today

| # | Gate | Where it is asked | Stored as | Answered by | UI | Bell kind |
|---|---|---|---|---|---|---|
| 1 | Run free-text HITL | `request_human_input(question, context)`, registered by `assignment_engine` when `resolve_hitl` (agent default, assignment override) is on | `AssignmentRun.human_input_question` = question and `"Context: ..."` joined in one string; status `awaiting_human` | `POST /assignments/runs/{id}/respond` (`answer_paused_run`), `response` text | `components/decisions/RunPauseDecision.tsx` (S8; replaced `components/ui/run-human-input.tsx`) (run card, Operations row, run detail, run activity list, fleet pending approvals) | `hitl` |
| 2 | Per-tool approval | the gate of `hitl_whitelist.py` / `tools/approval.py` before a gated tool call | `pending_approvals`, `approval_decisions`; standing approvals in `ToolApprovalPreference` (schema fingerprint) | same endpoint, `decisions: [{decision: approve, approve_always, reject}]` | `ActionProposalCard`, `FleetPendingApprovals` | `hitl` (push Approve / Reject actions) |
| 3 | Irreversible consent | a tool (today `merge_opencti_entities`) asks through gate 1 for `APPROVE <code>` | inside the free-text reply, `human_input_history` | parsed server-side by `hitl_consent.py` | none of its own | `hitl` |
| 4 | Flow Ask-user node | `flow_runner._exec_ask_user` (title, question) | park `{kind: human, question, released, response, responded_by}` in `FlowRun.state.waiting` | `POST /flows/runs/{id}/respond`, `response` text | flow builder run panel | `flow_hitl` (activity row) |
| 5 | Work-item decision | `needs_human` + `ask` (`work_item_ask.py`) | `work_items.ask` {question, context, options, recommendation, deadline}; recipient in `waits_for_user_id` (`work_item_recipient.py`) | `POST /work-items/{id}/answer`, `{option, text}` | `WorkItemDecisionCard` in the work item dialog; Tasks > Work Items "Waiting for me" (`/tasks/work-items`) | `work_item_waiting` |

Fleet gates are a source of decisions 2 and 5, never a gate of their own: a
gate of a fleet's approval catalog (`dev-docs/ctem-fleets.md` section 11) that
waits at the fleet's autonomy level and pins is an instruction to the members
(#1467): no tool call pauses unless an administrator opted the fleet in to
tool-level enforcement (`Fleet.enforce_gates_at_tool_level`, off by default),
in which case it pauses its tools through gate 2; it asks through gate 5 (a `needs_human` work item with an ask) when it has no
tool, with the gate key as the decision's source. The CTEM loop fleets (#916,
CTEM-34) declare one key per loop stage (`ctem.validation.non_readonly`,
`ctem.analyst_review`, `ctem.remediation.decision`, `ctem.mobilize`,
`ctem.production_change`, `ctem.accept_risk`, `ctem.close_without_retest`);
out of the box two gates wait per case, the OpenAEV validation (one decision
per validation: a gate's `grant` lets the approved call cover the creation and
the launch of the objects it named or created) and `ctem.mobilize` (one
decision per case). A built-in
fleet takes pins and added gates in its customer layer, kept across seed
upgrades. No CTEM-specific card, endpoint, table, bell kind or channel
message; the G2 migration backfills them like any other.

Adjacent gates, in scope for the card (B) but not for the router at first:
chat inline approvals (`request_approval`, general chat pause), runner
shell-command approvals (`RunnerApprovalCard`, `RunnersSection`), and run
action requests (`access_requests.py`, bell `run_action_request`: someone who
cannot answer a paused run asks its owner).

What diverges, read on main:

- Options, recommendation, deadline, the readability guard (#776) and source
  links exist on work items only; a run question is one concatenated string.
- Three answer endpoints, three audit trails (`human_input_history` and the
  resolution entry, `waiting.responded_by`, the work item answer stamp).
  A tool-approval decision on a run also writes one `AuditLog` row,
  `entity_type` `assignment_run_approval`, action `decide`
  (`chat_approval_resume.record_approval_decision`): the actor, each verdict,
  its reason, the tool, the run and the surface. A deadline expiry is written
  as `system:decision_deadline`, its calls read `not_run_expired` and the agent
  is told nobody answered; a call the set carried no verdict for reads
  `not_run_undecided`. Only a person's verdict reads "Rejected by human
  reviewer" (`human_reject`). `get_run` lists `human_input_history` and its
  `search` reads it.
- Three bell kinds, three presenters in `notification_feed.py`
  (`present_run`, `present_activity`, `present_waiting_work_item`):
  `URGENT_KINDS` holds all three, `UNREAD_UNTIL_ANSWERED_KINDS` holds `hitl`
  and `work_item_waiting` but not `flow_hitl`; mobile push marks `hitl` and
  `flow_hitl` urgent, not `work_item_waiting`.
- Three recipient rules: the run owner (admins for a company-managed run), the
  flow owner or the whole company (company-managed flow), the work item
  recipient (explicit, acting user, agent owner, then administrators).
- An `awaiting_human` run counts in the assignment's blocking statuses
  (`assignment_dispatch`), so a routinely pausing scheduled run holds its
  assignment's queue while it waits.

## 2. A. One decision model

Every gate writes one **decision** with this shape (the work item `ask`,
widened):

```
{
  "question": str,                       # one sentence, required
  "context"?: str,                       # 2 to 4 plain lines (#776)
  "options"?: [{"key", "label", "effect"?, "kind"?}],
  "recommendation"?: option key,
  "deadline"?: ISO-8601 UTC,
  "links"?: [{"label", "url"}],          # source links (decision-card links work, #834)
  "recipient"?: user id or email         # resolved at write, section 6
}
```

- `kind` on an option is new and optional: `approve`, `approve_always`,
  `reject`, `consent` or absent. It drives the push actions and the button
  tones; it never changes what the answer does (the source does).
- No options means a free-text answer. An answer always carries
  `{option?, text?}`; a decision with options accepts a text beside the option
  (a note), a decision without options needs the text.
- Limits and the readability guard are those of `work_item_ask.py` today,
  moved to a shared module (`decision_ask.py`) that `work_item_ask` re-exports,
  so the item contract does not move.

Per source:

| Source | Change |
|---|---|
| `request_human_input` | Gains `options`, `recommendation`, `deadline`, `links`. `question` alone, or with `context`, still works (backwards compatible: the old call writes a free-text decision). The readability guard refuses an unreadable question with the same message as a work item, so the agent retries in the same turn. The context is stored apart, never concatenated into the question any more; `human_input_question` keeps the joined text for legacy readers until the last slice. Shipped in S1: the ask (question, context, options) is the paused call's own arguments in the run's messages, the `decisions` row of S2 stores it apart; the resume reads a reply that is an option's key or label as that option (`Human chose option B (Support)`), any other reply as free text; an action code (`M-1A2B3C4D`) passes the guard, since the person types it back; an option effect that opens with "Recommended" is refused (the card marks the recommendation itself, J.1). |
| HITL prompt | `DEFAULT_HITL_PROMPT`, the agent and assignment overrides and the `agent_builder` tool texts teach the options form in one sentence each; no prompt grows by more than one sentence (no system prompt inflation). |
| Per-tool approval | Rendered as a decision whose options are Approve (`approve`), Approve always (`approve_always`, only where standing approvals are allowed today) and Reject (`reject`). Fingerprints, standing approvals and batch decisions are unchanged: the router turns the chosen option into today's `decisions` payload. |
| Irreversible consent | Becomes an explicit option (`kind: consent`, label "Approve this irreversible action", effect naming the action). Choosing it writes the same `APPROVE <code>` entry in `human_input_history`, so `hitl_consent.py` stays the server-side check, unchanged. A typed `APPROVE <code>` reply keeps working. |
| Flow Ask-user | The node config gains `options` and `recommendation` (and `deadline`); `{{ask.response}}` stays the text, `{{ask.option}}` is new. A node without options behaves as today. Shipped in S6: `decision_ask.flow_ask` normalises the node's ask with the work item's model and guard (a bad option list fails the node with the reason), the park stores it as `waiting.ask`, the decision opens with it and its deadline, the answer (an option key or label, or `<key>: <note>` from the router) sets `waiting.option` through `flow_option`, and the flow run detail exposes `awaiting_input_ask` for the run panel's `FlowDecisionCard`. |
| Work item | Unchanged: it is the model. |

## 3. B. One card

`WorkItemDecisionCard` becomes `DecisionCard`, fed by a source-neutral
`Decision` view model (question, context, options, recommendation, deadline,
links, source, agent, asked at, can answer). It renders:

- the question as the heading, the context under it, one button per option
  (the recommendation marked, an `approve_always` option secondary, a
  `consent` option needing a confirm), a text field when free text is allowed;
- the deadline in the reader's time zone;
- one line "From <source> by <agent>, <asked when>" with the link to the run,
  flow or item, never required to answer;
- "Technical details" (folded) for what only agents read: the tool name and
  arguments of a tool approval, the blocked reason of an item.

Surfaces that use it: run detail, Operations execution rows, run card, run
activity list, fleet pending approvals, flow run panel, work item dialog, the
mobile companion inbox, and chat where the surface allows (the chat inline
approval keeps its stream contract; the card replaces its body).
`run-human-input.tsx` becomes a thin wrapper that maps a run to a `Decision`,
then goes once every caller uses `DecisionCard`. `ActionProposalCard` keeps
its argument diff view inside "Technical details". Runner shell approvals
keep their own transport and adopt the card in the last slice.

## 4. C. One inbox

"Waiting for you" (Tasks, today the work items "Waiting for me" view at
`/tasks/work-items`, and
the home dashboard notice) lists every open decision of the caller, whatever
the source, oldest deadline first then oldest asked:

| Column | Content |
|---|---|
| Decision | the question |
| From | source type icon and name (assignment, agent, flow, fleet item) |
| Agent | the asking agent |
| Asked | when |
| Answer by | deadline or nothing |

A row opens the same `DecisionCard` in a side panel and answers there. The
companion inbox reads the same list. The list is one indexed read of the
`decisions` table (G2: recipient, status, deadline), no model call on the read
path.

**Implementation (S5, read).** `GET /api/v1/decisions/waiting` (`app/services/decision_inbox.py`,
`waiting_for`): the open rows whose `recipient` is the caller (`user:<id>`), one
of their groups (`group:<id>`), `company`, or `admins` for an administrator,
soonest `deadline` first (none last), then oldest `created_at`. Each row carries
the public id, kind, question, the whole ask for the card, `origin` (the
assignment, flow or work item title), `agent`, `asked_at`, `deadline` and the
`decision_links` page. The list is served only while the read switch is on
(section 8 step 3; its verdict reused one minute per process); otherwise the
answer is `{enabled: false, reason, items: []}` and the per-engine views stay.

## 5. D. One bell kind

A new kind `decision` replaces `hitl`, `flow_hitl` and `work_item_waiting` in
the feed: title = the question, subtitle = "<source> · <agent>", summary = the
deadline text (`waiting_deadline_text`), route = the inbox with the card open
(`/tasks/work-items?decision=<id>`; a work item decision keeps
`/tasks/work-items/<item id>`, the route the feed writes today; see I.7). It
is urgent, unread until answered and
pushed the same way: Approve / Reject push actions whenever the options hold
an `approve` and a `reject` kind, else a plain push that opens the card. The
Teams and email notify paths (`notify.py`, the waiting work item notification
setting) send the same text. The old kinds stay valid as aliases in
`NOTIFICATION_KINDS` and the frontend `NotificationKind` for one release so
persisted rows, dismissed ids and installed companions still read; the feed
writes `decision` only from slice 4 on.

## 6. E. One answer router

`POST /decisions/{decision_id}/answer` with `{option?, text?}`:

1. Resolve the decision id to its source (`run:<uuid>`, `flow_run:<uuid>`,
   `work_item:<uuid>`; the prefix is part of the id, the same convention as
   today's `work_item:` bell ids).
2. Check who may answer with **one recipient rule**: the stored recipient
   when the decision names one; else the source's owner (run owner, flow run
   owner, item recipient); a company-managed source falls back to the
   administrators. The flow rule "a company flow asks the company" is kept as
   an explicit recipient `company` set by the node, so it is a stated choice,
   not an exception of the router.
3. Dispatch to the existing path, under its row lock and refusals:
   `answer_paused_run` (option mapped to `response` or `decisions`), the flow
   park release, `work_item.answer_work_item`.
4. Write one audit entry: who, when, from which surface (web, inbox, bell,
   push, Teams card), which option, the text.

The three current endpoints stay as they are and call the same dispatch, so
legacy clients, the Teams card answer and runs paused before the change keep
answering.

## 7. F. Long waits (decided: F3)

A run that waits long holds a worker slot of nothing but also its assignment's
queue (`awaiting_human` is a blocking status) and is hard to find after a
day. Options:

- **F1, stay a paused run**: no change of storage; the inbox and bell already
  make it findable. A scheduled assignment that pauses routinely stacks runs
  behind the first pause.
- **F2, hand over to a work item after N hours**: past N hours (default 24,
  per assignment), the run completes with status `handed_over`, a work item
  owned by the agent is written with the same decision and the run as source;
  the answer requeues the item, whose run continues the work from the item.
  The run's conversation is not resumed.
- **F3, stay paused but release the queue**: the run stays resumable as
  today; past N hours it stops counting in the assignment's blocking statuses,
  so later triggers run; the decision stays in the inbox until answered or
  until its own deadline, then expires with a stated outcome (the default
  option when the decision names one, else "no answer", which the run receives
  as its tool result).

**Decision: F3** (Sam, 21 Nov 2026). It keeps the exact resume (the agent continues with
its context, which F2 loses), keeps one decision per question (F2 creates a
second object for the same question), and fixes the only real harm of F1 (a
routinely pausing scheduled assignment holding its queue). N defaults to 4
hours for scheduled and event runs and is off for chat-started runs.

**Implementation (S7).** `app/services/decision_waits.py`. `holds_assignment`
replaces the bare status filter of the overlap guard (`has_active_run`,
`count_active_runs`, `find_active_run`) and of the delivery queue's slot
counts: an `awaiting_human` run whose open `decisions` row is older than
`HUMAN_GATE_QUEUE_RELEASE_HOURS` (default 4, 0 turns it off) no longer holds
its assignment, unless its `trigger_context.type` is chat-started
(`chat_handover`). A pause on a tool approval (decision kind `tool_approval`,
`RELEASED_AT_ONCE_KINDS`) never holds a non-chat run's assignment, whatever
the hours: the gate asks before the tool runs, so nothing is half written,
and the answer resumes the run as before (#1647). The wait is read from the
decision row, so no column is added; a run that leaves its pause unanswered
and asks the same question of the same kind again (a rollout re-entry) keeps
the first asking time on its new decision row
(`decision_hooks.carried_asked_at`), so the release clock does not restart.
`expire_overdue_decisions` runs on the autonomous tick: an open run
decision past its `deadline` becomes `expired` (an `expired` event, surface
`deadline`), and the run resumes with the ask's recommendation as its answer,
else "No answer before the deadline"; a tool approval resumes with every call
rejected.

## 8. G. Storage (decided: G2)

- **G1, a projection over the existing storages**: decisions stay where they
  are (`assignment_runs` columns plus a new JSON `human_input_ask`, the flow
  run park, `work_items.ask`); a `decision_feed` service unions the three
  reads into `Decision` rows for the inbox, the bell and the router. No data
  migration; each engine keeps its write path and its tests.
- **G2, one `decisions` table**: every gate writes a row (source type, source
  id, ask, recipient, status, answer, audit); engines read their answer from
  it. One index for the inbox, one audit table, one place to expire; but every
  engine's pause and resume path changes and existing paused runs, parks and
  items need a backfill.

**Decision: G2, one `decisions` table** (Sam, 21 Nov 2026). One store gives
one index for the inbox and the bell, one audit trail, one place to expire
(F3) and one row the channel cards of section 9 point at, instead of a union
of three reads that every later surface would have to repeat. The regression
risk that made G1 the first recommendation (the pause and resume paths) is
handled by the migration contract below, not avoided.

The table, `decisions`: `id` (the `run:` / `flow_run:` / `work_item:` prefixed
id of section 6 stays the public id; the row keeps `source_type`,
`source_id`), `ask` (the model of section 2), `recipient` (user, group or
`company`), `status` (`open`, `answered`, `expired`, `cancelled`), `answer`
(`{option, text}`), `answered_by`, `answered_at`, `surface` (web, inbox,
bell, push, Teams bot card, answer link, Google Chat, Slack, WhatsApp,
email), `deadline`, `created_at`; one `decision_events` audit table (append
only: asked, notified per channel, answered, expired, refused with the
reason). Indexes: (`recipient`, `status`, `deadline`), (`source_type`,
`source_id`) unique while open.

Migration contract:

1. Dual write first: each engine keeps its own columns (`AssignmentRun`
   human input, `pending_approvals`, the flow park, `work_items.ask`) and
   writes the `decisions` row in the same transaction. Engines still resume
   from their own columns; the router answers by the row and dispatches to
   the engine as in section 6.
2. Backfill (Alembic data migration, idempotent, batched): every open run
   HITL (`status = awaiting_human` with a question), every open tool approval
   (`pending_approvals` without a decision), every flow run with an
   unreleased human park, every work item with `needs_human` and an `ask`
   gets its `decisions` row and an `asked` event stamped `backfill`. A source
   already answered is not copied.
3. Read switch: the inbox, the bell and the cards read the table only once
   the backfill count equals the source count (the migration logs it, a
   health probe repeats it).
4. Rollback: the downgrade turns the read switch off first (a setting read on
   every request), then drops the tables. The engines kept their columns
   through steps 1 to 3 and every answer is dispatched to its source, so a
   rollback loses no open question and no answer. The downgrade runs in CI on
   a seeded database.
5. Retire: the engine columns become derived only after one release with the
   read switch on and no rollback; a later slice, never part of the
   migration.

Tests the migration ships with: unit on the backfill (each source, already
answered skipped, re-run idempotent), the downgrade on a seeded database, and
an e2e on legacy paused objects: a run paused, a tool approval pending, a flow
parked and a work item asked BEFORE the migration each answer after it, from
the old endpoint and from the router, and resume the right engine.

## 9. I. Channel cards

A decision sent to a person in a discussion integration (Teams 1:1, group chat
or channel; Slack; Google Chat; WhatsApp) is a true card of that channel,
answerable in place, with the content and order of the web `DecisionCard`.

What exists today, read on main:

- Teams, pasted run links only: `services/teams_link_cards.py`
  (`dev-docs/teams-link-unfurl.md`) renders an Adaptive Card per viewer
  (Universal Actions refresh) with Approve / Reject per tool call and a
  free-text answer, verbs `xtm.run.decide` / `xtm.run.answer`, handled in
  `channels/teams.py` `_handle_invoke` (tenant check, rate limit, user by email,
  answer through `answer_paused_run`). Assignment runs only, and only when
  someone pastes a link.
- The work item ask notify (`work_item_notify.py`, `teams_dm`, `teams_channel`)
  sends plain text through the creating agent's Graph tool. No card.
- A Graph message posted as a user or a service account accepts only
  `Action.OpenUrl` (`providers/microsoft/_cards.py` `parse_card`); inputs and
  `Action.Execute` need the Teams bot (`teams_bot.py`
  `send_teams_bot_channel_card`, `send_teams_bot_chat_card`,
  `send_teams_bot_group_chat_card`).
- Slack: no Block Kit, no interactivity endpoint. Google Chat: cardsV2 for
  images only, no `CARD_CLICKED` handler. WhatsApp: reply buttons (3 at most)
  and lists, 1:1 only.

### I.1 One renderer per channel

A `render_decision_card(decision, channel, viewer?)` module, one renderer per
channel, all fed by the `Decision` view model of section 3:

| Channel | Format | Options | Answer in place |
|---|---|---|---|
| Teams | Adaptive Card 1.5, Universal Actions (`Action.Execute`, per-viewer `refresh`) from the bot; `Action.OpenUrl` answer links from an account (I.4) | bot: the options as one expanded `Input.ChoiceSet` (recommendation marked in words, never by colour) and a single Send, so Teams' six-action limit never drops an option; a tool approval lists its calls (tool name, redacted arguments for the decider) and its option labels come from the catalog, its choice is required (`isRequired`, a catalog error message; a Send without a choice is answered with that message, never a generic failure), and its decided card says the outcome (one verdict, or each verdict with its count); free text as `Input.Text`; account: the text link | yes, every path (I.4) |
| Slack | Block Kit (header, section, context, actions) | one button per option, recommendation `primary`, free text through a modal | yes (interactivity endpoint) |
| Google Chat | cardsV2 | one button per option, free text as a text input widget | yes (`CARD_CLICKED`) |
| WhatsApp | interactive message | up to 3 options as reply buttons, else a list; 1:1 only | yes (button reply webhook) |
| Discord, Telegram | components / inline keyboard, if the channel's API allows | buttons | after the others, if their APIs allow |
| Any other, email | plain text and one answer link per option (I.4) | the links | yes, through the answer link |

Each card carries the question, the context, the options with their effect,
the recommendation marked, the deadline (in UTC when the viewer's zone is
unknown), the links, and "From <source> by <agent>". Agents never write card
JSON: they pass a decision.

**Object buttons (#1177).** Every surface (Teams bot card, Teams and email
link card, the decided card, Google Chat, Slack) renders the ask's `links` as
open-url buttons placed before "Open in XTM One", from one list,
`decision_ask.card_links` (http(s) only; four on a link card, three where a
surface's action budget is tighter). A label the agent wrote is kept ("Open PR
#20153"); a label the platform derived from the URL is worded from the catalog
in the reader's language (`card_link_*`: "Open PR #N", "Open issue #N", "Open
ITS-42", "Open the ticket", "Open the page", "Open the document"). A work item
whose ask names no source gets the pull requests and issues it tracks
(`blocked_on_refs`, `payload.pull_requests`) when its decision opens
(`decision_ask.with_reference_links`). The decided card keeps these buttons and
loses only the answer buttons.

### I.2 Every path that sends a decision uses it

- The notify path of D (the bell copy sent to Teams, Slack or email).
- `work_item_notify` (`teams_dm`, `teams_channel`, `email` and the other targets).
- An agent posting a decision itself: the channel send tools take a
  `decision` argument (the ask contract) instead of hand-made card JSON, and a
  decision posted this way is the same decision the inbox lists (one id, one
  answer).
- Pasted links: `teams_link_cards` is generalised from runs to every source
  (run, flow run, work item, the inbox URL), the verbs become
  `xtm.decision.answer` (option or text) with `xtm.run.decide` and
  `xtm.run.answer` kept as aliases for cards already in conversations.

As built (S9, Teams bot path (a)): `services/decision_cards.py` renders the
card and answers `xtm.decision.answer` through `decision_router.answer_decision`
(surface `teams_card`); `services/decision_card_delivery.py` records every
conversation the bot hears from (`teams:bot_conversation:<id>` in Redis, 90
days, a channel thread recorded as its channel) and posts the card there.
`work_item_notify` sends the item's open decision as the bot's card when the
bot is in the target (the person's 1:1 with the bot, the chat or the channel),
else the account's text as before; the D notify path (`notify.py`
`_notify_teams`) sends a paused run's open decision as the card to its owner.
The bot's card tools (`send_teams_bot_channel_card`, `send_teams_bot_chat_card`,
`send_teams_bot_group_chat_card`) take `decision` (`run:<id>`, `flow_run:<id>`,
`work_item:<id>`) in place of `card`: `decision_card_delivery.decision_card_for_tool`
renders the same card, so an agent posts a decision without writing card JSON.

As built (S10, Slack): `services/slack_decision_cards.py` draws the same
`DecisionView` as Block Kit (header, source, question, context, one button per
option with the recommendation `primary`, "Answer with text" opening a modal,
the links, "Waiting for <name>"); a tool approval shows its tool names only
(a Slack message is read by every member) and has no text answer. Clicks and
the modal come to `POST /api/channels/slack/interactions` (`channels/slack.py`),
signing-secret checked and failing closed as `/slack/events`; the clicker is
resolved by Slack email then the linked Slack identity, and the answer goes
through `decision_router.answer_decision` with surface `slack_card`. An answer
replaces the message with the decided card (`chat.update`); a refusal (not the
recipient, already answered, asked again, not linked) is an ephemeral to the
clicker and the card is unchanged. The D notify path (`notify.py`
`_notify_slack`) sends a paused run's open decision as this card. Setup: the
Slack app's Interactivity Request URL is `{base_url}/api/channels/slack/interactions`.

### I.3 Answering in place

A button calls the answer router of E as the person who clicked: identity by
tenant and email (Teams), or the linked channel identity (Slack, Google Chat,
WhatsApp); the same recipient rule, row lock and audit as the web, with the
channel recorded as the surface. The card then refreshes to the decided state
for every viewer: who answered, when, which option. Slack requests are checked
with the signing secret, Google Chat with its token verification, WhatsApp
replies are mapped to the open decision of that conversation by the button
payload (never by the reply text).

### I.4 Every send path answers (choice H, decided: H widened)

**Decision** (Sam, 21 Nov 2026): a decision card WORKS, answerable and not
only readable, on every path that sends it, not only through the Teams bot. A
read-only card is never the end state. The former options H1 (bot when
present, else read-only), H2 (bot only) and H3 (plain text) are withdrawn.

The send paths:

- **(a) Teams bot**: the bot is installed in the 1:1, group chat or team.
- **(b) An agent's native Microsoft integration**: the agent posts through
  its Graph chat and channel tools as a user or a service account (for
  example a fleet's shared mailbox account).
- **(c) AI agent accounts**: the Outlook and Teams agent accounts, on the
  same Microsoft integration.
- **(d) Google Chat and the other discussion integrations** (Slack,
  WhatsApp, then Discord and Telegram), through the integration bound to the
  agent, the user or agent account included.

The constraints, stated rather than hidden: a Graph message posted as a user
or a service account accepts only `Action.OpenUrl` (no `Action.Execute`, no
inputs, `providers/microsoft/_cards.py` `parse_card`), and a Google Chat
message sent with user authentication carries no interactive card (cardsV2
needs app authentication).

The mechanism per path (when the Teams bot is installed in the target
conversation it posts instead of the account: detection reads the bot's
conversation reference for the 1:1, group chat or channel before the send):

| Path | Card | Answer | Decided state shown |
|---|---|---|---|
| (a) Teams bot | Adaptive Card, `Action.Execute` per option, `Input.Text` for free text | in place, verb `xtm.decision.answer` (I.3) | per-viewer `refresh` for every viewer |
| (b), (c) Microsoft as a user, service account or agent account | the same Adaptive Card content, one `Action.OpenUrl` button per option plus "Answer with text" | the answer link below: one click answers that option | the sender updates its message to the decided card (`PATCH` of the chat or channel message as its sender, verified by slice S9b before it is relied on); when the API refuses, the link page shows the decided state and the next card read shows it |
| (d) Google Chat with app auth (the Chat app is in the space) | cardsV2, one button per option, text input | in place, `CARD_CLICKED` (I.3) | the app updates its message |
| (d) Google Chat as a user | text with one link per option | the answer link | the link page; the user message is updated where the Chat API allows an edit by its sender |
| (d) Slack, WhatsApp | interactive per I.1 (bot token, business number) | in place | message update |
| Email, any channel without interactivity | text and one link per option | the answer link | the link page |

**The answer link** (`/decide/<token>`): one URL per option and per
recipient, signed by the server (HMAC with a platform secret, key rotation
kept), carrying the decision id, the option (or `text`), the recipient and
an expiry (the decision deadline, else 7 days). Opening it:

1. requires SSO; the signed-in user must be the token's recipient AND pass
   the one recipient rule of section 6 (a forwarded link answers nothing: a
   non-recipient gets "Waiting for <name>" and a `refused` event);
2. answers in one click for an option (no second confirmation), except an
   irreversible consent option, which asks for its code on the page (the
   `hitl_consent` check is unchanged); "Answer with text" opens the card with
   the text field;
3. takes the decision's row lock, writes the answer through the router of
   section 6 with `surface` = the channel the link was sent on (Teams chat,
   Teams channel, Google Chat, email ...), and is single use: a second open
   shows the decided state (who, when, which option), never answers twice;
4. shows the decided state, with the link to the source under it.

The link is never a bearer credential alone: without the SSO session of the
recipient it answers nothing, so a link read by another member of a group
chat or channel is harmless.

Admin setup, stated in the user docs: the Teams bot app is installed for the
tenant (manifest `bots` scopes `personal`, `groupChat`, `team`) and added where
agents post decisions, to get the in-place answer; the Google Chat app is added
to the spaces where it should answer in place. Without them, paths (b), (c) and
(d) still answer through the link.

### I.5 1:1, group and channel behave the same

In a group chat or channel every viewer sees the card; only the recipient (or
the recipient group, or `company`) can answer; any other viewer sees "Waiting
for <name>" in place of the buttons (Teams bot per-viewer refresh; on Slack
and Google Chat a click from a non-recipient is refused with an ephemeral
message and the card is unchanged; on an answer link the page refuses, I.4).

### I.6 Acceptance per path

For each send path of I.4 ((a) Teams bot, (b) Microsoft integration as a user
or service account, (c) AI agent account, (d) Google Chat with app auth and as
a user, Slack, WhatsApp, email), in a 1:1, a group chat and a channel where
the channel has them: the four sources (run HITL, tool approval, flow
Ask-user, work item) produce the card; the recipient answers FROM THE CARD (a
button in place or one answer link click) and the right engine resumes; a
non-recipient is refused; the card (or, where the API refuses the update, the
link page and the next card read) shows the decided state; a second click
answers nothing. No path passes with a read-only card. A UX Director round on
the card visuals and the answer link page, Teams dark and light themes
included.

### I.7 Every link resolves

Every link a card, a bell entry, a push or a message carries opens a page that
exists. A work item opens `/tasks/work-items/<item id>` (the route the feed
writes today, `notification_feed.py`); `/operations/work-items` is gone since
#785 and answers "Page not found", so no sender may build it. Runs open
`/operations/executions/<run id>`, flow runs their flow run page, a decision
the inbox with the card open. Links are built by one helper
(`decision_links.py`, used by the feed, the notify paths, `work_item_notify`,
the channel renderers and the answer link page), never by string in a caller.

Test: a unit table over every bell kind (`hitl`, `flow_hitl`,
`work_item_waiting`, `run_action_request`, `decision` and the others of
`NOTIFICATION_KINDS`) and every channel card renderer asserts that each route
it emits matches a route of the frontend router (the `App.tsx` route list the
lab seed tests already read), and an e2e opens the deep link of each bell kind
and each channel card on the lab and checks no "Page not found". A route moved
later fails that test, not a person.

### J. Card and drawer layout

**J.1 Decision card.** Every option is a full-width tile of the same width
(one grid column, `w-full` button): the label in bold on line 1, its effect
on line 2, the whole tile one button (keyboard and screen reader: the effect
is the button's description). The recommended tile is accented and carries a
small "Recommended" pill; there is no separate column. An effect is clamped
to 3 lines; "Show more" shows under the tile, outside the button, only when
the clamp actually cuts the text (measured on the rendered span and followed
on resize, never a character count). A leading "Recommended:" an agent wrote
into an effect is dropped at render; the ask guard refuses it (S1). With options, free text folds behind
"Answer differently"; without options it stays open. The header holds the
question, who asks (the owner agent, named when the reader may see it), the
sources (`ask.links`) and the deadline. After the answer, every surface and
channel card shows the decided state (who, which option, when, section I).
One component; S3 generalises it to `DecisionCard` for the other surfaces.

**J.2 Work item drawer.** Title, status, priority, owner, due and what it
waits for come first, in words. The stamped notes are an "Activity" timeline
with each long note folded, the brief under it. A release explanation is one
sentence per kind of reference, never one per reference (each reference stays
a chip under "Waits for"); the pull request sentence says "merged" or
"closed" when every pull request waits for that event, "merged or closed"
only when they differ.

Test: unit tests assert one tile per option, every tile `w-full` in a single
column, one pill, the prefix dropped, the clamp and its toggle outside the
button, the folded free text, and the pull request sentence per `release_on`;
the UX Director reviews the lab at drawer width and full page, light and dark,
with 2, 3 and 5 options and long and short effects.

## 10. H. Slices

Each slice is one PR on xtm-one with its dev item linked to the program item,
the e2e of every gate it touches, auto-merge at green after the UX Director
APPROVE for a UI slice. No slice starts before this spec is accepted, the
decision-card links work (#834 branch) is merged and the work items e2e
verification is done.

| Slice | Content | UI | Acceptance tests |
|---|---|---|---|
| S1 | `decision_ask.py` shared model and guard (re-exported by `work_item_ask`); `request_human_input` options / recommendation / deadline / links, context stored apart; HITL prompt and tool texts | no | unit: old call shape still pauses with the same question; options validated; unreadable question refused; e2e: assignment with HITL on pauses and resumes with an option and with free text |
| S2 | `decisions` and `decision_events` tables (G2), Alembic schema migration with downgrade; `Decision` model; dual write from the four engines in their own transactions; `POST /decisions/{id}/answer` router with the one recipient rule and the audit; old endpoints call the same dispatch; `decision_links.py` link helper (I.7) | no | unit per source dispatch and refusal; dual write per engine; downgrade on a seeded database; route table test of I.7 over every bell kind; e2e: answer a run, a flow park and a work item through the router; a run paused before the change answers through the old endpoint |
| S2b | Backfill data migration (idempotent, batched) of open run HITL, tool approvals, flow parks and work item asks; backfill count check and health probe; read switch setting (off by default) | no | unit: each source backfilled, answered ones skipped, re-run idempotent; rollback step (switch off, downgrade) loses nothing; e2e on legacy objects paused BEFORE the migration (run, tool approval, flow park, work item) answering after it from the old endpoint and the router |
| S3 | `DecisionCard` (generalised from `WorkItemDecisionCard`) on run detail, Operations rows, run card, fleet pending approvals, work item dialog; tool approvals as Approve / Approve always / Reject; consent option | yes | e2e: tool approval decided from the card (approve, approve always fingerprint kept, reject); consent option resumes `merge` with the check passing; card readable without the run page |
| S4 | Bell kind `decision` with aliases; push urgency and actions unified; `flow_hitl` unread until answered; Teams and email text aligned | yes (bell, companion) | unit: one presenter for the three sources; aliases still read; e2e: each source rings one `decision` entry, cleared when answered |
| S5 | "Waiting for you" inbox across sources, companion inbox on the same list, home notice | yes | e2e: four sources listed in one view, answered from the side panel |
| S6 | Flow Ask-user options / recommendation / deadline and the flow run panel on the card | yes | e2e: flow parks, answered by option from the inbox, `{{ask.option}}` downstream |
| S7 | Long waits per F3: past N hours (default 4 h for scheduled and event runs, off for chat-started runs) an `awaiting_human` run stops counting in the assignment's blocking statuses; expiry at the decision deadline with the default option or "no answer" as the tool result | no | e2e: a scheduled run paused past N h no longer holds its assignment's queue and still resumes on answer; a decision past its deadline expires with the stated outcome; a chat-started run is unaffected |
| S8 | Chat inline approvals and runner shell approvals on the card; `run-human-input.tsx` removed; old bell kinds dropped after a release | yes | e2e: chat approval and runner approval from the card; no caller of the old component left |
| S9 | Teams bot: `render_decision_card` Adaptive Card renderer; `teams_link_cards` generalised to every source with `xtm.decision.answer` (old verbs aliased); bot detection per conversation; `work_item_notify` and the D notify path send the card; channel send tools take a `decision` | yes (card visuals, dark and light) | e2e per I.6, path (a), on 1:1, group chat and channel; old verbs still answer a card posted before the change |
| S9b | Answer links (I.4): signed single-use per-recipient `/decide/<token>` page with SSO, recipient rule, row lock, audit with the channel as `surface`, decided state; Microsoft paths (b) and (c): the Graph card with one `Action.OpenUrl` per option posted by the user, service account or agent account, the message updated to the decided state by its sender (`PATCH` verified first, fallback stated); email carries the same links | yes (card, link page) | e2e per I.6, paths (b) and (c), on 1:1, group chat and channel; forwarded link refused; second click answers nothing; expired token refused |
| S10 | Slack: Block Kit renderer, interactivity endpoint with signing-secret check, modal for free text | yes | e2e per I.6 |
| S11 | Google Chat: cardsV2 renderer with app auth, `CARD_CLICKED` handler with token verification; user-auth sends carry the answer links of S9b | yes | e2e per I.6, path (d), app and user auth, in a DM and a space |
| S12 | WhatsApp (1:1): reply buttons or list, button reply webhook mapped to the open decision; Discord and Telegram if their APIs allow | yes | e2e per I.6 (1:1 only for WhatsApp) |

S12 as built (`services/decision_whatsapp.py`): 1 to 3 options are reply
buttons, 4 to 10 a list, anything else (more options, a free-text question,
a tool approval with more calls than a card shows, an answered decision) is
text with the link to the decision. Buttons whose 20-character clipped titles
read the same are sent as a list, and list rows that still collide as text.
A closed decision states its outcome (who chose what and when, in UTC, or
expired, or no longer needed) instead of the options. An open tool approval
lists its calls as the Teams card of S9 does: `card_tools_awaiting`, then one
line per tool name, plus the redacted arguments when the viewer decides (the
send tool builds the message with no reader: names only); past
`MAX_TOOLS_SHOWN` calls, `card_too_many_tools` with the count. Over the
1,024-character body, arguments go first; a message that still cannot name
every call is sent as text, so buttons never decide a call they did not
name. Each reply id is
`xtm.d:<decisions row id>:<option key>`; the webhook answers a 1:1
`button_reply` or `list_reply` through the router as the sender's linked
WhatsApp identity (surface `whatsapp_card`), never routes it to an agent, and
answers in the chat with the decided state or the refusal (WhatsApp cannot
edit a sent interactive message). Agents send it with
`send_whatsapp_bot_message(to, decision=...)`. Checked for the other two:
Telegram's Bot API allows it (an `inline_keyboard` whose `callback_data`, 64
bytes, holds the row id and key; `callback_query` on the existing webhook;
`editMessageReplyMarkup` for the decided state); Discord allows it only
through message components answered on an Interactions endpoint (Ed25519
signature, a 3-second deadline), which the gateway adapter does not expose
today. Both are left out of this slice.

Render for review: WhatsApp offers no surface a review can open, so every lab
answers `GET /api/v1/lab/whatsapp-decision` (on the preview, `/api` is
proxied) with each state drawn from the real `render_whatsapp_decision` /
`decision_text` payload and the `answer_reply` texts, laid out as a WhatsApp
chat (body formatting, footer, reply buttons, the list button and its opened
sheet; the Cloud API payload under each). Query: `lang` (en, es, fr, ja),
`theme` (light, dark), `width` (phone 390 px, desktop 760 px), `state` (one
id); `/api/v1/lab/whatsapp-decision/index` links them all
(`.xtm-one/lab/whatsapp_decision_preview.py`).

S2 as built: `app/models/decision.py` (the tables), `app/models/decision_hooks.py`
(one `before_flush` hook writes the dual write from the engines' own columns, so
no engine write path calls it: a run entering `awaiting_human`, a flow
`state.waiting` human park, a work item's `needs_human`), `app/services/decision_router.py`
behind `POST /api/v1/decisions/{id}/answer` (the id is `run:<uuid>`,
`flow_run:<uuid>`, `work_item:<uuid>` or a row id; the answer goes to
`answer_paused_run`, `answer_flow_park` or `answer_work_item`, the functions the
old endpoints call, so the recipient rule is theirs; a refusal is a `refused`
event), and `app/services/decision_links.py` (I.7, every bell route). The
missed scheduled one-off decision (reschedule or cancel) and the e2e through
the router ride with S2b.

S2b as built: `app/services/decision_backfill.py` (`backfill`, `backfill_report`,
`read_switch`), run by the data migration `c943decisionsbf1` (no DDL, keyset
batches of 200, the count check logged per source) and repeated by
`GET /api/v1/decisions/backfill` (admins: open gates, with a decision, missing
ids, the switch and why it is off). The open tests are the dual write's own
(`run_opening`, `flow_opening`, `work_item_opening`, `open_flow_park` in
`decision_hooks.py`), so a backfilled row is the row the hook would have
written; its `asked` event carries surface `backfill`. `DECISIONS_READ_ENABLED`
(off) is ANDed with a complete count and the table's presence, so the
downgrade turns the switch off by itself; the downgrade deletes only the open
rows the backfill wrote and keeps answered ones as audit. Still owed after
S2b: the missed scheduled one-off decision (its own slice: the scheduled
item's writes are refused by `_refuse_scheduled`, so it needs its own answer
function) and a Playwright e2e of the router (S3, with the card).

S9b as built: `app/services/decision_answer_links.py` mints and reads the
token (HMAC-SHA256 under a key derived from `SECRET_KEY`, the previous keys of
`DECISION_LINK_PREVIOUS_SECRETS` still read after a rotation; decision row,
option or `text`, one recipient, surface, expiry = deadline else 7 days) and
builds the link card (`Action.OpenUrl` only, accepted by `parse_card`), the
plain-text form and the decided card. `GET /api/v1/decisions/links/{token}`
reads a link without answering; `POST .../links/{token}/answer` answers
through `decision_router.answer_decision` with the link's surface
(`teams_chat`, `teams_channel`, `google_chat`, `email`). A non-recipient gets
403 and a `refused` event; a bad signature 404; an expired link 410; a decision
no longer open shows its decided state and answers nothing (the engine's own
row lock decides a race: the loser gets 409 internally and the decided view).
The page is `frontend/src/pages/DecisionLink.tsx` (`/decide/:token`, outside
the shell, sign-in return); an open decision is drawn by the shared
`DecisionCard` (context as Markdown, deadline, options, recommended pill), the
free text open on the `text` link, where an option picked on the card is sent
as `option` (an option link refuses any other option with 400). `app/services/decision_graph_cards.py` sends: the
scheduler tick (`deliver_work_item_cards`, next to the notification delivery)
posts the card of every open work item decision whose `payload.notify` names a
Microsoft integration and a `chat_id`, a `team_id` + `channel_id` or, with
channel `email`, an address, as the item's agent through its own Graph tools
(`work_item_notify.agent_graph_caller`: bindings, scope, deny list and approval
gate apply), once (`notified` event naming the message), at most 3 tries,
stopped at once by a permanent refusal; only a single-user recipient gets links
(a group or `company` has no single reader). Paths (b) and (c) are the same
code: the integration the agent posts with is a user, a service account or an
agent account. Once answered, `update_sent_cards` edits every sent message to
the decided card as its sender (`update_microsoft_chat_message` /
`update_microsoft_channel_message`, scheduled in the background by the
router); a refused edit is logged and counted, and the links of the old card
open the decided page (the I.4 fallback). The PostgreSQL e2e
(`tests/test_decision_answer_links_842_s9b_postgres.py`) runs I.6 for (b) and
(c) on a 1:1 chat, a group chat and a channel plus email, with Graph as the
only stub; the edit as its sender against a live tenant is the production
retest of the slice. Not in S9b: the run HITL, tool approval and flow Ask-user
sources send no link card yet (their notify paths are S9's), and Google Chat as
a user is S11.

The agent's own send: the coordination tool `send_decision_card(work_item_id,
chat_id | team_id + channel_id, integration)`
(`services/decision_graph_card_tool.py`, `decision_graph_cards.send_agent_card`)
lets a run post the link card of a needs_human item it can read to a Teams chat
or channel. It posts as the calling agent through its own Microsoft integration,
under the same `agent_graph_caller` rules. The item needs no `payload.notify`,
so its end sends no text. This is how a fleet's `work_item.needs_human` listener
DMs a decision. The send records the same `notified` event, so the message turns
into the decided card whichever channel closes the decision. A second send to
the same target answers the message already there. A test item reaches only
`WORK_ITEM_NOTIFY_TEST_RECIPIENTS` or `48:notes`. An item that waits for a
person (not done or cancelled, `needs_human`, an ask with a question and two
options) but has no open `decisions` row (parked by a bulk write, or its row
cancelled by a later write) gets one opened from the item with the hook's rule
(`work_item_opening`), whoever parked it and whatever its status (#1244); any
other item is refused with the field that is missing (`status`, `needs_human`,
`ask.question`, `ask.options`). The link card has the
`DecisionCard` layout: a kicker (source and agent), the question, the context,
one row per option with its effect (the recommended row highlighted), the
deadline, the option buttons, "Answer with text" and "Open in XTM One". It is
written in the recipient's language (en, fr, es, ja). A letter typed back in a
Teams chat posted as a user or an agent account does not answer the decision:
the platform receives no chat message there (only a channel bot's chats reach
`/api/channels/teams`), so the buttons are the answer path. The I.5 typed reply
holds for the bot card of the Teams app.

S11, as built (slice 1, backend): `services/google_chat_decision_cards.py`
renders the decision as one `cardsV2` message (header with the source and the
agent, question, context, options with their effects and the recommendation,
deadline, one button per option with the recommendation filled, a text input
with its Send button except on a tool approval, "Waiting for <name>", the
links). A click is a `CARD_CLICKED` event on `/api/channels/googlechat/webhook`:
the bearer JWT is verified as for every Chat event, and a click is refused
(401) when no Cloud project number is configured, so an unverified deployment
answers nothing. The clicker is resolved by email, else by the linked Chat
identity, and the answer goes through `answer_decision` with surface
`google_chat_card`; the answer replaces the card (`UPDATE_MESSAGE`), a refusal
is a private message to the clicker alone (`privateMessageViewer`) and the card
stays (I.5). As a user, `link_message` carries the S9b answer links (surface
`google_chat`).

S11, slice 2 (the sends): a run's `trigger_config.notify_via: google_chat`
posts to `trigger_config.notify_space`, else to the app's DM with the run's
owner (their linked `google_chat:users/<id>` identity, `spaces.findDirectMessage`);
a run paused on a decision goes as its card, anything else as text, and no
channel bot, no space and no DM is the web notification. A work item's
`payload.notify` takes `channel: google_chat` with `space` (`spaces/<id>`) or
`user` (an email, the app's DM), and an optional Google `integration`: no
Microsoft integration is needed. The scheduler tick sends an open work item
decision as the app's card (app auth) where the app is in the space or has a
DM with the person, else as a user through that Google integration with the
S9b answer links (`send_google_chat_message`, the item's agent's toolset,
scope and approval gate, as for every notify send), at most three attempts,
a transient Chat failure (network, timeout, HTTP 408, 429 or 5xx, no OAuth
token, a failed DM lookup) counted as one of them and only configuration (no
bot, no space for the target, Chat refusing the app) ending the delivery,
each recorded as a `notified` event (`via` `google_chat_card` with the message
name, or `google_chat_links`). An answer given anywhere else (web, link, Teams)
edits every card the app posted (`spaces.messages.patch`, decided state), as
the Graph cards are. The end notification follows the same order. Every Chat
resource name that reaches a URL path is checked against its documented shape.

Surfaces touched, all slices: `assignment_engine` (HITL tool), `assignment_helpers`
(prompt), `agent_builder` tool texts, `hitl_whitelist` / `tools/approval.py` /
`tool_approval_service` (mapping only), `hitl_consent` (unchanged check, new
writer), `flow_runner` and `flow_runs` API, `work_item_ask` / `work_item` /
`work_items` API, `notification_feed`, `mobile_push`, `notify`,
`teams_link_cards`, `access_requests`; frontend `run-human-input.tsx`,
`action-proposal.tsx`, `run-card.tsx`, `run-activity-list.tsx`,
`ExecutionRow.tsx`, `RunDetail.tsx`, `FleetPendingApprovals.tsx`,
`WorkItemDecisionCard.tsx`, `WorkItemDetailDialog.tsx`, `WorkItemsTab.tsx`,
`Header.tsx` and `notification-kinds.ts`, `CompanionInbox.tsx`, the flow
builder run panel, `RunnerApprovalCard.tsx`, `ComposerParts.tsx`; channels
`channels/teams.py`, `teams_link_cards`, `teams_bot.py`, `work_item_notify`,
`channels/slack.py` and the Slack provider, `channels/google_chat.py`, the
WhatsApp channel; user docs
(`docs/docs/administration/ai-governance.md` and the HITL pages).

## 11. Program acceptance

An assignment with HITL on, a tool approval, a flow Ask-user node and a fleet
work item each produce the same card in the same inbox with the same bell entry
and push; answering any of them from the bell, the inbox or the phone resumes
the right engine; one e2e test per source; no run page needs to be opened to
understand or answer; runs paused before the change still answer; a decision
sent to Teams, Slack, Google Chat or WhatsApp is a card of that channel,
answerable from the card on every send path (bot, the agent's own integration,
AI agent accounts), in place or through its answer link; every link it
carries resolves; legacy paused objects answer after the G2 migration.

## 12. Constraints

- No model call on the read path (inbox, bell, card).
- No change to the pause and resume semantics of a run beyond F3.
- The G2 migration is reversible (section 8, step 4) and never retires the
  engines' own columns in the same release.
- Fingerprints, standing approvals and the consent check keep their server-side
  rules; the card only changes how they are asked.
- Every slice ships with the e2e of the gates it touches.
