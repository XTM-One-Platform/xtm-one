# Human gates: one decision system

Status: proposal (spec only, no code). Tracking issue: #842.

A person meets one kind of human gate anywhere in XTM One: the same card, the
same inbox, the same bell entry, the same answer flow, whatever asked (an agent
run, an assignment run, a per-tool approval, a flow, a fleet work item). The
card is readable on its own: nobody has to open a run to understand the
decision or to answer it. The run, flow or item stays one link under the card.

This document is the contract of that system and the plan to reach it from the
five gates main ships today. Three choices are open for the reviewer:
**F** (long waits), **G** (storage) and **H** (the Teams sender when the bot
is not in the conversation, section 9); each carries a recommendation.

## 1. What exists today

| # | Gate | Where it is asked | Stored as | Answered by | UI | Bell kind |
|---|---|---|---|---|---|---|
| 1 | Run free-text HITL | `request_human_input(question, context)`, registered by `assignment_engine` when `resolve_hitl` (agent default, assignment override) is on | `AssignmentRun.human_input_question` = question and `"Context: ..."` joined in one string; status `awaiting_human` | `POST /assignments/runs/{id}/respond` (`answer_paused_run`), `response` text | `components/ui/run-human-input.tsx` (run card, Operations row, run detail, run activity list, fleet pending approvals) | `hitl` |
| 2 | Per-tool approval | the gate of `hitl_whitelist.py` / `tools/approval.py` before a gated tool call | `pending_approvals`, `approval_decisions`; standing approvals in `ToolApprovalPreference` (schema fingerprint) | same endpoint, `decisions: [{decision: approve, approve_always, reject}]` | `ActionProposalCard`, `FleetPendingApprovals` | `hitl` (push Approve / Reject actions) |
| 3 | Irreversible consent | a tool (today `merge_opencti_entities`) asks through gate 1 for `APPROVE <code>` | inside the free-text reply, `human_input_history` | parsed server-side by `hitl_consent.py` | none of its own | `hitl` |
| 4 | Flow Ask-user node | `flow_runner._exec_ask_user` (title, question) | park `{kind: human, question, released, response, responded_by}` in `FlowRun.state.waiting` | `POST /flows/runs/{id}/respond`, `response` text | flow builder run panel | `flow_hitl` (activity row) |
| 5 | Work-item decision | `needs_human` + `ask` (`work_item_ask.py`) | `work_items.ask` {question, context, options, recommendation, deadline}; recipient in `waits_for_user_id` (`work_item_recipient.py`) | `POST /work-items/{id}/answer`, `{option, text}` | `WorkItemDecisionCard` in the work item dialog; Operations > Work Items "Waiting for me" | `work_item_waiting` |

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
| `request_human_input` | Gains `options`, `recommendation`, `deadline`, `links`. `question` alone, or with `context`, still works (backwards compatible: the old call writes a free-text decision). The readability guard refuses an unreadable question with the same message as a work item, so the agent retries in the same turn. The context is stored apart, never concatenated into the question any more; `human_input_question` keeps the joined text for legacy readers until the last slice. |
| HITL prompt | `DEFAULT_HITL_PROMPT`, the agent and assignment overrides and the `agent_builder` tool texts teach the options form in one sentence each; no prompt grows by more than one sentence (no system prompt inflation). |
| Per-tool approval | Rendered as a decision whose options are Approve (`approve`), Approve always (`approve_always`, only where standing approvals are allowed today) and Reject (`reject`). Fingerprints, standing approvals and batch decisions are unchanged: the router turns the chosen option into today's `decisions` payload. |
| Irreversible consent | Becomes an explicit option (`kind: consent`, label "Approve this irreversible action", effect naming the action). Choosing it writes the same `APPROVE <code>` entry in `human_input_history`, so `hitl_consent.py` stays the server-side check, unchanged. A typed `APPROVE <code>` reply keeps working. |
| Flow Ask-user | The node config gains `options` and `recommendation` (and `deadline`); `{{ask.response}}` stays the text, `{{ask.option}}` is new. A node without options behaves as today. |
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

