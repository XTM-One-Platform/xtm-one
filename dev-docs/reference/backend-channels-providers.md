# Channel providers: Slack and Teams setup

Platform-specific setup of the Slack bot (token scopes, event subscriptions) and of Teams private and shared channels (Graph subscriptions); the rules live in `backend/app/channels/AGENTS.md`.

## Slack Bot Token Scopes

The Slack channel bot requires these **Bot Token Scopes** (the list the Settings > Channels setup guide shows, `CHANNEL_BOT_CATALOG["slack_bot"]`):

| Scope | Why it's needed |
|-------|----------------|
| `app_mentions:read` | Receive `@mention` events in channels |
| `assistant:write` | Show typing/thinking indicator via `assistant.threads.setStatus` (optional — degrades gracefully if missing) |
| `channels:history` | Read messages in public channels the bot is added to |
| `channels:join` | Join public channels in the workspace |
| `channels:read` | View basic information about public channels |
| `chat:write` | Send messages as the bot |
| `groups:history` | Read messages in private channels the bot is added to |
| `groups:read` | View basic information about private channels the bot is added to |
| `im:history` | Read direct message conversation history |
| `im:read` | View basic DM information |
| `im:write` | Initiate direct messages (for proactive notifications) |
| `users:read` | View user profiles in the workspace |
| `users:read.email` | Resolve Slack users to XTM One accounts by email |

**Event subscriptions** (under "Event Subscriptions" → "Subscribe to bot events"):

| Event | What it does |
|-------|-------------|
| `message.im` | Messages in DMs with the bot (always needed) |
| `app_mention` | When someone @mentions the bot in a channel |
| `message.channels` | ALL messages in public channels the bot is in |

**Recommended subscriptions:**
- **Default**: `message.im` + `app_mention` — bot responds to DMs and @mentions in channels
- **Advanced**: `message.im` + `message.channels` — bot responds to ALL messages in channels it's in (useful for dedicated support channels)

**Warning**: Do NOT subscribe to both `message.channels` and `app_mention` together — @mentions in channels would fire both events, causing duplicate responses.

After subscribing, invite the bot to each channel where it should respond (`/invite @BotName`).

**Interactivity** (under "Interactivity & Shortcuts"): turn it on and set the Request URL to `{base_url}/api/channels/slack/interactions`. Decision cards (`services/slack_decision_cards.py`) are answered from their buttons and the "Answer with text" modal; the request is checked with the same signing secret as the events and fails closed without it.

## Teams Private/Shared Channel Support

Standard (public) Teams channels receive all messages via Bot Framework (RSC grant). Private and shared channels **don't** — Bot Framework only delivers @mentions.

When the bot is @mentioned in a private/shared channel thread:
1. Bot Framework delivers the @mention message (normal flow)
2. After responding, `_setup_private_channel_monitoring()` fires asynchronously
3. A Graph API subscription is created for the channel (auto-renewed before expiry)
4. Follow-up messages in the thread arrive via `POST /api/channels/teams/graph-notifications`
5. Messages where the bot is @mentioned are skipped (Bot Framework handles those — no duplicates)
6. Threads inactive for 7 days are cleaned up and a "stopped monitoring" notice is posted

This gives private channels the same "smart thread continuation" behavior that standard channels get natively via Bot Framework.

**Implementation details** (in `channels/teams.py`):
- Channel membership type is detected via Graph API and cached in Redis for 30 days
- Graph API subscriptions expire after ~2.94 days (`GRAPH_SUB_MAX_MINUTES`) and are auto-renewed by the autonomous scheduler, and on a `reauthorizationRequired` lifecycle notification
- Thread monitoring window: 7 days (`PRIV_THREAD_TTL`)
- Graph names a thread reply under its root (`messages('{root}')/replies('{reply}')`): `_parse_graph_resource` returns both, the reply is fetched from `/messages/{root}/replies/{reply}` and acted on only when Graph's own `replyToId` is that root - the thread always comes from Teams, never from the notification; a reply another bot or app posted is not answered, so two bots cannot answer each other
- Subscriptions with no active threads are automatically deleted
- Graph credentials (app-only token) are acquired from the same tenant credentials as the Bot Framework bot
