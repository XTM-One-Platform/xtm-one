# Chat Turn Resilience — Assessment & Enhancement Plan

Full-lifecycle audit of a chat turn (send received -> pre-loop setup ->
agentic loop -> persistence -> delivery) against three invariants:

1. **Never blocked** — no await can hang indefinitely; every phase is bounded.
2. **Always performant** — no per-turn work that grows unbounded or repeats
   needlessly.
3. **Always visible** — the user and the operator can always tell the turn is
   alive (UI feedback + WARNING-level logs).

Trigger: the 2026-07-09 incident (conversation `34175dde`) — a legitimate
long-conversation turn ran 38+ minutes while the UI froze on
"Analyzing results...", a reload showed nothing in flight, and production
logs (WARNING+) were empty.

---

## 1. What was verified SAFE (rely on it)

**Agentic loop (`tools/agent_loop.py`)** — audited end-to-end:

- Every LLM await goes through `_streaming_chat` (`asyncio.wait_for`),
  `_race_cancellation(..., timeout=...)`, or the compaction summarizer with
  `llm_timeout` — hard cap `_LLM_CALL_TIMEOUT` (900 s).
- Every `executor.execute_tool` await is wrapped in `_race_cancellation`
  with `_TOOL_EXECUTION_TIMEOUT` (300 s) or `_AGENT_TOOL_TIMEOUT` (900 s),
  capped to the remaining `wall_clock_deadline`, with
  `tool_heartbeat` events every 10 s.
- All retry cycles are capped: continuations (3), truncation retries (2),
  consecutive empty responses (3), stall continuations (1), iterations
  (default 25, clamped).
- The forced-final-answer backstop is wall-clock aware (`wall_clock_deadline`): pre-checks remaining budget, caps its LLM calls, returns a
  deterministic digest when < 10 s remain.
- No Redis/DB I/O inside the loop body.
- Cancellation is cooperative everywhere (`cancel_event` ->
  `_CancelledMidCallError`, checked between stream chunks).

**LLM provider layer (`app/llm/`)**:

- `retry_llm_call`: hard cap of 6 attempts, full-jitter backoff bounded at
  60 s per sleep (~62 s total).
- Failover is sequential but runs INSIDE the caller's 900 s bound for all
  agent-loop calls; non-failoverable errors (413, context overflow) fail
  fast.
- Streaming providers never retry after the first emitted byte (no duplicate
  tokens).
- SDK HTTP timeouts are explicit (Anthropic 600 s; OpenAI-compat
  connect=10/read=600/write=30/pool=10).

**Chat connection runner (`chat.run_chat_connection`, served by
`POST /api/v1/web/chat/messages` as an SSE stream)**:

- A steering message is stored before it reaches the turn
  (`persist_web_steer`, `POST /api/v1/web/chat/messages/steer`); a refused or
  failed persist answers the request with an HTTP error whose `detail` the
  panel shows above the composer (never silently dropped). One no turn took
  (the turn ended on the way, or stopped reading steering at its last
  leftover check) is deleted and answered `409`: the client sends it as the
  next turn. One stored whose hand-off failed on Redis, or that cannot be
  deleted, stays in the thread and is answered `stored`: a `503` always
  means nothing was stored, so a resend never stores a second copy. The turn's
  leftover check also answers, in a follow-up pass, a steer handed over on
  its process that its bridge had not read.
- A message folded into the turn after that last leftover check - a send, a
  steer or a wake on another replica, while the turn still holds the lock
  (its @mentioned agents answering, its reply being stored) - was told the
  turn took it. The release-and-drain handshake returns it, and the turn's
  steering queue returns one its bridge queued after the loop's last look
  (`PartnerAnswerQueue.take_unanswered`; the turn's end stops the bridge with
  `chat_turn_guard.stop_bridge`, once the batch it holds is queued, never by
  a cancellation that would lose it). A turn that reached its own end
  answers them in a follow-up turn started once the lock is released
  (`parent_wake.schedule_fold_follow_up`: a wake turn's follow-up, which takes
  the conversation or folds them into the turn that took it meanwhile, and
  answers the agents they mention). A Stop, an account no longer allowed to
  chat or a torn-down turn logs them: stored messages, they shape the next
  turn. A Stop counts whether a pass read it or not (sent between two passes,
  or during the persistence): the turn guard's record of it (`record_stop`),
  at or after the moment the turn took the lock (`_stopped_during_turn`). The REST turn does the same (`_rest_end_turn`), a wake turn follows
  its own up, and a channel message's turn, which reads no fold,
  hands them to the next turn's history.
- `store_message` retries once after rollback; the user message commits
  BEFORE the loop starts (user input never lost).
- The `chat:processing:{conv_id}` marker is set at turn start, refreshed by
  the keepalive every ~20 s while the turn runs, and cleared on every exit
  path (this PR).

**Pre-loop setup**:

- Skills/prompts/KB names are DB-only reads at turn time (no URL fetches).
- Cached MCP tool definitions register without connecting; memory tiers,
  prompts+skills, and integrations/KB/handover loads are parallelized.
- Redis ops: 5 s socket timeout; DB pool acquire: 30 s.

---

## 2. Gaps fixed

