# Channel behaviour

How a channel message is gated and routed - agent and General Assistant routing, bot whitelists, private conversations, only-mention mode, thread continuation, `/stop` and anonymous access; the rules live in `backend/app/channels/AGENTS.md` and the full matrix in `backend/app/channels/ROUTING.md`.

## Channel Routing

New agents default to **web chat only**. External channels (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat) must be explicitly enabled per agent via `channel_config` in the Configuration tab. This lets admins control exactly which agents respond on which channels — e.g., internal corporate agents on Teams, community agents on Slack.

### Agent-Level Routing

The routing check happens in `router.py` via `_agent_allows_channel()`. If a message arrives on a channel that the bound agent doesn't allow, a direct message gets the `agent_not_on_channel` notice ("not available on this channel"); a group message is dropped silently.

For non-web channels, `_resolve_channel_agent()` finds the first enabled agent with `channel_config.<channel>.receive_enabled = True` that passes the full `_agent_allows_channel` check (including per-channel receive scoping). This runs before falling back to the General Assistant.

### General Assistant Routing

The General Assistant also supports channel configuration via `GeneralAssistantConfig.channel_config`, using the same structure as `Agent.channel_config`. When `channel_config` is set on the GA:
- Non-web channels are checked against it (`receive_enabled` must be `True`).
- Falls back to the legacy `available_channel_types` list when `channel_config` is not set.
- Web chat is always checked against `available_channel_types`.

## Channel Whitelists

Every channel bot (`slack_bot`, `teams_bot`, `whatsapp_bot`, `telegram_bot`, `discord_bot`, `googlechat_bot`) supports optional **channel whitelists** via `allowed_receive_channels` and `allowed_publish_channels` JSON fields on the `ChannelBot` model.

