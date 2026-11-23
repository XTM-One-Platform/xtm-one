# Chat API

The web app, the [Office add-in](office-add-in.md) and your own scripts talk to a conversation over HTTP: a message is sent with a `POST` whose response streams the request back as server-sent events, and a second stream follows everything else that happens in the conversation. This page describes those routes, under `/api/v1/web/chat`: how to open a conversation, send, what you receive while an agent works, how to answer an approval, how to tell your own requests from the ones sent elsewhere, and what you can count on when a request ends.

The REST routes under `/api/v1/chat/conversations` serve the same conversations without a stream; rate an answer there (`POST /api/v1/chat/conversations/<conversation_id>/messages/<message_id>/feedback`).

## Authenticate

Send a session token (the `access_token` that `POST /api/v1/auth/login` returns) on every request, as `Authorization: Bearer <access_token>`; an API key or a platform token is refused (`403`) on every route under `/api/v1/web`. The routes act on the caller's own conversations; one shared with the caller can be [opened and paged](#open-a-conversation), read-only. A session that ended answers `401` with `detail.code` `session_expired`. A stream still open when its session ends sends `{"type": "session_expired", "reason": "lifetime"}` (or `"idle"`) and ends; the web app then signs out. On the mobile app, a stream still open when the app locks behind its PIN sends `{"type": "app_locked"}` and ends; the app shows its lock screen, and a request still running is stored.

Each `data:` line of a stream carries one JSON frame. Once its response has started, a stream writes a `: keepalive` comment line at most 15 seconds after the last thing it wrote: skip it. A send's response writes none before it starts (see [below](#send-a-message)).