| # | Gap | Fix |
|---|-----|-----|
| 1 | `chat:processing` marker expired after 300 s while turns legitimately run 40+ min -> reload showed nothing in flight | Keepalive refreshes the marker every ~20 s for the turn's duration; cleared on all exit paths |
| 2 | Loop progress logs are INFO-only -> WARNING+ captures showed nothing for the whole turn | Slow-turn watchdog: WARNING heartbeat at iteration boundaries after 300 s (throttled, with iteration/elapsed/context/token stats) + WARNING completion trace in both chat services |
| 3 | `asyncio.CancelledError` bypassed the `except Exception` handler -> a killed turn died with zero logs and a phantom processing marker | Dedicated handler: WARNING log + marker cleanup + re-raise |
| 4 | Undeliverable `done` frame silently dropped | WARNING log with the persisted assistant message id |
| 5 | "Analyzing results..." static forever | Frontend shows loop iteration ("step N") |
| 6 | Conversation compaction output never persisted -> full summarizer LLM call re-ran from scratch on EVERY message past the threshold; >50-message history silently lost | `conversations.compaction_state_json` + sequence watermark; `get_conversation_history` rehydrates; summarizer now runs once per compaction cycle (triggered at 60% of the model's context window in estimated tokens, or past the `SUMMARIZE_THRESHOLD` safety net of 45 messages, #8497) with incremental roll-forward |
| 7 | A frame send unbounded on zombie clients; `_send_lock` serializes senders so ONE stuck send froze keepalives, events, and the loop | `_SEND_TIMEOUT` (10 s): a timed-out send marks the stream dead, the turn continues to persistence |
| 8 | `MCPConnection.connect()` unbounded (transport + initialize + list_tools); a hanging endpoint stalled setup forever and never tripped the circuit breaker (hangs never *fail*) | `_MCP_CONNECT_TIMEOUT_SECS` (60 s) via in-place `asyncio.timeout`; timeouts count as operational failures -> circuit opens |
| 9 | Compactor LLM call bounded only by SDK read timeout (600 s x up to 2 calls) in the pre-loop phase | `_COMPACTOR_TIMEOUT_SECS` (120 s); timeout falls back to prior directives |
| 10 | Title-generation LLM call unbounded AFTER the answer streamed but BEFORE the `done` frame | 20 s cap; timeout keeps the deterministic fallback title |
| 11 | Broken tool schema silently disabled argument validation (DEBUG log) | Raised to WARNING |

---

## 3. Enhancement plan (follow-ups, in priority order)

### Phase 1 — remaining blocking/latency bounds (small, high value)

- **P1.1 Wall-clock deadline inside the loop.** `wall_clock_deadline` is
  honored only by the forced-final-answer backstop today. Propagate a
  remaining-budget cap (min(900, remaining)) to in-loop LLM calls and the
  proactive/empty-recovery compaction summarizers so a turn near its budget
  degrades gracefully instead of being cancelled mid-call by the outer
  `asyncio.wait_for`.
- **P1.2 Streaming idle watchdog.** A stalled SSE stream (provider accepts,
  sends nothing) is bounded only by the 600 s SDK read timeout / 900 s outer
  cap. Add an idle-chunk watchdog in `_streaming_chat` (~90-120 s without a
  chunk -> abort + retry/failover path).
- **P1.3 Embedding call caps.** `load_persistent_context` /
  `embed_text` in the setup path ride provider read timeouts (up to 600 s).
  Wrap with `asyncio.wait_for(30)`; skip semantic memory search on timeout.
- **P1.4 Post-loop budget.** Cap the whole post-loop persistence phase
  (attachment linking + commit) with a soft timeout + WARNING log so the
  `done` frame can never be delayed indefinitely by a lock wait.

### Phase 2 — user feedback (visibility)

- **P2.1 Setup-phase status events.** Between the `thinking` ack and the
  loop's first event the user sees a static indicator through history load,
  compaction, memory/embedding, integrations/OAuth refresh, MCP connects,
  and tool assembly. Emit granular frames (`preparing:history`,
  `preparing:compaction`, `preparing:tools`, ...) from the chat runner and
  `run_agent_chat`/`run_general_chat`; render them in the status bubble.
- **P2.2 Compaction liveness event.** Emit a `context_compaction`-style
  event BEFORE the in-loop semantic compaction summarizer call (today the
  badge appears only after).
- **P2.3 Persisting status.** After the final token, emit
  `{"status": "persisting"}` before store_message/title/commit, and keep the
  keepalive task alive through the post-loop phase (it is currently
  cancelled before persistence starts).
- **P2.4 Elapsed time in the working label.** Frontend: show a live elapsed
  counter next to "Analyzing results (step N)" reusing the existing
  `elapsedStartMs` ticker machinery.
- **P2.5 Cancel-state coherence.** When the user cancelled, the post-loop
  currently persists "The request was cancelled." and emits `done`; emit
  `cancelled` instead to avoid double-state on the client. The stored reply is
  what the answer's current pass had streamed before the Stop, as the panel
  showed it, or that text when it had streamed nothing.

### Phase 3 — performance (setup latency)

- **P3.1 Parallelize integration OAuth refreshes.** `build_integration_tools`
  refreshes tokens sequentially (N x up to 30 s worst case) — gather with a
  per-integration cap.
- **P3.2 Kill the `_get_token` N+1.** Reuse the batch-loaded Integration rows
  instead of re-querying per binding.
- **P3.3 Reuse the LLM resolution.** The streamed send resolves `get_llm_for_user`
  twice per turn (compaction + chat service); pass the resolved provider
  through.
- **P3.4 MCP manager reuse.** Cache connected MCP managers across turns
  (keyed by server id + config hash, TTL) so uncached servers don't pay the
  connect handshake on every message; today only tool *definitions* are
  cached.
- **P3.5 Split-session parallelism.** MCP connect and custom-tool loading are
  forced sequential by AsyncSession safety; use a second read-only session to
  overlap them.

### Phase 4 — behavioural guards

- **P4.1 Consecutive-tool-failure brake.** The incident turn ground through
  failing Metabase SQL for 30+ minutes. Add a per-turn cap on consecutive
  failed calls to the SAME tool (e.g. 4) that forces the wind-down path with
  an honest "I could not get this working" answer instead of burning the
  full iteration budget. A result the tool marks `ToolResult.outcome_failure`
  (the call ran and its work came out rejected: a paid video or music
  generation the provider refused or a check of the platform failed) counts
  per CALL - the tool and its canonical arguments - instead: a retry the
  model fixed (another prompt, another model) starts its own count, the same
  call failing four times still brakes, and no tool-wide cooldown applies.
  Their cumulative backstop: eight such results of one tool in a row,
  whatever their arguments, with no success of it in between, wind the turn
  down the same way, the nudge saying how many paid attempts were rejected -
  a model rewording each generation a little is never left to spend the
  whole iteration budget.
- **P4.2 Turn-budget surfacing.** When a turn crosses ~50% of its wall-clock
  budget, inject a budget-warning nudge (the `_emit_budget_warning` channel
  already exists) so the model starts consolidating instead of exploring.
- **P4.3 Compaction-state hygiene.** Clear/rebase
  `conversations.compaction_state_json` if messages covered by the watermark
  are ever deleted (message-purge admin tooling), and surface the compaction
  summary in the conversation detail API for transparency.

### Non-goals

- No new system-prompt text for any of this (invariant: prompt inflation is
  banned; all fixes are deterministic code).
- No second migration lock / no schema changes beyond the compaction-state
  column added in this PR.

---

## 4. Operational runbook (what to look for in logs)

| Signal | Meaning |
|--------|---------|
| `Agent loop still running: iteration N/M, elapsed Xs...` (WARNING, every 5 min) | Turn is alive and grinding — not stuck. Check iteration growth between heartbeats. |
| `Agent chat turn completed after Xs` / `General chat turn finished after Xs` | Slow turn finished; pair with the heartbeats to reconstruct duration. |
| `Chat stream send timed out ... (zombie client?)` | Client-side network death mid-turn; answer still persisted. |
| `Chat turn cancelled mid-run` | Teardown killed the turn for another reason than the graceful shutdown timeout; the turn's final assistant message was not persisted (intermediate steering segments committed earlier may exist). Its checkpointed tool work is resumed by the next turn (section 5). |
| `Chat turn of conversation <id> cut by the graceful shutdown timeout (worker stop cause: <cause>); storing its partial reply` | uvicorn's bounded drain cancelled the turn; it stores what it did as its reply and its memory (section 7). The cause is `stop`, `recycle` or `unknown` (section 8). |
| `Web worker (pid N) was asked to stop by SIGTERM ... while it runs N chat turn(s) [...]` | A deployment, a scale-in, a node drain or a restart: the turns named have `GRACEFUL_TIMEOUT` left (section 8). |
| `Web worker (pid N) served its N requests (MAX_REQUESTS ...)` / `recycles now` / `recycles with agent work still running` | The worker's recycle: due, done at an idle moment, or forced past `MAX_REQUESTS_DEFER_SECONDS` (section 8). The pod stays up. |
| `web worker pid N of this launch stopped without shutting down` | A sibling worker was SIGKILLed: by the kernel's out-of-memory killer when the line says the `oom_kill` count rose, else by uvicorn's supervisor after a missed health check (section 8). |
| `Shutdown: cancelling N background agent turn(s) still running` | Parent wakes or channel turns outlived the drain; they close as interrupted (section 8). |
| `Shutdown finalizer <name> failed` / `was cancelled before it finished` | A cut turn's reply, memory or checkpoint write did not complete; the turn is left as section 6 describes. |
| `Shutdown: N request(s), background turn(s) or finalizer(s) still running after 15s` | The lifespan stopped waiting for the cut requests and their writes (`SHUTDOWN_DRAIN_SECONDS`); the process exits without them. |
| `Conversation <id> resumes N tool call(s) of turn <t>, which ended before it stored its answer` | A previous turn died (or failed) after doing tool work; this turn replays it instead of calling the tools again (section 5). |
| `Could not checkpoint the turn of conversation <id>` | The checkpoint write failed (once per turn); a crash of that turn would lose its work, the turn itself is unaffected. |
| `Chat connection cancelled during turn finalization` | Teardown hit AFTER the loop returned (persistence / title / done send); check surrounding log context for whether the assistant message was persisted. |
| `Chat stream closed before the done frame could be delivered` | Answer persisted; user will see it on reload. |
| `MCP server '...' circuit breaker open` / connect timeout | MCP endpoint down or hanging; tools skipped for the run. |
| `Persisted conversation compaction state for <conv>` | Compaction cycle completed and stored; next turns reuse it. |
| `Compactor returned non-JSON output` | Compaction degraded (prior directives preserved); investigate the model. |

---

## 5. The work of a turn that never stores its answer

A turn surviving its client connection and being visible everywhere
does not make it survive its PROCESS. The tool calls a turn made and the
results they returned live in the agent loop's memory; the answer, and a
mid-turn segment, are all it stores. A turn killed after ten minutes
of tool work (crash, OOM kill, restart, deploy) ends as it should - the relay
closes it on every viewer with `reason: "interrupted"` / `"lost"` - but the
next turn ("you crashed, go again") reads a history with none of the work
and calls every tool again.

`services/turn_checkpoint.py` keeps that work durable:

- **Capture.** `run_agent_loop(on_checkpoint=...)` hands the callback the
  messages it appended (everything not in the caller's list, by identity) at
  the top of every iteration and before the forced final answer: the
  boundaries where every tool call has its result. Synchronous, no I/O in the
  loop, a raising callback logged once and ignored.
- **What is kept.** `completed_work()` keeps completed tool rounds only (an
  assistant message whose every call has its result, plus assistant text
  between rounds); a round still waiting, a round with duplicated call ids,
  system nudges and steering messages (stored as user messages already) are
  left out. Bounded by `MAX_CHECKPOINT_CHARS`, oldest rounds first. Files a
  result carried are not kept: its text (with its `[[FILE:id]]` marker) is.
- **Store.** `TurnCheckpoint.offer()` keeps the latest snapshot and starts a
  writer task (created from the loop's context, so the JSON column's
  serializer stores a secret variable value as its marker) that
  upserts it into `chat_turn_checkpoints` (one row per conversation, FK
  cascade) on a session of its own. Latest wins; an offer that adds no round
  writes nothing.
- **End.** Before storing the answer the turn closes its writer
  (`aclose()`, bounded; a write still running is cancelled). A turn that
  stores its answer deletes the row in the same transaction, in a SAVEPOINT
  (`discard()`), after `store_message` so its rollback-and-retry cannot undo
  the deletion. A turn that ended on an error closes with `flush=True` and
  keeps the row. A turn that dies never gets there: the row stays.
- **Resume.** The next web chat turn (streamed and REST, both under the turn
  lock) reads the row (`load_resumable()`, younger than `RESUME_MAX_AGE`),
  re-checks the stored rounds, and appends them to its history right before
  the new message - after the conversation compaction, whose watermark is
  keyed on stored rows. The model receives the assistant tool calls and the
  tool results as they happened, so it continues instead of redoing them; no
  prompt text is added (invariant 16). The loop's tool gate preloads the
  groups those calls used. The resuming turn's writer carries the
  resumed rounds in front of its own, so a second crash loses nothing.

Not covered: a single long model call with no tool call (the reasoning lives
at the provider), parent wakes, channel turns, the platform chat API (a non-streaming call is
answered 503 `platform_restarting` instead, section 11) and the
mentioned agents of a multi-agent turn. The interrupted turn's activity cards
are not shown again.

---

## 6. A conversation is never left silent

Sections 1 to 5 keep a turn alive, visible and its work durable. What they
did not do is SAY anything when a turn ends without its answer, or when the
conversation waits on work that will resume it. The audit found five ways a
conversation went quiet with nothing on screen:

| # | Path | What the person saw | Fix |
|---|------|---------------------|-----|
| 1 | A turn killed by a restart, a deploy or a crash | The relay's `error` (`reason: interrupted` / `lost`) cleared "Thinking" and showed nothing; the reconnected client's poll found the lock gone and settled silently; after a reload, a finished-looking conversation | The turn's record (below) and `interrupted_turn` on `GET /chat/conversations/{id}`, rendered at the end of the transcript with `Resume` / `Dismiss` |
| 2 | A send no turn took (rate limit, empty message, refused tool selection, a conversation that cannot be continued) | "Thinking" cleared, the echo stayed, no reason | The send is answered with an HTTP error, and the panel writes its `detail` in place of the answer |
| 3 | The chat connection gave up after 8 reconnects (~2.5 min, shorter than a deploy) | A wake turn started meanwhile never reached the page; a message queued while hidden was dropped | The panel's events stream reconnects without a limit (1 s to 30 s backoff); a page hidden for 60 s lets it go, and shown again reopens it and reads the conversation back |
| 4 | A turn that handed a Video Studio job to the background ended normally | A finished-looking conversation until the wake | `GET /chat/conversations/{id}/awaited-work` (`services/conversation_awaited_work.py`, read through `jobs.job_state`) behind `AwaitedWorkBanner` |
| 5 | A Video Studio wake whose worker stopped during the grace | The jobs stayed owed for ever, the conversation never resumed | `video_studio.wake.sweep_lost_wakes()` (`VideoStudioWakeSweepAdapter.periodic`, every 5 min per worker): a conversation still owed jobs that ended over 2 min ago, with none in flight, is claimed (atomic, so a late wake and a sweep never both tell it) and resumed |

**The turn's record** (`chat_turn_guard`, Redis `chat:turn:open:{conv}`,
`OPEN_TURN_TTL_SECONDS` = the checkpoint's 24 h resume window). Every web
turn that takes the lock writes `{token, started_at}`; channel turns do not
(they answer on their channel). Its release removes the record BEFORE the
lock goes, while no other turn can write it, unless the record was marked
interrupted: `TurnPublisher.aclose(terminal=interrupted_frame())`, the exit of
every turn that never reached its own end (streamed send, REST, parent wake,
channel continuation), calls `mark_turn_interrupted()` first. A process that
dies leaves the record as it is, and its lock lapses
(`TURN_MARKER_TTL_SECONDS`). A record whose token no longer holds the lock is
the interrupted turn (`interrupted_turn()`); the next turn's acquisition
replaces it, `DELETE /chat/conversations/{id}/interrupted-turn` forgets it.
Plain GET / SET / DEL, no script: only the lock holder writes it. Best-effort
like the checkpoint: a record that cannot be written costs the notice, never
the turn.

**Resume** is an ordinary turn: the client sends `Continue where you left
off.` as the user's message, and that turn resumes the checkpointed tool
work (section 5) - no hidden prompt, no new turn type. Any other message does
the same. `tool_calls_kept` (`turn_checkpoint.kept_tool_calls()`, the count
`load_resumable()` would hand that turn, read without its log) tells the
person what is kept.

**Automatic resume of a platform stop.** A turn the platform itself
cut (worker stop cause `stop` or `recycle`) resumes with no click: section 11.
A turn whose process died for another reason (an out-of-memory tool result, a
crash the turn caused) keeps the one click above: resuming it could loop.

---

## 7. A turn cut by the graceful shutdown timeout

A worker recycle (`MAX_REQUESTS`), a rolling deploy or a restart drains the
process for at most `GRACEFUL_TIMEOUT`, then uvicorn cancels the requests still
running (`"Task cancelled, timeout graceful shutdown exceeded"`) and sends the
lifespan shutdown at once, without awaiting them. A chat turn in the middle of
a tool call ended there with its answer unstored (section 6's notice was all
the person saw) and its memory unwritten (`run_agent_chat` records the
interaction only once the loop returns). Section 5 keeps the completed rounds
for the next turn's model, nothing the person reads.

On that cancellation, and only that one (`is_graceful_shutdown_cancellation`):

- **The ledger.** `services/interrupted_turn.TurnProgress` folds the loop's
  events: the calls `tool_start` announced, the calls `tool_done` returned
  (`tool_trace_entry()`, with their outcome), and so the calls still running (a
  name multiset, since events carry no call id). The streamed turn's own
  tracking is this ledger (`_turn_progress`, reset with the rest of the tracking
  whenever a message is stored, so it describes the work since the last stored
  message); the REST turn observes the conversation agent's run and the
  mentioned agents'.
- **The reply.** The streamed and REST turns store `interrupted_reply()` (the
  completed calls grouped by tool with failures marked, the calls still
  running, the last completed action) with the tool trace in its metadata and
  `interrupted: true` (`INTERRUPTED_META_KEY`, served as `MessageResponse.interrupted`;
  the reasoning details icon turns into a warning). A REST turn cut while the
  agents its message mentions answer keeps the conversation agent's answer in
  front. The reply is stored BEFORE the turn's lock goes and its publisher
  closes with the interrupted terminal, so a viewer told the turn was
  interrupted reads it back. The checkpoint is flushed (`aclose(flush=True)`)
  and kept: section 6's notice still offers Resume.
- **The memory.** `run_agent_chat` records `User asked: ... -> ` +
  `interrupted_memory_note()` through `_store_chat_memories()`, the function
  its normal end uses: the same tiers, scope and switches, no consolidation
  (a model call), nothing in an evaluation. The ledger reads the event chain
  the turn already has; none is installed for it alone, so a loop without a
  sink runs as before and its note names no tool. A turn that later answers
  the same message (its resume) writes its reply in place of that note in
  both tiers: `_store_chat_memories(replaces_prefix=...)` with
  `interrupted_memory_prefix()` removes the entries that start with the
  question and the interrupted lead, so the recent activity never keeps
  "Interrupted" for an answered turn; an earlier real answer to the same
  question stays.
- **The drain.** Both writes are shutdown finalizers
  (`core/shutdown_finalizers.start_shutdown_finalizer()`): tasks of their own
  on sessions of their own, so the request's cancellation never reaches them.
  `GracefulShutdownDrainMiddleware` counts every HTTP and WebSocket request in
  flight (`track_request_task()`); the lifespan shutdown first awaits
  `drain_shutdown_finalizers()`, the cut requests and every finalizer they
  start, for at most `SHUTDOWN_DRAIN_SECONDS` (15 s), before Redis, the queue
  and the database engine go and before `asyncio.run` cancels what is left. A
  cut turn waits for its reply for `INTERRUPTED_REPLY_WAIT_SECONDS` (10 s)
  before it releases its lock; past it, the drain still waits for the write.
- **Reads at the stop.** A GET or HEAD that has not started its answer when
  the worker is asked to stop (`worker_shutdown.on_stop`) is cut
  `STOP_READ_GRACE_SECONDS` (5 s) later with `STOP_READ_CANCEL_MESSAGE`,
  which carries the drain hint, so the middleware answers it 503 +
  `Retry-After: 1` and the client retries on a live pod instead of waiting
  for the whole `GRACEFUL_TIMEOUT`. Writes, started answers (SSE) and agent
  turns keep the full drain (#2090).

A client disconnect, a Stop and a cancellation without uvicorn's drain message
store nothing, as before. Not covered: a turn cut in its persistence phase
(after the loop returned, before its answer committed); a process killed
(`SIGKILL`, out of memory); the reply of a platform chat API turn, whose memory
is recorded (it runs `run_agent_chat`) but whose stream stores no partial
reply. Background turns (parent wakes, channel turns) store no reply: section 8
ends them with the drain's message, so they close as interrupted and record
their memory note.

---

## 8. What interrupts a healthy turn, and why it looked like a restart

Sections 5 to 7 store what a cut turn did. This audit asked what cuts a turn
whose model and tools are healthy, and why "This turn was interrupted by a
server restart" appeared while no pod restarted, no container was OOM-killed
and nothing was logged. That reply is stored only on uvicorn's drain
cancellation, so a uvicorn server inside the pod really did shut down: a web
worker process, not the pod.

| # | Cause | Seen by Kubernetes | Before | Now |
|---|---|---|---|---|
| 1 | The `MAX_REQUESTS` worker recycle (10,000 to 15,000 requests per worker, probes and polling included): uvicorn closed the worker's WebSockets (1012), waited `GRACEFUL_TIMEOUT` (120 s) and cancelled the turns running in it | No: the pod stays up | "a server restart"; WARNING lines calling it "routine hygiene, NOT a crash" | The recycle is the app's (`core/worker_recycle.py`): a worker past its quota keeps serving and recycles at the first moment no agent turn runs in it, at most `MAX_REQUESTS_DEFER_SECONDS` (4 h) later |
| 2 | A stop signal: rolling deploy, scale-in, node drain, eviction, `rollout restart` | Pod replaced | "a server restart", no conversation named | The stop is recorded before uvicorn's handler sees it; a WARNING names the running turns and the grace they have; the reply names the cause |
| 3 | SIGKILL before the drain ends: `terminationGracePeriodSeconds` (Kubernetes default 30 s) below `GRACEFUL_TIMEOUT` + `SHUTDOWN_DRAIN_SECONDS` | Pod replaced | No reply; the notice once the lock lapses (up to 300 s) | Documented (`docs/deployment/installation.md`) and said by the stop WARNING; the chart change is a deployment follow-up |
| 4 | uvicorn's supervisor SIGKILLs a worker that misses its 5 s health-check ping (its GIL held by a long C call) | No | An INFO "Child process [pid] died" in the supervisor's plain-text output | The replacement reports it at WARNING (`core/worker_exits.py`) |
| 5 | The kernel's OOM killer kills one worker: the container survives (its main process is the supervisor), so no `OOMKilled` status | No | Nothing | The replacement's WARNING reads the container's `oom_kill` count |
| 6 | Background turns (parent wakes, channel turns) are bare asyncio tasks no drain waited for; `asyncio.run` cancelled them after Redis and the database were closed | -- | Nothing, not even their interrupted terminal | Tracked (`track_background_turn()`), given the requests' grace, then cancelled with the drain's message while Redis and the database are up |
| 7 | The turn lock (300 s TTL, refreshed every 20 s) lapses while the loop runs: refresh failures logged at DEBUG | -- | A viewer may be told the turn ended | Not changed: a follow-up |

Not interruptions, and so not in this audit: Stop and the emergency stop (the
person asked), the chat loop timeout (a visible "took too long" answer), a
chat stream that closed (the turn survives its stream, section 1 and #3712),
the iteration and budget limits (a forced final answer).

- **What stopped the worker** (`core/worker_shutdown.py`). `note_shutdown()`
  records the first request: `RECYCLE` (the recycle calls it before it signals
  itself) or `STOP` (the chained SIGTERM / SIGINT handler,
  `install_stop_signal_hook()`, main thread only, logging scheduled on the loop
  rather than done in the handler). `cut_cause()` is read by the cut reply and
  memory note (`interrupted_turn.cut_cause_text()`) and stored in the message
  metadata (`interrupted_cause`); `None` (a worker uvicorn stopped for a reason
  the app never saw) says only that the process shut down.
- **The turns a worker runs.** Every turn that takes a conversation's lock
  counts from `acquire_turn()` to its release (`turn_started()` /
  `turn_ended()` in `chat_turn_guard`): streamed, REST, parent wake and channel
  continuation turns alike. `agent_work_in_flight()` adds the background turns
  `core/shutdown_finalizers` tracks (`parent_wake._spawn_wake`,
  `channels/base.spawn_channel_task`).
- **The recycle.** `start-uvicorn.sh` no longer passes `--limit-max-requests`;
  it exports the validated quota, jitter and deferral (`XTM_ONE_RECYCLE_*`) and
  the graceful timeout (`XTM_ONE_GRACEFUL_TIMEOUT`). The outermost middleware
  counts every HTTP request, as uvicorn did; once due, a watcher checks every
  second and sends the worker SIGTERM when nothing runs in it (or past the
  deferral): uvicorn's own graceful shutdown, then the supervisor's
  replacement, exactly as after uvicorn's recycle. Started without the
  launcher, a worker recycles nothing.
- **The background turns at shutdown.** `drain_shutdown_finalizers()` waits for
  them until `background_grace_deadline()` (the stop request plus
  `GRACEFUL_TIMEOUT`), then cancels the rest with
  `BACKGROUND_TURN_CANCEL_MESSAGE`, which `is_graceful_shutdown_cancellation`
  recognises: `run_agent_chat` records its memory note and the turn's publisher
  closes with the interrupted terminal, while Redis and the database are up.
- **A killed sibling** (`core/worker_exits.py`). Every worker of a launch
  writes `<temp>/xtm-one-workers/<launch id>/<pid>` (its `/proc` start time and
  the `oom_kill` count) and removes it when its clean shutdown starts. A worker
  that starts reports every sibling record whose process is gone, then removes
  it; an earlier launch's directory is removed unread (Kubernetes reports a
  container restart itself).

- **One hand-back per rollout.** Work a stopping worker hands back to the head
  of the queue (an assignment run, a background task, a scheduled work item:
  `AdapterResult.defer(place="head")`) is held for a running worker of the
  incoming release that claims its adapter (`orchestration_reconciler._to_incoming_release`,
  `worker_presence.incoming_worker`, `claim_for` for
  `HANDBACK_INCOMING_HOLD_SECONDS`, 90 s), any worker after. Before, the next
  claimer was often another worker of the outgoing release, stopped by the same
  rolling update a minute later: one run was handed back 9 times in 40 minutes
  (#8819). No incoming worker yet (the new release not up, or a plain
  restart): the hand-back is kept from the workers of the stopping one's own
  release (`not_release`, `engine.NOT_RELEASE_PAYLOAD_KEY` `{version, until}`)
  for `HANDBACK_OUTGOING_HOLD_SECONDS` (300 s), a worker of any other release
  claiming it at once and any worker after (#8958: a render claimed by another
  outgoing worker sat at 23 % through two rollouts). A plain restart, whose new
  workers run the same release, waits at most that long. Redis unreadable or a
  release that names no version: unchanged. Video Studio renders torn down as
  their worker stops take the same two holds (`incoming_hand`, `_hand_back`),
  and `get_video`'s renders line names the frames each relaunch kept and the
  relaunches handed on before their drawing resumed (`relaunch_line`).
  A relaunched render gets back the time the stop cost it: from its last
  progress before the stop to the start of its next attempt, at most
  `RELAUNCH_CREDIT_MOST_SECONDS` (1 h) per relaunch, is added to its
  execution's deadline (`render_process.give_relaunch_time_back`,
  `orchestration_engine.give_deadline_back`) and kept on the relaunch entry
  (`lost_seconds`, `given_back_seconds`). A whole render drawing at a measured
  pace moves its deadline out to the end that pace gives, plus
  `ETA_MARGIN_SECONDS` (10 min), every `ETA_EXTEND_EVERY_SECONDS` (5 min)
  (`PaceNote`, `render_ranges.extend_to_eta`). Both are bounded together by one
  more `wall_seconds(quality)` past the deadline it was queued with
  (`DEADLINE_FIRST_KEY` in the execution payload), so a render handed on by
  every deploy never runs forever. A render that still times out says how many
  relaunches it had and what they cost (`relaunch_cost_words`, in
  `runners.dead_letter`). Before (#11782), a 9,061-frame render relaunched by
  3 deploys timed out at 73 % with 17 min of drawing left.

**Still open.** A turn longer than any grace period is cut by a deployment
whatever the grace: the fix is to run chat and assignment loops as leased jobs
in the worker tier, checkpointed after every tool result and resumed on
another worker, the conversation continuing instead of showing "interrupted".
Until then, the follow-ups are the deployment's grace period and rollout
strategy, the cut's cause on the run's LLM trace, the ids of the jobs still
queued in the resume notice, and lock refresh failures at WARNING.

---

## 9. Resuming with the results, not their names

Observed: four turns in a row were cut by restarts after 26 to 83 completed
tool calls, and each next turn received the names of the calls (the
interrupted reply of section 7) and none of their results. The cause was
section 5's bound: `bounded()` keeps `MAX_CHECKPOINT_CHARS` by dropping whole
rounds, oldest first, so ONE round larger than the bound (a code cell carrying
twenty pasted files, its result) dropped every round.

- **Per-result bound.** `turn_checkpoint._spill()` cuts a tool result past
  `MAX_REPLAYED_RESULT_CHARS` (20,000) to its first `REPLAY_HEAD_CHARS`
  (12,000) with a note, and a call's arguments past
  `MAX_REPLAYED_ARGUMENT_CHARS` have their longest strings cut (still valid
  arguments for the provider). `bounded()` runs on what is left, so no round
  is pushed out by another one's size.
- **Kept whole, when it matters.** An ordinary write keeps the cut text only:
  a turn that ends normally creates no file. A flushing close
  (`aclose(flush=True)`: the graceful cut of section 7, a turn that ended on an
  error) writes the latest snapshot once more and keeps each cut result whole
  as a conversation file (`FileAttachment.source = "agent_checkpoint"`); the
  note then names the file id, `read_file_content` and the `attachments/`
  path. A process killed without a flush keeps the cut text.
- **The resume note.** `turn_checkpoint.load_replay()` returns the replayed
  rounds followed by `resume_note()`: an assistant message, built by code,
  stating how many calls are replayed, the last completed call and its
  arguments, the results cut and where each is whole, and the manifest of the
  conversation's sandbox workspace. Facts only (invariant 16); the streamed
  and REST turns append it after the rounds.
- **The graceful stop.** Once a stop or a recycle was asked and less than
  `WIND_DOWN_MARGIN_SECONDS` (60 s, at most half of `GRACEFUL_TIMEOUT`) of the
  grace is left - at once when the grace is unknown - a loop with a
  checkpoint stops at its next iteration boundary (`worker_shutdown.wind_down_due`,
  checked right after the checkpoint is offered): the call in flight finished,
  no new model call started. It raises `TurnWoundDown`, a `CancelledError`
  carrying the graceful shutdown's message, so every handler of section 7 takes
  it as a cut: the interrupted reply, the memory note, the flushed checkpoint,
  the interrupted terminal. A loop without a checkpoint runs on to the end of
  the grace, as before.
- **`interrupted_resumable`.** The stored reply carries `interrupted_state`
  (`interrupted_resumable` when the checkpoint holds work, else
  `interrupted`), and `interrupted_turn.state` on
  `GET /chat/conversations/{id}` says the same from the checkpoint the next
  turn would resume. Resuming stays the person's decision (section 6).
- **The workspace.** The files a cut turn's code interpreter calls saved are in
  the conversation's sandbox workspace (`dev-docs/reference/sandbox-workspace.md`),
  committed call by call, so the resumed turn finds them as the interrupted one
  left them.

- **The calls in flight.** A round dispatched and not all answered is no work
  to replay, yet its calls may have reached their service (a pull request
  created, a message sent) before the attempt ended. The loop offers the
  checkpoint right after it appends an assistant message with tool calls,
  before it runs them, and `TurnCheckpoint.offer` stores the calls of the
  trailing unanswered round (`turn_checkpoint.in_flight_calls`) as one entry
  of role `in_flight` (`IN_FLIGHT_ROLE`) after the completed rounds. Every
  reader of the rounds (`_messages`, the completed-call meter) leaves it out;
  `load_resumable` and `background_checkpoint.load_task_replay` read it into
  `ResumedWork.in_flight`, and `resume_note()` names each call and its
  arguments as started, outcome not recorded. Observed: a run cut while
  `create_github_pr` ran resumed without it and created the pull request a
  second time ("A pull request already exists").

- **A repeated creation answers what exists.** Whatever the guard saw, a
  `create_github_pr` that GitHub refuses with 422 "A pull request already
  exists" reads the open pull request of the same head and base and answers
  it as a success, "already open, nothing created", binding `work_item_id` to
  it as a creation would (`pulls._open_pull_for`); a `create_github_branch`
  refused with "Reference already exists" answers success when the branch
  stands at the SHA the call would have used, and stays refused, both SHAs
  named, when it stands elsewhere (`branches._existing_branch_answer`). The
  read follows the 422 only: no extra request on the normal path (#12049).
  `create_github_issue` and `add_github_comment` keep the landed probes below
  and `dedupe_first_line`.

- **Every resume keeps its note.** The resume note is appended to
  `ResumedWork.entries` (`ResumedWork.with_note`, called by `load_replay` and
  `load_task_replay`), the inherited part of the resuming attempt's own
  checkpoint: a later resume of the same turn replays every earlier note, its
  in-flight line included, and the run transcript shows them all. Before, each
  checkpoint started from the replayed rounds alone, so a run resumed six
  times kept only its last note.

- **A write in flight is not repeated unseen.** Each in-flight call carries
  `digest`, `call_digest(name, arguments)` over the whole arguments (the stored
  arguments are cut). The note message carries the calls in
  `ChatMessage.in_flight` (never sent, never stored); `run_agent_loop` reads
  them with `in_flight_writes()` (calls the shared `classify_tool_name` reads
  as reads are left out: a read may run again) and answers the first identical
  repeat of each write without running it (`not_run="in_flight_repeat"`,
  `in_flight_repeat_refusal()`): read the state first, then make the same call
  again and it runs. Observed: a resumed run re-ran an in-flight
  `sandbox_edit_file` that had landed ("find not found").

- **A replay-safe write runs at once.** A tool declaring
  `ToolDefinition.replay_safe` answers or replaces, by itself, the job an
  identical call started: `render_video` reuses the render of the same
  revision and quality queued or running, `render_video_scene` supersedes the
  preview of the same scene. `in_flight_writes(messages, replay_safe=...)`
  leaves its calls out, so the first repeat runs. Observed: across nine
  rollouts a resumed run's `render_video_scene`, whose job never queued, was
  refused on every resume until an argument changed (issue #8819).
  `merge_github_branch` is replay-safe too: its repeat drops the alignment
  commits a stopped call left, answers a merge already landed, and writes the
  resolution chunks a call stopped at `Resolve the merge of <ref>: N files
  (k/n)` left unwritten (`finish_stopped_resolution`); a replay whose
  resolutions cannot be checked by blob id (`keep` only) over such a tip is an
  error naming the stopped commit, never "No change". Observed: a rollout cut a
  call after `(1/2)`, the resumed turn refused the repeat, and the branch read
  mergeable with two locale catalogs missing the PR's keys (issue #1205).

- **A declared read runs at once.** The name classifier fails safe to a
  write on a verb it cannot read (`compare`, `inspect`) and on a read whose
  name carries a write token (`sandbox_run_command` with a `run_id` only
  waits for a running command). A tool declares what it is instead:
  `ToolDefinition.read_only` (every call reads: `compare_github_commits`,
  `inspect_my_traces`, `sandbox_grep`, `sandbox_outline`,
  `sandbox_checkout_diff`, `sandbox_service_logs`) or
  `ToolDefinition.read_only_call`, a predicate on one call's arguments
  (`sandbox_run_command`: `run_id` and no `stop`; `sandbox_diff_files`: no
  `write_merge_to`). `in_flight_writes(..., read_only=...)` leaves the first
  out; the loop's guard lets through any call `call_is_read_only()` answers
  `True` for, on the live call's whole arguments (a predicate that raises
  reads as a write). The shared classifier, which backs the read badge and
  the approval whitelists, is unchanged. Observed: after a resume, a
  `compare_github_commits` and a `sandbox_run_command(run_id=...)` were
  refused as in-flight writes until an argument changed (issue #8851).

- **A sandbox write the cut segment started is told landed or not.**
  Before the note is written (`load_replay`, `load_task_replay`),
  `sandbox_workspace.landed.judge_for_resume` reads the workspace for each
  unanswered `sandbox_write_file` (landed when the path holds the content's
  sha256, not landed when the path is absent) and `sandbox_edit_file`
  (landed when each replacement is in the file and its find text gone, not
  landed when every find text is still there). The verdict is stored on the
  in-flight call (`landed`, `outcome`), named in the note ("it landed
  (workspace/co/a.py holds its change): do not repeat it" or "it did not
  land ...: repeat it as it was") and kept by the next checkpoint. A write
  told not landed runs on its first repeat; a landed one stays guarded. A
  single `insert_after_line` or `start_line`/`end_line` edit is told from
  the lines at its number (the block is there: landed; the anchor line still
  holds its expected text without it: not landed). Line-numbered edits
  combined in one call, an argument the replay cut, a file changed since or
  a storage error give no verdict: the note keeps "may have taken effect".
  Observed: a landed `sandbox_edit_file` was re-authored under new wording
  and duplicated a paragraph (issue #8851, part B).
- **A reworded sandbox edit at the same anchor is answered "already applied".**
  `sandbox_edit_file` declares the same verdict as its `landed_probe`
  (`landed.edit_probe`, read from the file) and a `landed_key`
  (`landed.edit_key`: the path and each edit's anchor, the find text or the
  line and its expected text, never the text written). An insertion keeps
  its find text, so a reworded repeat missed the digest and applied again;
  it now meets the interrupted call on its key and is answered with
  "already applied: workspace/<path> already holds every edit of that
  call" once; an edit at another anchor runs. `sandbox_replace_in_files`
  declares a probe (every match already starts its expansion) and a key
  (scope and pattern), and its `dry_run` is a read. Observed: across two
  resumes a reworded insertion landed in each attempt and left three copies
  of one bullet (issue #12097).
- **A GitHub write the cut segment started is read back at the resume and at its repeat.**
  `create_github_issue`, `create_github_commit`, `create_github_pr`,
  `create_github_branch` and `add_github_comment` declare a
  `ToolDefinition.landed_probe` (`integrations/providers/github/landed.py`),
  built with the tool's own credential: an issue of the repository with the
  same title opened by the account in the last two hours; a commit on the
  branch (the default branch when none is named) with the same subject line,
  authored in the last two hours; a pull request whose head is the call's
  branch, opened by the account in the last two hours; the branch itself; a
  comment of the account on the issue with the same body, in the last two
  hours. An argument the replay cut gives no verdict. At the start of the
  resumed turn, before its first model call, the loop probes every in-flight
  write that declares one on the interrupted call's own arguments, and a
  landed write is named on the resume note ("Read back since:
  create_github_issue landed: issue #N (...). Use each as that call's
  outcome; do not create it again."); the verdict is reused by the guard.
  The writes but the commit also declare a `ToolDefinition.landed_key`, the
  object a call creates whatever its wording (an issue's repository, a pull
  request's repository and head branch, a branch, a comment's issue): a
  REWORDED repeat on the same key of a write that landed is answered once with
  what landed ("... reworded ... and it landed"); a reworded repeat of a write
  that did not land, or that the probe cannot tell, runs. Observed: a resumed
  turn reworded the title of an interrupted `create_github_issue`, missed the
  digest guard and created a duplicate (xtm-one #11833 of #11832, issue
  #11847). The note names a cut result's conversation file only when the
  resuming user can open it (`turn_checkpoint.readable_spills`). When the resumed turn repeats the call as
  it was, the in-flight repeat guard calls `probe_landed` (15 s bound): a
  landed write is answered with what landed (`not_run="in_flight_landed"`,
  "... that call landed: issue #8833 (...). Use it as this call's outcome; do
  not create it a second time."), a write the read shows absent runs on the
  first repeat, and a probe that fails or cannot tell holds the repeat back
  once as before. Observed: a repeated `create_github_issue` created #8834,
  a duplicate of #8833 (issue #8851, part B).
- **A sandbox check is answered for the tree it ran on, by its rerun when
  lost.** `sandbox_workspace/jobs.landed` answers a repeated
  `sandbox_run_command` from the job's saved run only when no file of its
  checkout changed after that run's `finished_at`; an edit since makes the
  repeat run. A run lost with its container and started again once
  (`commands._rerun_lost`) leaves a link in `kernel_state["reruns"]`, so a
  later poll of the lost `run_id` is answered by the rerun. A stop or a
  timeout ends the command's whole process tree (the `remote.py` helper reads
  `/proc` before the kill), and a process still alive afterwards is named in
  the end marker (`survivors`): the answer names it and the container is
  discarded, never a clean stop over a live process.
- **A recording the cut segment started is named by the note.**
  `record_browser_demo` stores its take, then waits minutes for it: a stop in
  that wait left a recording the resumed turn could not see.
  `video_studio/resume_landed.py` (`judge_for_resume`, called by
  `turn_checkpoint.load_replay` and `background_checkpoint.load_task_replay`)
  reads the user's recordings of the same conversation (none for an
  assignment run) created within `SAME_TAKE_WINDOW_SECONDS` and matches the
  call's name (its default, or the re-recorded name of a `from_recording_id`
  call): the note says "it created recording <id> (running, created ...):
  get_video_recording(...) reads it; do not record it again", or that it
  created none. A name the replay cut or a read error gives no verdict. The
  tool is `replay_safe`: its identical repeat runs, and `same_take` answers a
  queued or recording take with the same submitted script whatever its name
  (a finished take only under its own name, so a new take stays possible).
  Observed: a retry under another name queued a second take that had to be
  cancelled (issue #10073).
- **A call in flight at two consecutive cuts is never run again unchanged.**
  The in-flight repeat guard lets a read run again, which is right for a read
  that a stop happened to interrupt and wrong for a read that is the stop: a
  43 MB job log parsed on the event loop took its worker down, every resume
  read it again, and a chat turn was cut four times without answering (issue
  #9078, after #9074). Each resume note now carries `in_flight_seen`
  (`{name, digest, cuts}` per unanswered in-flight call, stored with the note
  in the checkpoint): a call the previous note names with the same digest
  counts one more cut. From `POISON_CUTS` (2) on, the note says so and the
  loop answers every identical repeat in the turn unrun
  (`not_run="in_flight_poison"`), read or write, before the write guard. The
  same tool with other arguments (a window, `failures_only`) runs. Log line:
  `In-flight poison guard: '<tool>' ... was in flight at N consecutive cuts`.
  A tool declared `cut_safe` (`ToolDefinition.cut_safe`) is left out: its work
  runs outside the worker and an identical repeat answers the job the first
  call started, so a long call of it is the rollout's victim, never its cause.
  `sandbox_start_service` is `cut_safe` and `replay_safe`: a 75 s lab start in
  flight at two drains runs again and answers the held service (ready: its
  service_id; partial: replaced), never an orphan pod (issue #9243).
  `render_video` and `render_video_scene` are `cut_safe` too: the render
  worker draws, and a repeat answers the render of that revision and quality
  in flight (`projects.start_render`) or the one scene preview, so a render
  started before two hand-backs is answered by its id (issue #9280).
  A cut the worker's own stop caused is not counted at all (issue #9912):
  every checkpoint write made once `worker_shutdown.cut_cause()` is set (a
  SIGTERM from the host, a rollout drain, a node drain, the worker's
  recycle) stamps the `in_flight` entry with `cut_by` (`stop` or
  `recycle`), and `_seen_now` adds no cut for a call carrying it. The
  hand-back flush and the drain's close are such writes. A long wait
  (`get_video_jobs`, 180 s) in flight at two rollouts therefore runs again;
  a call in flight at two cuts with no recorded cause (a crash, an OOM kill)
  is still refused.
- **A job log is downloaded once, then answered from the cache.** The
  realistic bound of a first `get_github_job_logs` read is its download: about
  7 s for the 45 MB log of job 127609262804 (the summary itself takes 0.47 s in
  a thread since #9075), so "under 1 s" holds for a repeated read, not for the
  first. A published log never changes (a re-run is a new job id), so the
  answer of each read mode (default, `failures_only`, `tail`) is kept in Redis
  for 24 h, keyed by a digest of the credential, the repository and the job
  (`github:job_log:...`), and a repeated read answers without any request
  (issue #9098). The key also carries a digest of the code that builds the
  answer (`ci_logs`, `playwright_context`, the GitHub actions module), so a
  deploy that changes the extractor never serves an answer cached before it
  (#12194). The whole log for the workspace (`to_workspace_path`) and any
  error are never cached; a Redis that is down only costs the download.

Metrics: `xtmone_turn_resume_results_total{kind="replayed"|"truncated"}`.
Tests: `tests/test_turn_resume_replay.py`, `tests/test_turn_resume_in_flight.py`,
`tests/test_turn_resume_in_flight_repeat.py` (a double resume with a write in flight),
`tests/test_turn_resume_in_flight_poison.py` (a call in flight at two consecutive cuts),
`tests/test_turn_resume_landed_writes.py` (the landed verdict of a sandbox write),
`tests/test_turn_resume_landed_github_writes.py` (the landed probe of a GitHub write),
`tests/test_turn_resume_readback_writes.py` (the read-back at the resume, a reworded repeat, the files the note names),
`tests/test_github_job_logs_cache.py` (a repeated job log read answered from the cache).

---

## 10. A channel turn cut by a platform update is resumed, never silent

Observed: turns cut by rolling deploys within two minutes of each new build.
On the web the cut turn's reply (section 7) and the notice (section 6) say so.
On Slack and Teams nothing was posted: the channel turn stored no reply, its
adapter's `except Exception` never saw the `CancelledError`, and the sender
had to write "Continue where you left off".

**Resumed by the next process.** A Slack or Teams turn on a conversation the
web also reaches holds its lock through `ChannelTurn`. The router's
`_holds_channel_turn` catches the drain's cancellation
(`is_graceful_shutdown_cancellation`: uvicorn's message, the background-turn
message, the wind-down of section 9) and, while the turn still holds the
lock, runs `channel_continuation.hand_on_cut_turn()` as a shutdown finalizer
(bounded by `CUT_HANDOFF_SECONDS`, 10 s; the lifespan drain waits past it):

- the turn's checkpoint is flushed (`ChannelTurn.checkpoint`, the same
  `TurnCheckpoint` the web turns write, offered after every completed round);
- the message goes back at the head of the conversation's queue
  (`requeue_claimed_channel_messages`, which also replaces the claim of a
  queued turn), with `metadata.resumed_after_cut`, its stored files marked
  queued again, and the conversation in the sweep's index;
- the lock is released WITHOUT the hand-over (`release(hand_over=False)`), so
  the process that is stopping never starts it again;
- nothing is posted on the thread: the resume is silent, the sender only
  reads the reply (#10772; `channel_turn_resuming` is no longer posted).

The next process's scheduler sweep (`sweep_channel_queues`) drains the queue;
the router takes the turn over from the drain (`adopt_queued`), so the
redelivery claim (`claim_channel_message`) is not asked again: the same
message id, the same conversation, the same thread. Its history replays the
cut turn's tool work (`turn_checkpoint.load_replay`, section 9), so it resumes
from its last completed tool call. The answered turn deletes the checkpoint
with its reply.

**At most three times, never twice answered.** Not put back, and the sender told
`channel_turn_cut` (send it again):

| Case | Why |
|---|---|
| Already resumed `cut_turn_resume.MAX_AUTO_RESUMES` (3) times, the web turn's cap (`chat:channel_resume:{channel}:{thread}:{message}`, an `INCR` counter living the queue's lifetime) | A cut the turn itself causes (an out-of-memory tool) would loop |
| Stored as a user message without a reply (`_stored_turn` is `False`) | It would be stored twice |
| Carrying files not stored yet | Their platform URLs do not outlive a deploy |

A message whose reply was stored (`_stored_turn` is `True`) is left alone:
answered, nothing more posted. A Stop or a client gone is not a shutdown and
hands nothing on.

**Never silent elsewhere.** Every other cut channel turn (a group thread, a
channel the web does not reach) ends in its adapter: the Slack and Teams
adapters catch the shutdown's `CancelledError` and post `channel_turn_cut` in a
DM or a mention through `channels.base.tell_cut_turn` (a shutdown finalizer,
`CUT_NOTICE_WAIT_SECONDS`), unless the router already handed the turn on
(`metadata.turn_cut_handled`).

**Not done here.** Assignment runs and A2A tasks are not re-enqueued by this
section; the web, REST chat, platform API, parent-wake and agent account
(e-mail) turns are, by section 11. A configurable grace is not in this change. Tests:
`tests/test_channel_turn_cut_resume.py`.
Background tasks (sub-agent delegations) resume from their own checkpoint:
[`background-task-resume.md`](background-task-resume.md).

---

## 11. A web or REST turn cut by a platform stop resumes by itself

Observed: after every rollout the person clicked Resume (section 6) on each
conversation the rollout cut. A platform stop is the platform's doing, not the
turn's, so its turn now resumes with no click
(`services/cut_turn_resume.py`).

**Queued before the lock goes.** `_store_cut_turn` (section 7), on the
WebSocket and REST cut paths, calls `enqueue_cut_turn()` while the cut turn
still holds the conversation, after its interrupted reply and its checkpoint
flush were handed to the shutdown drain. Queued only when the worker recorded
a platform cause (`resumable_cause()`: `core.worker_shutdown.cut_cause()` is
`stop` or `recycle`), the user message had fewer than `MAX_AUTO_RESUMES`
(3) automatic resumes IN A ROW that completed no tool round of their own, and
less than `MAX_AUTO_RESUME_SPAN_SECONDS` (12 h) since the message's first cut
(`CutTurn.first_cut_at`, carried from resume to resume; a record of an earlier
release takes its `cut_at`) with fewer than `MAX_TOTAL_AUTO_RESUMES` (100, the
runaway ceiling) in all (20 in all stopped a progressing turn
after an afternoon of 20 rollouts). A resume cut again
after it completed at least one new tool round (`TurnCheckpoint.advanced`)
made progress: it is queued again with `stalled = 0`, so a long turn crossing
several rollouts in a day of frequent deploys keeps resuming by itself; one
cut with no new round is queued with `stalled + 1`. The record (Redis
`chat:turn:auto-resume:{conv}`, JSON `CutTurn`: user message id, cut turn id,
cause, resumes made, stalled resumes, cut time, entry `web` or `rest`; 24 h,
the checkpoint's resume window; a record of an earlier release, with no
`stalled`, counts its resumes as stalled) and the index
`chat:turn:auto-resume:pending`. The interrupted reply then carries
`metadata.auto_resume: true`. One WARNING per cut, queued or not.

**Drained by the next process.** Step 17 of the scheduler tick
(`autonomous._run_autonomous_tick_inner`, only while agentic AI runs) calls
`sweep_cut_turns(parent_wake.schedule_cut_turn_resume)`. A conversation whose
lock is still held (the cut process draining, or a turn the person started)
waits for the next tick; otherwise the record is taken with `GETDEL`, so two
workers never both resume it, and the resume starts detached
(`_spawn_wake`, tracked by the shutdown drain).

**The same user message, continued.** `parent_wake.resume_cut_turn()` runs a
REST-shaped turn through `_run_parent_wake_turn` with no new message
(`persist_user=False`, invariant 16): its history ends at the stored user
message (`history_before_sequence`), the cut turn's checkpoint is replayed
after it (`turn_checkpoint.load_replay`, section 9), and the turn writes its
own checkpoint (`on_checkpoint`). Viewers read a `turn_resuming` frame
(`resuming_frame()`: cause, resume n of 100) then the stream; the reply carries
`metadata.resumed_from` (`resumed_from_meta()`: cut turn id, cause, cut time,
resume n of 100). The answered turn deletes the checkpoint.

**One card per cut turn in the web chat (issue #8943).** The transcript
draws every cut of a turn as ONE `InterruptedReplyCard`
(`frontend/src/lib/cut-turn-group.ts`), anchored at the first interrupted
reply, whose state follows the stream and the conversation live: resuming by
itself (`interrupted_turn.auto_resume`, or the `turn_resuming` frame, which
also re-reads the conversation so a cut stored while the socket was down
shows at once), resumed by itself (a reply with `resumed_from`), resumed by
you (any user message after the cut, which keeps the group open: the reply
that follows is its resume, never a second marker), could not resume (Resume
and Dismiss on the card itself), not resumed (dismissed: the conversation no
longer serves `interrupted_turn`). A further cut of the same turn, before or
after the person's message, folds into the same card. No resume counter is
shown. A turn in flight after the trailing cut is its resume: the card says
resuming by itself from the conversation's `is_processing` (a page reloaded
mid-resume, before any `turn_resuming` frame) and for `RESUME_SETTLE_MS`
after the turn ends, until the stored transcript holds the reply that
answered, so it never reads "not resumed" between resuming and resumed. A
cut, or its resumed reply, that the stored transcript ends with but the
panel's thread does not hold after `PANEL_CATCH_UP_MS` (its stream closed on
the cut before the reply was stored) makes the page read the panel's thread
back once (`ChatPanelHandle.reloadConversation`), so the turn is never left
blank until a reload (`components/chat-panel/cut-turn-cards.tsx`, #10910).
A conversation read while it says `is_processing` is stale at once
(`useConversation`): a page that left it during the turn and comes back reads
it again instead of the copy cached mid-turn, and a host that follows no
stream of that turn (`followTurn`, the chat page while no `useTurnFrames`
stream works) reads it every `CONVERSATION_TURN_POLL_MS` until the turn
ends, so the card never stays on resuming with no answer (#10910 B1).
`InterruptedTurnNotice` remains only for a cut that stored no
interrupted reply, and `ResumedTurnNotice` only where no card says the cut.
Read off what messages already carry: stored conversations render the same
way.

**Not resumed, the one click stays:**

| Case | Why |
|---|---|
| Cause not `stop` / `recycle` (OOM kill, a crash the turn caused, unknown) | Resuming it could loop |
| `MAX_AUTO_RESUMES` resumes in a row with no new tool round, more than `MAX_AUTO_RESUME_SPAN_SECONDS` since the first cut, or `MAX_TOTAL_AUTO_RESUMES` in all | A turn that does not advance could loop; the next cut keeps the one click |
| The person wrote another message after the cut, or pressed Stop, since the cut | The person decided; never answered twice |
| The conversation or the message is gone | Nothing to continue |
| Agentic AI unavailable on the resuming process | The gate refuses; the checkpoint stays |
| The record could not be written | Best-effort, like the checkpoint |

**A message steered into the cut turn.** A message the person sent
while the turn ran is stored as a user message after the one the turn
answered, and before the turn's interrupted reply. It was the turn's to
answer, not the person moving on: `parent_wake._steered_before_cut` finds the
first assistant message after the cut turn's message, and when it is the
interrupted reply (`metadata.interrupted`), the latest visible user message
before it is the one the resume answers (its history ends there, so it reads
every steered message and the cut turn's replay). A user message after the
interrupted reply, or no interrupted reply stored, still stands the resume
down, unless nobody answered it: a message sent between the cut and the
sweep finds the cut turn's lock still held (its process drains, or died and
the lock waits out `DEAD_LOCK_GRACE_SECONDS`), is folded into a turn that no
longer runs and starts no turn of its own. `parent_wake._left_unanswered_since_cut`
finds it (the latest visible user message with no assistant message after it,
and no turn holding the conversation now) and the resume answers it, its
history ending there; before, the resume stood down and the message stayed
unanswered under a stale Waiting card (#9030). The other queues behind a cut turn are resumed by their own sweeps:
agent account queues by `turns.orphaned()` in the maintenance pass,
channel queues by `sweep_channel_queues` (section 10), web widget sessions by
`public_chat.resume_unanswered_widget_sessions` (below).

**A web widget message a restart left unanswered.** A widget session
has no cut record: its queue is its stored messages, answered by the turn
holder only (`public_chat._finish_widget_turn`), so a worker stopped or killed
mid-turn left the visitor's message with no answer and no turn coming. Step
16b of the scheduler tick (only while agentic AI runs, never on a process
asked to stop) lists the `web_embed` sessions whose last visitor-facing
message is a visitor message, quiet for `WIDGET_RESUME_AFTER_SECONDS` (60 s)
and at most `WIDGET_RESUME_MAX_AGE_SECONDS` (1 h) old, then per session: takes
the widget turn (`chat_turn_guard.acquire_turn`, NX: a session a turn holds is
left to it), reads the last message again under the turn, claims it once
(`public_chat:widget_resume:{message}`, Redis `SET NX`, 24 h) and answers it
in the background through `_answer_queued` (messages queued behind it come
with it). A live widget turn renews its lock every 60 s
(`_widget_turn_keepalive`), so a turn longer than the lock's TTL is never
taken; a follow-up turn cut by a stop gives the session back at once. The
widget polls when its stream ends after `accepted` without `done`, so the
resumed answer shows without a reload. Without Redis nothing is resumed.
Tests: `tests/test_public_chat_restart_resume.py`.

A resume cut again by the next platform stop flushes its checkpoint and is
queued once more with `resumes_made + 1`. It is queued by the turn's own
teardown (`_run_parent_wake_turn(on_cut=...)`, with its lock token) FIRST,
before the interrupted close and the release, like a cut wake: queued
after them, the conversation read as interrupted with `auto_resume: false`
for that gap, the notice offered the Resume click, and its "Continue where
you left off." made the queued resume stand down ("the person wrote"), with
no `resumed_as` on the cut trace. `resume_cut_turn` still queues it from its
`except` when the cut came before the turn took the conversation, never twice.

Tests: `tests/test_cut_turn_resume.py` (queue, cause, cap, atomic take,
sweep), `tests/test_cut_turn_resume_runner.py` (same message, declines,
re-queue), `tests/test_chat_shutdown_cut_turn.py` (WebSocket and REST cut
paths queue). Slack and Teams keep section 10's hand-on.

**The other entry points (slice 3).** The same record and sweep resume:

| Entry | Cut caught in | Queued with | Resumed as |
|---|---|---|---|
| Parent wake (`parent_wake._run_parent_wake_turn`) | its outer `finally`, cancelled by the background-turn drain | `_queue_cut_wake`: the stored wake message, entry `wake` | `wake_from_platform=True`: the wake's secret markers restored as on its first run |
| Platform API stream (`api/v1/platform.py`, channel `api`, `stream: true`) | the SSE generator's `CancelledError` when `is_graceful_shutdown_cancellation` (a client that leaves is not a cut) | `queue_cut_platform_turn`: the stored user message, entry `platform_api` | a person's message (markers defused); the platform's session restore reads the reply; dropped again (`forget_cut_platform_turn`) when the turn answered during the drain |
| Agent account (e-mail, Teams, Slack as the person: `agent_accounts.pipeline._answer`) | `run_agent_chat`'s `CancelledError`, nothing posted yet | `_park_cut_turn`: parked due now in `agent_account:pending` | the scheduler's `state.overdue` sweep runs it like a message past its grace period, so a message the owner answered meanwhile is left (`owner_replied`, `_owner_replied_since`); never on the agent's own account (`ai_user`) without a grace period, where no person writes and the resumed turn always posts its reply (#10779) |

The agent account counter is the channels' one
(`claim_channel_resume("agent_account:{account}", thread, message)`), so a
message is handed on at most `MAX_AUTO_RESUMES` times; every hand-on and every
refusal logs one WARNING. Tests: `tests/test_cut_turn_resume_entries.py`.

**A non-streaming platform API turn is answered retryably, never resumed.**
A `stream: false` call to `POST /api/v1/platform/chat/messages` (a playbook
step of a calling platform) waits for one JSON answer. When a platform stop
cuts its turn before it answered (uvicorn's graceful drain, its cancellation
recognised by `is_graceful_shutdown_cancellation`, or the loop wound down at
an iteration boundary, `worker_shutdown.TurnWoundDown`), the route survives
the delivered cancellation and answers what `classify_platform_turn_failure`
maps it to: HTTP 503, `error_type: platform_restarting`, `retryable: true`,
`Retry-After: 5` (`PLATFORM_CHAT_RESTART_RETRY_AFTER_SECONDS`), the
`detail` naming the cause (`drain_cut_cause`: `recycle` or `stop`) and the
`conversation_id`. One WARNING names the conversation and the cause. The turn
is NOT queued for resume (`queue_cut_platform_turn` is the streaming path's
only): the caller sends the same request again, and a queued resume would
answer the same message a second time in the conversation. The stored user
message stays, unanswered. A turn that answered before the cut is unchanged,
and a cancellation that is not a platform stop (the client left) propagates
as before. When the process dies before the answer is written (`SIGKILL`,
out of memory, a grace shorter than the drain), the caller sees the dropped
connection and its own retry policy applies; the drain middleware's bare 503
(`GracefulShutdownDrainMiddleware`) covers a request cut before the route
ran. Tests: `tests/test_platform_chat_nonstream_cut.py`.

**An agent account turn resumes its tool work (#10651).** Until then the
parked message ran again from scratch: every tool call the cut turn had
completed ran a second time, and with builds rolling out every 4 to 8 minutes
a Teams direct message whose turn needed more than one build interval never
got its reply. The turn now checkpoints its completed tool rounds at every
iteration boundary with the background tasks' writer
(`background_checkpoint.TaskCheckpoint`, Redis, 24 h) under
`agent_accounts.cut_turn_checkpoint.checkpoint_id(account, event)` (a uuid5
of the account, the conversation and the message, the same for every
attempt). The run that answers the parked message replays them before the
message with the resume note (`load_task_replay`: an in-flight call is named,
never run again blindly) and opens its first trace with
`metadata.resumed_from` (entry `agent_account`, the replayed and completed
call counts). A cut before the turn acted flushes the checkpoint and keeps it
for the resume; any other end (answered, failed, cut after it acted, cap
reached) deletes it. Nothing is posted in the conversation on a cut, for any
account mode (`ai_user`, `on_behalf`) and any response mode (`auto`,
`approval`, `shadow`): the parked message is answered by the next process as
if the cut never happened (#10772, Sam, 10 Nov 2026: no in-chat
interrupted-turn notice on any channel or account; it replaces the gate of
#10768). The resumed turn keeps the send gate: `process_event` decides again
and a shadow reply is recorded, never sent. Tests:
`tests/test_agent_account_cut_turn_checkpoint.py`.

**The cut trace and its resume name each other.** A cut agent account turn
handed on keeps its trace id beside the checkpoint
(`cut_turn_checkpoint.remember_cut_trace`, Redis, the checkpoint's TTL); the
resumed turn writes it as `metadata.resumed_from.trace_id`, the key a cut web
turn's resume carries, and marks the cut trace `resumed_as` with its own trace
id (`cut_turn_resume.mark_trace_resumed_as`). A turn a graceful stop cut that
is NOT handed on says why on its trace, `metadata.resume_skipped`:
`acted_on_the_platform` (it had already posted, drafted or filed as the
account: run again it would duplicate) or `not_handed_on` (the cap of
automatic resumes reached, or the stop cause not resumable). A tool call that
sent a message by another integration (a DM through a service account) is not
an action of the account: the turn is resumed, the send is replayed and never
run again, and the resumed turn decides its final answer with it in view.
A cut trace with neither `resumed_as` nor `resume_skipped` is a resume that
was lost. Before this, an agent account resume carried no cut trace id, so a
reader could only match it by `resumed_from.message_id`.

**Rollout side, assessed (#10651).** Two levers were weighed: draining a
worker until its live turns end, and batching builds so two never land inside
one turn's median length (about 5 minutes on 10 Nov 2026). Neither ships here.
A drain long enough for a 5-minute turn holds every rollout by as long, and a
turn longer than the window is still cut; batching is a deployment policy, not
a code change, and Sam's standing decision of 6-7 Nov 2026 keeps the rollout
policy as is. Resuming from the checkpoint makes the cut cost one model
iteration instead of the whole turn, whatever the cadence, so the turn
progresses across builds. The per-message cap of this path stays the
channels' counter (`MAX_AUTO_RESUMES` hand-ons, progress or not): a turn cut
more often than that is told to be sent again, as before.

**The traces of a resume (slice 4).** `resume_cut_turn` reads the cut
turn's durable trace (`cut_turn_trace_id`: the conversation's latest root
`llm_traces` row started before the cut) and opens the resumed turn inside
`langfuse_tracing.next_root_trace(resumed_from_trace_meta(...))`: the FIRST
trace `trace_llm_call` opens in that context (the resumed turn's root) takes
`metadata.resumed_from` (cut trace id, cut turn id, cause, cut time, resume n
of 3, entry) and records its own id on the link; a consult or transfer opened
after it keeps its own metadata. Once the turn returns, the cut turn's trace
is given `metadata.resumed_as` (the resumed trace id) by a JSONB merge
(`mark_trace_resumed_as`), so the Trace Explorer walks both ways. A cut trace
the stopped process could not flush leaves `resumed_from.trace_id` null and
no `resumed_as`: missing, never wrong. The lookup skips a consult flushed
before its parent (its `metadata.parent_trace_id` is set). `resumed_as` is
written to PostgreSQL only: the ClickHouse read store is append-only, so
there the link is read from the resumed trace's `resumed_from`. A failed
read or link rolls the session back, so the checkpoint discard that follows
still runs. The resume's WARNING names the
conversation, the cause, the attempt and the cut trace. Tests:
`tests/test_cut_turn_resume_traces.py`.

**Why it did not take effect in production, and the fix.** With the
four slices deployed, the person still clicked Resume after every rollout
and no cut trace carried `resumed_as`. The cut traces of 1 Nov 2026 settle the
case: the turns WERE queued and drained. Chain 9d983b83 (cut 10:42Z) to
fa302ba3 (`resumed_from` resume 1 of 3, cause `stop`, cut 10:55Z "(background
turn)") to a3d1cbaa (resume 2, cut 11:15Z) to 266e62f0 (resume 3 of 3, started
11:16:24Z, cut 1.1 s later on the same build ea795cd): every resume started on
a process of the build being rolled out and was cut again by the next pod's
stop, until the cap was spent and the one click was all that was left. It is
none of the four cases the issue suspected: the cause was `stop`, the record
was queued, the pending set drained, and no dead lock held it. Four defects
made it so:

| Defect | Fix |
|---|---|
| The sweep ran only in scheduler step 17, once a minute fleet-wide on whichever worker won `copilot:scheduler_tick_lock`, a worker of a pod being drained included | `cut_turn_resume.run_sweep_loop()`: every worker, started with the scheduler, sweeps at once then every `SWEEP_INTERVAL_SECONDS` (10 s); an empty queue costs one `SCARD`, no database session; the agentic gate is read only when something waits. Step 17 is gone |
| A process asked to stop still took and started resumes, which its own drain cut within seconds | `sweep_cut_turns` takes nothing once `worker_shutdown.shutdown_request()` is set |
| A rollout replaces pods one by one: a pod of the old build not yet signalled took the resume and was the next one stopped | The record carries the cut process's `build` (`settings.app_version`); a turn a `stop` cut waits up to `SAME_BUILD_DEFER_SECONDS` (90 s) for a process of another build, then any process takes it (a scale-in or a node drain brings no new build). A `recycle` is resumed by its own build at once |
| `resumed_as` was written only when a resume returned: a resume cut in turn left none, so no trace of the chain showed the link | `parent_wake._link_cut_trace_on_drain`: on the drain's cancellation the link is written by a shutdown finalizer on a session of its own |

Two hardenings the issue asked for ride along. A turn cut by uvicorn's
graceful drain with no recorded cause (`cut_cause()` is `None`: a supervisor
that does not chain the signal) is queued as `stop`
(`worker_shutdown.drain_cut_cause()`): only a stopping server raises that
cancellation. The reply's `interrupted_cause` keeps what was recorded, so its
text still says the cause is unknown. And the record carries the cut turn's
lock token: a conversation lock still holding that token
`DEAD_LOCK_GRACE_SECONDS` (30 s) after the cut belongs to a process killed
before its release, so the sweep frees it (`release_turn`, compare-and-delete:
a lock another turn took holds another token and is never touched) instead of
waiting the marker's 300 s TTL. One WARNING per freed lock.

**The notice while it resumes.** `GET /conversations/{id}` serves
`interrupted_turn.auto_resume` (`cut_turn_resume.resumes_by_itself()`: the
record is queued, `is_queued()`, AND the agentic gate is on): while the
record waits, `InterruptedTurnNotice` says "It is resuming by itself: no need
to click anything." and shows no Resume button, so no click races the sweep.
With agentic AI off the sweep leaves the queue waiting (24 h), so the notice
shows Resume (disabled by the same gate) and no "resuming by itself" line; the
gate is read only for a queued record. The button comes back once the record
is gone and the turn did not resume (the cap, a non-platform cause, expiry):
while `auto_resume` is true, `useConversation` (hooks/api/useChatApi.ts, so
the chat page and the studio rails alike) reads the conversation again once
after `AUTO_RESUME_RECHECK_MS` (100 s, `SAME_BUILD_DEFER_SECONDS` plus one
`SWEEP_INTERVAL_SECONDS`; `hooks/useAutoResumeRecheck.ts`), so Resume returns
without a reload. The wait restarts only when the conversation or the value
changes: no polling.

Once the resume runs, the transcript says so, with no new frame and
nothing in the system prompt:

- **Live.** `turn_resuming` is in `chat_turn_stream.RELAYED_FRAME_TYPES`, so
  `TurnPublisher.offer` relays it to every viewer (it used to drop it: no
  viewer ever read it). The publisher journals it under `resuming` in the
  turn's state hash; `resume_frames` replays it right after the ack, so a
  stream attaching mid-resume (a reload, another device) is told too. The
  panel hands it to the host (`onStreamEvent`): `useTurnFrames` keeps it as
  `resumingTurn` until the turn ends, a replay acknowledges, or the
  conversation changes, and `transcriptFooter` shows `ResumedTurnNotice`
  (informational, not the warning of `InterruptedTurnNotice`) while the turn
  runs: "Resumed by itself after a platform restart (resume n of 20):
  continuing from its N completed tool calls." `recycle` reads "a worker
  recycle"; N is the `tool_call_trace` of the interrupted reply when it is
  the last assistant row, else the sentence ends at the parenthesis.
- **Persisted.** `MessageResponse.resumed_from` (cause, resume, of, cut_at)
  is read from `metadata.resumed_from`, and the resumed reply's `done` frame
  carries it too, so the reply lands with its marker. `MessageExtras` draws it
  under the reply's text, never inside its Markdown (what is copied and read
  aloud): a reload, another device
  and a shared transcript tell a resumed answer from a fresh one. A resume
  cut and resumed again ends on one reply, marked with the last resume; the
  chain stays readable through the interrupted replies, which keep their own
  notice. A turn the person resumed with the Resume click carries no
  `resumed_from`, so no marker.

Tests: `tests/test_chat_turn_fanout.py` (relay, journal, replay),
`tests/test_resumed_turn_marker.py` (serializer),
`frontend/src/components/chat-panel/resumed-turn.test.tsx` (frame handler,
live line, marker with and without `resumed_from`).

Tests: `tests/test_cut_turn_resume.py` (the stopping process, the same-build
deferral on the 266e62f0 timings, the dead lock freed and the lock left to the
drain or to another turn, the loop, the empty queue, the gate, the cause
default), `tests/test_chat_shutdown_cut_turn.py` (a drain cut with no recorded
cause is queued, with its lock token). Live retest: a web turn cut by a real
rollout resumes with no click within 60 s of the new pod, its cut trace
carrying `resumed_as`.

**The chat-handover parent run follows the turn.** A turn that
delegates, consults or transfers opens a chat-handover parent run
(`agent_chat._ensure_chat_parent_run`: `assignment_id` NULL, no orchestration
execution). Before, the shutdown sweep `autonomous.fail_orphaned_in_process_runs`
failed it unconditionally, the resumed turn opened a new parent, and the runs
the cut turn delegated stayed under a red run: the person saw a failed run and
clicked Resume although the turn had resumed. Now:

| Step | What happens |
|---|---|
| Tracked | `in_process_runs.track_in_process_run(run_id, conversation_id=...)` names the parent's conversation; `chat_parent_run_for(conv)` reads it back |
| Queued | `_store_cut_turn`, `_queue_cut_wake` and `_requeue_cut_again` pass it to `enqueue_cut_turn(parent_run_id=...)`; the record (`CutTurn.parent_run_id`) carries it, and a record of an earlier release has none (the resume opens its own parent, as before) |
| Kept | The shutdown sweep runs after the drain queued the turn: a chat-handover run (`trigger_context.type`) whose conversation is queued (`cut_turn_resume.queued_conversations`) is kept running, `trigger_context.awaiting_auto_resume` = the conversation id and `awaiting_since`; one not queued, or Redis unreadable, is failed as before. Consult and transfer children are still failed: their own turn ended |
| Adopted | `resume_cut_turn` sets `cut_turn_resume.RESUMED_PARENT_RUN` around the resumed turn; `run_agent_chat` adopts it at its start, on a session of its own, under `FOR UPDATE`, only when `adoptable_parent_run` holds (same agent, same user, still `running`, chat-handover, same conversation), and clears the marker. The children the resumed turn delegates go under it and it completes with the turn. A resume cut again re-queues the parent it holds |
| Missed | `_cleanup_stale_assignment_runs` leaves an `awaiting_auto_resume` run while its record is queued (or Redis cannot say), while a live turn holds its conversation (`chat:processing`: the resumed turn that adopted it), and for `PARENT_ADOPTION_GRACE_SECONDS` (120 s) after the record left the queue; past it, unadopted, it is failed with a reason naming the missed resume (`orphan_class="missed_auto_resume"`) |
| Already taken | A rollout's next process takes the cut turn (`take_cut_turn`: GETDEL, then the `resuming` marker) seconds after the cut, while the stopping process's shutdown sweep runs only after its drain, 40 to 60 s later. `queued_conversations` therefore counts the `resuming` marker as queued: the sweep keeps the parent the resumed turn is adopting instead of failing it as "interrupted by a platform shutdown" (#8579) |

Tests: `tests/test_chat_parent_run_resume.py` (the record, the registry, the
shutdown sweep keeping, failing, Redis down and a child, the stale sweep while
queued, Redis down, the grace and the miss, the adoption guard, the hand-off
set by the resume). Live retest: a chat turn with a delegation cut by a
rollout ends with ONE parent run, `completed`, holding every child, and no
Resume click.

### 11.x A taken resume that never started is put back; each hop names its trace

The 24 h tally after the "resuming" marker still showed first cuts with no
`resumed_as` and a Resume click 44 to 56 s later, or no resume at all, and
resumed traces linked to the wrong hop. Two causes in the code:

| Case | Before | Now |
|---|---|---|
| Taken, then the process is asked to stop | `take_cut_turn` removed the record (GETDEL); the resume started on a stopping process was cancelled by its drain while it still read the conversation (`resume_cut_turn` before `_run_parent_wake_turn`), where nothing queues it again. The marker was cleared, the notice offered Resume | `sweep_cut_turns` checks the stop again after each take and puts the record back unchanged (`put_back_cut_turn`, same resume count and first cut) for the next process; `_resume_cut_turn_settled` puts it back when a graceful cancellation lands before the turn started (`on_start`). Once started, the turn's own cut handling queues it with its progress, as before |
| Link of a chain | The resumed turn found the cut turn's trace by querying the latest root trace before `cut_at`: on a chain it could return the hop before, the cut hop's trace not flushed yet (`resumed_as` / `resumed_from` off by one hop) | `CutTurn.trace_id`: `_requeue_cut_again` writes the trace the cut resume opened (else the one it resumed); `resume_cut_turn` links to it and queries only for a record without it (a first cut, a record of an earlier release) |

Tests: `tests/test_cut_turn_resume.py` (the put back on a stopping sweep, the
record's trace), `tests/test_cut_turn_resume_runner.py` (put back before the
turn starts, not after, not on another cancellation; the hop's trace queued and
linked without a query). Live retest: the 24 h tally of #7132 with no Resume
click on a first cut and no miss.

### 11.y A resumed hop is queued before the drain cancels it; a first cut names its trace

The 24 h tally after #8597 (#7132 comment 6675215366): 6 of 16 resumed hops
ended with no `resumed_as` and no successor linked to them, against 3 of 26
first cuts, and one resume linked with `resumed_from.trace_id` null.

| Case | Before | Now |
|---|---|---|
| A resumed hop cut by a rollout | It runs as a background turn (`_spawn_wake`): the lifespan drain cancels it after the requests' grace, then waits `SHUTDOWN_DRAIN_SECONDS` (15 s) before the process tears down. Its next resume was queued only at the end of its unwinding (`on_cut` in `_run_parent_wake_turn`'s `finally`); an unwinding that outlived the drain or the pod's kill lost it | `shutdown_finalizers.before_cancel`: `resume_cut_turn` registers a hand-over the drain awaits (bounded by `BEFORE_CANCEL_SECONDS`) BEFORE it cancels the hop; it queues the next resume with the hop's own trace and links the cut trace. The teardown still writes the record again with its lock token; a hop that finishes after the hand-over forgets the queued resume (`forget_cut_turn`) |
| A first cut resumed before its trace was flushed | `_store_cut_turn` queued no trace id, and the resume queried `llm_traces` for one the stopped process had not flushed yet (27445211, 0.37 s after the cut): `resumed_from.trace_id` null, no `resumed_as` | `langfuse_tracing.arm_root_trace_link` at the start of each web and REST turn records the id of the turn's trace; `_store_cut_turn` passes it as `CutTurn.trace_id` |

Tests: `tests/test_cut_turn_hand_over.py` (the hand-over runs before the
cancellation, bounded, a failure still cancels, a finished turn leaves none,
the armed link), `tests/test_cut_turn_resume_runner.py` (a hop queued by the
drain with its own trace; a hop that finishes after the hand-over forgets it).
Live retest: the 24 h tally of #7132.

### 11.z A cut a person waited on is a friction, never auto-accepted (#11924)

A chat turn cut by a rollout and answered by its automatic resume left no
record in the friction ledger: `turn_resumed` covered assignment runs and
background tasks only, and every one of those was accepted as a rollout
hand-back. The person who waited minutes on "Interrupted" was invisible.

`parent_wake.resume_cut_turn` now calls `_note_resumed_chat_cut` once the
resumed answer is stored. The wait runs from the message's first cut
(`CutTurn.first_cut`) to that moment:

| Turn | Ledger row (`turn_resumed`, agent of the conversation, run = the chat-handover parent run) |
|---|---|
| A person's turn (any entry but `wake`) answered more than `frictions.PERSON_VISIBLE_CUT_SECONDS` (30 s) after its first cut | `person-visible cut: ...`, undisposed: it raises `run.friction_recorded` and is classified by hand |
| A shorter wait, or a platform wake | written disposed `platform_acceptable` / `accepted` |

The counter `xtmone_chat_turn_cuts_total{visible, version}` counts both per
build (`rollout_counters.count_chat_turn_cut`): the cut count per rollout,
before and after a drain change, is read off it. A turn the person moved on
from or stopped is not resumed and records nothing.

The wait above mixes the restart with the resumed turn's own remaining work
(#498). `resume_cut_turn` also measures the restart alone: the seconds from
the hop's cut (`CutTurn.cut_at`) to the start of its resume. It is logged on
the "Resuming the chat turn" line, observed on the histogram
`xtmone_chat_turn_resume_start_seconds{version}`
(`rollout_counters.observe_chat_turn_resume_start`), and named in the ledger
row: "... after 900 s (resume started 42 s after its latest cut)". A long
total with a short start is the turn's own work; a long start is the drain,
the sweep, the build defer or the lock grace.

Tests: `tests/test_chat_cut_person_visible.py`.

### 11.za A cut Slack or Teams turn is answered within seconds of the new pod (#11924 S3)

A channel turn a rollout cut is put back at the head of its conversation's
queue (`channel_continuation.hand_on_cut_turn`), silently (#10772). Only the
scheduler tick drained that queue (step 16, `sweep_channel_queues`): once a
minute, on whichever worker won the tick lock, a worker of the pod being
drained included. The sender saw no typing and no answer for a minute or
more after the cut, the web turn's "resuming by itself" having no channel
counterpart.

| Before | Now |
|---|---|
| Drained by the scheduler's minute tick only | `cut_turn_resume.run_sweep_loop` (every worker, at start then every `SWEEP_INTERVAL_SECONDS`, 10 s) also calls `sweep_channel_turns_once`; the tick keeps its step as a fallback |
| A stopping process could drain the queue and start the turn its own drain cuts again | `sweep_channel_queues` drains nothing once `worker_shutdown.shutdown_request()` is set |

The drain takes the conversation's lock before it claims, so two workers
never answer one message. The resumed turn runs through the adapter's
`run_queued_message`, inside `typing_keepalive`: the sender sees typing again
from the drain's start and gets the answer on the same thread. Tests:
`tests/test_channel_turn_cut_resume.py` (the loop drains a cut turn, a
stopping process drains none, an error never reaches the loop),
`tests/test_cut_turn_resume.py` (the loop calls the channel sweep).