- When a whitelist is `null` or empty, **all channels are allowed** (default behavior).
- `allowed_receive_channels`: only process inbound messages from these channels.
- `allowed_publish_channels`: only allow sending/publishing to these channels. Enforced at runtime by `build_channel_tools()` — the effective publish scope is the intersection of the agent's `publish_channels` and the bot whitelist (agent selection empty → bot whitelist applies; Teams IDs are compared per-entry, exact first then by the normalized channel component). When the intersection is empty, the tool definitions stay loaded but every publish/write call fails closed with an explicit "Access denied" (channel-keyed writes via a never-matching sentinel whitelist, channel-less writes such as WhatsApp mark-read/react via the `publish_blocked` builder kwarg), while read tools keep working scoped to the agent's own channel selection (passed as the `read_channels` builder kwarg). Chat/DM tools (e.g. Teams personal and group chat messaging) are not governed by channel whitelists in any case. Agents with the admin-only `bypass_whitelist` flag skip the bot boundary entirely (mirrors the receive-side bypass).
- Format: a list of `{id, name, group}` entries (the channel picker's), matched on `id` — Slack channel IDs, Teams `{team_id}:{channel_id}`, WhatsApp phone numbers or group WA IDs, Telegram chat IDs, Discord channel IDs, Google Chat space names.
- The router checks the receive whitelist first, for every channel (step 0 of `handle_message`, `_bot_receives()`). Messages from non-whitelisted channels are silently ignored, unless an enabled agent has `bypass_whitelist` for the channel: the message then goes on, tagged `bot_whitelist_passed = False`, and only such an agent may answer it.
- **Teams ID normalization**: whitelist entries key teams by the Entra ID group GUID (from the Graph picker) while Bot Framework reports thread-style team IDs, so matching normalizes both sides to the globally-unique channel-id component (`teams_channel_component()` in `base.py`, shared by the receive matching in `router.py` and the publish intersection in `services/integration_tools.py`).
- **Teams group chats bypass receive whitelists** — their conversation IDs can never be enumerated by the channel picker, so they are gated by `allow_private` / `only_mention` / agent routing instead (like DMs).
- The whitelists are configured per-channel-bot via Settings > Channels in the UI.

## Private Conversations

When channel receive restrictions are configured, private/DM conversations can bypass them via the `allow_private` flag (default `true`).

**Bot-enforced resolution** (resolved in `router.py` via `_resolve_allow_private()`):
- Bot `allow_private = false` -> always false. The bot-level setting is a hard constraint the agent override **cannot** defeat.
- Bot `allow_private = true` -> the agent's per-channel override (`channel_config[channel].allow_private`) applies; `null` inherits the bot's `true`.
- No channel bot -> `true`.

The bot-level toggle is admin-only in Settings > Channels. See `ROUTING.md` for the full matrix.

**Detection** (via `_is_private_conversation()` in `router.py`):
- **Slack**: `metadata.channel_type == "im"` (direct message with the bot)
- **Teams**: `metadata.conversation_type == "personal"` (also bypassed via `_extract_channel_id()` returning None)
- **Telegram**: `metadata.chat_type == "private"` (1:1 chat with the bot)
- **WhatsApp**: `metadata.chat_type == "private"` (DM).  Group messages have `chat_type == "group"` and return `false`, so channel restrictions (group ID whitelists) apply to groups.
- **Discord**: `metadata.is_dm`
- **Google Chat**: `metadata.space_type == "DM"`

When `allow_private` is true:
- Private/DM conversations bypass both bot-level and agent-level receive channel whitelists.
- Users can always message the bot directly even when the bot is restricted to specific public channels.
- The toggle is available at channel bot, agent, assignment, and general assistant levels.

## Only-Mention Mode

When a bot is added to group channels, it responds to every message by default. The `only_mention` flag (default `false`) restricts the bot to only respond when explicitly @mentioned.

**Bot-enforced resolution** (resolved in `router.py` via `_resolve_only_mention()`):
- Bot `only_mention = true` -> always true. The bot-level setting is a hard constraint the agent override **cannot** disable.
- Bot `only_mention = false` -> the agent's per-channel override (`channel_config[channel].only_mention`) applies; `null` inherits the bot's `false`.
- No channel bot -> `false`.

The bot-level toggle is admin-only in Settings > Channels. See `ROUTING.md` for the full matrix.

**Mention detection** (via `_is_bot_mentioned()` in `router.py`, all reading the adapter-set `metadata.bot_was_mentioned` flag):
- **Teams**: set by the adapter from Bot Framework `entities` array (type `mention`)
- **Slack**: set by the adapter for both `app_mention` events and `<@BOT_ID>` detected in message text
- **Telegram**: set by the adapter from Telegram message entities (type `mention`/`text_mention`)
- **Discord**: set by the adapter from `message.mentions`
- **Google Chat**: set by the adapter from `USER_MENTION` annotations of type `BOT`
- **WhatsApp**: always `false` — WhatsApp groups support @mentions in message text, but the Cloud API does not provide structured mention entities.  If `only_mention` is needed, disable it per-channel or per-agent.

When `only_mention` is true:
- Messages in group channels without an explicit @mention of the bot are silently dropped (suppressed outbound).
- Private/DM conversations always bypass this check — users can always message the bot directly.
- The check runs at two levels: bot-level (early gate in `handle_message`) and agent-level (in `_agent_allows_channel`).
- The toggle is available at channel bot, agent, assignment, and general assistant levels.

## Thread Continuation Tracking

When `only_mention` is enabled, follow-up messages in a thread where the bot has already responded should continue to be processed — even without a new @mention. This avoids the awkward UX of requiring users to re-mention the bot in every reply within an active conversation.

**How it works** (implemented in `router.py`):

1. **`_get_thread_key(inbound)`** — derives a unique thread identifier from the inbound message:
   - **Slack**: uses `channel_thread_id` (already encodes `channel_id:thread_ts`)
   - **Teams channels**: the conversation ID with its `;messageid=<root>` suffix (thread-specific); bare channel IDs return `None` (too broad)
   - **Teams group chats**: the chat-scoped conversation ID (the whole chat is the "thread", mirroring WhatsApp groups)
   - **WhatsApp**: uses `group_id` for group chats (all group messages share the same key); DMs return `None`
   - **Google Chat**: uses the thread resource name (`spaces/AAA/threads/BBB`) from `metadata.thread_name`, when the message carries one
   - **Telegram / Discord**: return `None` (their thread IDs would cover the entire chat/channel)

2. **`_mark_thread_active(channel, thread_key)`** — sets a Redis key (`active_thread:{channel}:{thread_key}`) with a 24-hour TTL (refreshed on each bot response)

3. **`_is_thread_active(channel, thread_key)`** — checks if the thread was previously activated by a bot response

**Flow:**
- When the bot responds in a thread, `_mark_thread_active()` records it in Redis.
- On subsequent messages in that thread, if `only_mention` would normally suppress the message, `_is_thread_active()` checks whether the bot previously participated — if so, the message is processed as a continuation.
- The 24-hour TTL ensures stale threads eventually expire without manual cleanup.

## Thread Mute (`/stop` Command)

Users can mute the bot in a specific thread by sending `/stop`. This is useful when the bot was @mentioned in a thread but the conversation has moved on and the bot's continued responses are unwanted.

**How it works** (implemented in `router.py`):

1. **`/stop` command** — detected by `_is_stop_command()`, handled by `_handle_stop()`:
   - Calls `_mute_thread()` which deletes the `active_thread` Redis key and sets a `muted_thread:{channel}:{thread_key}` key with a 7-day TTL
   - Returns a confirmation message to the user

2. **Muted-thread gate** (step 0e in `handle_message`) — runs before user resolution:
   - Checks `_is_thread_muted()` for the current thread
   - If muted, silently suppresses the message (no response)
   - If the bot is explicitly @mentioned in a muted thread, `_unmute_thread()` lifts the mute and the message is processed normally

3. **Teams private/shared channels** — the muted-thread check also runs in `_route_graph_channel_message()` in `teams.py` (the Graph notification and catch-up path)

**Supported platforms:** every conversation `_get_thread_key()` names (same as thread continuation): Slack threads, Teams channel threads and group chats, WhatsApp groups, Google Chat threads. Where it names none (Telegram, Discord, a WhatsApp DM, a Teams personal chat or channel root post), `/stop` answers that it works in threads only (`stop_threads_only`).

## Anonymous Access

External channels often receive messages from users who don't have an XTM One account. The `allow_anonymous` feature lets agents process these messages without requiring user identification.

**Bot-enforced resolution** (resolved in `router.py` via `_resolve_allow_anonymous()`):
- Bot `allow_anonymous = false` -> always false. The bot-level setting is a hard constraint the agent override **cannot** defeat.
- Bot `allow_anonymous = true` -> the agent's per-channel override (`channel_config[channel].allow_anonymous`) applies; `null` inherits the bot's `true`.
- No channel bot -> `false`.

The bot-level toggle is admin-only in Settings > Channels. See `ROUTING.md` for the full matrix.

When anonymous access is enabled:
- Messages are processed normally; the channel user's metadata (name, phone, platform ID) is passed as `message_context` to the agent.
- Conversations are created with `user_id = NULL` and matched by `channel_thread_id` (via `_find_or_create_anonymous_conversation()`).
- All enabled agents with `allow_anonymous` and `receive_enabled` for the channel are eligible for anonymous routing (via `_resolve_channel_agent_anonymous()`). Company-managed agents are preferred (sorted first) but not required — the admin enabling `allow_anonymous` on the channel bot is treated as sufficient authorisation.
- The General Assistant is **not** available for anonymous users.
- Downstream code receives `effective_user_id = "anonymous"` so LLM model resolution and memory degrade gracefully to defaults.
- The turn counts against the platform-wide agentic limits. The quota gate runs inside the agent turn, so its refusal is answered exactly where the licence refusal is (`_TurnRoute.runs_model`: wherever a model would have answered); `handle_message` reads it from `run_agent_chat`'s `error_out` (code in `QUOTA_REFUSAL_ERROR_CODES`) and generates no conversation title for it.
