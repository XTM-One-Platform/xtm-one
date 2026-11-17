# Channels overview

The channel layer as a whole - platforms, how a channel reads its bot credentials, the message flow, the key files, users, conversations, web chat and how to add a channel; the rules live in `backend/app/channels/AGENTS.md`.

## What are Channels?

Channels are the communication layer between users and XTM One. They allow users to chat with their agents from multiple platforms:

| Channel    | Transport           | Credentials                    |
|------------|--------------------|---------------------------------|
| **web**    | HTTP + SSE, REST    | Built-in (always on)           |
| **slack**  | Events API webhook  | `slack_bot` channel bot in DB  |
| **teams**  | Bot Framework       | `teams_bot` channel bot in DB  |
| **whatsapp** | Meta Cloud API    | `whatsapp_bot` channel bot in DB |
| **telegram** | Telegram Bot API  | `telegram_bot` channel bot in DB |
| **discord** | Discord Gateway (WebSocket, `discord.py`) | `discord_bot` channel bot in DB |
| **google_chat** | Google Chat HTTP endpoint | `googlechat_bot` channel bot in DB |

**All bot credentials are stored in the `channel_bots` table (separate from integrations), encrypted with Fernet.** Users manage channel bots via Settings > Channels in the UI, or via `/api/v1/channel-bots`.

## How Channels Read Credentials

Each channel adapter queries the `channel_bots` table at runtime for the first connected bot of its provider type (preferring company-managed bots):

- **Slack**: Finds `slack_bot` channel bot → reads `api_key_enc` (bot token) and `extra_config_enc.signing_secret`
- **Teams**: Finds `teams_bot` channel bot → reads `client_id_enc` (app ID), `client_secret_enc` (app password) and `extra_config_enc.tenant_id`
- **WhatsApp**: Finds `whatsapp_bot` channel bot → reads `api_key_enc` (access token) and `extra_config_enc.phone_number_id`, `extra_config_enc.verify_token`, `extra_config_enc.app_secret`
- **Telegram**: Finds `telegram_bot` channel bot → reads `api_key_enc` (bot token) and `extra_config_enc.bot_username`, `extra_config_enc.webhook_secret_token`
- **Discord**: Finds `discord_bot` channel bot → reads `api_key_enc` (bot token) and `extra_config_enc.public_key`
- **Google Chat**: Finds `googlechat_bot` channel bot → reads `api_key_enc` (service account JSON key) and `extra_config_enc.project_number`

Secret fields go through `reveal_field()` (they may be vault references); an extra config that cannot be decrypted yields `DECRYPT_FAILED_CREDENTIAL`, so webhook verification fails closed.

## Architecture

```
User (any platform)
    ↓
Channel Adapter (normalize to InboundMessage)
    ↓
Message Router (resolve user, find/create conversation, run LLM)
    ↓
LLM (agent persona if conversation is bound to an agent)
    ↓
OutboundMessage
    ↓
Channel Adapter (format + send via platform API)
```

## Key Files

