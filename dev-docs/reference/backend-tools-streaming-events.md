# Agent loop streaming and tool events

How the agentic loop streams reasoning and answer text to its consumer (frames, hold-back window, segments, retracts) and how a tool reports progress while it runs (`tool_progress`). This is the one description of the loop's side of streaming; the web chat's fold-in of a delivered segment is in `dev-docs/reference/backend-services-chat.md`, the frontend handling in `dev-docs/reference/frontend-common-pitfalls.md`. The rules of this area live in `backend/app/tools/AGENTS.md`.

## Reasoning and answer streaming

### The frames

`run_agent_loop` reports through two callbacks: `on_event` for status frames, `on_token` for answer text. The streaming frames, in the order a turn sees them:

1. `{"status": "thinking", "iteration": N, "context_tokens", "context_window", "context_breakdown", "context_level", "context_detail"}` - once per iteration, before its model call; a consumer starts a fresh request on it. `context_detail` (`_context_detail`) is what each bucket is made of - the system prompt by the sections its assembler measured (`context_sections`, from `prompt_budget.section_chars`), one row per offered tool (name, kind, its `tool_gating.tool_group` label), tool results by tool name, the compaction gate's own figures - every split summing to its bucket; server-only, for the chat's `/context` command (#6590): `chat_turn_stream.slim_frame` drops it from every published frame.
2. `{"status": "thinking_text", "content": text}` - reasoning chunks, held-back preambles (below), and the loop's own user-visible notes (budget, wind-down, loop-breaker and failure-brake warnings, through `_emit_budget_warning`).
3. `{"status": "streaming"}` - answer text is about to start; the text then arrives through `on_token` (the web chat's stream forwards it as `{"type": "stream", "content": ...}`).
4. `{"status": "stream_retract"}` - the answer text streamed so far is taken back (below).
5. `{"status": "segment_complete", "content": ...}` - opt-in: the answer text streamed so far is a completed intermediate message (below).

The tool frames (`tool_start`, `tool_heartbeat`, `tool_done`, `tool_progress`) and the other status frames (`composing`, `steering`, `wait_start`, `wait_status`, `llm_step`, `context_compaction`, `model_downgraded`, `wind_down`, `terminal_tool_decision`) ride the same `on_event`.

### Reasoning text

- The `on_thinking` that `_new_on_thinking()` in `agent_loop.py` makes for each model call emits `{"status": "thinking_text", "content": text}`; its first chunk opens a new paragraph (`\n\n`) so the reasoning of consecutive model calls - continuations, retries and forced answers included - never runs together. The generation's own capture keeps the text as the provider sent it.
- Providers that stream reasoning call it from `_streaming_chat()` / `chat_stream_with_tools()` (Anthropic `thinking_delta`, OpenAI-compatible `reasoning_content` / `reasoning`, Gemini thought parts). Passing an `on_thinking` that reads the text is what asks a provider for it - Anthropic's Claude 5 family and Gemini return none otherwise; the `ThinkingActivitySink` that `_streaming_chat()` passes for the idle watchdog when nothing reads the reasoning asks for nothing (`reads_reasoning()`); see `dev-docs/reference/backend-llm-thinking-and-caching.md` > "Streaming reasoning text".

### Answer text

- **A tool-free call streams directly.** When `on_token` is set and the iteration offers no tools (stripped by the wind-down, or none at all), the loop emits `streaming` and the answer streams token by token.
- **A call with tools holds its text back.** While a stream-with-tools call is in flight the loop cannot know whether the text is a pre-tool-call preamble ("Let me check X…") or the final answer. The first `_PREAMBLE_HOLDBACK_CHARS` (900) characters are buffered: if tool calls follow, the held text is emitted as `thinking_text` (reasoning pane); once the buffer exceeds the window the text is treated as the final answer and flushed to the live answer stream (`streaming` + `on_token`).
- **Text the loop already holds** (a park message, a non-streamed answer) is streamed in chunks by `_stream_text_chunked()` after a `streaming` frame.

### Text shown is never taken back

When more than the window streamed and the turn then continues, the flushed text is a provisional answer the user has already read:

- **With `deliver_segments`** (the streamed web chat: `api/v1/chat.py` passes it to `run_agent_chat` / `run_general_chat`, #3293) the loop emits `{"status": "segment_complete", "content": ...}` for ANY tool round (wait-family after a partial wake, an ordinary side-effect or lookup tool such as `store_memory`), for the implicit park and the spent-wait-budget park, and for the stall-guard / resume-promise re-prompts, provided the text is not blank. The consumer persists it as a completed intermediate message and the continuation streams as the next bubble; it is NOT re-emitted as `thinking_text`. Both chat services finalize the segment before it leaves the service layer (`services/segment_finalize.py`: chart injection, URL guard), as the final answer is.
- **`stream_retract` is reserved** for consumers without `deliver_segments` (public widget, REST, channels, assignments, sub-agents) and for the cases where the text is about to be re-produced: the LLM timeout retry, the `max_tokens`-truncated tool round the guard discards and retries (a segment is never delivered for it), and a mid-answer failover. On a tool round and the resume-promise re-prompt the retracted text is then re-emitted as `thinking_text`; a park and the stall re-prompt emit the retract alone.
- **Mid-answer failover.** The loop hands `on_stream_reset` to a provider that declares `supports_stream_reset` (the `FailoverLLMProvider`): on the stream-with-tools path it retracts what flushed (not re-surfaced as `thinking_text`: the fallback's fresh answer follows) and re-arms the hold-back window; on a tool-free streamed call (`_streaming_chat`: the tool-free iteration, its retries, the forced answer) `_retract_answer_stream` emits `stream_retract` then `streaming`. A retract that cannot reach the caller (no `on_event`, or a delivery that raised) raises (`_retract_or_raise`), so the wrapper keeps the partial answer and surfaces the error. The max_tokens continuation passes none, since a retract there would take back the answer it continues. A provider that cannot restart never receives the kwarg.

### Critical invariant

`thinking_text` events can arrive during or after the `streaming` event. `streaming` only signals that answer tokens are about to start - it must NEVER cause the consumer to discard accumulated thinking text. Thinking text is cleared only on `"done"`, `"error"`, `"cancelled"`, or when a new `"thinking"` event starts a fresh request.

## Tool-side progress events (`tool_progress`)

The loop emits `tool_start` / `tool_heartbeat` / `tool_done` AROUND a tool call. A handler that needs to say something WHILE it runs - it committed a row the user is looking at and is now doing slower follow-up work - calls `await emit_tool_event(event, **payload)` from `tool_events.py`. Both execution sites (sequential and the parallel `_exec_with_span` batch) bind a per-call sink (`_progress_sink`) right after they push the Langfuse tool span and reset it in the same `finally`, so the event arrives on `on_event` as `{"status": "tool_progress", "tool": tc.name, "tool_call_id": tc.id, **payload}`, between the call's `tool_start` and its `tool_done`. The binding is a `ContextVar`, per call, so a parallel batch keeps each call's events on its own call.

The events (`event` values, constants in `tool_events.py`):

| Event | Emitted by | Payload |
|---|---|---|
| `PRESENTATION_SAVED_EVENT` (`presentation_saved`) | the deck tools, right after the commit and before the post-save layout render, so the Presentation Studio stage refreshes when the revision is readable | `presentation_id`, `revision`, and `focus_slide_id` only when an edit wrote a slide: the stable id of the slide to move to (first changed > first inserted > first moved) |
| `RUNNER_APPROVAL_REQUIRED_EVENT` / `RUNNER_APPROVAL_RESOLVED_EVENT` | a runner command held for the user's approval, then out of the queue | `command_id`, `runner_id`, `runner_name`, `kind`, `summary`, `expires_in_seconds` / `command_id`, `status` |
| `RUNNER_COMMAND_STARTED_EVENT` / `RUNNER_COMMAND_FINISHED_EVENT` | a `computer_use` command appeared or changed state, then ended (`services/runner_activity.py`) | `command`: the full bounded display record, an upsert keyed by its `command_id` |
| `PAGE_ACTION_REQUIRED_EVENT` (`page_action_required`, in `services/builtin_tools/page_actions.py`) | `act_on_page`, once per host-page action: the embedded widget performs it and the tool waits for the page's answer on the approve route (`chat_turn_guard.await_page_action_result`) | `conversation_id`, `action_id`, `action` |

Rules:

- The frame is **data, not agent state**: consumers must not feed it to their status label / bubble (the web app reads it in `handleToolProgress`, `hooks/api/useChatApi.ts`). A consumer that does not know the `event` ignores the frame.
- Consumers key on `event`, never on `tool` - the LLM-visible name may be an alias or a reordered spelling the executor resolved. The loop owns `status`, `tool` and `tool_call_id`; a payload cannot override them.
- `on_event=None` binds `None`: the emit is a silent no-op, and a nested loop without a consumer shadows the outer call's sink that way instead of leaking its tools' reports onto it. Outside the loop entirely (REST, tests) it is a no-op too. It never raises into the handler.
- The `finally` CLOSES the binding (`ToolEventBinding.closed`), it does not merely unbind it: a task the handler spawned inherits the binding through its copied context, and a report it makes after the call ended is a silent no-op instead of a frame tagged with a finished call. Do not hand a sink to a background task by any other route.
- The payload is made JSON-safe once, in `emit_tool_event` (`json.dumps(default=str, allow_nan=False)` round-trip, bounded by `MAX_TOOL_EVENT_PAYLOAD_CHARS`, 8000, #3445): the web chat's stream serialises each frame as it writes it (`chat_web_transport.sse_data`), the platform SSE proxy serialises in its response generator (a bad value ends the stream). What still cannot be serialised (any `Exception` - `default=str` runs the value's own `__str__`) or is oversized is dropped with a warning naming the tool and the event, never raised into the handler. Pass ids and revisions, not the row - a frame is a pointer to state the consumer refetches.
- Do not add a second channel (a callback parameter threaded through tool builders, a module-level queue): every handler that wants to report goes through this one, or the parallel-batch isolation is lost.
- The binding also carries the call's own context: `current_tool_deadline()` is the `time.monotonic()` instant the loop cancels the call (from `timeout=`, the `_tool_timeout_for` value each execution site computed), never later than the enclosing call's in a nested loop; `current_tool_iteration()` is the 1-based iteration the call runs in. Both are `None` outside the loop or once the binding closed. A tool whose work is waiting (the `computer_use` result wait) stops before its deadline to say what happened instead of leaving a bare timeout to the loop - read it there, never re-derive the loop's limits in a tool.
