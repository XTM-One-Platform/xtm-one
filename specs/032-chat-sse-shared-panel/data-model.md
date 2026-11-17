# Data model: Chat over SSE on the shared Filigran chat panel

No database change and no migration. State is in-process (per replica) and in Redis keys that exist today, plus two new keys.

## Turn (in-process)

A send's turn runs in `chat.py` `run_chat_connection`, over the `HeadlessTurnConnection` of its `POST /chat/messages` stream (`services/chat_web_transport.py`, research R1). The turn's own state (publisher, lock token, progress, steering queue, cancel and wait-skip events) is local to that call; the routes reach the turn through its connection.

| Field | Meaning |
|---|---|
| `ChatConnectionAuth` | Who runs it, resolved by the route: `user_id`, `session` (rechecked every `SESSION_RECHECK_SECONDS`), `impersonated_by`, `token_issued_at`, `origin` |
| `origin` | The send's `X-Chat-Client-Id` (see Client id), else a fresh id: stamped on every publish so the relay never echoes it (R6) |
| `turn_id` | `public_turn_id(token)` of the lock token, set when the runner sends its own `turn_started` |
| `stream_turn_id` | The turn the stream's frames belong to (its own, or the one its `folded_into_active_turn` names): every frame of the turn without a `turn_id` is named with it as it is written, the held `thinking` acknowledgement included |
| inbox | The send frame first, then the `cancel` / `skip_wait` frames a route `inject()`s on this replica |
| steering hand-off | `accept_steering(handoff)`: while set, the steer route hands a stored steer to the turn directly (`steer()`); `None` once the turn reads no more steering (`steering_closed`) |
| `refusal` | Set when the send is refused before its turn starts: the response is that HTTP error, not a stream (R11) |
| detached | The client went away: frames are dropped, the turn runs on; `client_present()` is `not detached` (approval presence) |

**States**: refused before the turn (HTTP 4xx), or folded into a running turn (`folded_into_active_turn`, then that turn relayed to its end); else `starting` (lock acquired, `turn_started`) → `running` (may pause at `approval_required` or an announced wait) → `ended` with one terminal: `done`, `error` or `cancelled`. An `error` or `cancelled` marked `provisional: true` is provisional and the turn's final end follows, stored or an `error` (`is_provisional_end`). The runner runs in the request's own ASGI task (`DetachedTurnStreamResponse`), so a process shutdown's graceful drain cancels a running turn and its cut is stored (`_store_cut_turn`); `chat_turn_guard` brackets the turn with `worker_shutdown.turn_started` / `turn_ended`.

**Registry**: per-process map `conversation_id → HeadlessTurnConnection` (`register` / `unregister` / `live_connection`), entered on the turn's own `turn_started`, left on `turn_released()` (the lock released) or the stream's end. Read by the control routes' direct path (research R5). A folded send's stream always relays the turn's frames from Redis, whichever replica runs it.

## Redis keys

| Key | Status | Use |
|---|---|---|
| `chat:processing:{id}` | existing | Single-flight turn lock (`chat_turn_guard`) |
| `chat:steering:{id}` | existing | Steering inbox, drained by the turn's bridge |
| `chat:cancel:{id}` | existing | Cross-replica Stop, addressed to the running turn's token |
| `chat:stopped:{id}` | existing | The moment of the last Stop |
| `chat:approval:{id}`, `chat:approval:pending:{id}` | existing | Decisions and pending proposals |
| `chat:approval:seen:{id}` | existing | Client presence for a paused approval (`mark_client_seen`, `CLIENT_SEEN_TTL_SECONDS` 60 s) |
| `chat:turn:{id}` (pub/sub), `chat:turn:state\|status\|reasoning\|answer:{id}:{token}` | existing | Fan-out and resume journal |
| `chat:skip_wait:{id}` | **new** | Continue now from another replica: set by `request_skip_wait` for the running turn's token (`SKIP_WAIT_REQUEST_TTL_SECONDS`, 60 s), consumed once by that turn's bridge (`consume_skip_wait`) (R5) |
| `chat:cancel:pending:{id}:{client_id}` | **new** | A Stop that found no turn, kept for the caller's send still on its way (`awaiting_send`, `PENDING_CANCEL_TTL_SECONDS`, 30 s): that send's turn ends as it starts; a Stop that reaches a turn drops it (R5) |

## Client id

A random id per panel instance, sent as `X-Chat-Client-Id` on every turn route and as `client_id` on the event stream (which also reads the header). At most 128 printable characters; any other value is replaced by a random one for that request (`chat_web_stream._client_id`). The panel takes a new one when its message stream drops while its turn runs on, so its event stream replays that turn. Used as the relay `origin` and as the suffix of a kept Stop's key; not stored otherwise.
