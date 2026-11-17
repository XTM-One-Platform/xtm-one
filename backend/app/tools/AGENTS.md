# Tools & Agentic Loop – Developer Guide

The agentic loop (`run_agent_loop`), tool executors, gating, run budget and MCP client.

## Where to look

| Need | Read |
|---|---|
| Loop, key files, name resolution, failure classifiers, steering, `wait`, long runs | `dev-docs/reference/backend-tools-agent-loop.md` |
| `load_tools` gating, `max_tools` cap, LRU, search | `dev-docs/reference/backend-tools-tool-gating.md` |
| Context curation, vision, dedup cache, compaction | `dev-docs/reference/backend-tools-context-compaction.md` |
| Run budget, downgrade, override layer | `dev-docs/reference/backend-tools-run-budget.md` |
| Argument coercion/validation, loop breaker | `dev-docs/reference/backend-tools-loop-guards.md` |
| MCP lifecycle, artifacts, resource links | `dev-docs/reference/backend-tools-mcp.md` |
| Reasoning/answer streaming, `tool_progress` | `dev-docs/reference/backend-tools-streaming-events.md` |
| Code | `agent_loop.py`, `base.py`, `tool_gating.py`, `run_budget.py`, `wait_policy.py`, `tool_events.py`, `mcp_client.py`, `services/budget_policy.py`, `services/schema_guard.py` |

## Rules

- **Generic loop** (invariant 18): no tool, vendor or provider name, no word list read from the answer, no parsing of a platform tool's text; a tool-specific behaviour is a property on its `ToolDefinition` (MCP: annotations). `test_no_workarounds.py` only shrinks.
- **No prompt inflation** (invariant 16): guards are deterministic, steer through tool results and one-shot nudges, never system-prompt text.
- **Tool names**: `base.ToolNameResolver` is the one rule for which model name means which tool; gate and executors share one instance over the offered surface (`_ExecutorNameMap`, `visible_aliases()`). Never re-implement it or pre-process names; only `_exec_call` maps names back.
- **Failures**: classify with the `base.py` classifiers (`is_error_shaped_result`, `is_auth_failure_result`, `is_rate_limit_error`); never a substring search for "401".
- Set `ToolResult.target_throttled` for a per-target throttle, `outcome_failure` for a right call whose paid work came out rejected (counted per call), `refused` for a deliberate refusal; a no-op says so in one line and sets `no_op`. Never word an error to dodge the classifier, nor turn a no-op into one.
- A result or description names another tool only if `offered_tools()` carries it.
- `load_tools` and `wait` run inside the loop, never through the executor; the loop offers `wait`, never call sites. A clamped or refused wait names its bound.
- Park messages: only `build_waiting_on_tasks_message()`. Pair calls and results in transcript order, never by a global `tool_call_id` map.
- **Gating**: `max_tools` caps the visible list, never the inventory; a gated run never drops a tool, keeps room for `load_tools` + one gated tool (`MIN_GATED_BUDGET`), and lists every not-loaded name (no "and N more").
- "The offered tools" are read live (`_offer_at(iteration)`), never a snapshot.
- **Context gates** measure with `_transcript_chars` / `message_payload_chars` (provider extras included, never stripped), never a `len(m.content)` sum. Triggers: real signals, never a fixed char threshold.
- A tool result is not curated before a completed request carried it; `_consume_transcript()` alone marks the transcript consumed and writes `FileContext.vision_views`. Never raise the vision budget to fix a vision problem.
- Dropped pixels: only `_drop_tool_result_files()` (file id + `read_file_content`). A new file-bearing tool's "attached" sentence must match `_ATTACHMENT_CLAIM_RE`, or extend it in the same change.
- `read_file_content` stays in `DEDUP_EXEMPT_TOOLS`; tools declare output-naming arguments (`ToolDefinition.cosmetic_arguments`) and pollers `ToolDefinition.live_state`, never a name list; the dedup cache stores successes only.
- Emergency truncation: `_emergency_truncate_text()` (never longer, never splits an untrusted block).
- **Budget**: enforced in the loop, never per provider. `_merge_usage` is the single usage sink (compaction and backstop calls included, each once); `cost_calculator.price_usage` the one pricing rule; no price row fails open. A budget-cut answer is flagged `stop_reason="budget_exhausted"`, never delivered as complete.
- Budget authority sits at the API edge: an endpoint writing `budget_config` or a graph refuses (`assert_budget_within_ceiling`) or lowers (`lower_budget_to_ceiling`); the agent's budget bounds canvas layers at resolution (`bound_by`).
- **Validation**: `coerce_tool_arguments()` alone decides what a string may become; never a lenient branch in the validator.
- Tool schemas are attacker-controlled: validate via `_argument_validator()` on `schema_guard`; never `jsonschema.validate`, `validator_for(schema)` or `validators.extend` alone (`test_schema_guard.py`).
- **Loop breaker**: every append of a counted result calls `_record_breaker_answer(tc)`; the re-fetchable stub is written and matched from `_REFETCHABLE_STUB_TEMPLATE` only. The failure brake is for failures, never no-ops. Failure streaks count failed turns per tool, never parallel calls (`tool_error_turns`).
- **Streaming**: answer text shown is never taken back (`segment_complete` with `deliver_segments`, else `stream_retract` for regeneration only); `streaming` never clears thinking text.
- **`tool_progress`**: `emit_tool_event()` is the one channel; consumers key on `event`, never `tool`; pass ids, not rows; tools read `current_tool_deadline()`, never re-derive loop limits.
- **MCP**: env/headers Fernet-encrypted, connections closed after the run (`disconnect_all()`); binary results persist via `persist_tool_artifact(source="mcp")` under the run's `MCPArtifactScope`; `read_mcp_resource` reads only links a tool of the run returned, never a platform fetch.

## Tests

`test_no_workarounds.py`, `test_progressive_tool_disclosure.py`, `test_loop_breaker.py`, `test_*run_budget*.py`, `test_tool_arg_*.py`, `test_semantic_compaction.py`, `test_tool_events.py`, `test_wait_tool.py`.