| File                | Purpose                                   |
|---------------------|-------------------------------------------|
| `base.py`           | `BaseChannel`, `InboundMessage`, `OutboundMessage`, `ChannelType`, `CHANNEL_TO_BOT_PROVIDER` (mapping from channel type to bot provider key), `get_channel_bot()`, `typing_keepalive()`, `spawn_channel_task()` (bounded + tracked inbound-message dispatch — drop-in for `asyncio.create_task` at webhook entry points), `log_id()` (the form a channel id takes in a log line: a WhatsApp id - a phone number, a `wamid.` that encodes one, a group, a key built from one - as a keyed digest, any other id unchanged) and `log_preview()` (bounded message text, DEBUG lines only) — no channel log line at INFO or above carries message text, a sender's name, email or number, or an attachment's name or URL (`ROUTING.md` section 14) |
| `router.py`         | Message routing: user resolution, conversation management, agent resolution, LLM execution, thread continuation tracking. Key helpers: `_find_existing_external_conversation()`, `_store_user_message()`, `_finalize_message()` (shared post-processing) |
| `slack.py`          | Slack adapter + `/slack/events` endpoint (with thread fallback) |
| `teams.py`          | Teams adapter: `/teams/messages` (Bot Framework JWT-validated via `_verify_botframework_jwt`), `/teams/graph-notifications` (private/shared channel Graph subscriptions, their renewal and catch-up), `/teams/tab` (the app's personal tab page) |
| `teams_graph_verdict.py` | The pure (no I/O) verdict on a Graph notification and the decision on a whole notification POST (`summarize_post`, `decide_post`) |
| `teams_graph_worker.py` | The worker process that decides large Graph notification POSTs off the event loop |
| `whatsapp.py`       | WhatsApp adapter + `/whatsapp/webhook` (GET verification, POST messages; DM + group support) |
| `telegram.py`       | Telegram Bot API adapter + `/telegram/webhook` |
| `discord.py`        | Discord adapter: a Gateway connection per process (`start_discord_gateway()`, started by `main.py`) + the optional `/discord/interactions` endpoint |
| `google_chat.py`    | Google Chat adapter + `/googlechat/webhook` |
| `__init__.py`       | Re-exports `BaseChannel`, `ChannelType`, `InboundMessage`, `OutboundMessage`, `get_channel_bot` |
| `AGENTS.md`         | The channel rules |
| `ROUTING.md`        | Routing reference: gates, thread keys, mention detection, per-platform checklist, decision matrix, routing logs |

## Agent Accounts (not bots)

Everything in this file is a bot: an application with its own identity, hearing what is addressed to the bot. An **agent account** is an agent operating a real user account through that account's delegated integration - answering when a person is mentioned or messaged, running an AI user that follows whole channels, or triaging a mailbox. It does not go through `handle_message()`: its own pipeline, rule engine and provider interface live in `services/agent_accounts/`, its contract in `dev-docs/reference/agent-accounts.md`. It reuses this layer's `spawn_channel_task()` and `NO_RESPONSE_MARKER`, and `agent_chat`'s message-context handling (`response_policy`, `account_brief`).

## User Resolution

`handle_message` resolves the sender by email first, then by a linked channel identity:

1. **Email** (`InboundMessage.user_email`, `resolve_user_by_email()`):
   - Slack: the `users.info` API (requires `users:read` + `users:read.email`)
   - Teams: `from.email` of the activity, else the Bot Framework member API by `aadObjectId`; on the Graph path, the Graph sender, else a Graph user lookup
   - Google Chat: `sender.email` of the event
   - WhatsApp, Telegram, Discord: no email
2. **Channel identity** (`resolve_user_by_channel_identity()`), when there is no email or no user with it: `User.channel_identities` holds `{channel}:{channel_user_id}` keys (the Slack member id, the WhatsApp number, the Telegram or Discord user id, the Google Chat `users/...` name, ...). An adapter's `channel_user_id` is always the sender's own id, never a channel or group id, or every member of a channel would resolve as the first one linked; a Slack reply targets the channel of `channel_thread_id`. A key is added by an admin (`POST /api/v1/users/{id}/channel-identities`) or linked automatically when a sender resolves by email (`link_channel_identity()`).

If the user can't be resolved, the channel responds with a "please register" message, unless anonymous access applies (`dev-docs/reference/backend-channels-behaviour.md`).

## Conversation Model

- `Conversation`: Groups messages, tracks `channel`, `channel_thread_id`, optional `agent_id`
- `Message`: Individual message with `role`, `content`, `channel`, `sequence`
- Platform threads (Slack thread_ts, Teams conversation ID) map to `channel_thread_id`
- The router uses `channel_thread_id` to continue existing threads
- If a conversation is bound to an agent, the agent's persona is used as the system prompt

## Web Chat

The web chat (`api/v1/chat.py`, under `/api/v1/chat`, and `api/v1/chat_web_stream.py`, under `/api/v1/web`) does not go through the channel router. It has two modes:
1. **REST API** (`POST /api/v1/chat/conversations/{id}/messages`)
2. **HTTP + SSE** (`POST /api/v1/web/chat/messages`): the send streams its turn back as SSE `data:` frames; Stop, "Continue now", steering and approvals are POSTs beside it, and `GET /api/v1/web/chat/conversations/{id}/events` follows the conversation from another tab or after a reload

The frames a turn sends (`thinking`, `status` events, `stream`, `done`, `error`, ...) are documented in `dev-docs/reference/backend-tools-streaming-events.md` and, for the turn fan-out and resume protocol, `dev-docs/chat-turn-fanout.md`.

## Adding a New Channel

The checklist is in `backend/app/channels/AGENTS.md` ("Adding a new channel"), then `ROUTING.md` section 11. A sketch for a hypothetical `example` platform:

### Step 1: Create the adapter

```python
# backend/app/channels/example.py

from app.channels.base import BaseChannel, ChannelType, InboundMessage, OutboundMessage, get_channel_bot, spawn_channel_task
from app.channels.router import handle_message
from app.services.vault import reveal_field
from fastapi import APIRouter, Request

router = APIRouter()

class ExampleChannel(BaseChannel):
    channel_type = ChannelType.EXAMPLE  # add to the ChannelType enum

    async def send_message(self, message: OutboundMessage) -> str | None:
        pass

    async def send_typing_indicator(self, channel_thread_id=None) -> None:
        pass

async def _get_example_creds():
    """Load the bot's credentials from the DB using the shared helper."""
    bot = await get_channel_bot("example_bot")
    if not bot:
        return None
    # The token may be a vault reference: reveal_field resolves it.
    return await reveal_field(bot, "api_key_enc")

@router.post("/example/webhook")
async def example_webhook(request: Request):
    creds = await _get_example_creds()
    # Verify the request, parse the payload → InboundMessage, then
    # spawn_channel_task(...) a coroutine that calls handle_message() and sends the reply;
    # answer 200 at once.
    pass
```

### Step 2: Register in main.py

```python
from app.channels import example as example_channel
application.include_router(example_channel.router, prefix="/api/channels", tags=["channels"])
```

### Step 3: Add to channel bot catalog

In `backend/app/api/v1/channel_bots.py`, add to `CHANNEL_BOT_CATALOG` (the existing entries also carry `extra_fields` and a `setup_guide`):

```python
"example_bot": {
    "name": "Example",
    "fields": ["api_key"],
    "description": "Receive and reply to Example messages.",
    "webhook_path": "/api/channels/example/webhook",
},
```

### Step 4: Add the `ChannelType` value and its `CHANNEL_TO_BOT_PROVIDER` key in `base.py`

**No env vars needed!** Admins create an `example_bot` channel bot in the UI (Settings > Channels).
