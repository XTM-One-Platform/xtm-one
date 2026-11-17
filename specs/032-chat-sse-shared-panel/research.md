# Research: Chat over SSE on the shared Filigran chat panel

Each entry: decision, why it beat the strongest alternative, alternatives named.

## R1. The turn leaves the transport

**Decision**: The WebSocket handler's send path became `run_chat_connection` (`backend/app/api/v1/chat.py`), a runner that serves one send over the `ChatConnection` protocol (`send_json`, `receive_json`, `end_session`, `refuse`, `accept_steering`, `turn_released`, `client_present`). The web transport is `HeadlessTurnConnection` (`backend/app/services/chat_web_transport.py`), one send per `POST /chat/messages` stream, served by `DetachedTurnStreamResponse`: a raw ASGI response whose producer runs in the request's own task, where an `http.disconnect` only detaches the connection (frames dropped, the turn runs on and stores its answer).

**Rationale**: An SSE `StreamingResponse` cancels its producer on disconnect, so a turn run inside it dies with the stream (FR-005). Running the turn in the request task rather than a spawned one keeps it under uvicorn's graceful drain, which cancels it and stores the cut (`_store_cut_turn`), and under the worker's request accounting, with no second lifecycle for the drain and the recycle wait to learn.

**Alternatives**: A detached `asyncio.Task` per turn in a process registry (the drain and the recycle wait must be taught about it); `asyncio.shield` around an inline turn (cancellation still reaches every `await` outside the shield); a SAQ worker job (moves the turn off the replica holding its in-process approval/wait events and adds queue latency to every first token).

## R2. Extract first, then swap transports

**Decision**: The send path was extracted into `run_chat_connection` behind `ChatConnection`, the SSE transport added on it, and the WebSocket route and its helpers then removed. The ~204 backend tests that drove `chat_websocket(fake_ws)` drive `run_chat_connection` through a fake `HeadlessTurnConnection` (`backend/tests/test_chat_turn_steering.py`: `_FakeConnection`, `_run_connection`).

**Rationale**: The extraction is the risky step. Doing it under the existing test suite catches behaviour drift before any transport changes.

**Alternatives**: A new runner written beside the old handler (two turn implementations until removal, and drift between them).

## R3. Route set and prefix

**Decision**: A new router, `backend/app/api/v1/chat_web_stream.py`, mounted at `/api/v1/web` (`WEB_CHAT_PREFIX`), serving the panel's REST contract on the embedded chat's relative paths (`/chat/*`) for XTM One's own conversations (`contracts/web-chat-api.md`). The panel gets `apiBaseUrl = API_BASE` (`/api/v1`) and paths starting `/web/chat/…`, so the same base reaches `/api/v1/chat/tts` and `/api/v1/chat/files/{id}/download`. The platform routes' builders are shared from `platform.py` (`restore_chat_session_window`, `restored_message_entries`, `apply_approval_decisions`), and the package's reads reuse the existing handlers (`add_api_route`: quota, prompts, suggestions, conversation references, artifacts, workspaces).

**Rationale**: The panel's contract shapes (`/chat/sessions`, `/chat/messages`, `{conversations:[{conversation_id,…}]}`) differ from the existing `/api/v1/chat/conversations` routes, and the platform routes are pinned to channel `api` with no turn lock (FR-014 keeps them). Calling the platform module's functions and handlers directly keeps one implementation of each shape; a separate contract service would only move them.

**Alternatives**: A `channel` switch on the platform routes (mixes two auth populations and two turn models on one route); reshaping `/api/v1/chat/*` (breaks the REST API scripts already use); a shared `panel_chat_contract` service (a move of code both routers already share by import).

## R4. Auth, gates and session end

**Decision**: Every route `chat_web_stream.py` defines takes `web_chat_user`: `get_current_user`, then a session JWT only (`auth_credential_kind == "session_jwt"`, else 403 "The web chat needs a web session"); the reused handlers take `get_current_user`. `require_chat_enabled` sits only on the routes that start or feed a turn: send, steer, approve, a new conversation from `POST /chat/sessions` and upload; reads (a restore included), a session's own `PATCH` / `DELETE`, the events stream, pending approvals, Stop and Continue now stay open with AI off (specs/008-ai-disabled-mode). Both streams re-run `recheck_session` every `SESSION_RECHECK_SECONDS` (15 s), the message stream in `run_chat_connection`, the events stream in its own loop, and end with `{"type": "session_expired", "reason"}`. A request whose session has ended answers 401 `{"detail": {"code": "session_expired", "reason", "message"}}` (`session_ended_error`). The panel reports either as `onSessionEnded(reason)`, which `XtmChatPanel` turns into `endSession()`.