"Waiting for you" (Operations, today the work items "Waiting for me" view, and
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
companion inbox reads the same list. The list is a read of the store chosen in
G, no model call on the read path.

## 5. D. One bell kind

A new kind `decision` replaces `hitl`, `flow_hitl` and `work_item_waiting` in
the feed: title = the question, subtitle = "<source> · <agent>", summary = the
deadline text (`waiting_deadline_text`), route = the inbox with the card open
(`/operations/waiting?decision=<id>`). It is urgent, unread until answered and
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

## 7. F. Long waits (open choice)

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

**Recommendation: F3.** It keeps the exact resume (the agent continues with
its context, which F2 loses), keeps one decision per question (F2 creates a
second object for the same question), and fixes the only real harm of F1 (a
routinely pausing scheduled assignment holding its queue). N defaults to 4
hours for scheduled and event runs and is off for chat-started runs.

## 8. G. Storage (open choice)

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

**Recommendation: G1 now, with the router and the audit trail as the only new
write.** The regression risk sits in the three pause and resume paths, and G1
leaves them untouched; the inbox query unions three indexed reads
(`assignment_runs.status = awaiting_human`, flow runs with a human park,
`work_items.needs_human`), each already filtered by owner today. A single
`decision_answers` audit table (append only) gives one trail without moving
the questions. G2 stays open if the union read measures above budget at
production volume; the `Decision` view model makes that switch internal.

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
| Teams | Adaptive Card 1.5, Universal Actions (`Action.Execute`, per-viewer `refresh`) | one button per option, recommendation as the positive style, free text as `Input.Text` | yes, bot only (I.4) |
| Slack | Block Kit (header, section, context, actions) | one button per option, recommendation `primary`, free text through a modal | yes (interactivity endpoint) |
| Google Chat | cardsV2 | one button per option, free text as a text input widget | yes (`CARD_CLICKED`) |
| WhatsApp | interactive message | up to 3 options as reply buttons, else a list; 1:1 only | yes (button reply webhook) |
| Discord, Telegram | components / inline keyboard, if the channel's API allows | buttons | after the others, if their APIs allow |
| Any other, email | plain text and an Open link | none | no (the link opens the card) |

Each card carries the question, the context, the options with their effect,
the recommendation marked, the deadline (in UTC when the viewer's zone is
unknown), the links, and "From <source> by <agent>". Agents never write card
JSON: they pass a decision.

### I.2 Every path that sends a decision uses it

- The notify path of D (the bell copy sent to Teams, Slack or email).
- `work_item_notify` (`teams_dm`, `teams_channel` and the other targets).
- An agent posting a decision itself: the channel send tools take a
  `decision` argument (the ask contract) instead of hand-made card JSON, and a
  decision posted this way is the same decision the inbox lists (one id, one
  answer).
- Pasted links: `teams_link_cards` is generalised from runs to every source
  (run, flow run, work item, the inbox URL), the verbs become
  `xtm.decision.answer` (option or text) with `xtm.run.decide` and
  `xtm.run.answer` kept as aliases for cards already in conversations.

### I.3 Answering in place

A button calls the answer router of E as the person who clicked: identity by
tenant and email (Teams), or the linked channel identity (Slack, Google Chat,
WhatsApp); the same recipient rule, row lock and audit as the web, with the
channel recorded as the surface. The card then refreshes to the decided state
for every viewer: who answered, when, which option. Slack requests are checked
with the signing secret, Google Chat with its token verification, WhatsApp
replies are mapped to the open decision of that conversation by the button
payload (never by the reply text).

### I.4 The Teams sender (choice H)

A Teams card that answers in place must be posted by the Teams bot, installed
in that chat, group chat or team. Detection: before the send, the bot's
conversation reference for the target (1:1 with the recipient, the group chat
id, the team and channel) is read from the bot's installation store; present =
the bot posts the interactive card.

- **H1, bot when present, else a read-only card from the account**: the
  agent's account (user or service account, Graph) posts the same card with
  one `Action.OpenUrl` "Open" button to the inbox card, and the notify result
  says the bot was not installed there. No silent text-only send.
- **H2, bot only**: refuse the send when the bot is not installed.
- **H3, plain text fallback**: today's behaviour.

**Recommendation: H1.** The person always gets the decision as a card in the
conversation it was meant for, and the gap is visible to the sender instead of
a lost message. Admin setup stated in the user docs: the bot app is installed
for the tenant (manifest with `bots` scopes `personal`, `groupChat`, `team`),
added to each team or group chat where agents post decisions; 1:1 needs the
app installed for the user (proactive install through Graph where the tenant
allows it).

### I.5 1:1, group and channel behave the same

In a group chat or channel every viewer sees the card; only the recipient (or
the recipient group, or `company`) can answer; any other viewer sees "Waiting
for <name>" in place of the buttons (Teams per-viewer refresh; on Slack and
Google Chat a click from a non-recipient is refused with an ephemeral message
and the card is unchanged).

