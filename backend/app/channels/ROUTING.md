# Channel Routing – Definitive Reference

> **Canonical source of truth** for how inbound messages flow from platform
> webhooks to agent responses.  Read this before modifying any channel adapter,
> router gate, or agent prompt logic.

---

## 1. High-Level Flow

```
Platform webhook (Slack / Teams / WhatsApp / Telegram / Discord / Google Chat)
  │
  ▼
┌──────────────────────────────────────────────┐
│  CHANNEL ADAPTER  (slack.py, teams.py, …)    │
│                                              │
│  1. Parse payload                            │
│  2. Ignore bot's own messages                │
│  3. Detect bot @mention → bot_was_mentioned  │
│  4. Strip bot mention from text              │
│  5. Dedup (Slack, Discord — Redis SET NX)    │
│  6. Resolve user email                       │
│  7. Build InboundMessage with metadata       │
│  8. spawn_channel_task(process_message)      │
│     (bounded + tracked — see base.py)        │
│  9. Return 200 immediately                   │
└──────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────┐
│  ADAPTER PROCESS FUNCTION                    │
│  (_process_slack_message, etc.)              │
│                                              │
│  1. Compute show_typing                      │
│     (DM OR bot_mentioned OR threaded_reply)  │
│  2. If show_typing → typing_keepalive wrap   │
│  3. Call handle_message(inbound)             │
│  4. If suppressed → clear typing, return     │
│  5. If not suppressed → send reply           │
└──────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────┐
│  ROUTER  handle_message()  (router.py)       │
│                                              │
│  Gate 0a: Bot receive whitelist              │
│  Gate 0b: Bot-level only_mention             │
│       └─ Thread continuation bypass          │
│  Gate 0b': Pre-compute thread continuation   │
│           for agent-level checks             │
│  Cmd  0c: /reset → archive conversation      │
│  Cmd  0d: /stop  → mute thread (Redis)       │
│  Gate 0e: Muted-thread gate                  │
│       └─ @mention lifts the mute             │
│  Step 1: Resolve user (email, then identity) │
│  Step 2: Anonymous access check              │
│  Step 3: Resolve agent                       │
│       └─ _resolve_channel_agent()            │
│           Pass 0: User DM override           │
│           Pass 1: User-visible agents        │
│           Pass 2: All enabled agents         │
│       └─ _agent_allows_channel()             │
│           • receive_enabled check            │
│           • receive_channels whitelist       │
│           • Agent-level only_mention check   │
│             (with thread continuation bypass)│
│  Step 4: Find/create conversation            │
│  Step 5: Load history + compact              │
│  Step 6: Build message_context               │
│  Step 7: Run agent chat (LLM)               │
│  Step 8: Check [NO_RESPONSE]                 │
│  Step 9: Finalize (persist, mark thread)     │
│  Return OutboundMessage                      │
└──────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────┐
│  AGENT CHAT  (agent_chat.py)                 │
│                                              │
│  System prompt includes:                     │
│  • Agent persona + guardrails                │
│  • Message Context (platform, channel type)  │
│  • MUST RESPOND (if DM or @mention)          │
│    OR Silent Response (if group, no mention) │
│  • Thread Continuation Mode (if active)      │
│  • Date/time, user identity, memory          │
│                                              │
│  Agent can return [NO_RESPONSE] ONLY when:   │
│  • NOT a DM and NOT @mentioned               │
│  • Message is clearly not directed at it     │
└──────────────────────────────────────────────┘
```

---

## 2. The `show_typing` Decision

The typing indicator ("is thinking…" / "Analyzing…") should ONLY show
when the message is likely to produce a response.  Showing typing then
going silent is confusing.

| Signal            | Slack | Teams | Telegram | WhatsApp | Discord | Google Chat |
|-------------------|-------|-------|----------|----------|---------|-------------|
| DM / private chat | Yes   | Yes   | Yes      | n/a (no-op) | Yes  | n/a (typing is no-op) |
| Bot @mentioned    | Yes   | Yes   | Yes      | n/a (no-op) | Yes  | n/a (typing is no-op) |
| Threaded reply    | Yes   | Yes (has `reply_to_id`) | No | n/a (no-op) | No | n/a (typing is no-op) |
| Group, no mention | No    | No    | No       | n/a (no-op) | No   | n/a (typing is no-op) |

**Slack-specific:** When the response is suppressed and `show_typing` was
True, `clear_typing_status()` is called to remove the persistent "is
thinking…" indicator.  Teams/Telegram typing auto-expires after ~3–5s.
**Discord:** typing is triggered via the REST `/typing` endpoint and
auto-expires (~10s). **Google Chat:** the REST API has no typing indicator
endpoint, so `send_typing_indicator` is a no-op.

---

