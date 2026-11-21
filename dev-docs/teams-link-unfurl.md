# XTM One links unfurled in Microsoft Teams - PRD

| | |
|---|---|
| Status | Decisions D1 to D7 validated 2026-10-27. v1 implemented: run cards (read, answer) and action requests on runs; share requests (conversations, v1.1) and the Teams copy of a request's notification not yet |
| Area | Teams channel (`backend/app/channels/teams.py`, the Setup Guide's manifest example in `backend/app/api/v1/channel_bots.py`), human-in-the-loop (`backend/app/tools/approval.py`, `backend/app/services/chat_approval_resume.py`, `POST /api/v1/assignments/runs/{id}/respond`) |
| Inspiration | GitHub and Azure DevOps link unfurling in Teams |

## 1. Problem

When someone pastes a GitHub pull request link in Teams, the link turns into a card: title, state, author, a button. When someone pastes an XTM One link, Teams shows a bare URL, at best a generic page preview.

The cost is highest when the link points at something waiting for a person. An assignment run paused for approval stops until somebody acts, and today that somebody has to leave Teams, open XTM One, find the run and answer it there. The Teams channel itself cannot collect an approval: a chat turn that hits a gated tool stops with `no_approval_channel_message` ("This chat can't collect that approval"), and a channel message retires the pending approval (`retire_pending_approval`). So people paste the link in a channel with "can someone approve this?", and the work waits for whoever opens the web app first.

## 2. Goal

Turn an XTM One link pasted in Teams into an Adaptive Card that shows each viewer the state of what it points to, as far as their own XTM One access allows, and, when that thing waits for a person, lets the viewers who may answer act on it from the card: approve or reject a tool call, answer a question.

### Goals

- G1. A pasted link to an assignment run (`/operations/executions/:id`) unfurls into a card rendered for each viewer: agent, assignment, status, timing and, when the run is `awaiting_human`, what it is waiting for. A viewer without access sees that they have none, and nothing about the run.
- G2. From that card, a person with the right to respond approves or rejects each pending tool call, or answers a free-text question (`request_human_input`), without leaving Teams.
- G3. The card stays current: once someone decides, or the run moves on, every viewer sees the new state the next time Teams refreshes the card.
- G4. Every action goes through the same access rule, the same locking and the same audit trail as the web app's respond endpoint.
- G5. The cards XTM One's own bot posts (run notifications, section 10) use the same card, so a run paused for approval reaches its owner as an actionable card instead of plain text.

### Non-goals (v1)

- Zero-install unfurling (`supportsAnonymizedPayloads`): Teams forbids Universal Actions in that response, so it could only show a read-only preview. Left for later.
- Standing approvals ("always allow this tool") and dismissing a run from the card. They stay in the web app.
- Unfurling in Slack, Google Chat, Discord or Outlook. The card renderer is written so another surface can reuse it, but v1 ships Teams only.
- Starting a run ("Run now") or chatting with an agent from a card.

## 3. Decisions

| # | Decision | Consequence |
|---|---|---|
| D1 | **The card is rendered for the person who views it**, as GitHub does: each viewer sees what their own XTM One access allows, whoever pasted the link. | Two members of the same conversation can see two different cards for the same message. A revoked share or a deleted run applies at the viewer's next refresh. |
| D2 | **The message itself holds only the neutral card.** The answer to the paste carries no run detail; details only arrive through the per-viewer refresh (Universal Actions `refresh`). | What Teams stores and shows outside a refresh (activity feed, search, notifications, clients without Universal Actions, channels over 60 members before a manual refresh) reveals nothing about the run. |
| D3 | **A viewer without read access is told so, and told nothing else**: "This run doesn't exist or you don't have access to it", or, for a Teams user who matches no XTM One user, "Sign in to XTM One with the email address of this account to see this run". Never the run's title, agent or state. | Like GitHub signed out. "Missing" and "forbidden" read the same, so the card is no oracle for guessing ids. |
| D4 | **Buttons are shown only to viewers who may respond**; a viewer who may read but not respond sees the state without them. The server checks the clicker's rights again on every click. | Hiding buttons is a convenience; the server-side check is the rule. A refused click gets a private, generic message, writes nothing and is audited. |
| D5 | **Channels over 60 members rely on the manual refresh.** Teams refreshes automatically only up to 60 members; above, a viewer chooses Refresh card in the message menu. | No extra delivery (no private copy to responders). The neutral card says how to load the details. |
| D6 | **A viewer without access can request it from the card, and the request is a share request inside XTM One**: a notification to whoever can grant the share, who grants it with the entity's own sharing. | Section 9. The request is a platform entity, not a Teams one: any surface that shows "no access" can offer it. |
| D7 | **Runs are not shared.** A request on a run asks whoever can answer it to act themselves; it grants nothing. Run visibility stays `run_visible_filter` (owner, company-managed, admins). | On a run card the button reads "Ask the owner to act", and the requester never sees the run (section 9). |

Earlier draft (2026-10-26) rendered the card with the paster's rights, treating a paste as a share. Dropped: the stored message content would have shown the paster's view to every member, including those Teams never refreshes for.

## 4. Users and access

- **Paster**: anyone with the Teams app. Pasting gives no right and shares nothing beyond the neutral card (D2).
- **Viewer**: any member of the conversation, resolved to an XTM One user by email (SSO users link by email, invariant 8). Sees the run when the read rule of the run detail page (`GET /api/v1/assignments/runs/{id}`) lets them; otherwise the D3 card. That rule is `run_visible_filter`: the run's owner, every user for a company-managed run, every run for an admin. Runs have no share, and get none (D7): sharing an agent or an assignment does not make its runs visible, on purpose.
- **Responder**: a viewer who may answer the run, by the rule of `POST /api/v1/assignments/runs/{id}/respond` (`require_run_access(run, user, action="respond to")`): an admin for any run, otherwise the owner of a private run. A company-managed run is visible to everyone but answerable by admins only. Only responders see the buttons (D4).
- **Grantor**: whoever may share the entity a viewer requests access to (section 9).

Both rules are the web's rules. They are not re-spelled for Teams: the card reads with the same `run_visible_filter` and `require_run_access` as the run detail and respond endpoints, and answers through the respond endpoint's own function (FR-12).

## 5. Scope: which links unfurl

| Link | Phase | Card | Actions |
|---|---|---|---|
| `/operations/executions/:id` (assignment run) | v1 | Agent, assignment, status, trigger, started / duration; when `awaiting_human`: the question, or each pending tool call: with its arguments for a responder (secrets masked as on the web, `redact_known_secrets_for_display`), its name only for a reader (`redact_proposals`) | Approve / Reject per call (with an optional reason), or an answer box for `request_human_input`; always "Open in XTM One" |
| `/agents/:id/chats/:conversationId` and `/chat?c=<id>`, with a pending approval | v1.1 | Agent, conversation title, the gated calls; read rule: the owner, `conversation_shares` grants, or anyone signed in when `link_access` is `org_read` (made for links pasted in Teams) | Approve / Reject, resumed through `chat_approval_resume` (a turn that ended at the gate) or `push_approval_decision` (a turn still waiting) |
| `/agents/assignments/:id`, `/agents/:id` | Later | Read-only summary: last run, schedule, agent description | "Open in XTM One" |
| Agent account replies, runner commands, flow runs awaiting a decision | Later | As runs | Their existing approve endpoints, through the same extraction pattern |

A link unfurls only when its origin and path prefix are XTM One's own `BASE_URL` (including `BASE_PATH`), and the id parses as a UUID. Any other link gets no response, and Teams falls back to its default preview.

## 6. User experience

### Run awaiting approval, as a responder sees it

```
┌──────────────────────────────────────────────────────────┐
│ [XTM One]  Assignment run · Awaiting approval            │
│ Phishing triage  ·  agent: Mail Triage                   │
│ Started 10:42 by schedule · paused 3 min ago             │
│──────────────────────────────────────────────────────────│
│ The agent wants to run 2 tools:                          │
│                                                          │
│ 1. opencti_create_indicator                              │
│    pattern: [domain-name:value = 'evil.example']         │
│    ( ) Approve   ( ) Reject                              │
│                                                          │
│ 2. send_email                                            │
│    to: soc@acme.example · subject: Weekly triage         │
│    ( ) Approve   ( ) Reject                              │
│                                                          │
│ Reason (optional) [__________________________]           │
│                                                          │
│ [Approve all]  [Reject all]  [Submit]  [Open in XTM One] │
└──────────────────────────────────────────────────────────┘
```

### After a decision

```
┌──────────────────────────────────────────────────────────┐
│ [XTM One]  Assignment run · Running                      │
│ Phishing triage  ·  agent: Mail Triage                   │
│ ✔ 2 tools approved by Jane Doe from Teams, 10:46         │
│                                       [Open in XTM One]  │
└──────────────────────────────────────────────────────────┘
```

### Viewer who may read but not respond

The same card as above without the choices, the reason box and the buttons other than "Open in XTM One", and one line: "Waiting for the run owner's approval." (D4).

### Viewer without access (D3)

```
┌──────────────────────────────────────────────────────────┐
│ [XTM One]  Assignment run                                │
│ This run doesn't exist or you don't have access to it.   │
│               [Ask the owner to act]  [Open in XTM One]  │
└──────────────────────────────────────────────────────────┘
```

After "Ask the owner to act", the same card reads "Your request has been sent." to that viewer, whatever happened (D7, section 9, FR-17). On a conversation card the button will be "Request access" (D6).

A viewer who can read a run but not answer it (a non-admin on a company-managed run awaiting a decision) gets the same "Ask an admin to act" button under the state.

A Teams user who matches no XTM One user reads "Sign in to XTM One with the email address of this account to see this run" instead, with no request button: there is no account to notify about or to grant a share to.

### Neutral card (stored in the message, D2)

What the paste posts and what every client shows before, or without, a refresh: "XTM One assignment run", "Details load for each viewer; if they don't, choose Refresh card in the message menu", and "Open in XTM One". It names no run, agent or state.

### Other states

- **Awaiting an answer** (`request_human_input`): the question, an answer box, Send (responders only).
- **Running / completed / failed / cancelled**: status, duration, last error (truncated) for a failed run, Open.

### Refused or stale action

Shown only to the person who clicked (an `adaptiveCard/action` response reaches the invoking client only):

- No right to respond: "You can't respond to this run."
- Already answered: "This run has already been answered." The card shows the current state, with who answered.
- Calls not the ones the card showed: "This request changed. Check it again."; a partial set: "Choose Approve or Reject for every tool."
- Teams account not linked to an XTM One user: the D3 "sign in" card.

## 7. Functional requirements

- **FR-1** The Teams app manifest declares a `composeExtensions` entry on the existing bot, with a `messageHandlers` entry of type `link` whose `domains` list XTM One's host. The host is per deployment, so each admin builds the app from the manifest example served by `GET /api/v1/channel-bots/catalog`, which carries the handler with the host placeholder. The product ships no Teams app package: Filigran's own tenant package is built in the release repository.
- **FR-2** The Teams webhook handles `invoke` activities named `composeExtension/queryLink` and `adaptiveCard/action`, behind the same Bot Framework JWT check as messages. Today every activity other than `message` and `conversationUpdate` is dropped (`teams.py`, `teams_messages`).
- **FR-3** `queryLink` parses the URL (section 5: own origin, UUID id) and answers with the neutral card (D2) and its required `preview`. It reads no run: the paste reveals nothing, not even whether the run exists. A link that is not ours gets no response.
- **FR-4** The neutral card carries a `refresh` action (`Action.Execute`, no `userIds`), so Teams asks the bot for each viewer's card automatically in conversations up to 60 members, and from the message menu above that.
- **FR-5** The card's data carries the entity type and id, and, on a card with decisions, the fingerprint of each proposal it shows. None of it is trusted: card data comes back from the client and is hostile. The viewer always comes from the activity (FR-7), never from card data.
- **FR-6** A refresh resolves the viewer (FR-7) and renders one of three views: no read access or no such run → the D3 card; read access → the state; respond right on an `awaiting_human` run → the state with the actions (D4). The view is rebuilt at every refresh, so a revoked share or a deleted run applies at once.
- **FR-7** Teams identity resolution: `from.aadObjectId` → email through the Bot Framework member API (`_resolve_teams_email`), cached for a short time → active XTM One user by email. No match: the "sign in" variant of the D3 card for a viewer, the "not linked" message for a clicker.
- **FR-8** Approve / Reject and answers call the extracted respond service (FR-12) as the clicker, never as the paster. It locks the run row, refuses a run that is not `awaiting_human` or already answered, and refuses a partial decision set (every pending call must be decided).
- **FR-9** A decision applies only to the calls the clicker was shown: the card carries the `tool_call_id`s it rendered, in order, and a mismatch with the run's current `pending_approvals` refuses the decision and refreshes the card ("This request changed. Check it again."). The ids are enough: a new pause gets new call ids, and a call's arguments never change under its id. A run with more calls than a card shows (10) is decided in the web app, whatever the card data says. A card action (refresh or button) is answered only for an activity from the Teams bot's configured tenant; a bot without one answers none, and its links keep the neutral card.
- **FR-10** Every decision and every refusal is recorded as the web ones are (`record_human_input_resolution`, `record_refused_decision`), with `surface = "teams_card"` and the responder's identity.
- **FR-11** The card text comes from the notice catalog (`channel_notices`), in the viewer's language (`notice_language`); the neutral card and an unknown viewer's card in the platform default language.
- **FR-12** The body of `POST /assignments/runs/{run_id}/respond` moves into `answer_paused_run(db, run_id, user, body, surface=...)`, which the endpoint and the Teams card both call; the card reads with the run detail's `run_visible_filter`. Tests run against the real route and the Teams invokes.

## 8. Security requirements

Everything in the card that a third party controls is hostile: run titles, agent and assignment names, the question, tool names and arguments, error messages.

- **Rendering**: values go into `TextBlock` text only, escaped for Adaptive Card markdown (links, emphasis, line breaks), truncated, and never into a URL, an image or an action. The only URLs in the card are built from `BASE_URL` and an id. A table-driven test covers every injection variant for this destination.
- **Arguments**: shown to responders only, with secrets masked as the web approval popup masks them (`redact_known_secrets_for_display`), each value truncated; readers get the tool names (`redact_proposals`).
- **Errors**: the clicker gets a generic message; the precise reason only goes to the logs (`%r`, truncated).
- **Identity**: the viewer and the clicker are resolved from the Bot Framework activity (`from.aadObjectId`, verified by the JWT), never from card data.
- **No oracle**: the neutral card is the same for every link, and the D3 card is the same for a missing run and a forbidden one.
- **Rate**: invokes are authenticated by Bot Framework, but every viewer refreshes. Rendering is cached per (entity, viewer) for a few seconds, and the per-tenant invoke rate is bounded.
- **Replays**: a decision is idempotent through the row lock and the "already answered" refusal; a replayed action with an old fingerprint is refused (FR-9).
- **Edition**: a decision resumes the run through `resume_assignment_run`, as the web does, so it keeps every gate the web resume applies. The card itself is not gated.

## 9. Access requests

Decisions D6 and D7. A viewer who is an XTM One user can ask, from the card, for what they lack. The request lives in XTM One; Teams is only where it was clicked. It takes one of two kinds, set by the entity:

| Entity | Kind | Button | What the recipient does |
|---|---|---|---|
| Conversation (v1.1) | **Share request** (D6) | Request access | Grants a named read share in `conversation_shares`, or declines |
| Assignment run (v1) | **Action request** (D7) | Ask the owner to act / Ask an admin to act | Opens the run and acts on it; nothing is shared |
| Agent, assignment (later) | Share request | Request access | Grants `shared_user_ids` at the `read_only` tier, or declines |

- **FR-13** Clicking creates a request (`access_requests`): kind, requester, entity type and id, source surface (`teams_card`), created at, status. A share request asks for the read tier and ends `granted` or `declined`; an action request ends `closed`. One open request per requester and entity (a partial unique index): a second click creates nothing and is answered like the first, "Your request has been sent." - an "already requested" would tell a requester the run exists.
- **FR-14** The request notifies its recipients through the notification bell (an `Activity` row and the real-time push, as `notify_web_user` does); a copy in their Teams personal chat comes with section 10. Recipients: for a share request, the entity's owner, or the administrators when it has no active owner; for an action request, whoever may answer the run by `require_run_access`: its owner for a private run, the administrators for a company-managed run or a run whose owner is deactivated.
- **FR-15** A share request opens the entity's share dialog in XTM One with the requester and the tier filled in. Granting writes the share through the entity's own share endpoint, with its rule and its audit; declining closes the request. The requester is notified of either outcome in the bell, and their Teams card shows the new view at its next refresh.
- **FR-16** An action request reads "Jane Doe asks you to act on the run *Phishing triage*" and opens the run. It grants nothing and the requester never sees the run (D7). It closes when a recipient dismisses it, when the run is answered (every answer goes through `resume_assignment_run`, which closes them) or when the run is dismissed; the requester is not told how, since that would describe the run. An action request is recorded only for a run that waits for a person, and never by someone who could answer it themselves.
- **FR-17** No oracle: on a missing entity the click answers exactly as on a real one ("Your request has been sent."), creates nothing and notifies nobody. The run button reads "Ask the owner to act" whatever the run's state, so it does not say whether the run waits for anything.
- **FR-18** Bounds: one open request per requester and entity, and a per-requester rate limit across entities, so a card cannot be used to flood an owner. The requester's name and the entity title are hostile values in the notification and are escaped there as in the card (section 8).
- **FR-19** Open requests are listed where the recipient acts: share requests in the entity's share dialog, action requests on the run page (`GET /assignments/runs/{id}/action-requests`, dismissed with `POST .../{request_id}/dismiss`, both behind `require_run_access`), so a recipient who missed the notification still finds them.

## 10. Cards posted by the bot (phase 2)

`services/notify.py` (`_notify_teams`) already posts a plain-text message to the owner's personal chat when a run completes or pauses. In phase 2 it attaches the same card. Because the bot owns that message, it can also update it for everyone after a decision (`PUT /v3/conversations/{id}/activities/{activityId}`), which a pasted card cannot have: the bot cannot edit a message a user sent. The same applies when an agent posts in a channel about a paused run, with the same rules: in a conversation with several members, the posted content is the neutral card and the details arrive per viewer (D1, D2). Only a personal chat with the run's owner may carry the details in the posted message itself.

## 11. Teams platform constraints and risks

| Topic | What Microsoft documents | Impact |
|---|---|---|
| Card version in unfurls | The link unfurling page says unfurling supports Adaptive Cards 1.3 and earlier; Universal Actions (`Action.Execute`, `refresh`) need 1.4. The Universal Actions overview also says link unfurling cards "must call `Action.Submit`". | **Main risk. Spike first**: paste a 1.4 card with `Action.Execute` and `refresh` from a test bot, on desktop, web and mobile. Per-viewer rendering (D1) rests entirely on `refresh`: without it, every viewer keeps the neutral card. Fallback: the neutral card with "Open in XTM One" only, the GitHub signed-out experience, with no detail and no action in Teams. |
| Installation | `queryLink` reaches the bot only for users who have the app (installed or deployed by their tenant admin). Universal Actions in group chats and channels need the bot in `team` / `groupChat` scope; Teams adds the bot to the conversation on send (just-in-time install). | Rollout note for admins: deploy the app tenant-wide. The bot already declares those scopes. |
| Cache | An unfurl result is cached by Teams for 30 minutes unless the bot sets a `no-cache` policy. | No effect: the posted card is the same neutral card for a link whatever the run's state, and the state comes with each viewer's refresh. |
| Refresh | Automatic for every viewer in conversations of 60 members or fewer, manual from the message menu above. | In channels over 60 members, each viewer sees the neutral card until they choose Refresh card; the neutral card says so. The action path refuses stale decisions anyway (FR-8, FR-9). |
| Mobile | Mobile unfurls only links that need no authentication. | Ours needs no user authentication (the bot resolves the user), to be confirmed in the spike. |
| Domain | The manifest lists the domains to unfurl; `validDomains` is fixed to `copilot.filigran.ai` in the published template. | Self-hosted installs need a manifest generated with their host (FR-1). |
| Response time | Message extension invokes must answer within a few seconds. | `queryLink` does one member lookup (cached) and one read. No LLM call, no outbound request to a third party. |

## 12. Rollout

1. Spike on the 1.3 / 1.4 question (section 11), with a throwaway bot. It decides between the target design and the `Action.Submit` fallback.
2. v1: `/operations/executions/:id` cards, read and act; respond service extraction; the link handler in the Setup Guide's manifest example; user docs for admins (re-upload the app). Action requests on runs, with the request entity and its bell notification.
3. v1.1: chat conversations with a pending approval, with share requests on them.
4. Phase 2: bot-posted run notifications use the card (section 10).

A manifest change needs every tenant admin to update the app, so it ships once with the link handler; later phases change the backend only.

## 13. Success measures

- Share of `awaiting_human` runs answered from a Teams card, among runs whose link was unfurled.
- Median time from pause to decision, before and after, for runs whose link was shared in Teams.
- Refused clicks per reason (no right, not linked, stale): a high "not linked" rate points at email mismatches between Teams and XTM One.

## 14. Open questions

- Q1. Does the spike confirm `Action.Execute` and `refresh` in an unfurled card on every client? (Decides the design.)

Resolved 2026-10-27: manual refresh is enough in channels over 60 members (D5); the D3 card offers a share request (D6); runs are not shared, a request on a run asks its owner to act (D7), rather than a new read-only run share or no request on runs at all.

## 15. Test plan

- Unit: URL parsing (own origin and base path only, UUID ids, trailing slashes, query strings, other hosts, lookalike hosts); card rendering per run state and per view (none, read, respond); the neutral card is byte-identical for every link; the escaping table (section 8).
- Integration (`pytestmark = pytest.mark.integration`): `queryLink`, refresh and `adaptiveCard/action` through the real webhook with a signed Bot Framework activity. `queryLink` reads no run (spy on the session). Per viewer and per clicker on runs: owner, non-owner, non-admin on a company-managed run (reads, no buttons), admin, deactivated owner, unlinked Teams user, missing run (same card as forbidden). On conversations: named grant, revoked grant, `org_read`, group grant. Tampered card data (another run id, another fingerprint); concurrent decisions (one wins, one is told); partial decision set.
- Action requests (runs): the notification reaches exactly the users `require_run_access` lets answer (owner of a private run; admins for a company-managed run or a deactivated owner) with the exact Activity row; the requester's card still shows the D3 view afterwards; a second request is refused; a request on a missing run answers the same and writes nothing; the request closes when the run leaves `awaiting_human`.
- Share requests (conversations): the notification reaches the owner (the admins when there is no active owner); granting goes through the conversation's share endpoint and the requester's next refresh shows the content; declining notifies the requester.
- Contract: the respond REST endpoint and the Teams action produce the same run state and audit rows for the same input.
- Manual: paste on Teams desktop, web, iOS and Android; personal chat, group chat, channel under and over 60 members.