### I.6 Acceptance per channel

For each channel: the four sources (run HITL, tool approval, flow Ask-user,
work item) produce the card; answering from it resumes the right engine; the
card refreshes for every viewer; a non-recipient is refused; the fallback
renders. A UX Director round on the card visuals, Teams dark and light themes
included.

## 10. H. Slices

Each slice is one PR on xtm-one with its dev item linked to the program item,
the e2e of every gate it touches, auto-merge at green after the UX Director
APPROVE for a UI slice. No slice starts before this spec is accepted, the
decision-card links work (#834 branch) is merged and the work items e2e
verification is done.

| Slice | Content | UI | Acceptance tests |
|---|---|---|---|
| S1 | `decision_ask.py` shared model and guard (re-exported by `work_item_ask`); `request_human_input` options / recommendation / deadline / links, context stored apart; HITL prompt and tool texts | no | unit: old call shape still pauses with the same question; options validated; unreadable question refused; e2e: assignment with HITL on pauses and resumes with an option and with free text |
| S2 | `Decision` view model and `decision_feed` projection (G1); `POST /decisions/{id}/answer` router with the one recipient rule and `decision_answers` audit; old endpoints call the same dispatch | no | unit per source dispatch and refusal; e2e: answer a run, a flow park and a work item through the router; a run paused before the change answers through the old endpoint |
| S3 | `DecisionCard` (generalised from `WorkItemDecisionCard`) on run detail, Operations rows, run card, fleet pending approvals, work item dialog; tool approvals as Approve / Approve always / Reject; consent option | yes | e2e: tool approval decided from the card (approve, approve always fingerprint kept, reject); consent option resumes `merge` with the check passing; card readable without the run page |
| S4 | Bell kind `decision` with aliases; push urgency and actions unified; `flow_hitl` unread until answered; Teams and email text aligned | yes (bell, companion) | unit: one presenter for the three sources; aliases still read; e2e: each source rings one `decision` entry, cleared when answered |
| S5 | "Waiting for you" inbox across sources, companion inbox on the same list, home notice | yes | e2e: four sources listed in one view, answered from the side panel |
| S6 | Flow Ask-user options / recommendation / deadline and the flow run panel on the card | yes | e2e: flow parks, answered by option from the inbox, `{{ask.option}}` downstream |
| S7 | Long waits per F; scheduled run pause rule | no | e2e per the chosen option |
| S8 | Chat inline approvals and runner shell approvals on the card; `run-human-input.tsx` removed; old bell kinds dropped after a release | yes | e2e: chat approval and runner approval from the card; no caller of the old component left |
| S9 | Teams: `render_decision_card` Adaptive Card renderer; `teams_link_cards` generalised to every source with `xtm.decision.answer` (old verbs aliased); bot detection and the H read-only fallback; `work_item_notify` and the D notify path send the card; channel send tools take a `decision` | yes (card visuals, dark and light) | e2e per I.6 on 1:1, group chat and channel; old verbs still answer a card posted before the change |
| S10 | Slack: Block Kit renderer, interactivity endpoint with signing-secret check, modal for free text | yes | e2e per I.6 |
| S11 | Google Chat: cardsV2 renderer, `CARD_CLICKED` handler with token verification | yes | e2e per I.6 |
| S12 | WhatsApp (1:1): reply buttons or list, button reply webhook mapped to the open decision; Discord and Telegram if their APIs allow | yes | e2e per I.6 (1:1 only for WhatsApp) |

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
answerable in place where the channel allows it.

## 12. Constraints

- No model call on the read path (inbox, bell, card).
- No change to the pause and resume semantics of a run beyond what F adopts.
- Fingerprints, standing approvals and the consent check keep their server-side
  rules; the card only changes how they are asked.
- Every slice ships with the e2e of the gates it touches.
