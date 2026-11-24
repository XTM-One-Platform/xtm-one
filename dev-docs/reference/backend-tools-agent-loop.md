# Tools and the agentic loop

How the agentic loop of `backend/app/tools/` is built and run: the generic-loop principle, the key files, tool name resolution, failure classification and the offered surface, tool definitions and sources, the loop itself, mid-run steering, the `wait` primitive, long runs, how the assignment engine and the providers use tools; the rules of this area live in `backend/app/tools/AGENTS.md`.

## Purpose and genericity

This directory implements the **tool calling** and **MCP (Model Context Protocol)** layer of XTM One. It allows agents to use external tools during assignment execution and chat, making them truly agentic.

The loop and the tool base classes are generic (invariant 18): no tool, vendor or provider name, no list of words or phrases read from the model's answer, no parsing of text a tool of the platform wrote. A behaviour that depends on a tool is a property the tool declares on its `ToolDefinition` (an MCP tool: its MCP annotations); a loop decision reads state, ids and structured results, never prose. `backend/tests/test_no_workarounds.py` lists what is still open and only shrinks.

## Architecture Overview

```
                 ┌─────────────────────────────────────────┐
                 │          Assignment Engine / Chat           │
                 │   calls run_agent_loop(llm, messages)    │
                 └──────────────────┬──────────────────────┘
                                    │
                 ┌──────────────────▼──────────────────────┐
                 │           Agent Loop (agent_loop.py)     │
                 │  LLM → tool_calls? → execute → loop     │
                 └──────────────────┬──────────────────────┘
                                    │
                 ┌──────────────────▼──────────────────────┐
                 │      CompositeToolExecutor (base.py)     │
                 │   Routes tool calls to correct executor  │
                 └───────┬───────────────────┬─────────────┘
                         │                   │
            ┌────────────▼───────┐ ┌─────────▼──────────┐
            │ CallableToolExec   │ │    MCPManager       │
            │ (custom tools)     │ │ (MCP server tools)  │
            └────────────────────┘ └─────────────────────┘
```

## Key Files

| File | Purpose |
|---|---|
| `agent_loop.py` | The agentic loop (`run_agent_loop`): LLM → tool calls → execute → feed back → repeat, with context curation and compaction, the loop breaker, argument validation, the `wait` and `load_tools` primitives, streaming, budget enforcement |
| `base.py` | Core types (`ToolDefinition`, `ToolCall`, `ToolResult`), the executors (`CallableToolExecutor`, `CompositeToolExecutor`, and `ScopedToolExecutor`, a fail-closed wrapper that runs only the offered definitions, used by the public embed chat, the agent chat and assignment paths, and the MCP endpoints), `ToolNameResolver`, the failure classifiers and the offered surface (`offered_tools()`): see "Tool name resolution, failure classification and the offered surface" below |
| `tool_gating.py` | Progressive tool disclosure ("tool multiplexer"): groups tools by source, exposes a compact catalogue + the `load_tools` meta-tool so large tool sets are revealed on demand instead of sent up-front (`backend-tools-tool-gating.md`) |
| `run_budget.py` | Per-run spending budget (`RunBudget`): cost (USD) / tokens / tool calls / duration in one object, enforced with reserve -> call -> debit -> block, plus the optional cost downgrade to a cheaper fallback model. Provider-independent and DB-free: the caller resolves the model's price row once and hands it in (`backend-tools-run-budget.md`) |
| `wait_policy.py` | The `wait` primitive: `WaitPolicy` / `WaitBudget`, the tool definition, `execute_wait`, `WAIT_TOOL_NAMES` and the terminal task states (`WAIT_TERMINAL_STATUSES`); dependency-free so the models layer imports it too (3g below) |
| `tool_events.py` | The one channel for a tool handler to report progress from inside its execution: `emit_tool_event()`, the per-call binding the loop wraps around every call, `current_tool_deadline()` / `current_tool_iteration()`. Dependency-free (`backend-tools-streaming-events.md` > "Tool-side progress events") |
| `approval.py` | Pure decision layer (no I/O) for per-tool human-in-the-loop approval: `evaluate_gate()` answers "does this call pause for a human?" and records an outcome for every call; `fingerprint()` detects that a tool changed since a standing approval was granted; `call_always_asks()` reads a call's `always_ask_argument` and `definition_always_asks()` adds the tool-wide `always_ask`; `no_approval_channel_message()` does not offer a standing approval for a pause made only of such calls. Persistence lives in `services/hitl_whitelist.py` and `services/tool_approval_service.py` |
| `builtin_suites.py` | Which built-in tool suite owns a tool name, so an executor refusing an unknown name says which suite (`Agent.builtin_tool_ids`) the agent lacks instead of suggesting an unrelated tool; indexed lazily from `build_builtin_tool_catalog(include_disabled=True)` |
| `mcp_client.py` | MCP client: `MCPManager` connects to stdio / SSE / streamable_http servers, discovers and runs their tools, persists binary results, offers `read_mcp_resource` (`backend-tools-mcp.md`) |
| `mcp_destinations.py` | Where a remote MCP server may be reached: the `mcp_server_url_policy` (`permissive` / `private_networks` / `public_only`, plus `mcp_server_trusted_origins`), checked on save (`url_problem`, `assert_server_url_allowed`) and on every connection (`guarded_client`); a cloud metadata address is refused in every mode |
| `__init__.py` | Package docstring only |