## 3. Bot-Level Enforcement (Settings → Agent → Assignment)

Three channel-config flags follow a **bot-enforced** model. The channel
bot setting (configured in Settings → Integrations → Channels & Bots) acts as a hard
constraint that downstream levels cannot violate.

### `allow_anonymous`

| Bot setting | Agent / GA / Assignment |
|-------------|----------------------|
| **false** | **Forced false** — toggle disabled, cannot be overridden |
| **true** | Free to set true or false (default: true) |

Additional constraint: if agent sets false → assignment is forced false.

### `allow_private`

| Bot setting | Agent / GA / Assignment |
|-------------|----------------------|
| **false** | **Forced false** — toggle disabled, cannot be overridden |
| **true** | Free to set true or false (default: true) |

Additional constraint: if agent sets false → assignment is forced false.

### `only_mention`

| Bot setting | Agent / GA / Assignment |
|-------------|----------------------|
| **true** | **Forced true** — toggle disabled, cannot be disabled |
| **false** | Free to set true or false (default: false) |

At the assignment level, `only_mention` is **always inherited from the
agent** (read-only). The assignment cannot independently control it.

---

## 3b. The Only-Mention Gate (Two Levels)

The gate fires at **two places** in the router:

### Level 1: Bot-level gate (before agent resolution)

```python
if channel_bot.only_mention
   AND NOT private_conversation
   AND NOT bot_mentioned
   → check thread continuation
     → if thread active → allow (set is_thread_continuation=True)
     → else → SUPPRESS (return suppress=True)
```

### Level 2: Agent-level gate (inside `_agent_allows_channel`)

```python
if _resolve_only_mention(channel_bot, agent, channel)
   AND NOT private_conversation
   AND NOT bot_mentioned
   AND NOT is_thread_continuation
   → reject agent (try next candidate)
```

**Why two levels?** The bot-level gate catches the common case quickly
(before any DB queries for agents). The agent-level gate handles per-agent
overrides where the bot says `only_mention=False` but a specific agent
says `only_mention=True`.

### Typed @mentions of other agents

Opt-in: `CHANNEL_AGENT_MENTIONS_ENABLED` (off by default), read first by
`_read_channel_mentions`. Off, the `@` tokens of a channel message are never
read and the message is answered by its routed agent alone, metered as any
message. On, after the routed agent answers an identified user, every other
agent the message names as `@key` answers in turn (`_answer_channel_mentions`),
but only an agent that can answer that message under its OWN channel settings
(`_answers_channel_mention`, applied by `_read_channel_mentions` before the
routed agent runs and again before the mentioned ones do): `enabled`, not
`disable_chat`, `channel_config[channel].receive_enabled`,
`_agent_allows_channel` with the same `bot_whitelist_passed` the routing saw
(so an agent without `bypass_whitelist` never answers where the bot whitelist
failed), `receive_channels`, the bot-enforced `allow_private` and
`only_mention` (read off the inbound platform mention), and
`publish_enabled`. Once the switch is on, the agent's channel settings decide.
Any other `@` token is plain text - no reply, no note, and the routed agent's
run is metered exactly as for a message without mentions - so a channel whose
messages carry `@support` or `@sales` is answered as before unless an agent
with that key is set up there. Only the per-message limit
(`MAX_MENTIONED_AGENTS`) still leaves a note, for an agent that could have
answered. Anonymous senders never trigger mentions. Every agent that runs is
one agentic action, charged before it runs by the chat quota gate its own
`run_agent_chat` meets (`enforce_agentic_quota_ex`), with its own execution
count and its own credit-back when it fails; a consult or transfer inside a
run rides that run's action. A quota refusal ends the message: a refused
routed agent asks no mentioned agent, and a refused mentioned agent answers
with the gate's refusal while the agents after it are not asked.

As on the web chat, every agent with others answering after it reads who they
are on the message it answers (`mention_dispatch.turn_lineup`): the
routed agent gets the agents `_read_channel_mentions` kept as `agents_after`
(`_channel_mention_targets`, resolved like `_answer_channel_mentions` resolves
them - which does it again once the routed agent ran; a lookup error leaves
them out), each mentioned agent the ones still to answer. The run renders the
line-up without the agent answering (a transfer may hand the message to one
of them) and appends it to the message sent to the model, never to the stored
message nor to the system prompt. Told who answers after it, a routed agent
may leave the message to them: its `[NO_RESPONSE]` does not silence them.
They still answer, the outbound message holds only their sections, only their
replies are stored (`_finalize_message(store_reply=False)`), and the last one
is the turn's answer.

---

## 4. Thread Continuation (Smart Follow-Up)

When `only_mention` is enabled, the bot normally ignores messages without
a mention. Thread continuation extends this: once the bot has been
mentioned and responded in a specific thread, it keeps monitoring that
thread for follow-up messages — even without further @mentions.

