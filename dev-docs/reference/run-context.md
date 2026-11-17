# Run context: a run is told its own identity

Source: `backend/app/services/run_context.py`. Issue #8626.

## Why

An assignment run or a background task never saw its own `run_id`. When it
recorded something about itself (a friction disposition, a shared-state note,
a deploy-watch entry) it named itself by the trace id `inspect_my_traces`
shows, and every reader keyed on the run id (`get_run`, the friction ledger,
the `run.completed_with_undisposed_frictions` event) missed the record.

## What the model sees

One block appended to the run's prompt, after the trigger information:

```
--- Run context ---
run_id: 3f0c..., assignment_id: 9a1e..., trigger: platform_event
Name this run by its run_id (the id get_run reads) in anything you record about it.
Started at 2026-11-05T05:30:12Z (UTC); after a wait or a resume, the time to stamp what you write is the one the wait answer or the resume note ends with (It is now ...).
```

| Run class | Where it is built | Fields |
|---|---|---|
| Assignment run (schedule, webhook, platform_event, work_item, ...) | `assignment_engine` after the follow-up override, `build_run_context(run)` | run_id, assignment_id, trigger |
| Run outside an assignment (chat turn parent run, follow-up) | same | assignment_id `null` |
| Background task (delegation) | `sub_agent` handover context `run_context`, rendered by `format_handover_context_prompt` for `sub_agent_task` only | run_id (the task's child run), assignment_id `null`, trigger `delegation`, parent_run_id, task_id |

The block carries identifiers only: no payload, header or secret.
`parent_run_id` and `task_id` are printed only when set.

## The live clock (issue #8660)

Source: `backend/app/services/run_clock.py`.

The prompt states `Current date and time (UTC)` once, at the start of the
turn. After a `wait`, a long tool call or a resume the model extrapolated and
the times it wrote in its notes drifted by minutes from the platform stamps.

- The `wait` answer (`wait_policy._format_outcome` and `_format_partial`)
  carries `It is now YYYY-MM-DDTHH:MM:SSZ (UTC).`, the time the pause ended.
- The resume note (`turn_checkpoint.resume_note`) carries the same sentence,
  the time the turn resumes at.
- Other tool results are left as they are: their bytes stay what the
  tool returned (tests, dedup and caching compare them), and the time a
  model writes is drifted by waits and resumes, not by ordinary calls.
- The Run context block names the start time (`AssignmentRun.started_at`, else
  now) and where the live clock is read.

Tests: `backend/tests/test_run_clock.py`.

## inspect_my_traces

The in-progress item (the first item when called inside a run) carries
`run_id` and `assignment_id` beside its trace `id` (`null` for a chat turn
without a run).

The answer is bounded for a long run (#8828): with `include_spans=true`, each
trace lists at most `max_spans` spans (default `_DEFAULT_MAX_SPANS`, 30) from
`span_offset`, with `spans_total`, `spans_offset`, `spans_next_offset` and a
`spans_note` when spans are left out; the rollup is computed from every span
before the cut. All previews of one answer share `_RESPONSE_PREVIEW_BUDGET_CHARS`
(12,000), and the serialized spans of one answer share
`_SPAN_LISTING_BUDGET_CHARS` (18,000, #10603): a page stops before the span that
would cross it, `spans_next_offset` names that span and `spans_note` says the page
stopped at the size budget, so walking the offsets skips nothing. A span's
`status_message` is cut at `_STATUS_MESSAGE_MAX_CHARS` (400) with a marker naming
`get_trace_payload(trace_id, span_id, field=output)`. The per-tool table of a
rollup keeps the `MAX_TOOLS` (25) heaviest tools with `tools_omitted`, as the
stored rollup does. `rollup_only=true` answers the trace headers and their whole rollup,
per-tool table included, with no span and no preview: the form a listener or a
self-measurement reads. `inspect_agent_traces` takes the same three arguments.

Span search: `span_tool` (a tool name, any case, bare such as
`sandbox_run_command` or with the tracer's `tool:` prefix, both match the
same spans) and `span_match` (a substring,
any case, of the output or the arguments), alone or together, answer only the
matching TOOL spans of each trace as `span_matches` (span id, bare tool name, start,
failed, the arguments' first 300 characters and 400 characters of the output
around the first hit), with `span_matches_total`, `spans_searched` and
`spans_next_offset`; `max_spans` and `span_offset` page the matches. A
`span_tool` that names no tool span of the trace adds `span_tool_note`, the
tools the trace did call, so a wrong name is never read as no match. No rollup,
no span listing and no trace preview are repeated, so walking the matches of a
50-iteration run costs a few thousand characters where paging the listing cost
hundreds of thousands. An offloaded output is searched in its stored excerpt;
`get_trace_payload(span_id=...)` reads the whole of a match. Both listing tools
take it.

Reasoning (#12356): the listings and `get_trace_payload` show a model call's
`reasoning` output, a replayed message's `thinking` and every closed
`<reasoning>` / `<thinking>` / `<think>` block in their text as
`[reasoning omitted]` (`leaked_reasoning.without_reasoning`), since Claude
declines a prompt that carries it back (`reasoning_extraction`). `span_match`
searches that text. `get_trace_payload(include_reasoning=true)` returns the
stored text, paged from offset 0, and is audited as such. Stored traces never change.

`trace_id` (UUID or 8+ hex prefix, #9180) answers one
of the calling agent's own traces in `inspect_my_traces`, however old, with its
rollup and the same span window; another agent's trace answers not found there
(supervised reads stay with `inspect_agent_traces` and `get_trace_payload`). Every GENERATION span
carries `ttft_ms`, `cache_read_tokens` and `cache_write_tokens`.

A run id works as `trace_id` too (#9225), in `inspect_my_traces` and
`get_trace_payload`: the id a work item names as its holder ("held by run") is
not a trace id, so an id (or 8+ hex prefix) that is no trace is looked up as
`llm_traces.assignment_run_id` among the calling agent's root traces
(`_own_trace_of_run` in `builtin_tools/traces.py`). One trace answers; a run
with several (a resumed attempt) is refused naming them in `get_trace_payload`;
another agent's run stays not found, unless the caller supervises it.

A supervised agent's run by its run id (#10586,
dev:supervised-run-id-trace-lookup): when an id is neither a visible trace nor
a run of the calling agent, `get_trace_payload` looks it up among the root
traces of the agents the caller may supervise (the allow-list; every agent for
an admin granted the supervision surface), under `trace_visible_query`
(`_supervised_run_root_traces`). One trace is read with the access check and the
audit row of any trace id; a resumed run is refused naming each trace; a caller
with no grant searches nothing and gets "trace not found, or not visible to
you" with no audit row. `inspect_agent_traces(trace_id=...)` takes a trace id,
prefix or run id inside the listing's supervision scope and answers every root
trace of a run, oldest first (`_scoped_traces_by_id`), resolved before the audit
write. Every listed trace carries `run_id` when it was recorded by a run.

The root traces of one run are not bounded by the candidate cap of an
ambiguous id prefix (`MAX_CANDIDATES`, 5; dev:supervised-run-id-trace-lookup-retest):
a run resumed through a series of rollouts records more, and the newest, the
attempt that holds the outcome, was the one cut. A run id reads up to
`RUN_ROOT_TRACES_MAX` (50) root traces; past it the listing shows the oldest 49
and the newest with a `run_note` saying the run was cut. The refusal of
`get_trace_payload` on a resumed run says how many traces it has, names each one
oldest first, names the newest apart, and names the listing call
(`inspect_my_traces` or `inspect_agent_traces` with the same `trace_id`) that
answers them all.

In `inspect_my_traces` the run id resolves before any trace lookup
(dev:inspect-my-traces-own-run-id): the calling run's own run id (full, 8+ hex
prefix, any case) answers its live summary while it is in progress, earlier
segments of a resumed run folded in, exactly as `limit=1` does; it is not
stored yet, so a lookup in `llm_traces` answered "trace not found". Any other
run of the agent answers every stored root trace of that run, oldest first
(`_own_traces_of_run`), one per resumed attempt, instead of the ambiguity
refusal.

`get_work_item(field="result", offset, max_chars)` windows a work item's
result the way `offset`/`max_chars` window its description (#9225): the full
view cuts a long result to its share of the answer and names this call.

Tests: `backend/tests/test_run_context.py`, `backend/tests/test_trace_builtin_tools.py`,
`backend/tests/test_trace_generation_figures.py`.