## Tool name resolution, failure classification and the offered surface

All three live in `base.py`.

**Name resolution.** `ToolNameResolver` is the single definition of which model-supplied names denote a registered tool: the exact name, an unambiguous reordering of its tokens (`normalize_tool_key`), or an alias the tool declares (`ToolDefinition.aliases`: a former name kept callable after a rename, or the near-universal guess its toolset invites; `declared_aliases()` builds the table from the definitions, the platform holds no list). A resolver over the sanitised names the model is offered uses that alias table translated into the visible name space by `visible_aliases()`. Both executors and the progressive-disclosure `ToolGate` share it; how the loop keeps the gate and dispatch on one instance (`_ExecutorNameMap`) is in `backend-tools-tool-gating.md`.

**Failure classification.** The shared classifiers are `is_rate_limit_error`, `is_auth_or_permission_error`, `looks_like_tool_error`, `is_error_shaped_result` and `is_auth_failure_result`.

- `CallableToolExecutor.execute_tool` stamps `ToolResult.is_error` from `is_error_shaped_result`, so a handler that catches an upstream failure and returns the message as a plain string (`"GraphQL error: …"`, `format_http_error`'s `"Error: …"`, a `{"error": true}` envelope) is still recorded as a failure on the trace span, on the `tool_done` event and in the loop's `tool_error_counts`. `_SafeIntegrationExecutor` (`services/integration_tools.py`, every native integration) applies the same stamp in its override. The dedup cache also refuses to store an error-shaped result.
- The auth arm of `is_error_shaped_result` is `is_auth_failure_result`, the whole-result counterpart of `is_auth_or_permission_error`: the phrase must be the message (error-prefixed, leading the first line, one short line that leads with it after a `<Provider>: ` label or opens with a failure statement, or a small JSON error body whose message is itself one of those), never a result that merely mentions "401" / "unauthorized" / "access denied" (an echoed title such as `Issue created: SEC-12 — Login failed (401) ...`, a record field such as `Title: Access denied - ...`, a JSON success message such as `{"message": "Comment added to PROJ-401"}`). A false positive records a success as an error and puts the tool on the auth cooldown (`_AUTH_FAILURE_COOLDOWN_ITERS`, 8 iterations).
- The agent loop chooses that cooldown for a failed call with the same classifier (`_track_tool_result`), never a raw substring search. An error-prefixed result counts only when its own statement names the failure (not the provider detail `format_http_error` quotes after ` Detail: `), and a raised error wrapped by the MCP client (`Tool 'x' failed with error: ...`), the integration executor or the loop (`Error executing tool...: ...`) is read by the first paragraph after the wrapper, so an error that merely quotes "401" keeps the normal error handling. The quoted details are every one `format_http_error` writes (after ` Detail: `, `— bad request. `, `— validation error. ` or `while trying to {action}. `) and an OpenCTI `GraphQL error in '...':`'s server messages; such a detail counts only when it itself states the failure, when it is GitHub's 422 permission failure in disguise, or (GraphQL) through the platform's trailing ` — unauthorized: ` / ` — permission denied: ` hint.
- A bare HTTP status number counts only where it reads as a status. One rule, `_http_status_re`, sits behind both `is_auth_or_permission_error` and `is_rate_limit_error`: opening the message, `(401)`, after a status word (`HTTP 401`, `status code 401`, `"statusCode": 401`, `Error code: 401`, `API error: 401`) or before its reason phrase (`401 Unauthorized`), never inside an id (`PR #401`, `PROJ-401`, `/pulls/401`, `the number of 401`), because `safe_request` writes the tool's action, ids included, into every error statement. The rate-limit keywords stay unanchored: GitHub's secondary rate limit arrives only as a 403 detail.

**The offered surface.** `offered_tools()` is what the running loop offers: `run_agent_loop` binds the tools it was handed for its whole run through `offering_tools()`, a `ContextVar` (a nested loop hands its caller's surface back, the calls of a parallel batch copy it, `None` outside a loop). A tool result or an inlined attachment names another tool only when that surface carries it (`code_interpreter_offered()` for the code interpreter), and a definition whose description points at another tool carries `describe_for_surface`, resolved once by the loop against the same list (`fit_descriptions_to_surface`). The MCP servers (`/mcp/opencti`, `/mcp/openaev`, `/mcp/opencrq`, `/mcp/xtm`) do the same with the tools they serve: `tools/list` goes through `mcp_tool_metadata.tools_to_mcp()` and `tools/call` runs inside `offering_tools()`. An instance-suffixed integration tool keeps its account label on its surface description (`integration_tools._suffix_instance_tools`).

## How It Works

The numbered sections kept here are 1-3, 3a, 3g, 3h and 5. The others have their own reference docs: 3b in `backend-tools-tool-gating.md`, 3c in `backend-tools-context-compaction.md`, 3d in `backend-tools-run-budget.md`, 3e and 3f in `backend-tools-loop-guards.md`, 4 in `backend-tools-mcp.md`.

### 1. Tool Definitions

Every tool is described by a `ToolDefinition`:

```python
ToolDefinition(
    name="github_create_issue",
    description="Create a GitHub issue",
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["title"],
    },
    source="mcp:GitHub",
)
```

### 2. Tool Sources

Tools come from **three sources**, all routed through the `CompositeToolExecutor`:

1. **MCP Servers** — External tool servers (Model Context Protocol). Users add MCP servers (like `@modelcontextprotocol/server-github`) and the system discovers their tools at runtime.
2. **Custom Tools** — User-defined JavaScript/Python functions with input schemas, executed in the hardened Docker sandbox when available (`CUSTOM_TOOL_SANDBOX`: `auto` by default, `docker`, `subprocess` or `opensandbox`) with a timeout-enforced subprocess fallback (see `services/custom_tool_executor.py`).
3. **Integration Tools** — Tools of the connected integrations (e.g. `search_github_issues`), registered by each provider under `integrations/providers/` and loaded by `services/integration_tools.py`.

Additionally, built-in tools like `search_knowledge`, `store_knowledge`, and `request_human_input` are registered directly via `CallableToolExecutor`.

### 3. The Agentic Loop

When the assignment engine or chat system calls `run_agent_loop(llm, messages, tools, executor)`:

1. Tools are sent to the LLM alongside the messages. When **progressive tool disclosure** is active (3b, `backend-tools-tool-gating.md`) only a compact catalogue + the `load_tools` meta-tool are sent up-front; otherwise the full tool list is sent.
2. If the LLM returns `tool_calls`, each is executed via the `CompositeToolExecutor`.
3. Results are fed back into the conversation.
4. The loop repeats until the LLM gives a final text response (no more tool calls).
5. Safety limit: `max_iterations=25` by default — but see 3g: a round that only *waited* is refunded, and 3h: a caller passing `extend_iterations_to` lets a run still making progress go on in blocks.

### 3a. Mid-Run Steering (`steering_queue`)

`run_agent_loop` accepts an optional `steering_queue: asyncio.Queue[str | SteeringMessage]`. User messages sent **while the agent is still generating** (from the chat composer) are pushed onto this queue by the web chat's turn runner (`run_chat_connection`); the steer route stores each one first. The loop drains the queue at the start of every iteration — the safe boundary where all `tool_use`/`tool_result` pairs are complete — appends each item as a `user` message, and emits a `{"status": "steering", "count": N}` event so the UI can show "Incorporating your message…". Rich `SteeringMessage` items carry `FileRef` attachments: a screenshot or document sent mid-run is injected as a true multimodal user message (use `steering_text()` to extract the plain text of either item shape). Messages that arrive too late to be injected (during the final answer stream, or on the direct no-tools chat paths) are drained by the turn runner after the run returns (`drain_steering_queue`) and folded into an immediate follow-up turn, so a mid-run message is never dropped. `steering_queue=None` (assignments, sub-agents, consults, external channels) disables steering with zero behavior change.

The same boundary hands the turn's work to `on_checkpoint`: a synchronous callback given the messages the loop appended (by identity, never the caller's), at the top of every iteration and before the forced final answer. The web chat stores the completed tool rounds through `services/turn_checkpoint.py`, so a turn whose process dies is resumed by the next turn instead of calling the same tools again. The loop does no I/O for it, and a callback that raises is logged once and ignored.

Steering also works **across connections and replicas**: the chat send paths acquire a per-conversation single-flight turn lock (`services/chat_turn_guard.py`); a message arriving while another turn holds the lock (page reload mid-turn, second tab, REST client) is folded into the running loop through a Redis steering inbox instead of spawning a second concurrent agent loop. Folds are always persisted as regular user messages before being pushed, so a NEW turn's acquisition purges whatever is still in the inbox: an unconsumed entry from a crashed or requeued previous turn is already part of the history the new turn loads, and bridging it in again would inject a stale message as if it were the user's latest instruction.

### 3g. The `wait` Primitive (the agent's timer)

Implemented in `wait_policy.py` and executed **inside** `run_agent_loop`, like `load_tools` — it never routes through the executor. An agent that delegated work (`spawn_background_task`, a slow integration call) has to be able to *pause* before re-checking, and a pause is not an action:

- **It does not consume the iteration budget.** `max_iterations` bounds reasoning and acting; a round whose tool calls were all waits (`_is_wait_only_round`: every call's original name is in `WAIT_TOOL_NAMES` — `wait` and `check_task_status`) gets its iteration refunded (`_waive_wait_round`), which is what stops a 10-minute background task from burning a 25-iteration budget in 20-second polls. Two guards keep it finite: the round must have *actually* paused (`MIN_GRANTED_WAIT_SECONDS`), so a refused or malformed wait or an instantly answered `check_task_status` is still charged, and refunds are capped at one budget's worth per run. Tokens are **not** refunded.
- **It does not consume the tool-call budget either** (`RunBudget`, 3d): one pause the run owns per batch rides free. Which pauses the run owns, and why a second one in the same batch is charged, is stated once, in the Tool calls row of `backend-tools-run-budget.md`.
- **It is bounded by policy instead.** `maximum_wait_time` (longest single pause) and `maximum_total_wait_time` (per-run cumulative) come from `general_assistant_config`, resolved into a `WaitPolicy` by each call site; the loop additionally clamps every pause to the remaining `wall_clock_deadline` minus a reserve, so waiting can never eat the budget the final answer needs. A clamp or refusal always names the bound in the tool result — a silent clamp reads as a completed wait and the model moves on too early.
- **It is interruptible from both sides.** A user cancel ends it instantly (the loop already races tool execution against `cancel_event`); a `skip_wait` ("Continue now") sets `wait_skip_event`, which ends *this* pause only and lets the turn continue. A skip-wait request reaching a process that does not run the turn (a turn resumed after a worker stop, a parent wake, a REST turn, a web turn on another process) is addressed to the in-flight turn through Redis (`chat_turn_guard.request_wait_skip`, bound to the marker token like the cross-process cancel); every steering bridge consumes it (`consume_wait_skip`) on its next tick and sets the event, and the ack `wait_skipped` carries `delivered`. The agent wakes itself early by naming a condition: `wait("5m", until="<ids>")` returns the moment those tasks are terminal, resolved by the `wait_condition` callback (`services/builtin_tools/wait.make_task_condition`) because the loop has no user context of its own. An id that is no background task is read as an assignment or flow run the person may see (`watched_run_statuses`: the agent tools' run rule plus the runs they started; a flow run under its flow's), its terminal status in the wait's words (`skipped` is `cancelled`) and its summary returned as the extract; any other id stays pending, like an unknown task. A run of the chat's own conversation that a wait reads ended is marked told (`assignment_run_wake.mark_told`), so the worker does not report it again.
- **It shows what it watches.** `wait_start` carries `items` - each watched id's kind, label, status and start, read through the condition's `details` hook - and the loop sends `wait_status` with fresh items whenever a poll sees a watched id change state (`execute_wait(on_status=)`), for the `wait` tool and the implicit park alike (`_WaitCard`), so the card shows the run running, then ended, instead of a bare countdown. Cosmetic and bounded (`_WAIT_ITEMS_TIMEOUT`): a failed read keeps the countdown.
- **It is offered by the loop, not by call sites** — appended to the per-iteration tool list after cooldown filtering, only when real tools are offered too (an empty tool list is the loop's "wind down now" signal) only when the provider's `max_tools` has room to spare, so the timer never displaces an agent's own tool, and never when the run already offers a tool named `wait` (that tool is the run's, and is charged like any other). `WaitPolicy.disabled()` opts a surface out entirely (the public embed chat).
- **Mixed rounds run work first.** If the model calls `wait` together with other tools, those sibling tools execute before the pause. Waiting is for when the parent is blocked, not a substitute for continuing independent work while background tasks run.
- **A final answer never parks on a child that never started** (#1661). The implicit park after a tool-free answer is skipped when every open child of the turn reads `pending` (queued, never started) and the answer is a deliverable, not a "waiting for" narration (`never_started_task_ids`): the run ends on its answer, the children keep running on their own and are named on the response (`ChatResponse.pending_task_ids`) and the iteration outcome (`detached_task_ids`). A running child, an unknown or unread status, or a waiting narration keeps the park.
- **A park names the task, never its id**. When the run ends `waiting_on_tasks`, the user-facing message renders each still-running task as `**<target agent>** - "<task description>"` (`TaskLabel`): the spawn exchange in the transcript first (`collect_task_labels` — description from the tool CALL's `task` argument, agent from the tool RESULT's `Target agent:` field), then the wait condition's `.labels` database hook for whatever the transcript lacks, and a count for the rest. Every park site and the wait-budget digest go through `build_waiting_on_tasks_message(...)`; never hand-build labels at a new site. **The spawn result block is one-field-per-line by construction**: `builtin_tools/handover.py` flattens the agent name (the block's only free-text field) when it renders `Task ID: / Target agent: / Status:`, and both parsers (`_SPAWN_TASK_ID_RE`, `_SPAWN_TARGET_AGENT_RE`) anchor on line starts — a `Task ID:` inside a name never registers a task, one result yields one task (first match). Transcripts persist results produced before that flatten, so the name parser still reads a multi-line value whole, up to the LAST `Status:` line (nothing free-text follows the block's real `Status:`, so `Research\nStatus: Analyst` reads whole; anchoring on a status token would be defeated by the same name). Three more invariants: **calls and results pair in transcript order, never through a global `tool_call_id` map** — an id is only unique within the response that minted it (Gemini derives `call_{function_name}_{index}` deterministically, so two turns opening with a spawn share `call_spawn_background_task_0`; every other provider passes opaque ids through with no cross-turn contract either), so a result consumes the oldest still-unanswered call carrying its id (FIFO per id, the same local pairing `_build_evictable_message_groups` relies on) and a repeated id never renders the older pending task under the newer task's description; **completeness, not mere presence, gates the fallback** — a spawn result whose tool call was pruned yields the agent alone, and that half-label still triggers the lookup (`TaskLabel.complete`), which then fills only the missing half (`TaskLabel.filled_from`) and never overrides what the transcript had; a fully-known label (or a deliberate self-copy omission, `agent_omitted`) costs no query. And **the lookup is bounded** (`_PARK_LABEL_LOOKUP_TIMEOUT_SECS`, shrunk further by `_label_lookup_budget(wall_clock_deadline)` inside the loop, down to zero): the label is cosmetic, so a stalled pool degrades to the transcript label instead of holding the park past the outer `asyncio.wait_for`. The forced-answer backstop's **wall-clock shortcut stays transcript-only** (sync `summarise_tool_results`, no database at all) — its contract is to return immediately, and a bounded round-trip is still a round-trip on the path that exists because the deadline is already upon us; the per-run-budget shortcut and the empty-forced-answer fallback keep the resolved digest under the deadline-aware bound.

### 3h. Long runs: the iteration cap as a block (`extend_iterations_to`)

`run_agent_loop(extend_iterations_to=N)` makes `max_iterations` the size of a block instead of the end of the run. At the top of an iteration, once the run reaches its first wrap-up point (the step-budget nudge, the wind-down, the tool strip - whichever comes first for the current budget), `_extend_iterations_if_working()` asks whether the block just spent made progress:
- a tool result succeeded since the block began (`tool_successes_observed`, the counterpart of `tool_errors_observed`, counted in `_track_tool_result`);
- the loop breaker refused nothing (`loop_breaker.events`);
- the failure brake and the token wind-down are off;
- the wall clock, when there is one, leaves `_EXTENSION_WIND_DOWN_ROUNDS` (4) rounds at the run's own pace plus `_IN_LOOP_MIN_REMAINING_SECS`.

When it did, another block of the original `max_iterations` is granted, up to `N`, by raising `max_iterations` as the wait refunds (3g) do, so every threshold, nudge and event payload keeps describing the real budget. The model is not told. The user reads one `thinking_text` line ("Still making progress after N steps ...") and the log one WARNING per grant. The first block that made no progress closes the extensions for the run, which then winds down through the usual nudge, tool strip and forced answer. The per-run budget (3d) still refuses calls, and its duration still bounds the wall clock.

`N` comes from `general_assistant_config.iteration_reach()`: `MAX_ITERATIONS_CEILING` (1000) while `AGENT_LOOP_AUTO_EXTEND_ITERATIONS` is on (the default), else the cap itself. Which callers opt in:
- `run_agent_chat`: a run of its own on the configured cap - metered (`meter_quota`), no turn of an evaluation (the evaluated one nor a task it delegated), no `max_iterations_override` pin (a flow node, a task an assignment delegated), an identified user (never the public widget or an anonymous channel sender). Its chat wall clock is `chat_loop_timeout_within(reach)`.
- `run_general_chat`: always; its wall clock is `max(300, reach * 60)`.
- The assignment engine: within the assignment's own timeout, which the loop winds down on. Not in dry-run mode.
- A chat-spawned task's deadline (`resolve_chat_sub_agent_timeout`) is sized for the reach too, except in an evaluation's chain, where it is sized for the cap.

### 3i. A run that ends on a limit says so

The loop answers even when it runs out of rounds or of wall clock, so that answer must not read as a finished task. The returned `ChatResponse.stop_reason` names the limit, set by `_with_limit_stop()` only over a plain `end_turn` (a `max_tokens` or `budget_exhausted` answer keeps its own reason):
- `ITERATION_LIMIT_STOP_REASON` (`"max_iterations"`): the run reached the iterations the cap leaves tool-free (`_iteration_limit_strip`: the Stage-2 strip at `strip_tools_at`, not the token budget, the failure brake or a park hand-off) and answered there or in the forced-final-answer backstop, or the loop reached `max_iterations` (after any 3h extension);
- `DEADLINE_STOP_REASON` (`"deadline"`): the wall clock left less than `_IN_LOOP_MIN_REMAINING_SECS` at the top of an iteration, or the backstop's immediate fallback had no time for a synthesis.

`UNFINISHED_STOP_REASONS` is the one list of stop reasons that mean "stopped before the task was done" (the two above, `budget_exhausted`, `degenerate_repetition`, `timeout`, `waiting_on_tasks`); `unfinished_stop_reason(response)` reads it. The assignment engine writes it to `AssignmentRun.stop_reason` on completion (NULL on a run that ended on its own terms), the run page shows the result as partial on it, and the agent builder's `get_run` names it. The run stays `completed`: the answer is kept, nothing failed. A chat turn is flagged from the loop's `wind_down` / `composing` events (`is_truncated`) instead.

### 3j. Session references: `$collected`, `$input` and `$workspace:PATH`

A string tool argument that is a session reference (`$name`, or `$name` followed by a path) is resolved before the call by `_resolve_session_reference()`: a whole object or array is injected as a JSON string (excluded from schema validation), a scalar natively. A reference to a known variable whose path does not navigate errors the call with feedback, never passes the literal placeholder through.

Path segments (`_SESSION_PATH_TOKEN_RE`, walked by `_walk_session_path()`): `.identifier`; `[0]`, a list index or the i-th value of an object (insertion order, the shape `collect_task_results` stores); `["any key"]` or `['any key']` for a key holding a dash, dot, colon or space (STIX `extension-definition--UUID` keys), a backslash escaping the quote or the backslash. The quoted alternatives are disjoint, so matching is linear. The error (`_session_reference_error()`) names the variable used, the failing segment and why (malformed, key not found, index out of range, not a container), and the syntax for that variable; a dotted key with a dash is answered with its bracket form.

- `$collected`: what `collect_task_results` merged (chat path, persisted per conversation).
- `$input` (`app.tools.session_input`): the JSON document of the newest user message: what follows a `--- NAME ---` delimiter line (the last one first), else the whole message, else what follows a fence line, else the first STIX bundle object anywhere in the text; a JSON value found first that is not a STIX bundle (an example in the instructions) gives way to a STIX bundle after it (#12263). Enabled by `run_agent_loop(input_reference=True)`, which `agent_chat` and `assignment_engine` pass when the agent's config sets `output_mode: "patch"` or `input_reference: true` (or finalises on `stix_bundle_validator`); `agent_chat` also passes it when the message carries a STIX bundle (`input_reference_for_run`: an OpenCTI playbook sends its instructions and the bundle as one message), unless the config sets `input_reference: false`. It lives beside `session_vars`, never persisted. A model's quotes around the placeholder (`"\"$input\""`, an escaped pair `\"$input\"` too) are dropped. A document over `INPUT_INLINE_MAX_CHARS` (100,000 characters) is cut out of its message before the first model call (`detach_input_document`: a new message object, the text around the document kept byte for byte) and replaced by a reference block (size, object count, counts by `type`, how to read it); it is staged for `code_interpreter` as `attachments/input.json` (`FileContext.sandbox_inputs`, listed in `attachments/index.json`, never a conversation file). A smaller document stays inline. A run without the opt-in (`input_reference: false` included) keeps the size protection: a document over `oversize_input_chars` (half the model window in characters, never under `INPUT_INLINE_MAX_CHARS`) is detached and staged the same way, its block naming `attachments/input.json` only, `$input` left unexposed; a smaller one is sent unchanged (#12263). A delegated run's system prompt never copies the document back: `format_handover_context_prompt` renders the task ("Original question") through `prompt_safe_task_text`, the same reference block then a cap of `PROMPT_TASK_TEXT_MAX_CHARS` (4,000) with a pointer to the message and `attachments/input.json`, and the source agent response through it uncapped; it is the one place `run_agent_once`, `spawn_background_task`, consult, transfer and assignment delegation copy the task text into a prompt (#12095). Opted in with no JSON document in the input, a `$input` reference errors the call. Without the opt-in, a `$input` placeholder in a parameter that is not declared as a string errors the call with the opt-in named; a string parameter (a search text, a span filter) that reads `$input` is passed through untouched.
- `$workspace:PATH` (`app.tools.base.resolve_workspace_references`, called by `CompositeToolExecutor._dispatch`): a top-level string argument that is `$workspace:` followed by ONE path token (`is_workspace_reference`: surrounding whitespace ignored, no whitespace inside, at most `WORKSPACE_REFERENCE_MAX_PATH_CHARS` = 1024 characters) is the content of that file of the run's sandbox workspace, read on the server at dispatch, so a large argument built in the sandbox (an RFC 6902 patch, a bundle) reaches the tool without the model typing it again and the transcript keeps the short reference. A parameter whose schema takes an object or an array (and no string) receives the parsed JSON, checked against that type; any other parameter receives the file as UTF-8 text. `PATH` follows the workspace path rules (`sandbox_workspace.paths.normalize`: relative to `workspace/`, no `..`, no absolute path); the file is at most `WORKSPACE_REFERENCE_MAX_BYTES` (4 MiB). The loop excludes the reference key from coercion and schema validation, like a `$collected` key. A run with no workspace, a path refused, a missing file, a file over the cap, not UTF-8, not JSON or of the wrong JSON type: the tool is not executed and the refusal names the fix. Works with `finalize_on_builtin_tool`: `stix_bundle_validator(input_bundle="$input", patch="$workspace:work/patch.json")` finalises exactly as with an inline patch. Prose that merely starts with the prefix (a memory, a note, a message body with a space or a newline after it) is not a reference and reaches the tool unchanged (#12034).

Patch output mode (`config.output_mode = "patch"`, the built-in `CTI STIX Transformer`): the agent calls `stix_bundle_validator(input_bundle="$input", patch=[RFC 6902 ops])` and `finalize_on_builtin_tool` returns the validated patched bundle as the run's output, so a value the instruction did not touch is never regenerated by the model and output tokens follow the patch, not the input.

### 5. How the Assignment Engine Uses Tools

In `services/assignment_engine.py` (simplified; the chat paths, `services/agent_chat.py` and `services/general_chat.py`, follow the same shape):

```python
from app.services.builtin_tools import cleanup_mcp_manager, load_mcp_tools
from app.tools.agent_loop import run_agent_loop
from app.tools.base import CompositeToolExecutor
from app.tools.mcp_client import MCPArtifactScope

composite = CompositeToolExecutor()

# MCP servers: the assignment's tool sources, else the agent's servers
mcp_manager = await load_mcp_tools(
    db,
    assignment.tool_sources or agent.mcp_server_ids or [],
    composite,
    allowed_tools=agent.mcp_allowed_tools or {},
    artifact_scope=MCPArtifactScope(user_id=..., assignment_run_id=str(run.id)),
)
# Integration, custom and built-in tools: composite.add_tools(definitions, executor)

# Scoping, in order, before run_agent_loop and the progressive tool gate:
# integration_allowed_tools (filter_integration_tools) and mcp_allowed_tools
# (load_mcp_tools) at load time; builtin_allowed_tools
# (filter_builtin_tools: built-in group id -> function names, keyed on the
# definition's source "builtin:<id>", agent and assignment lists intersected
# with merge_tool_allow_lists, a core tool never removed); then
# apply_tool_deny_list, which wins over every allow list. tool_deny_list is
# written by POST/PUT /agents and the agent builder through one rule,
# normalize_tool_deny_list: names trimmed, blanks and repeats dropped, at most
# 500 names of 256 characters, a core tool refused; an empty list is NULL.
# A release that adds a function to a group an agent scopes never grants it:
# builtin_tool_release_notice sends each owner one bell entry per release
# (kind builtin_tools_outside_allowlist) opening
# /agents/<id>/config/tooling?added=<group>.<fn>,..., where the Tooling
# section lists the ones still left out with an Add / Add all button.
# Dismiss / Dismiss all (#12245) call POST /agents/<id>/builtin-tools/notice
# {dismiss: ["group.fn", ...]}: the names land in agent.config
# ["builtin_tools_dismissed"] (audited, never granted), the section and later
# releases stop naming them for that agent (still_outside), and
# resolve_notices marks read the caller's bell entries about the agent once
# nothing they name is left out; an Add calls it with dismiss [] for that.

try:
    result = await run_agent_loop(
        llm=llm,
        messages=[system_msg, user_msg],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=...,  # plus budget=, wall_clock_deadline=, extend_iterations_to=, ...
    )
finally:
    await cleanup_mcp_manager(mcp_manager)  # mcp_manager.disconnect_all()
```

## LLM Provider Tool Calling

Every provider (`app/llm/*_provider.py`) supports native tool calling through the `BaseLLMProvider` interface:

- `tools` parameter on `llm.chat()` accepts `list[ToolDefinition]`
- Response `ChatResponse.tool_calls` contains `list[ToolCall]` when the LLM wants to use tools
- The agentic loop handles the back-and-forth automatically