### How it works

1. **Bot responds** → `_mark_thread_active(channel, thread_key)` sets a
   Redis key with 24h TTL.
2. **New message arrives** without @mention → router checks
   `_is_thread_active()` → if True, sets `is_thread_continuation=True`
   in metadata → message passes the only_mention gate.
3. **Agent prompt** receives "Thread Continuation Mode" instruction →
   defaults to RESPOND (not silent).

### Thread key per platform

| Platform  | Thread key                        | Supported |
|-----------|-----------------------------------|-----------|
| Slack     | `{channel_id}:{thread_ts}` (`channel_thread_id`) | Yes - true threads |
| Teams     | conversation_id with `;messageid=` suffix | Yes - channel threads |
| Teams     | group-chat conversation_id (`conversation_type == "groupChat"`) | Yes - chat-level |
| WhatsApp  | `group_id` (all group messages)   | Yes - group-level |
| Google Chat | thread resource name (`spaces/AAA/threads/BBB`, `metadata.thread_name`) | Yes - true threads |
| Telegram  | None (chat_id too broad)          | Not supported |
| Discord   | None (channel_id too broad)       | Not supported |

Teams personal chats, Teams channel root posts and WhatsApp DMs have no
key (`_get_thread_key()` returns `None`).

---

## 4b. Thread Mute (`/stop` Command)

Users can tell the bot to stop monitoring a specific thread by sending
`/stop`. This is useful when the bot was @mentioned in a thread but the
conversation has moved on and the bot's continued responses are unwanted.

### How it works

1. **User sends `/stop`** → `_handle_stop()` runs:
   - Deletes the `active_thread:{channel}:{thread_key}` Redis key
   - Sets a `muted_thread:{channel}:{thread_key}` Redis key (7-day TTL)
   - Returns a confirmation message

2. **New message arrives** in a muted thread → router checks
   `_is_thread_muted()` → if True, the message is silently suppressed.

3. **User @mentions the bot** in a muted thread → the mute is lifted
   (`_unmute_thread()` deletes the Redis key) and the message is
   processed normally. The thread becomes active again.

### Redis keys

| Key pattern | TTL | Set by | Cleared by |
|-------------|-----|--------|------------|
| `muted_thread:{channel}:{thread_key}` | 7 days | `/stop` command | @mention (unmute) or TTL expiry |

### Platform support

Thread mute works on any platform that supports thread keys (same as
thread continuation):

| Platform  | Supported | Notes |
|-----------|-----------|-------|
| Slack     | Yes       | Per-thread mute |
| Teams     | Yes       | Per-thread mute (standard + private/shared channels), per-chat mute in group chats |
| WhatsApp  | Yes       | Per-group mute (group_id as thread key) |
| Google Chat | Yes     | Per-thread mute (thread name as thread key) |
| Telegram  | No        | No thread-level tracking |
| Discord   | No        | No thread-level tracking |

Where there is no thread key, `/stop` answers with the `stop_threads_only`
notice.

### Teams private/shared channels

The muted-thread check also runs in the Graph path
(`_route_graph_channel_message` in `teams.py`, shared by notifications and
the catch-up read), so muting works for
private/shared channel threads that use Graph API subscriptions.

---

## 4c. One Conversation Across the Web and the Channels

A conversation keeps the channel it started on; it is also reachable from the
web and from a direct-message thread it was continued in. Rules in
`services/channel_continuation.py`, the endpoints in
`api/v1/chat_continuation.py`.

