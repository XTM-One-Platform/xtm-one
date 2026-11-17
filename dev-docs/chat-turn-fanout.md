# Chat turn fan-out and resume

Companion to [`chat-turn-resilience.md`](chat-turn-resilience.md), which
keeps a turn running past its client connection: the agent loop runs
on and the `chat:processing:{conv_id}` marker tells a reloading client a turn
is in flight. This one makes the turn VISIBLE beyond the connection that sent
it.

## The problem

The agent loop reports through `on_event` / `on_token`, and the connection
runner (`run_chat_connection` in `api/v1/chat.py`) forwards those to the ONE
client that sent the message (`_safe_send_json`). Every other view of the
conversation needs the turn fanned out to it: a page reloaded mid-turn, a
second device or tab, an impersonating admin, and the turns no client
connection runs (REST `send_message`, the parent wake, #3243).

## The design

Three pieces, all in `backend/app/services/chat_turn_stream.py`, and the
guarantee that ties them to every client (section 4, #4395): a turn the
relay shows a client always ends on it.

### 1. `TurnPublisher` — the turn owner publishes

Held by whoever holds the turn lock (`chat_turn_guard.acquire_or_fold`):
the connection runner behind `POST /api/v1/web/chat/messages` binds it right
after the lock is acquired and every frame `_safe_send_json` sends is also
`offer()`ed to it — BEFORE the dead-stream early return, because a turn that
outlived its stream is exactly the turn the reloaded page needs to follow. REST `send_message` and the parent wake
pass `publisher.on_event` to `run_agent_chat` / `run_general_chat`
(`on_event` only — never `on_token`, so the LLM call mode of those paths
is unchanged) and publish their `done` once the assistant row is stored —
and COMMITTED, with the turn lock still held: every producer ends a
turn in the same order (persist, commit, terminal frame, close, release), so
the marker never disappears while no reply is readable and a second send
never acquires the lock with a history that omits the answer. On the REST
path that is `_rest_end_turn`, run by the persistence phase's `finally` on
every exit (the loop's own `finally` only stops the steering bridge, and
ends the turn itself only when the request is being torn down).

A Slack or Teams message on a conversation the web also reaches (a direct
message of its owner, or a thread it is continued in) is the fourth
producer: `ChannelTurn` (`services/channel_continuation.py`) takes the lock
with a channel-tagged token, starts its publisher only once the message will
run (never for one that is queued or handed back), passes `on_event` like
REST, and once `_finalize_message` committed publishes the stored channel
message as `user_message` and a `done` that names its `channel`, then
closes and releases. A web send refuses rather than folds while such a turn
holds the lock: it has no steering bridge and answers on its channel. A
Slack or Teams message that finds any turn running is queued instead and
runs as the next `ChannelTurn` once that turn releases the lock (the guard's
release starts `drain_channel_queue`), shown on the web like any channel
turn.

`offer()` is synchronous and never blocks the loop. A drainer task wakes,
waits one batch window (`DRAIN_INTERVAL_SECONDS`, 50 ms — the runner's
`_StreamCoalescer` already batches tokens at the same cadence), coalesces adjacent `stream` /
`thinking_text` chunks, strips the heavy `tool_done` payload no client
state reads (a consulted agent's, under `consult_progress.inner`, the same
way), stamps `origin` / `turn` / `seq`, and writes ONE Redis pipeline
(MULTI/EXEC):

- `PUBLISH chat:turn:{conv}` — the live batch (a JSON array of frames);
- the **journal**, keyed by the turn token so two turns can never
  contaminate each other:
  - `chat:turn:status:{conv}:{token}` — the `status` frames in order
    (`RPUSH` + `LTRIM` to `MAX_JOURNALED_STATUS_FRAMES`), minus the chatty
    ones (`thinking_text` → the reasoning string, `tool_heartbeat` — stale
    the moment it is replayed, `stream_retract`, a consult's inner chatter);
  - `chat:turn:reasoning:{conv}:{token}` / `chat:turn:answer:{conv}:{token}`
    — `APPEND`ed text of the CURRENT segment, bounded owner-side
    (`MAX_JOURNALED_TEXT_CHARS`), with the client's own resets mirrored: a
    per-iteration `thinking` status or a `stream_retract` deletes the
    answer, `transfer_start` deletes the reasoning, an intermediate `done`
    deletes both;
  - `chat:turn:state:{conv}:{token}` — `seq`, `started_at`, `origin`, and
    `agentic_actions`: the turn's agentic-action charges as running
    totals per quota period. A charge travels as an `agentic_action` frame,
    a type of its own that clients released before it ignore, and never
    enters the status list, which keeps the LAST statuses.

Terminal frames (`done` / `cancelled` / `error`) and the `thinking` acks are
never journaled: the resume synthesizes its own ack, and a journal only
exists while its turn does. Every producer publishes the terminal frame and
closes the publisher (`aclose()` flushes, deletes the status / reasoning /
answer keys and leaves an `ended` **tombstone** in the state hash,
`TOMBSTONE_TTL_SECONDS`) BEFORE releasing the lock, so a viewer never sees
the marker gone while its turn is "in flight". The tombstone covers the
other half of that interval: a viewer attaching between the close and the
release sees the marker but has missed the terminal frame, and resumed from
an empty journal it would sit on "Thinking" until its safety poll —
`read_snapshot()` reads the tombstone as "no turn in flight" instead. It
outlives the marker TTL, so a marker whose release failed cannot resume
anyone either until it lapses.

The exits that never reach a terminal frame of their own — a cancelled
handler, a failure in the persistence phase, a server shutdown — close
with `aclose(terminal=interrupted_frame())`: an `error` frame is offered
on the turn's behalf (only when none was offered before, and an
intermediate `done` does not count), so every viewer leaves the working
state at once instead of at its next safety poll. That is the connection
runner's `_clear_processing_marker()` funnel, the REST `_rest_end_turn` (a no-op
after the `done`) and pre-loop teardown, and the parent wake's `finally`.

Every key carries a TTL (`JOURNAL_TTL_SECONDS`, refreshed on every flush),
so a process that dies mid-turn leaves nothing behind for long. The two
text strings are capped owner-side (`MAX_JOURNALED_TEXT_CHARS` per
segment): a resumed viewer of a segment longer than that holds its head
and the live tail — a hole the size of the excess, accepted for a bound
no real answer reaches. Frames offered while Redis is in a quiet period
(below) are dropped, journal writes included, so a resume after such a
window replays whatever the journal holds. The charge totals are the
exception: a charge is counted as its batch is flushed, quiet or not, so
the journal's next write still carries it - it is the first frame of its
pass, and nothing later in the turn would bring it back.

Best-effort by contract. A failure trips a PROCESS-WIDE quiet period
(`FAILURE_COOLDOWN_SECONDS`, the `flow_run_stream.py` contract) — shared by
every publisher and the live path, because a dead Redis is a property of
the deployment and a per-turn state would make every new turn pay the
connect timeout again. Nothing here ever raises into the turn; the
client's `is_processing` poll stays the floor.

`publish_live()` publishes a journal-less frame: `user_message`, sent by
whoever persists a user message (the streamed send — acquired OR folded —,
the steer route once the message stays in the thread: a running turn took
it, whether one did is unknown, or none did and it could not be withdrawn
(answered `stored` with `answered: false`) - REST), so the other
clients append the row instead of waiting for the end-of-turn refetch.

### 2. `_RelayHub` — one subscription per process

A viewer stream must not cost a pooled Redis connection: the shared pool is
50 by default and the SSE activity stream already pins one per tab. The hub
holds ONE pub/sub connection per process (per event loop), `SUBSCRIBE`s a
conversation's channel on its first listener and `UNSUBSCRIBE`s on its
last, and fans messages out to bounded per-listener `asyncio.Queue`s. No
network I/O ever runs under its lock — a stream tearing down must never
wait on a connect timeout.

A subscription confirmation queues `RESYNC` for every listener of the
channel; so does a reconnect (backoff 1 s → 30 s, every channel
re-subscribed) and a listener whose queue overflowed (its backlog is
dropped — a partial frame sequence would leave holes in its text buffers).
Readiness is declared under the same lock the connect loop uses to check
for channels attached meanwhile, so a channel is subscribed exactly once
per connection: by the connect loop when it landed before, by `attach`
itself when it lands after. Subscription changes are applied from the
DESIRED state (`_apply`: "subscribed iff the channel has listeners", read
right before each command under a wire lock that serializes the commands
of the connection), never from the change that scheduled them — so the
unsubscribe a channel's last listener schedules is a no-op once a
replacement attached in between, and that replacement is announced with a
`RESYNC` even though no confirmation will come; `detach` applies through a
task, so a stream tearing down still never waits on Redis; a channel whose
listeners all left while the connect was in flight is applied the same way
once the connection is ready, and a confirmation for a channel nobody holds
any more leaves no confirmation event behind. One generation at a time:
the last `detach` cancels the reader without waiting for it (a stream
tearing down never waits on Redis), and the next `attach` waits for that
retiring reader before starting a replacement — the reader's `finally`
closes whatever connection the hub holds at that moment, which would
otherwise be the replacement's.

### 3. `relay_turn_frames` — the viewer is resumed, then follows

Run by the conversation's events stream
(`GET /api/v1/web/chat/conversations/{id}/events?client_id=`,
`api/v1/chat_web_stream.py`) for the stream's lifetime, and by a send's
stream that folded into another turn, up to that turn's end
(`_relay_folded_turn`). Owner-gated: both routes serve the conversation's
owner only (`owned_conversation` in `api/v1/helpers.py`, evaluated server-side from the
authenticated user against the conversation's `user_id`, a 404 otherwise),
so an anonymous conversation (`user_id IS NULL`, external channels) and a
shared reader get no relay at all; Stop
(`POST /api/v1/web/chat/messages/cancel` — the cross-process cancel request
AND the sub-agent cancel cascade, which takes the conversation id alone) is
gated the same way. On every `RESYNC` it reads `read_snapshot()`: the
marker decides IF a turn is in flight, the journal under its token
describes it; a marker with no readable journal (a turn started before its
publisher flushed, a Redis blip) yields the ack alone. It replays `resume_frames()` through the client's ordinary
handlers:

1. `{"type": "thinking", "resumed": true, "elapsed_s": N}` — re-arms the
   working state from a clean slate, followed by one `agentic_action` frame
   per quota period carrying the turn's charges;
2. the journaled `status` frames, in order — rebuild the label, the
   tool-usage flag, the consult map, the transfer;
3. one `thinking_text` frame with the reasoning so far;
4. one `stream` frame with the answer so far.

Then it forwards live frames flagged `relayed`, minus its own (`origin`),
minus the frames of the replayed turn the snapshot already covered
(`seq <= snapshot.seq` — the subscription is confirmed BEFORE the snapshot
is read, so nothing is lost and the overlap is deduplicated).

The relay follows ONE turn at a time: the marker holder at the last
resync, or the turn whose frames it saw since. A TERMINAL frame stamped
with another turn is the late `done` of a turn that is over and is dropped.
Every producer publishes its terminal frame before releasing the lock, so
the channel is ordered today; the guard is the belt for a producer that
does not (the REST path did, until its persistence phase was brought under
the lock), because forwarded unqualified such a `done` closes the newer
turn on every viewer. Any other frame of another turn moves the
relay onto that turn (its opening ack, or its first frame after a quiet
period that swallowed the ack). While the caller owns a turn no other turn
can be in flight, so every foreign-turn frame is stale by construction
(frames with no turn at all — `user_message` — always pass).

A resync that finds NO turn in flight — no marker, or the `ended`
tombstone of a turn that already published its terminal — or whose
snapshot could not be read, follows nothing from then on. The guard must
never turn into a loss: a whole newer turn can start AND end between the
subscription confirmation and the snapshot read (a reconnect, an overflow
resync), so the resync sees its tombstone while its `done` is already
queued right behind the `RESYNC`; kept on the previously followed turn, the
relay read that `done` as the stale frame of another turn and dropped it,
and the viewer got neither the newer answer nor a safety poll. Following
nothing, the queued terminal names the turn and is delivered. The same
rule applies once the relay forwarded the followed turn's own terminal:
`following` is a turn in flight as far as the relay knows, never one it saw
end — so the `done` of a turn whose opening frames a publish quiet period
swallowed (frames offered during the cooldown are dropped) still names its
turn and lands, instead of being dropped as another turn's terminal by a
viewer that never learned the turn had started.

Such a resync also says how the conversation's last turn ended when a
shutdown cut it (`_report_interrupted`, #6630): the turn's interrupted
terminal went out while the client's streams were down, and the stream it
reopens once the backend is back showed no turn, so it would otherwise never
hear it. While the conversation's turn record is marked interrupted
(`chat_turn_guard.interrupted_turn`, `interrupted_at` set) and the tombstone
still keeps the terminal (`TOMBSTONE_TTL_SECONDS`), the resync delivers it
through `_deliver_end`: relayed, named by the turn's `turn_id`, once per
stream. A turn that reached its own end leaves no such record, and a turn
whose process died without closing leaves no tombstone terminal - its
journal and lock outlive it, and the snapshot replays it instead.

The hub's live `SUBSCRIBE` (`_apply`, a channel attached after the
connection was ready) can fail on a connection that stays alive — a
timeout, a transient error. The reader would have no reason to reconnect,
and the channel would stay unsubscribed for as long as the connection
lived, its listeners getting neither a confirmation nor a frame. `_apply`
therefore flags the reader (`_resubscribe`), which tears the connection
down; the reconnect subscribes every listener channel again and every
listener resyncs.

A parent-wake reply may carry the finished task's files
(`attach_rows_to_message`): its relayed `done` carries them through the
same `parse_attachments` serializer the streamed and REST terminals use, so a
viewer never caches a same-id assistant row without its file chips. On a
channel conversation the wake also delivers the reply outbound (an HTTP
call that can take seconds): the marker is released right after `done` is
published, BEFORE that delivery, or a relayed viewer that handled `done`
and refetched would read the held marker as a running turn and re-adopt a
phantom "Thinking" for the duration.

### 4. Every relayed turn ends

Every turn a relay SHOWS is owed an end: the relay runs for EVERY owner
stream that follows the conversation, whatever the client is, and a client
following a relayed turn waits for its terminal with no poll behind it. A
relay SHOWS a turn once it forwarded one of its frames (or resumed the viewer
into it) and has not forwarded its terminal; `_TurnRelay` in
`chat_turn_stream.py` tracks it. Each way a terminal can go missing ends the
turn:

| Failure | What ends the turn |
|---|---|
| A resync (hub reconnect, listener overflow) finds the shown turn over - its live `done` was published while the stream was not subscribed, or dropped with an overflowing backlog. | The **tombstone keeps the terminal** (`terminal` in the `ended` state hash, `_stored_terminal`, bounded by `MAX_STORED_TERMINAL_CHARS`: the reasoning-panel fields go first). The resync reads it (`probe_turn_end`) and delivers the turn's own `done` / `cancelled` / `error`. |
| A publish failure trips the process-wide quiet period: the terminal of every turn ending in the next 30 s is dropped, journal and tombstone included. | The **publisher's terminal recovery** (`TurnPublisher._recover`): a closed publisher whose terminal (or tombstone) Redis refused publishes it, with the tombstone, in one pipeline once the quiet period ends, retrying after each new one for `TERMINAL_RECOVERY_WINDOW_SECONDS`. The frame keeps its `turn` / `seq` stamp. |
| The process running the turn dies: no terminal, no tombstone, and its lock lapses after `TURN_MARKER_TTL_SECONDS`. | The relay's **liveness probe**: a shown turn silent for `LIVENESS_CHECK_SECONDS` is probed. The lock is the liveness signal (every producer refreshes it; it is what `is_processing` reads, so the relay never ends a turn the conversation still reports as running). Once the lock no longer names the turn and no terminal can be recovered, the relay delivers `lost_turn_frame()` - `error` with `reason: "lost"` - after `TERMINAL_GRACE_SECONDS` (longer than a quiet period plus the REST / parent-wake lock refresh, so a recovery or a lock re-created by its refresher lands first). A tombstone without a terminal (a producer of the previous build, an end too large to keep) ends the turn at once: nothing more of it will come. |
| Another turn starts while the shown one never delivered its end. | The shown turn is **ended before the next turn's first frame** (its kept terminal, or `lost`), so a client never holds two turns open - but **never while it still holds the lock**, or while Redis cannot say whether it does: the other turn is then not in flight (a producer that outlived its lock, an end recovered late), its frame is the stale one and is dropped, and the shown turn stays on screen. A send the caller starts meanwhile is decided first. |
| A lone terminal of another turn arrives while a turn is followed or shown: the late end of a turn that is over, or the end of the newest turn when that turn's opening frames AND the shown turn's end were both lost to Redis failures. | The relay **reads the lock**: every producer publishes its terminal before it releases the lock, so a terminal whose turn holds it is the newest turn's - the shown turn is ended first, then that answer lands. Any other lone terminal is dropped. |
| A resync reads the snapshot right after the batch that published the terminal: the journal already counts the terminal's `seq`. | A terminal is **never deduplicated against a replay**: a replay never carries one. |
| A late frame of a turn the viewer already saw end (a producer that outlived its lock), or a resync landing between a turn's terminal and its journal drop. | The relay **remembers the turns it closed** (`ENDED_TURNS_KEPT`) and drops every later frame of them, a second copy of an end included; a resync never resumes into one. So does a turn the caller's own turn replaced on screen. |
| Two turns end twice: a Stop of a turn sends its acknowledgement (`cancelled`) on the stream running it, and a streamed turn whose agent loop raised sends `error` - each then stores the reply and ends with that stored end (`cancelled` / `done` carrying `message_id`, what a client reads the thread back on). | A **provisional end** (`is_provisional_end`: `cancelled` or `error` marked `provisional: true`; any other end is final) releases the viewer; what the turn sends before its final end (the `persisting` step, a token in flight) reopens nothing, and the final end still lands, once: the stored end, or `interrupted` when the reply could not be stored (a provisional end does not count as the turn's end when the publisher closes, so the close still offers it and the tombstone keeps it). |

No relay runs on a connection that runs its own turn: each web chat send
runs on a stream of its own, which relays only the turn it folded into,
and the tab's events stream never receives that send's live frames (they
carry its `origin`, the tab's `X-Chat-Client-Id`).

Every frame of a turn also names it: `turn_id` (`public_turn_id`, a
one-way digest of the lock token - the token never leaves the server), on
the stream that runs the turn, on every stream the turn is relayed to, on
the resumed replay and on every synthesized terminal. On a send's stream
the transport names them (`HeadlessTurnConnection`): its turn is the one
its `turn_started` or `folded_into_active_turn` names, and the
acknowledgement written before the lock is held until then and named on
the way out. Only `user_message`, `conversation_title` and
`session_expired` name no turn (`NON_TURN_FRAME_TYPES`): `publish_live`
sends nothing else, so every turn frame on the channel comes from a
publisher, which names its turn. The relay derives it from the transport stamp for a
producer of the previous build. `folded_into_active_turn` (the send's
stream) and the REST `202` (`_rest_fold_response`) name the turn that
answers the message
(`fold_turn_id`): the turn that accepted the fold, read by the fold's own
marker check (`chat_turn_guard.acquire_or_fold(accepted_by=...)`) - never
by reading the lock again, when that turn may have released it or the next
one taken it - and, for the rare requeue fallback (`folded: false` /
`queued_for_next_turn`), the running turn whose bridge picks it up live. A
fold taken by an exiting turn's release-drain names no turn: that turn is
gone. A fold the named turn accepted after its last look at the inbox (past
its last leftover check, the lock still held while its @mentioned agents
answer and its reply is stored) is not in that turn's answer: the turn's
end hands it to a follow-up turn started once the lock is released
(`parent_wake.schedule_fold_follow_up`), which reaches every stream of the
conversation under its own `turn_id`, like any turn of another origin.
The fold frame also comes first: the send's stream starts relaying
the turn it folded into only once it wrote `folded_into_active_turn`, so a
turn that ends right after the fold cannot deliver its `done` to a client
that does not know yet it is its answer. The stream probes the turn
(`probe_turn_end`) as soon as the relay attached (`relay_turn_frames(attached=)`)
and every `FOLD_RELAY_PROBE_SECONDS` after: a turn that ended before the
relay attached is closed at once from its tombstone, or as `lost`; a turn
Redis cannot vouch for during `FOLD_RELAY_UNCONFIRMED_SECONDS` (the turn
marker's TTL) is closed as `lost`, so the stream never stays open on an
unreadable Redis. A turn Redis confirms running keeps it open.

### The frontend (`@filigran/chatbot`)

Every web surface that shows a live turn (the `/chat` page, the studio rails,
the XTM One Desktop window, which hosts the same bundle) is
the `@filigran/chatbot` panel over the web chat's routes
(`components/chat-panel/endpoints.ts`): each send's own stream, plus the
conversation's events stream under the same client id. The panel applies the
rules below itself (a `resumed` or `relayed` ack, a fold, `turn_started`).
The host reads the frames the panel does not render through `useTurnFrames`
(`pages/chat-panel/useTurnFrames.ts`, the panel's `onStreamEvent`), relayed
ones included: agentic charges, runner activity and approvals, what a pause
waits on, the caches a tool changed. A turn that ends (a final `done`,
`cancelled`, `error`, `session_expired`) refetches the conversation after
`TURN_END_REFETCH_DELAY_MS`: a relayed turn that ends on an `error`
(`reason: "interrupted"` or `"lost"`) may still have stored a reply, and the
conversation then serves `interrupted_turn`, which the chat page and the
studio rails show (`InterruptedTurnNotice`, #6046,
`dev-docs/chat-turn-resilience.md` section 6).

## The wire protocol for clients

What a client of the web chat's streams can rely on (`api/v1/chat_web_stream.py`;
user docs: `docs/docs/usage/chat-api.md`). `POST /api/v1/web/chat/messages`
streams one send's turn back as SSE `data:` frames;
`GET /api/v1/web/chat/conversations/{id}/events?client_id=` follows the
conversation (a reload, another tab or device):

- **Streams.** A send's stream ends once its turn (and the conversation
  title) is delivered, or once the turn it folded into ends; a client that
  goes away leaves the turn running and stored. The events stream ends after
  `EVENTS_MAX_LIFETIME_SECONDS` (15 min) and the client reconnects. An idle
  stream writes a `: keepalive` comment every 15 s once its response has
  started, and one whose session ends closes on a `session_expired` frame. A
  send's stream writes none before its turn starts or folds: a refusal
  decided late in setup stays an HTTP error with its status and headers, and
  the admission limiter keeps counting the setup. A setup that outlasted a
  proxy's response-header timeout would be cut by the proxy instead. A client that stops reading
  (no write taken within `STREAM_WRITE_TIMEOUT_SECONDS`, or more than
  `EVENTS_QUEUE_SIZE` frames waiting on an events stream) has its stream
  ended, the turn left running; it reconnects and resyncs from the snapshot.
  One still reading nothing `STREAM_ABANDON_SECONDS` (60 s) later has its
  response given up and its connection closed.
  Both streams are counted by the worker's admission limiter
  (`ConcurrencyLimitMiddleware`, a `429` past it) until their response
  starts, never while they stream.
- **Whose turn.** A frame of a turn this client did not start carries
  `relayed: true`. Every turn frame (`thinking`, `status`, `stream`, `done`,
  `cancelled`, `error`, `agentic_action`, `approval_*`) carries `turn_id`,
  the acknowledgement of a send included; a send's own frames and the
  relayed copies of the same turn carry the same id; `turn_started`, the
  send's own turn taking the lock, carries it too.
  `user_message` frames (a user message persisted elsewhere) carry
  `relayed` and no `turn_id`. The live frames a send produces are never
  relayed to the events stream of the same client (`X-Chat-Client-Id` on the send,
  `client_id` on the events stream).
- **Folded sends.** A message sent while another turn holds the
  conversation is folded into it: `folded_into_active_turn` on the send's
  stream / the REST `202 {"status": "folded_into_active_turn"}` name that
  turn with `turn_id`; the send's stream then relays that turn, after the
  fold frame, up to its end. On the rare requeue fallback
  (`folded: false` / `"status": "queued_for_next_turn"`) both name the
  running turn that picks the message up, when there is one.
- **Every relayed turn ends.** A relayed turn always reaches `done`,
  `cancelled` or `error` on the stream it was shown on (section 4), within
  `LIVENESS_CHECK_SECONDS + TERMINAL_GRACE_SECONDS` of its lock disappearing
  in the worst case. An `error` the platform synthesized carries `reason`:
  `interrupted` (the turn was torn down before its end) or `lost` (its end
  could not be delivered; the conversation history holds what it stored).
- **A refused send names no turn.** A send refused before it took the lock
  (a rate limit, an empty message, a refused tool selection, a conversation
  that cannot be continued) is an HTTP error with its `detail` (and
  `Retry-After` for a rate limit), never a frame: it is the only answer that
  message gets.
- **An end without an answer is remembered.** Once a turn ended that way,
  `GET /chat/conversations/{id}` serves `interrupted_turn` until the next
  turn takes the lock.
- **Two ends, one turn.** A Stop acknowledgement (`cancelled`) and the
  `error` of a streamed turn whose agent loop failed say they are
  provisional (`"provisional": true`): the turn's final end follows, on the
  stream that runs the turn and on every stream it is relayed to - its
  stored end, a `cancelled` / `done` carrying `message_id`, or an `error`
  when the reply could not be stored. Both mean the turn is over; the second
  says what to read back. Any end not marked `provisional` is final.
- **Mid-turn segments.** A `done` flagged `intermediate: true` is a
  committed segment, not the end of the turn.
- **Several agents, one turn.** A message that @mentions several
  agents is answered in ONE turn - one lock, one `turn_id`, one journal,
  one end. Each reply but the last is an intermediate `done` carrying its
  `answered_by` (or `mention_failed` for a note), a `status` frame
  `mention_answering` announces the next agent, and the last reply is the
  turn's terminal, the one the tombstone keeps. No frame type was added for
  it. A streamed turn commits each reply before the next agent starts; a
  REST turn stores its replies once every agent answered, so its viewers see
  the mentioned agents' progress first and the `done` frames together at the
  end. The agents a FOLDED message mentions answer it inside the
  turn the fold named, after that turn's own agents - or, when the turn ends
  before it can ask them, in the next turn, which the fold did not name.

A client that only cares about its own sends ignores `relayed` frames,
except those whose `turn_id` a `folded_into_active_turn` named. A client
that follows the whole conversation handles them as the SPA does. Either
way, a client that waits for a terminal keeps a safety check of its own
(`is_processing` on `GET /chat/conversations/{id}`): the server guarantees
the end, a client-side bound guards against everything else (a server of
the previous build, a network that drops frames).

## The clients

| Client | What it does with relayed turns |
|---|---|
| The web app (`@filigran/chatbot` on `/api/v1/web/chat`: the `/chat` page, the studio rails, the XTM One Desktop window, which hosts the same bundle) | Follows every turn of the conversation through its events stream; plus the refetch after a relayed `error` and the build check. |
| Voice calls (the panel's voice-call mode, `ChatPanelHandle.startVoiceCall()` from the `/chat` composer toolbar) | Part of the panel: it speaks only the final answer of the panel's own turn by the agent it addressed; a relayed turn, or a reply by another agent, stays text in the transcript. |
| The Office add-in (`apps/microsoft-office`: the same panel, `src/components/OfficeChatPanel.tsx` over `src/lib/panelConfig.ts`) | As the web app: the panel follows the conversation's events stream. A `mention_failed` note offers no document action. |
| The browser extension (separate repository) | Must apply the rule above if it follows these streams: ignore `relayed` frames except a folded turn's. |
| The public chat widget (`public_chat.py`) | Its own transport: no relay. |
| The chat embedded in OpenCTI / OpenAEV / OpenCRQ (`@filigran/chatbot`, `POST /api/v1/platform/chat/messages`) | Its own transport - one streamed response per message, steered through its own Redis list - that takes no turn lock and reads no mentions: neither a relayed nor a multi-agent turn reaches it. For the same reason it is refused on a conversation a Slack or Teams turn can reach (`platform_chat_refusal`, #5134). |
| Channel bots (`channels/router.py`) | Server-side, no stream: their @mentions (`CHANNEL_AGENT_MENTIONS_ENABLED`) are answered in one outbound message, a labelled section per agent. A message on a conversation the web also reaches runs as a `ChannelTurn` (section 1): its turn shows on the conversation's events streams like a REST turn. |

## Older bundles

A tab opened before an upgrade keeps running the previous bundle until it
is reloaded. The served `index.html` names its build
(`<meta name="build-id">`, `branding.frontend_build_id`: a digest of the
built file, whose asset names carry the bundle's content hashes) and
`GET /auth/config` reports the build the server serves now (`build_id`,
empty without a built frontend). `useStaleBuildNotice` compares the two
whenever `/auth/config` is refetched (window focus) and offers a reload once
per server build.

Residual, stated plainly: a bundle from before the HTTP + SSE chat
cannot chat until it is reloaded, since the WebSocket it opens answers 404;
the Office add-in is cached by Office, and the previous add-in cannot chat
until Office refreshes it. A build id is
a digest, so it says two builds differ, never which is newer: during a
rolling deploy a page can read `/auth/config` from a replica of the other
build, and a page of the new build is then offered a reload too. It is
offered once per server build, never reloads on its own, and stops as soon
as every replica serves one build.

## What it is not

- Not a message bus for the rest of the platform: the channel carries a
  conversation's turn frames and nothing else, and readers are the
  conversation's owner streams.
- Not a replacement for polling: it is the accelerator, `is_processing`
  is the floor, and every failure degrades to polling - except that a turn
  the relay SHOWED is always ended on that stream (section 4): a client
  following a relayed turn has no floor of its own for it.
- Not persistent: the journal is Redis with a TTL, deleted with the turn.
  History is the `messages` table, as before.
- No system-prompt text, no schema migration, no new environment variable.

## Follow-ups

- Read-only viewers of a SHARED conversation are not relayed to
  yet; the relay is owner-gated because the read-only chat surface has not
  been reviewed for a live working state (Stop button, composer).
- The public embed widget (`public_chat.py`) has its own transport and is
  out of scope.