**Rationale**: `get_current_user` already does what the WebSocket handshake re-implemented: confined-token refusal, account checks, Terms of Use for session JWTs (FR-007). The token travels in the `Authorization` header (the panel's fetch-based SSE client), so it leaves the URL. A 401/403 before the stream starts replaces close codes 4001/4003/4004, with a machine `code` beside the readable `detail` (`CodedHTTPException`: `chat_unavailable`, `terms_required`).

**Alternatives**: `?token=` on a browser `EventSource` (puts the token in proxy logs; `activity.py` does it and is not a model to copy).

## R5. Control actions across replicas

**Decision**: Stop, Continue now, approve and steer are POST routes. On the replica running the turn (`live_connection(conversation_id)`, the `HeadlessTurnConnection` registered when its turn started) Stop and Continue now `inject()` a `cancel` / `skip_wait` frame and a steer is handed to the turn directly (`steer()`), without the inbox or a bridge poll. Anywhere else they go through Redis: `request_turn_cancel` (`chat:cancel:{id}`), `request_skip_wait` (the new `chat:skip_wait:{id}`) and `fold_into_inflight_turn` (the steering inbox), all addressed to the running turn's token and consumed by its steering bridge (`BRIDGE_POLL_SECONDS` = 1 s); an inbox steer carries its files and ends an announced wait like a direct one. Approvals always go through Redis (`push_approval_decision`), which the paused turn reads wherever it runs. A Stop that finds no turn is kept for the caller's send still on its way only when its body says `awaiting_send` (`chat:cancel:pending:{id}:{client_id}`, 30 s).

**Rationale**: A control POST can land on any replica, not the one running the turn (Principle IV), and a steer on the turn's own replica must not wait a bridge poll. Redis down with the turn elsewhere → 503, never a silent no-op (Principle II); a steer already stored when its hand-off fails answers `stored`, since a 503 means the client keeps its text.

**Alternatives**: Sticky sessions per conversation (a deployment requirement the product does not have today).

## R6. Fan-out, re-attach and echo suppression

**Decision**: The event stream is `relay_turn_frames` (`chat_turn_stream.py`) with a queue-backed `send`, `origin` = the client id (`client_id` query or `X-Chat-Client-Id`). The turn a POST starts publishes with that same origin (`ChatConnectionAuth.origin`), so the relay never echoes it back. Re-attach after a reload is the existing `read_snapshot` → `resume_frames`; a resync skips a snapshot of the stream's own origin (`_resync`), so a client whose events stream reopens mid-turn is not handed its own turn.

**Rationale**: `relay_turn_frames` already takes a generic `send` callback and does snapshot, dedup by `(turn, seq)`, liveness probes and `lost` endings. It needed no change beyond the origin source and the origin check on resync.

**Alternatives**: SSE `id:` + `Last-Event-ID` resume (duplicates the journal the relay already reads).

## R7. Frame format and keep-alive

**Decision**: One module for both web chat streams, `backend/app/services/chat_web_transport.py`: `sse_data()` (`data: <json>\n\n`), `SSE_KEEPALIVE` (`: keepalive\n\n`) written at most `SSE_KEEPALIVE_SECONDS` (15 s) after the last write (`keepalive_delay()`, read by the send stream's ticker and the events stream's loop alike), and `SSE_HEADERS` (`text/event-stream; charset=utf-8`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`). Frames are redacted for display where the runner sends them (`redact_frame(display=True)`). The platform and public chat routes keep their own code (FR-014, out of scope).

**Rationale**: There was no shared SSE helper, and the routes that stream each spelled the format by hand (Principle VIII). The 15 s interval stays under the 100 s Cloudflare and 300 s nginx limits `installation.md` documents.

## R8. Connection budget

**Decision**: Production requires HTTP/2 between browser and reverse proxy (`installation.md`'s requirements table). A panel holds at most two streams: the event stream and, during a turn, the message stream.

**Rationale**: Under HTTP/1.1 a browser allows six connections per origin, and the activity stream already takes one per tab; two tabs mid-turn reach the limit. HTTP/2 multiplexes streams on one connection. For HTTP/1.1, the panel closes its event stream once its page has been hidden for 60 s (`EVENTS_HIDDEN_CLOSE_MS`, package gap G5) and reopens it, resynced, when visible. Development through the Vite proxy stays on HTTP/1.1.

**Alternatives**: One shared event stream per browser through a `SharedWorker` (a package-level rewrite for a limit HTTP/2 removes).

## R9. The panel's stylesheet

**Decision**: The package ships its CSS isolated from the host (package gap G1): `postcss/isolate-from-host.js` scopes every utility to `.filigran-chatbot` and its descendants and declares the theme variables on the panel root instead of `:root, :host`; `postcss/stylesheet.test.js` fails on any rule outside the panel. XTM One still imports the package root and stylesheet only from modules no prefetched page loads (`XtmChatPanel.tsx` on lazy pages, `LazyXtmChatPanel.tsx`, the Office add-in), for the bundle's size, as `frontend/AGENTS.md` states.

**Rationale**: The package's stylesheet redefined Tailwind utilities this app also uses, and loading it broke the whole layout once (#3927).

**Alternatives**: Loading it inside a low-priority `@layer` in XTM One (the app's utilities then restyle the panel's internals).

## R10. Capabilities the panel cannot host today

**Decision**: Each gap goes into `@filigran/chatbot` as a generic, opt-in feature (FR-012), in 3.15.0 (FiligranHQ/filigran-ui#455, pinned by T006). The list (G1-G5), the props that carry them and each surface's mapping are in `contracts/panel-host-integration.md`.

**Rationale**: Host-side workarounds would rebuild the UI this feature deletes (SC-005).

## R11. Pre-turn refusals

**Decision**: Refusals before a turn starts answer the POST with an HTTP error and `{"detail"}`: 400 empty message or refused tool selection (`refused_chat_selection`), 401 account rejection, 404 unknown conversation, 409 a conversation held by its channel (`web_send_refusal`, `hold_off_channel_turns`), 422 unreadable pack options, 429 with `Retry-After`. `HeadlessTurnConnection` holds the `thinking` ack until the turn starts and `refuse()` answers the whole response instead (`SendRefusal`). Failures after the turn starts are `error` frames, as before.

**Rationale**: The panel resolves `send()` to `'failed'` on a refused request and shows its detail. A 4xx is also what script clients expect.

## R12. Tabs open across the upgrade

**Decision**: No WebSocket tombstone. An old bundle's socket gets a 404 and keeps retrying, and `useStaleBuildNotice` offers the reload on the next window focus.

**Rationale**: The stale-build check already refetches `/auth/config` on focus. A tombstone route would contradict SC-001 for a few minutes' benefit.

## R13. Public Chat API and outside clients

**Decision**: `docs/docs/usage/chat-api.md` documents the SSE routes as the public chat API. `UPGRADING.md` and the release notes name the removal and the replacement. The browser extension (`FiligranHQ/xtm-browser-extension`) is not affected: it never opened the WebSocket, and the routes it calls (`/api/v1/extension/execute-task`, `/api/v1/auth/me`, `/api/v1/platform/config`) stay (T007).

**Rationale**: The WebSocket is documented for users' own scripts. Removing it in one release (FR-015) is a breaking API change and has to be announced as one.

## R14. What the web routes add to the panel's contract

**Decision**: Each carries a behaviour the old `/chat` page had or a constraint of long streams:

- The web chat's own agent picker (`GET /web/chat/agents`, `list_web_chat_agents`: the General Assistant entry `general-assistant:` unless an agent stands in for it, the chat agents, chat flows as `flow:<id>` under "Flows", then a "Not recommended for chat" group) instead of the platform agent builder: the old page's picker offered all of these (FR-011). The studio filter (`?studio=presentation|video`, `STUDIO_PICKER_TOOL`) is on it.
- Restore and paging of a conversation shared with the caller, read-only, and `GET /web/chat/sessions?scope=shared` with continued channel threads: the old page's "Shared with me" list and read-only shares (US1 sharing).
- A 15-minute lifetime on the events stream (`EVENTS_MAX_LIFETIME_SECONDS`): the panel reopens it and resyncs from the snapshot, so a long-lived connection is periodically re-authenticated and rebalanced, as the activity stream's `SSE_MAX_LIFETIME_SECONDS` does.
- The two streams counted by `ConcurrencyLimitMiddleware` only until their response starts (`is_long_lived_stream`, `POST` send and `GET` events): a stream holds its request for minutes and would starve the worker's request slots, while its authentication and setup are shed past the limit like any request. Each stream is still bounded by its owner check, the send rate limit and the turn lock.

No web feedback route: the panel's feedback endpoint stays unset and the host renders `MessageFeedbackButtons` on the existing `/api/v1/chat` routes.

**Rationale**: Each answers a requirement or a constraint above; an unused route is surface without a caller.