| Where | What happens |
|-------|--------------|
| Conversation lookup, direct message of an identified sender | `_find_bound_conversation()` first: a `ConversationChannelBinding` for `(channel, channel_thread_id)` made by THIS user, whose conversation they still own and have not archived. Else the usual `_find_existing_external_conversation()`. Anonymous senders and group messages never read bindings |
| Agent resolution | A bound conversation keeps its agent (or the General Assistant): the DM re-resolution of section 10 is skipped |
| Before history | A conversation the web also reaches (a Slack or Teams DM of its owner, or a bound thread) takes the `chat:processing` lock as a channel turn (`ChannelTurn`, one attempt). A redelivered platform message is dropped (`claim_channel_message`). A message that finds the turn taken, or messages already waiting, is queued (`take_turn_or_queue`, `chat:channel_queue:{id}`, at most `CHANNEL_QUEUE_MAX_MESSAGES` = 5, `CHANNEL_QUEUE_TTL_SECONDS` = 1 h) and acknowledged, its files stored first (`_store_queued_files`, marked `channel:<channel>:queued`; the queue keeps their ids: a platform URL can be a short-lived credential) and claimed when it runs (`_stored_channel_files`, `claim_queued_files`); every release of the lock (`chat_turn_guard._release_turn_inner`) starts `drain_channel_queue`, which takes the lock, moves the next message and the ones of the same thread behind it to the claimed list (`chat:channel_queue:{id}:claimed`, dropping one queued over the TTL), folds them into one inbound and hands it to its adapter (`run_queued_message`), where the router adopts the held lock (`ChannelTurn.adopt_queued`) and forgets the claim just before releasing it; a claim a dead process left, or one whose adapter raised before the router (`QueuedMessageUndeliverableError` when it cannot answer at all: no bot, no Teams service URL), is put back and started again, at most `QUEUED_MESSAGE_ATTEMPTS` = 2 times - unless the dead process's turn already stored it (its platform message id on the user message, `_stored_turn`): never stored twice, it is then dropped; a message dropped without an answer (started too often, queued over the TTL, stored with no reply) is told so on its thread in the sender's language (`_tell_dropped`, the adapter's `post_queued_notice`, the `channel_queued_dropped` notice); a queued message whose thread no longer continues the conversation it was queued for is not answered elsewhere (the `channel_queued_elsewhere` notice). A queued message waits in Redis in clear text - its text, the sender's email and platform ids, its file ids, never the raw payload or a file URL - for at most the queue's hour, like a steering inbox entry its ten minutes; nothing else keeps it. The files of a message that never runs (refused once stored, dropped, its conversation gone) or that its turn did not attach are deleted (`discard_queued_files`), and the hourly upload purge takes any still marked after `QUEUED_FILE_RETENTION`. The scheduler's sweep (`sweep_channel_queues`) restarts a queue, or a claim, whose lock holder died. A tool call left waiting for approval is retired as a web send retires it (`retire_pending_approval`), the reply prefixed with a notice. A DM on any other channel takes no lock |
| History | A Slack DM thread reply of such a conversation keeps its DB history (the web turns), the platform backfill only fills an empty history. A turn holding the lock replays the checkpointed tool work of a turn a shutdown cut (`turn_checkpoint.load_replay`) and checkpoints its own (`ChannelTurn.checkpoint`) |
| Cut by a platform update | The drain's cancellation reaches `_holds_channel_turn` while the turn holds the lock: `hand_on_cut_turn()` flushes the checkpoint, puts the message back at the head of the queue for the next process's sweep (at most `cut_turn_resume.MAX_AUTO_RESUMES` times per message, the web turn's cap; never after a stored reply), releases without hand-over, and posts nothing (a silent resume, #10772), or `channel_turn_cut` when it cannot resume. Any other cut turn in a DM or a mention gets `channel_turn_cut` from its adapter (`tell_cut_turn`). See dev-docs/chat-turn-resilience.md section 10 |
| Run | The conversation's saved session tools (`tool_overrides_json`, through `usable_saved_selection`: only one saved for the conversation's agent, less an id unbound since that its owner could not bind) plus the agent's defaults added since the save (an integration bound later, as the web's reopen adds them; a default turned off for the conversation stays off), the web Stop (`cancel_event`), the turn fanned out to the web (`TurnPublisher`) |
| `/new` | Also releases a bound thread (`_release_bound_thread`); the conversation is never archived |

Offering a continuation (`GET` / `POST .../channel-bindings`) asks the router
itself: `direct_message_refusal()` runs the gates of steps 0 and 0b
(`_bot_receives`, `_only_mention_gates`) and the route of a bound thread
(`_route_turn(probe=True)`, which decides the same way and logs at DEBUG) on
a direct-message probe, so the option is offered exactly where the replies
are answered. Both pass `_bot_receives`' answer on as `bot_whitelist_passed`:
a message off the whitelist, let in because some agent bypasses it, is for
an agent with `bypass_whitelist` only, never the bound agent without it nor
the General Assistant.

The web lists and continues only web conversations and Slack / Teams DM
threads (`web_listable_clause()`); it refuses a send or a file upload into a
Slack / Teams group thread (`web_continuation_refusal()`) and, while a channel
turn holds the lock, refuses a send rather than folds it (`web_send_refusal()`;
the reservation that closes the race is renewed until released, until the task
that took it ends - a send that failed on its way lets channel turns in at
once - and at most `TURN_ENTRY_RESERVATION_MAX_SECONDS`). The platform chat API, whose turn takes
no lock, refuses every conversation a Slack or Teams turn can reach
(`platform_chat_refusal()`). A Teams personal-chat conversation is not offered
"Continue in Teams" into that same chat (`started_there`).

---

## 5. Bot Mention Detection Per Platform

