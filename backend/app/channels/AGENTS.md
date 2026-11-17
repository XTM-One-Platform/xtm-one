# Channels – Developer Guide

Slack, Teams, WhatsApp, Telegram, Discord, Google Chat and web chat: an adapter builds an `InboundMessage`, `router.py` runs the agent, the adapter sends the `OutboundMessage`.

## Where to look

| Need | Read |
|---|---|
| Platforms, credentials, key files, users, conversations, web chat | `dev-docs/reference/backend-channels-overview.md` |
| Routing, whitelists, private, mentions, threads, `/stop`, anonymous | `dev-docs/reference/backend-channels-behaviour.md` |
| Routing matrix, typing, logs, notices | `ROUTING.md` (this directory) |
| Slack scopes and events, Teams private/shared channels | `dev-docs/reference/backend-channels-providers.md` |
| Adapter patterns (dispatch, Teams auth and Graph, files, markers) | `dev-docs/reference/backend-channels-patterns.md` |
| Agent accounts (a user account, not a bot) | `dev-docs/reference/agent-accounts.md` |

## Rules

- Bot credentials live only in `channel_bots` (Fernet), read with `get_channel_bot()` + `reveal_field()` (vault references); never env vars or integrations.
- Channel types come from `ChannelType` and `CHANNEL_TO_BOT_PROVIDER` in `base.py`; never a hand-written list.
- `router.handle_message()` is the single entry point of every bot channel; agent accounts run their own pipeline (`services/agent_accounts/`).
- A webhook answers 200 at once and dispatches via `spawn_channel_task()`, never a bare `asyncio.create_task`; platform file lookups run in that task.
- No log line at INFO or above carries message text, a sender's name, email or number, or an attachment's name or URL: use `log_id()` and `log_preview()` (DEBUG only). Telegram Bot API URLs carry the token: never log one.
- New agents are web-only: a channel answers only if `channel_config.<channel>.receive_enabled` (`_agent_allows_channel()`); the General Assistant uses its own `channel_config` (fallback `available_channel_types`).
- Bot-level `allow_private = false`, `only_mention = true` and `allow_anonymous = false` are hard limits no agent override defeats; resolve them only with `_resolve_allow_private()`, `_resolve_only_mention()`, `_resolve_allow_anonymous()`.
- Receive whitelists: null or empty allows all; the router (`_bot_receives()`) drops the rest unless an agent has `bypass_whitelist`; DMs (with `allow_private`) and Teams group chats bypass them.
- Publish scope is the agent's `publish_channels` intersected with the bot whitelist (`build_channel_tools()`); an empty intersection fails closed on writes, reads stay scoped; only admin-set `bypass_whitelist` skips it. Teams ids compare through `teams_channel_component()`.
- An adapter sets `metadata.bot_was_mentioned`; `_is_bot_mentioned()` reads only that flag. Private conversations always bypass `only_mention`.
- Thread state is Redis keyed by `_get_thread_key()` (`active_thread:*` 24 h, `muted_thread:*` 7 d); the mute gate runs before user resolution and in Teams' `_route_graph_channel_message()`.
- Users resolve by email, then by a linked channel identity; an unresolved sender gets "please register" unless anonymous applies.
- Anonymous: `user_id = NULL` conversations matched by `channel_thread_id`, `effective_user_id = "anonymous"`, never the General Assistant; it still counts against the agentic quota.
- Web continuation binds only an identified user's DM thread (`_find_bound_conversation()`, owner-scoped); a group thread is never bound nor continued on the web.
- Teams: `/api/channels/teams/messages` validates the Bot Framework JWT (`_verify_botframework_jwt()`); an unreadable credential fails closed.
- Graph notifications: one verdict `_notification_verdict()`, one switch `verification_required()`; a refused one is never trusted, a message processed once (`_first_delivery`), a possible gap is caught up (`_catch_up_channel`), subscription work holds the channel lock.
- Graph notification POSTs answer 2xx (refusals too), 503 only when Redis is unreadable; bodies bounded by `read_bounded_body`.
- `TeamsChannel.send_message()` returns `""` on 202: gate success on `is not None`, never truthiness.
- A Teams activity never carries both `text` and `attachments`: images go as separate hero cards.
- Inbound files go through `_download_channel_file()`, bounded by `MAX_UPLOAD_SIZE_BYTES`; never `resp.content` before the size check. A file-only message gets `FILE_ONLY_PLACEHOLDER`.
- Outbound: split with `chunk_message()` (`CHANNEL_MAX_CHARS`); Slack text via `markdown_to_slack()`; wrap `handle_message()` in `typing_keepalive()`.
- No marker reaches a channel raw: `[[FILE:id]]`, `[[PRESENTATION:id]]`, `[[REPORT:id]]`, `[[VIDEO:id]]` and image addresses become links only via `resolve_channel_*` (`base.py`), scoped to the conversation and user (no scope signs nothing); `[[INVESTIGATION:id]]`, `[[WORK_ITEM:id]]` are stripped (`strip_channel_*_markers()`).
- Bare platform hosts and WhatsApp links: `services/outbound_links.py` (patterns doc).
- On a processing error, tell the user (DMs, @mentions), never silence after a typing indicator.
- Slack: never subscribe to both `message.channels` and `app_mention` (double replies).

## Adding a new channel

1. Adapter `<name>.py`: a `BaseChannel` subclass (`send_message`, `send_typing_indicator`), a webhook route that reads credentials with `get_channel_bot("<name>_bot")` + `reveal_field()`, builds an `InboundMessage` and calls `handle_message()` via `spawn_channel_task()`.
2. Register the router in `backend/app/main.py` under `/api/channels`.
3. Add the `<name>_bot` entry to `CHANNEL_BOT_CATALOG` (`api/v1/channel_bots.py`).
4. Add the `ChannelType` value and its `CHANNEL_TO_BOT_PROVIDER` key in `base.py`. No env vars: admins create the bot in Settings > Integrations > Channels & Bots. Then the per-platform checklist, `ROUTING.md` section 11.

## Tests

`tests/test_channel_*.py` (routing, webhook security, file download, image scope, continuation; `test_channel_task_bounding.py` is the no-bare-dispatch tripwire) and `test_teams_graph_client_state.py`.