Name your client with an `X-Chat-Client-Id` header (up to 128 characters) on every route that sends or acts on a request, and pass the same value as `client_id` when you [follow the conversation](#requests-you-did-not-send): the frames of your own sends are then never repeated there.

### Refusals

A refusal is an HTTP error whose body carries a readable `detail`; where a client may need to branch on it, a `code` sits beside it:

| Status | Body | When |
|---|---|---|
| `403` | `{"detail": "...", "code": "terms_required"}` | The platform's Terms of Use wait for acceptance. Every route. |
| `403` | `{"detail": "...", "code": "chat_unavailable"}` | No licence covers chat. Only the routes that start or feed a request: send, steer, approve, start a conversation (`POST /sessions` without one to restore) and upload. Restoring a conversation stays open. |
| `403` | `{"detail": {"code": "ai_disabled", "message": "..."}}` | An administrator switched AI off. The same routes as `chat_unavailable`. |

Everything else keeps working without chat: listing, reading, paging, renaming and deleting conversations, the events stream, the pending approvals, Stop and Continue now.

## Open a conversation

`POST /api/v1/web/chat/sessions` with `{"agent_slug": "<agent slug>"}` (or `agent_id`, and optionally `workspace_id`) creates a conversation and answers its `conversation_id`; without an agent it opens the General Assistant. With `{"conversation_id": "<conversation_id>"}` it restores that conversation instead (a `conversation_id` the caller cannot read starts a new one). A restore answers:

| Field | Meaning |
|---|---|
| `messages` | The latest messages, oldest first. `has_more` is `true` when older ones exist. Both count only the messages a conversation shows: the platform's own hidden messages are left out. |
| `agent_id`, `agent_name`, `workspace_id` | The conversation's agent and workspace. A conversation without an agent (`agent_id` `null`) is answered by the agent standing in for the General Assistant, else by the General Assistant, and `agent_name` names that one. |
| `is_processing` | A request is running. |
| `pending_approvals` | The tool calls a running request waits for you to [approve](#approve-a-tool-call), or `null`. |
| `interrupted_turn` | The last request stopped before its answer (`started_at`, `interrupted_at`), or `null` ([below](#how-a-request-ends)). |

The other conversation routes:

| Route | Effect |
|---|---|
| `GET /api/v1/web/chat/sessions` | The caller's conversations, continued Slack and Teams threads included, as `{"conversations": [...]}`: `limit` (1 to 200, default 50), `offset`, `workspace_id`, and `scope=shared` for the ones shared with the caller. |
| `GET /api/v1/web/chat/sessions/<conversation_id>/messages?before=<message_id>&limit=50` | The messages older than `before` (`limit` up to 500), as `{"messages": [...], "has_more": ...}`, counted as a restore counts them. |
| `PATCH /api/v1/web/chat/sessions/<conversation_id>` | `{"title": "..."}` renames the conversation, `{"workspace_id": "..."}` files it into a workspace. |
| `DELETE /api/v1/web/chat/sessions/<conversation_id>` | Deletes (archives) the conversation. `204`. |
| `DELETE /api/v1/web/chat/sessions` | Deletes (archives) all the conversations `GET /api/v1/web/chat/sessions` lists for the caller. A conversation from another channel that list leaves out (an OpenCTI, OpenAEV or OpenCRQ chat, a Slack or Teams group thread) is kept. `204`. |
| `POST /api/v1/web/chat/upload` | Multipart `conversation_id` and `file`: stores the file for the next message and answers `{"file_ids": ["<file id>"]}`. Pass them as `file_ids` when you send or steer. |

The assistant OpenCTI, OpenAEV and OpenCRQ embed lists its conversations with `GET /api/v1/platform/chat/sessions`: the caller's own, each with its `title`, `updated_at`, `message_count`, `agent_id` and `agent_name`, `workspace_id`, `last_message_preview` (its last message, as the web chat's list writes it) and `channel`. `PATCH /api/v1/platform/chat/sessions/<conversation_id>` renames one (`{"title": "..."}`) or files it (`{"workspace_id": "..."}`, `null` takes it out of its workspace) and answers `{"conversation_id", "title", "workspace_id"}`; another person's conversation, or one from another channel, answers `404`.

## Send a message

```bash
curl -N https://<your-xtm-one>/api/v1/web/chat/messages \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -H "X-Chat-Client-Id: my-script" \
  -d '{"conversation_id": "<conversation_id>", "content": "Summarize the last incident report"}'
```

Besides `conversation_id` and `content`, the body takes `file_ids` ([uploaded](#open-a-conversation) files), `mentioned_agent_ids` and `referenced_conversation_ids` (below), `"supports_tool_approval": true` when your client [answers approvals](#approve-a-tool-call), and `"supports_reply_suggestions": true` when it offers the user a [suggested next message](#how-a-request-ends).

A send refused before its request starts is answered with an HTTP error, `{"detail": "<why>"}`, instead of a stream:

| Status | Why |
|---|---|
| `400` | The message is empty, or it adds a tool, knowledge base or integration the caller may not use. |
| `401` | The account may no longer chat (deactivated, for instance). |
| `404` | No such conversation of the caller's. |
| `409` | The conversation does not take a message from here: a group channel thread, or a request a channel message runs. |
| `422` | The Deep Investigation choices could not be read. |
| `429` | Too many messages: `Retry-After` says when to send again. `503` instead while the rate limiter cannot be reached on a platform set to fail closed. |

The response's status and headers therefore arrive with its first frame past `thinking`, and nothing, not even a `: keepalive`, is written before them, however long that takes. Once the request has started, a failure is an `error` frame.

The response streams the request's frames, all JSON objects with a `type`, and ends once the request (and the conversation's new title, if any) is delivered. Closing it early, or not reading it for about 10 seconds (the platform then ends it), does not stop the request: it runs on and stores its answer.

| Frame | Meaning |
|---|---|
| `thinking` | Your message was received. It is sent before the request starts running. |
| `turn_started` | Your message started a request of its own; `turn_id` identifies it. A message folded into a running request gets `folded_into_active_turn` instead ([below](#messages-folded-into-a-running-request)). |
| `status` | Progress: `status` says which step (`preparing`, `tool_start`, `thinking_text`, `streaming`...). |
| `stream` | A piece of the answer as it is written (`content`). |
| `done` | The answer: `content` and `message_id`, the stored assistant message. |
| `conversation_title` | The conversation was given a title (`title`). |
| `approval_required` | The request waits for you to [approve](#approve-a-tool-call) tool calls (`proposals`). `approval_resolved` says the wait is over. |
| `error` | The request ended without an answer (`content` says why). |
| `cancelled` | The request was stopped. |

A `done` with `"intermediate": true` is part of the answer that was already delivered while the agent keeps working: the request is not over.

An `error` or `cancelled` with `"provisional": true` (the request was stopped, or the agent failed) ends the request, but its stored end still follows on the same stream: a `done` or `cancelled` with `message_id`, the stored reply, or an `error` when the reply could not be stored. Any other `error` or `cancelled` is the request's last frame.

The other actions on a running request are plain `POST`s with the same headers, each taking `{"conversation_id": "<conversation_id>"}`:

| Route | Effect |
|---|---|
| `/api/v1/web/chat/messages/cancel` | Stops the request and the background work it started. `202` with `{"status": "cancelled", "turn": ...}`. |
| `/api/v1/web/chat/messages/skip-wait` | Continue now: ends the pause the agent announced (a `status` frame `wait_start`) and the request carries on. `202` with `{"status": "wait_skipped", "turn": ...}`. |
| `/api/v1/web/chat/messages/steer` | Also takes `content` (and `file_ids`): stores the message and hands it to the running request, which reads it at its next step. `{"status": "steered", "turn_id": ...}`. `409` when no request takes it - none is running, it was stopped, or it ended while the message was on its way (nothing is stored then, and the message does not count against the rate limit): send the message as a new request. `400` for an empty message, `429` with `Retry-After`. `{"status": "stored", "turn_id": null}` when the message is stored but whether the request took it is unknown, and `{"status": "stored", "turn_id": null, "answered": false}` when no request took it but it could not be taken back: either way it stays in the conversation and shapes the next request, so do not send it again (with `answered: false` no reply answers it). `503` when nothing was stored: try again. |

`turn` says where the request was reached: `local` or `remote` (another server of the platform), or `none` when nothing was running. A Stop that adds `"awaiting_send": true` to its body, sent while a request of your client (`X-Chat-Client-Id`) is on its way and has not started, is kept for about 30 seconds when answered `none`: that request ends `cancelled` as soon as it starts. Without it, a Stop answered `none` keeps nothing. Stop and Continue now answer `503` when the request runs on another server and the platform's message broker cannot be reached: nothing reached it, so try again.

## Approve a tool call

An agent set to ask before running some tools pauses on them and sends `approval_required`, whose `proposals` each name a `tool_call_id`, when its send declared `"supports_tool_approval": true`. Answer with:

```bash
curl https://<your-xtm-one>/api/v1/web/chat/messages/approve \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -H "X-Chat-Client-Id: my-script" \
  -d '{"conversation_id": "<conversation_id>", "decisions": [{"tool_call_id": "<tool_call_id>", "decision": "approve"}]}'
```

`decision` is `approve`, `approve_always` or `reject`, with an optional `rejection_reason`. The answer is `{"status": "accepted", "decided": <count>}`; `400` without any decision, `404` for an unknown conversation, `409` when nothing waits for an approval.

A client that reloads reads what is still waiting with `GET /api/v1/web/chat/conversations/<conversation_id>/pending-approvals`: `{"proposals": [...], "turn": "running" | "idle"}`. It answers `503` when the platform's message broker cannot say, rather than an `idle` that would drop a prompt the request still waits on.

## Messages that mention several agents

To have several agents answer one message ([@mentions](chat.md#ask-several-agents-at-once-with-mentions)), add their ids to the message, in the order they should answer:

```json
{"content": "@mail-desk @deal-desk what do you think?", "mentioned_agent_ids": ["<agent id>", "<agent id>"]}
```

The conversation's agent answers first, then each mentioned agent, all in the same request: every frame carries the same `turn_id`. Each reply but the last arrives as a `done` with `"intermediate": true`, and a `status` frame with `"status": "mention_answering"` (`agent_name`, `position`, `total`) announces the next agent. The last reply is the request's final `done`. On such a request every reply carries `answered_by` (`agent_id`, `agent_name`, and `agent_icon` when the agent has one) naming the agent that wrote it - the conversation's agent included, except the General Assistant, whose replies carry none - and a note written in place of an agent that did not answer carries `"mention_failed": true`. Over REST, the replies after the first are in `mention_messages`.

A request you sent can become such a request without you mentioning anyone: a message another place folds into it (below) may mention agents, and they answer inside your request, after its agent. To tell your agent's reply from theirs, compare `answered_by.agent_id` with the conversation's agent: voice mode speaks only its agent's reply and shows the others as text with their name, and the Office add-in, like the web app, labels every reply with the agent that wrote it.

## Messages that reference other conversations

To have the agent read other conversations ([references with @](chat.md#point-the-agent-at-another-conversation-with)), add their ids to the message, in the streamed send and in the REST body alike:

```json
{"content": "@q3-threat-report what did we decide there?", "referenced_conversation_ids": ["<conversation id>"]}
```

`GET /api/v1/chat/conversation-references` lists the conversations the caller can reference, as the composer's list does: every word of `q`, separated by spaces, hyphens or underscores (`q3-threat` like `q3 threat`), must appear in the title; `limit` (1 to 20, default 8) bounds the list and `exclude` leaves one conversation out. Each entry has the conversation's `id`, `title`, `key` (what the composer inserts after `@`), `updated_at`, `is_own` and, on a conversation shared with the caller, `owner_name`.

Only the conversations the sender can open are kept, at most 5, and never the conversation the message is sent in. The stored user message carries what it referenced in `conversation_refs` (`conversation_id`, `title`, `key`). A REST request that answers a held tool call with `decisions` stores no new message, so it is refused with a 422 when it also names conversations, as it is with files. The embedded assistant uses the same field on `POST /api/v1/platform/chat/messages` and lists the candidates with `GET /api/v1/platform/chat/conversation-references`, whose entries carry no `owner_name`.

## Requests you did not send

A conversation can be open in several places: another browser tab, another device, voice mode, a script calling the REST API, or a background task reporting back. To see what the others do as it happens, follow the conversation:

```text
GET /api/v1/web/chat/conversations/<conversation_id>/events?client_id=<your client id>
```

This stream receives every request of the conversation your client did not send, so each place can show the same answer as it is written. It closes after 15 minutes, or when your client stops reading it for about 10 seconds: open it again, and it resumes the running request. Two fields tell those frames apart:

| Field | Where | Meaning |
|---|---|---|
| `relayed` | Every frame of a request your client did not send, and `user_message` frames | `true`: the request was sent somewhere else. Frames of your own requests never carry it. |
| `turn_id` | Every frame of a request: `turn_started`, `folded_into_active_turn`, `thinking` (the one that acknowledges your message included), `status`, `stream`, `agentic_action`, `approval_required`, `approval_resolved`, `done`, `cancelled` and `error` | Identifies the request. Your own frames and the copies other clients receive carry the same value. `user_message`, `conversation_title` and `session_expired` belong to no request and carry none. |

An events stream opened while a request is running first receives a `thinking` frame flagged `"resumed": true`, then the steps, the reasoning and the answer written so far, then the rest as it happens; a request your client sent itself is not sent again when it reopens its events stream under the same `client_id`. A `user_message` frame (`message`) is a user message stored from another place.

If your script only cares about its own messages, ignore every frame flagged `relayed`, except for the request your message was folded into (below).

## Messages folded into a running request

A conversation runs one request at a time. A message you send while another request is running is folded into it: that request takes your message into account, and you receive:

```json
{"type": "folded_into_active_turn", "conversation_id": "<id>", "folded": true, "turn_id": "<turn id>"}
```

`turn_id` names the request that answers your message: its frames follow on the same response, flagged `relayed`, with that `turn_id`, up to its end. The agents your message mentions answer it in that request, after the agents of that request, or in the next request when it ends before it can ask them. The REST route that sends a message answers `202` with `"status": "folded_into_active_turn"` and the same `turn_id` in that case. Rarely, the message is kept for the next request instead (`"folded": false` on the stream, `"status": "queued_for_next_turn"` over REST). `turn_id` is then present only when a request is running, and that request picks the message up.

## How a request ends

Every request you are shown ends with a `done`, `cancelled` or `error` frame, including one sent somewhere else, even when the connection to the platform's message broker was briefly lost or the process running the request stopped. When the platform itself had to end it, the `error` frame carries a `reason`:

| `reason` | Meaning |
|---|---|
| `interrupted` | The request was stopped before it produced its answer (a server restart, for instance). |
| `lost` | The request is over, but how it ended could not be delivered. Read the conversation to see what it stored. |

A request still running when a normal server restart stops waiting for it stores an assistant message before that `error` goes out: it says the request was interrupted and names the tool calls that completed, the ones still running and the last completed action. Read back, the message carries `"interrupted": true` and the completed calls in `tool_call_trace`. The REST request that started it gets a `503` with `Retry-After`; read the conversation before sending the message again.

A request that ended before storing its answer, or whose agent failed, leaves its tool work to the next message of the conversation, streamed or over REST: the tool calls it made and their results reach the agent before that message, so it continues instead of calling the same tools again. This holds for 24 hours.

After a `done`, the stream that ran the request may send one `reply_suggestion` frame before it closes: `{"type": "reply_suggestion", "conversation_id": ..., "message_id": ..., "text": ...}`, the message the user would likely send next, for the answer `message_id` names. It comes only when the message you sent set `"supports_reply_suggestions": true`, your administrator enabled reply suggestions and set a routing model, and not for every answer: without the field, the stream closes at the end of the request, with no suggestion to wait for. It is a suggestion to show, never something the platform sends on its own; ignore it if your client has no use for it.

A stopped request can end with two `cancelled` frames on the stream that runs it: the acknowledgement of the stop, then, once the stop is stored, a second one carrying `message_id`. In the same way, a request whose agent failed can end with an `error` (without `reason`), then, once that reply is stored, a `done` carrying `message_id`. Both frames mean the request is over; the second one says what to read back.

!!! tip "Keep a safety check"
    If your script waits for an answer, also read `GET /api/v1/chat/conversations/<conversation_id>` when the request stays silent for a long time: `is_processing` is `false` once no request is running, and the stored messages show the answer. The platform guarantees the end frame; the check covers a network that drops frames on your side.

## Next step

Go back to [Chat](chat.md) to see how the web app shows the same conversation.