| Platform  | Detection method | `bot_was_mentioned` set by |
|-----------|-----------------|---------------------------|
| **Slack** | `<@BOT_ID>` regex in text (from `authorizations[0].user_id`) OR `app_mention` event type | Adapter (`slack.py`) |
| **Teams** | Bot Framework `entities` array: `mentioned.id == recipient.id`; **Graph fallback** for group chats / channels: with receive-all RSC permissions (`ChatMessage.Read.Chat` / `ChannelMessage.Read.Group`) Teams can strip the mention entity from the delivered activity ([msteams-docs#14422](https://github.com/MicrosoftDocs/msteams-docs/issues/14422)), so `_process_teams_message` re-reads the Graph copy of the message and sets the flag when `mentions[].mentioned.application.id` equals the bot app id | Adapter (`teams.py`) |
| **Telegram** | Message entities (`mention`, `text_mention`) matching bot username; `/cmd@botname` patterns. A caption's are its `caption_entities`; offsets count UTF-16 code units | Adapter (`telegram.py`) |
| **WhatsApp** | Not supported (Cloud API has no structured mentions) | Always False |
| **Discord** | discord.py `message.mentions` contains the bot user | Adapter (`discord.py`) |
| **Google Chat** | `USER_MENTION` annotations with `userMention.type == "BOT"` | Adapter (`google_chat.py`) |

### Mention stripping

All adapters strip the bot's own mention from the message text before
forwarding to the agent. Only the bot's mention is stripped — other
`@user` references are preserved.

| Platform  | What's stripped | What's preserved |
|-----------|----------------|-----------------|
| Slack     | `<@BOT_ID>` and `<@BOT_ID\|name>` | Everything else |
| Teams     | Bot's own mention entity text only | Other `@user` mentions |
| Telegram  | `@botname` mentions and `/cmd@botname` → `/cmd` | Other `@user` mentions |
| WhatsApp  | Nothing (no mention detection) | Full text |
| Discord   | `<@BOT_ID>` and `<@!BOT_ID>` | Other `@user` mentions |
| Google Chat | Bot mention (uses `argumentText`, which Google pre-strips) | Everything else |

---

## 6. Event Deduplication

### Slack (Redis-backed)

Slack fires both `message.channels` and `app_mention` events for the
same @mention when both subscriptions are active. Dedup uses Redis
`SET … NX EX` with a 60-second TTL, keyed by `slack:dedup:{channel_id}:{ts}`.

- Atomic across multiple uvicorn workers.
- Fail-open: if Redis is unavailable, the message is processed (better
  to double-process than silently drop).

### Discord (Redis-backed)

The gateway delivers each message once PER SESSION, and every platform
process opens its own session: each uvicorn worker (`WEB_WORKERS`, 2 by
default) and each replica. Without dedup a message would be answered once
per process. The first process to claim `discord:dedup:{message_id}`
(Redis `SET NX EX`, 10-minute TTL) answers it; fail-open like Slack.

### Teams / Telegram / WhatsApp / Google Chat

No application-level dedup. These platforms generally don't fire
duplicate events for the same message, and a webhook delivery reaches one
process only:
- **Teams**: Bot Framework delivers once (retries are rare when the
  webhook returns 200 quickly via `spawn_channel_task`).
- **Telegram**: Uses `update_id` internally; retries only on webhook
  failure (we return 200 immediately).
- **WhatsApp**: Meta Cloud API may retry on failure, but each message
  has a unique `msg_id` that could be used for dedup if needed.
- **Google Chat**: Webhook events are delivered once; the webhook
  returns an empty 200 immediately via `spawn_channel_task`.

---

## 7. Agent System Prompt — Response Modes

The `agent_chat.py` system prompt adapts based on message context:

### Mode 1: MUST RESPOND

**Triggers:** `bot_was_mentioned=True` OR `is_direct_message=True`

The user explicitly addressed the bot (via @mention or DM). The agent
MUST respond — `[NO_RESPONSE]` is forbidden.

### Mode 2: Silent Response

**Triggers:** Group/public channel, bot was NOT mentioned, NOT a DM

The agent MAY use `[NO_RESPONSE]` if the message is clearly not directed
at it (group chatter, off-topic, someone else handling it).

### Mode 3: Thread Continuation

**Triggers:** `is_thread_continuation=True` (added by router)

The agent was previously @mentioned in this thread and responded. New
messages (without @mention) are treated as follow-ups. Default behavior
is RESPOND — only use `[NO_RESPONSE]` when the message is clearly not
for the bot (side conversation between others, messages to a different
person by name).

### Priority

Thread Continuation overrides Silent Response. MUST RESPOND overrides
both.

```
MUST RESPOND  (DM or @mentioned)    → always respond, no [NO_RESPONSE]
  │
  ▼ (else)
Thread Continuation (active thread) → default respond, [NO_RESPONSE] rare
  │
  ▼ (else)
Silent Response (group, no mention) → [NO_RESPONSE] for off-topic chatter
```

---

## 8. Metadata Forwarded to Agent

The `_build_message_context` function in `router.py` builds a structured
context dict from the `InboundMessage`. The agent sees:

| Field | Example | Description |
|-------|---------|-------------|
| `platform` | `slack` | Which messaging platform |
| `channel_type` | `public_channel` | Normalized channel type |
| `is_direct_message` | `yes` / `no` | Whether it's a DM |
| `bot_was_mentioned` | `True` / `False` | Whether bot was @mentioned |
| `contact_name` | `John Doe` | WhatsApp / Teams / Slack contact name |
| `sender_name` | `John Doe` | Google Chat sender display name |
| `sender_email` | `john@corp.com` | Google Chat sender email |
| `first_name` | `John` | Telegram first name |
| `last_name` | `Doe` | Telegram last name |
| `username` | `johndoe` | Telegram / Discord username |

**Excluded from agent context** (internal routing / infrastructure):
`event_type`, `service_url`, `tenant_id`, `user_aad_id`, `reply_to_id`,
`is_threaded_reply`, `group_id`, `channel_bot_id`, `conversation_type`,
`chat_type`.

### Identity metadata per channel

Every adapter MUST populate at least one of the `PLATFORM_IDENTITY_KEYS`
(defined in `builtin_tools/prompting.py`) so anonymous users always have a display
name available to the agent:

| Channel      | `user_email` | Identity metadata keys |
|-------------|-------------|----------------------|
| **Slack**   | `users.info` API (`profile.email`) | `contact_name` (real_name / display_name) |
| **Teams**   | Bot Framework member API / Graph | `contact_name` (Teams display name) |
| **Google Chat** | Webhook payload (`sender.email`) | `sender_name`, `sender_email` |
| **WhatsApp**| — (not available) | `contact_name` (profile name) |
| **Telegram**| — (not available) | `username`, `first_name`, `last_name` |
| **Discord** | — (not available) | `username` (author display) |

---

## 8b. Centralised Identity Context (`build_identity_context`)

User identity injection into system prompts is handled by a **single
function** in `builtin_tools/prompting.py` — used by `agent_chat`, `general_chat`,
and `assignment_helpers`.

```
build_identity_context(user_record, context, is_anonymous, role_verb)
  │
  ├─ Always: date/time (UTC + local if timezone set)
  │
  ├─ user_record exists (authenticated)?
  │    → Full profile: name, email, title, timezone
  │    → Anti-impersonation guardrail (identity clause in build_security_policy, shared/external only)
  │
  ├─ is_anonymous + platform metadata available?
  │    → Platform-reported identity (contact_name, username, etc.)
  │    → Trust caveat (unverified, no elevated permissions)
  │
  └─ is_anonymous + no metadata?
       → Unidentified user notice
       → Minimal guardrail
```

**Resolution flow** (how a channel user becomes an XTM One user, `handle_message` step 1):

```
InboundMessage.user_email (from adapter)
  │
  ├─ Email present → query User by email → user_record loaded
  │    → Full profile available (name, title, timezone, etc.)
  │    → Agent sees: "Assisting John (john@co.com, timezone: Europe/Paris)"
  │    → `{channel}:{channel_user_id}` auto-linked in User.channel_identities
  │      (channel_user_id is the sender's own id - the Slack member id -
  │       never a channel or group id)
  │
  ├─ No email, or no user with it → resolve_user_by_channel_identity()
  │    (User.channel_identities holds `{channel}:{channel_user_id}`,
  │     auto-linked above or added by an admin:
  │     POST /api/v1/users/{id}/channel-identities)
  │    → If linked → user_record loaded (same as above)
  │    → If not linked → anonymous
  │
  └─ Anonymous → agent sees platform metadata (contact_name, etc.)
       → No timezone, no title, no email in context
```

---

## 9. Typing Indicator Lifecycle

```
Message arrives
  │
  ├─ show_typing=True?
  │    │
  │    ▼ YES
  │  typing_keepalive starts (sends typing every 3s)
  │    │
  │    ▼
  │  handle_message() runs
  │    │
  │    ├─ Response generated → typing stops → send reply
  │    │
  │    └─ Suppressed ([NO_RESPONSE] / only_mention gate)
  │         │
  │         ▼
  │       Slack: clear_typing_status() (clears "is thinking…")
  │       Teams/Telegram: auto-expires (3-5s)
  │
  └─ show_typing=False?
       │
       ▼
     handle_message() runs silently (no typing indicator)
       │
       └─ If suppressed → no visible effect to user ✅
```

---

## 10. Agent Resolution Order

```
_resolve_channel_agent(db, user_id, channel, inbound, channel_bot)
  │
  ├─ Pass 0: User DM override (private conversations only)
  │    User.channel_agent_config[channel] → specific agent for DMs
  │
  ├─ Pass 1: User-visible agents (own + group-shared + company-managed)
  │    Filter: enabled=True, channel_config[channel].receive_enabled=True
  │    Check: _agent_allows_channel() (receive whitelist + only_mention)
  │
  └─ Pass 2: ALL enabled agents (channel routing override)
       Same check as Pass 1, but ignores user visibility
       (admin-configured agents work for all users)
  │
  └─ No agent found → try General Assistant (if enabled for channel)
       ├─ GA linked to an agent every user can chat with (Settings >
       │  General Assistant > Link to Agent) → that agent answers, the
       │  conversation stays unbound (services/general_assistant_link.py
       │  turn_stand_in, asked with the conversation's channel and the
       │  sender); it is the turn's answering agent all the same:
       │  compaction, @mentions (it never answers its own mention twice),
       │  the reply's attribution and the Activity entry are its own. The
       │  web chat and the wake ask the same rule, so the conversation is
       │  answered by the same agent wherever it is continued; a
       │  web conversation continued here keeps the General Assistant, and
       │  so does a sender who may not chat with the linked agent
       └─ GA not enabled → return suppress=True
```

---

## 11. Per-Platform Adapter Checklist

Every adapter MUST implement:

| Requirement | Slack | Teams | Telegram | WhatsApp | Discord | Google Chat |
|------------|-------|-------|----------|----------|---------|-------------|
| Bot message filtering | Yes: `bot_id` / `subtype` | Yes: activity type | Yes (implicit) | Yes (implicit) | Yes: own user + system msgs | Yes: `sender.type == "BOT"` |
| Bot mention detection | Yes: regex + app_mention | Yes: entities array + Graph RSC fallback | Yes: entities + /cmd | No (not available) | Yes: `message.mentions` | Yes: `USER_MENTION` annotations |
| Mention stripping (bot only) | Yes: regex | Yes: bot entity only | Yes: entity-based | n/a | Yes: `<@id>` / `<@!id>` | Yes: via `argumentText` |
| Event deduplication | Yes: Redis SETNX | Not needed | Not needed | Not needed | Yes: Redis SETNX (one gateway session per process) | Not needed |
| Smart show_typing | Yes: DM/mention/thread | Yes: personal/mention/reply | Yes: private/mention | n/a (typing is no-op) | Yes: DM/mention | n/a (typing is no-op) |
| Clear typing on suppress | Yes: clear_typing_status | Auto-expires | Auto-expires | No-op | Auto-expires | No-op |
| INFO-level logging (router reasons at DEBUG, section 14) | Yes: every decision | Yes: every decision | Yes: every decision | Yes: every decision | Yes: every decision | Yes: every decision |
| Empty-after-strip protection | Yes | Yes | Yes | n/a | Yes | Yes (empty text ignored) |
| Threaded reply detection | Yes: `thread_ts != ts` | Yes: `reply_to_id` | No true threads | No threads | No (channel-level convs) | Thread name captured for reply threading |
| Error message safety | Yes: DM/mention only | Yes: personal/mention only | Yes: DM/mention only | Yes: DM only | Yes: DM/mention only | Yes: DM/mention only |
| Identity metadata | Yes: `contact_name` | Yes: `contact_name` | Yes: `username`, `first_name`, `last_name` | Yes: `contact_name` | Yes: `username` | Yes: `sender_name`, `sender_email` |
| User email resolution | Yes: `users.info` API | Yes: Bot Framework member API | No (not available) | No (not available) | No (not available) | Yes: webhook `sender.email` |
| Async processing | Yes: `spawn_channel_task` | Yes: `spawn_channel_task` | Yes: `spawn_channel_task` | Yes: `spawn_channel_task` | Yes: `spawn_channel_task` | Yes: `spawn_channel_task` |

---

## 12. Decision Matrix — Will the Bot Respond?

| Scenario | only_mention=OFF | only_mention=ON |
|----------|-----------------|-----------------|
| **DM / private chat** | ✅ RESPOND (must) | ✅ RESPOND (DM bypasses gate) |
| **Group + @mention** | ✅ RESPOND (must) | ✅ RESPOND (mention passes gate) |
| **Group + no mention + active thread** | ✅ RESPOND (thread cont.) | ✅ RESPOND (thread cont.) |
| **Group + no mention + no thread** | 🤖 Agent decides (Silent Response mode) | ❌ SUPPRESS (gate blocks) |
| **Group + no mention + bare greeting** | 🤖 Likely [NO_RESPONSE] | ❌ SUPPRESS (gate blocks) |
| **Any thread after /stop** | ❌ SUPPRESS (muted) | ❌ SUPPRESS (muted) |
| **Muted thread + @mention** | ✅ RESPOND (unmutes) | ✅ RESPOND (unmutes) |

---

## 13. When Agentic AI Is Unavailable

`handle_message` asks the background gate (`services/agentic_gate.agentic_verdict`) once per message, before anything else:

| Gate answer | What the sender gets |
|-------------|----------------------|
| `ai_disabled` (the administrator's AI switch) | "AI is disabled on this platform..." (the `ai_disabled` notice) on every message, before any routing rule is read. |
| `license_required` (Community Edition) | The chat gate's sentence (`channel_notices.chat_unavailable_notice()`, the words of `deps.chat_unavailable_detail()`), only where a model would have answered: every rule above runs first - receive whitelist, bot- and agent-level only-mention, muted thread, anonymous access, the routed agent's own channel rules and publish setting, the General Assistant's channels (`_route_turn`). Where the bot stays silent while licensed, it stays silent; where the router answers itself (a disabled agent in a DM), it says that. No conversation or message is stored (a prompt-injection detection is still audited, as when licensed), and no file download, backfill or compaction runs. |
| Unreadable (`transient`: the switch or the licence could not be read, or a database / Redis error or a timeout escaped the read) | Not a refusal, and nothing is processed (no routing, nothing stored, no agent run): a direct message (`_is_private_conversation()`) is told "The AI service is temporarily unavailable..." (the `ai_temporarily_unavailable` notice, the words of `deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL`), a group channel gets silence. Logged once per minute at most, without message content. |

---

## 13b. The Language of a Notice

Every sentence the channel layer sends of its own - the router's replies (a reset, `/stop`, an agent not available, an unidentified account, a processing error), the queue notices, the continuation opening, the adapters' error fallbacks, the Teams inactivity notice, the Google Chat and Discord acknowledgements, the mention notes, and the label of a link to an agent's file, deck or report with no name (`resolve_channel_file_markers` / `_resolve_channel_artifact_markers`, given the reader's `language`) - comes from `services/channel_notices.py`: `notice(key, notice_language(user))`, never a literal.

| Sender | Language |
|--------|----------|
| Identified (`user` resolved by email or channel identity) | `User.language`, when it is not `auto` |
| Identified with `auto`, anonymous, or answered before the sender is looked up (AI switched off, unreadable gate, `/stop`, the adapters' error fallbacks, the Teams inactivity notice, the Google Chat and Discord acknowledgements) | The platform default (`PlatformSettings.platform_language`, read from the branding snapshot) |
| Neither set | English |

A notice that a service shared with the web chat writes in English (a quota refusal, a prompt-guard refusal, a withheld answer, an approval the channel cannot collect, a timeout or overflow, the agent loop's terminal texts, the per-run budget's "the agent stopped because ..." and "continuing on a cheaper model" lines with the exhausted dimension they name, an assignment notification) is stored as written and translated on the outbound copy only (`localized_reply`, by template, the whole reply or the budget notice closing an answer the budget cut): the conversation, and every comparison made against it, keeps the English. A mentioned agent's note is written in the reader's language (`mention_dispatch`'s `language` argument, English for the web chat). `tests/test_channel_notices.py` fails on a channel module spelling a notice itself, on a notice missing a language or a placeholder, and on a service's English drifting from the catalog's.

---

## 14. What a Routing Outcome Logs

A bot in a busy channel sees every message; most are not for it. The level
says who has something to do:

| Outcome | Level |
|---------|-------|
| A group message not for the bot: not @mentioned under `only_mention`, outside an agent's `receive_channels` or the bot whitelist, no General Assistant for the channel, an accountless sender no agent serves | DEBUG, with every agent's reason (`_resolve_channel_agent`, `_resolve_channel_agent_anonymous`, `_route_turn`) |
| The router answers a person itself (a direct message told the assistant is unavailable or their account is unknown) | INFO, one line |
| A configuration that cannot work: a conversation bound to an agent that is deleted, disabled or does not receive on its channel at all (no channel entry, `receive_enabled=false`), an @mention in a group nobody can answer (no agent receives on the channel and the General Assistant is not enabled for it) | WARNING, naming the deciding setting |

The adapter still logs every suppressed reply at INFO, so the default level
shows that a message was dropped; DEBUG shows why. No routing log carries a
user email (the user id, or `has_email`, is logged instead) or a config dict.
Nor does any channel log at INFO or above carry the message text, a sender's
display name, username or phone number, or an attachment's file name or URL:
the adapters log the text length and platform ids, a bounded
`log_preview()` is allowed at DEBUG only, and a WhatsApp id - the sender's
number, which is also a direct message's thread and is encoded in every
`wamid.` message id, a group id, or a key the router builds from one - is
logged at every level through `log_id()` (`base.py`), a digest keyed from
`SECRET_KEY`, or from a random per-process key while `SECRET_KEY` is
guessable (short or a published placeholder).
