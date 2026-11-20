# Harness performance baseline, targets and instrumentation plan

Program `dev:harness-perf`, slice P0. The metric catalogue, targets and instrumentation plan every later slice measures against. It covers the whole platform, not only agentic coding.

- **As of:** 1 Nov 2026, main.
- **Where the numbers live:** production observations (counts, latencies, error and failure figures, queue ages, rollout cadence) are not committed to this repository. They are recorded in the access-controlled program record `dev:harness-perf` (the fleet's shared state) and read live from the trace and analytics surfaces, which require authentication. This page defines what is measured, from which source, and what the target is.
- **Rule:** every later performance PR quotes, from the program record, the value of the metric it moves, before and after, on the same task class and the same window length, and states the relative change in its PR. A PR that cannot quote a metric first lands the instrumentation it needs (section 4).

## 1. Sources and their limits

| Source | What it gives | Limit |
|---|---|---|
| Trace analytics (platform scope) | traces, errors, average duration by source; median and p95 by model and by day; tokens and cache | Average only by source; no time to first token; `background task`, `consult` and `platform_event` are not separate sources |
| Release history | build and first boot | No backfill before its start |
| Platform error traces | error class of the newest errors | No grouping by error text; weekly counts per text not obtainable |
| Agent self traces with rollup | iterations, generations, per-tool calls and result size, span timings | Sample, not a census; a run cut before its end has no tokens |
| Event channel ledger | `work_item.completed` to run start | Upper bounds (run start read from the run's own clock); feeds section (c) only |
| Fleet orphan ledger | `run.orphaned` and `claim_released` events, counted per rollout | Rollout count in the window is estimated from the release history; feeds section (h) only |
| Desk run metrics | iterations, minutes, biggest waste | Hand-recorded; tokens often missing |

Every value in the program record states its source, its window and its `n`. p90 on fewer than 10 samples is the second-highest value and is marked `(small n)`.

## 2. Metric catalogue per component

Each metric below has a value in the program record, or "no data" with the instrumentation that fills it (section 4).

### Platform totals

| Metric | Source |
|---|---|
| Traces and error traces per 7 days, error rate, against the previous 7 days | trace analytics |
| Average trace duration per 7 days | trace analytics |
| Tokens and cached tokens per 7 days | trace analytics |
| Platform median latency by day | trace analytics |

### (a) Chat turn

| Metric | Source or missing instrumentation |
|---|---|
| agent-chat trace duration and error rate | trace analytics (average only by source) |
| Trace latency by model, median and p95 | trace analytics |
| One model generation inside a turn, median and p90 | agent self traces |
| Tool round trip by tool family (GitHub read, shared state and work items) | agent self traces |
| Time to first token | no data: needs `ttft_ms` on the generation span (4.2) |
| Turns per minute per conversation | no data: needs a per-conversation turn counter |
| Cut attempts per completed turn | error traces cancelled by a graceful shutdown against completed agent-chat traces (derived) |

### (b) Agent loop

| Metric | Source |
|---|---|
| Input tokens per generation, median and p90 | agent self traces |
| Iterations and minutes per task (desk) | desk run metrics |
| Share of generations that miss the prompt cache, and their share of the turn's cost | agent self traces |
| Agent memory block injected per turn (chars) | memory span |
| Tool schemas offered per turn | agent configuration |
| Skill load result size | agent self traces |

### (c) Work items

| Metric | Source or missing instrumentation |
|---|---|
| `get_work_item` result size, median and p90 | agent self traces |
| `work_item.completed` to event run start, median and p90 | event channel ledger (upper bounds) |
| Open queue depth and age of the oldest open item, per owner | work items listing |
| Queue age distribution | no data: needs `open_since` percentiles per owner in the work items analytics |

Known cause of the long tail: event runs of one agent are delivered one after another during a completion burst.

### (d) Assignment runs and background tasks

| Metric | Source or missing instrumentation |
|---|---|
| Share of assignment runs that end in 1-2 s with 0 tokens | agent self traces |
| Input tokens and duration of runs with tokens | agent self traces |
| Wait share of run wall time (runs that watch CI) | agent self traces |
| Resume cap deaths | platform error traces |
| Start latency (queued to started) | no data: needs `queued_at` on the run trace (4.5) |

### (e) Sandbox

| Metric | Source or missing instrumentation |
|---|---|
| Minutes lost to create answered 5xx (cold pod) | desk run metrics |
| Lab service start time | agent self traces |
| Check durations (`sandbox_run_command`) | agent self traces |
| `code_interpreter` timeouts on a large workspace, against workspace size | desk run metrics |
| Container create time, warm or cold | no data: needs `create_ms` on the span (4.3) |

### (f) GitHub and other integrations

| Metric | Source or missing instrumentation |
|---|---|
| GitHub read latency, median and p90 | agent self traces |
| `create_github_commit` duration | agent self traces |
| GraphQL search answered 5xx | desk run metrics |
| Result size of job logs, GraphQL searches and PR bodies against the useful part | desk run metrics |
| 5xx rate per provider, payload bytes per call | no data: needs the tool histogram (4.2) |

### (g) Video Studio and presentations

| Metric | Source or missing instrumentation |
|---|---|
| Preview and render queue wait | no data: needs `queued_at`, `leased_at`, `finished_at` on the render (4.5) |
| Time a render stays without a lease holder | no data: same fields |

### (h) Rollouts

| Metric | Source |
|---|---|
| Builds per day, minutes between builds | release history |
| Orphaned runs per rollout (orphan events / 2, as `claim_released` and `run.orphaned` come as twins, over the rollouts of the same window) | fleet orphan ledger; the gate in section 3 recounts on the same numerator and denominator |
| Error traces per rollout | trace analytics and release history |

## 3. Targets and the ten highest-impact fixes

### Targets (end of program, same window length, same task class)

| Component | Target |
|---|---|
| Platform | error rate under 1 %, daily median latency back to the level before the rollout burst |
| (a) Chat turn | agent-chat average -50 %, cut attempts per completed turn 0, time to first token measured and p90 under 3 s |
| (b) Agent loop | input tokens per generation -40 %, cache misses only on the first generation of a turn, desk minutes per task -50 % |
| (c) Work items | `get_work_item` median under 5 k chars, event pick-up p90 under 60 s, no item older than 24 h without a holder or a block |
| (d) Runs | zero-token runs not created at all, wait share under 15 %, idle wait over 2 min 0, resume cap deaths 0, duplicate holders 0 |
| (e) Sandbox | minutes lost to 5xx 0, create p90 under 15 s, `code_interpreter` p90 under 10 s on a 250 MB workspace |
| (f) Integrations | minutes lost to 5xx 0, GitHub read result under 5 k chars by default |
| (g) Video Studio | queue wait measured; render never without a lease holder over 2 min |
| (h) Rollouts | orphans per rollout 0; builds batched so a run sees at most one rollout an hour |

### Ranked fixes

Every gate runs the named checks of `.xtm-one/checks.toml` and records the metric before and after in `dev:harness-perf`.

| # | Fix | Component | Expected gain | Files | Gate |
|---|---|---|---|---|---|
| 1 | Batch production builds (merge train or a minimum interval between rollouts) and finish the worker drain so a rollout never cuts a turn | (h), (a), (d) | error traces -80 %, cut attempts per turn to 0, platform median back to its level before the burst | `.github/workflows/deploy.yml` (concurrency), `backend/app/core/worker_shutdown.py`, `backend/app/core/shutdown_finalizers.py` | backend-unit, backend-tripwires; error rate under 1 % and orphans per rollout 0 over 24 h |
| 2 | Keep the cached prompt prefix byte-stable inside a turn: find and move volatile bytes (the workspace manifest with modification times in the `code_interpreter` description, any clock or file list in the system prompt) after the last cache breakpoint | (b), (a) | cost per turn -40 to -60 %, generation latency lower on the turns that miss today | `backend/app/services/builtin_tools/media.py` (`code_interpreter_description`), `backend/app/services/sandbox_workspace/manifest.py`, `backend/app/llm/anthropic_provider.py` | backend-unit, backend-snapshots; cache-read share per generation over 90 % after the first |
| 3 | Deliver events in parallel per agent instead of one run after another | (c) | event pick-up p90 under 60 s | `backend/app/services/platform_events.py`, `backend/app/services/assignment_engine.py` (`drain_assignment_queues`) | backend-unit, backend-tripwires; pick-up p90 under 60 s on a 10-event burst |
| 4 | Drop non-matching events before a run exists (server-side filter expressions), so no 0-token run and no full-context run that concludes "nothing to do" | (d) | Developer assignment runs -50 %, their tokens -30 % | `backend/app/services/platform_events.py` (`in_scope`, `mismatch_reason`) | backend-unit; 0-token run share under 5 % |
| 5 | Sandbox create: warm pool per heavy image, fail fast with the reason instead of a long wait on a cold pod | (e) | minutes lost to 5xx to 0, create p90 under 15 s | `backend/app/services/_sandbox_selector.py`, `backend/app/services/sandbox_workspace/commands.py` | backend-unit; 5xx count 0 and create p90 under 15 s over 24 h |
| 6 | Stage only what changed (hash diff) into `code_interpreter` and checks, never the whole workspace | (e) | timeouts to 0, `code_interpreter` p90 under 10 s | `backend/app/services/sandbox_workspace/session.py`, `kernel.py`, `commands.py` | backend-unit; p90 under 10 s on a 250 MB workspace |
| 7 | `get_work_item` answers the brief, the holder and the latest notes, the rest on request | (c), (b) | result size -80 %, median under 5 k chars | `backend/app/services/builtin_tools/work_items.py` (`_full_item_text`) | backend-unit, backend-snapshots; median result under 5 k chars |
| 8 | Shrink the fixed context of every generation: lazy groups for rarely used built-in tools, a capped memory block, old tool results compacted | (b) | input tokens per generation -40 % | `backend/app/services/builtin_tools/` (group registry), `backend/app/services/agent_memory*`, the agentic loop history builder | backend-unit, backend-tripwires; input tokens per generation -40 % on the same task class |
| 9 | GitHub compact reads by default (failure blocks, counts instead of nodes) and a split plus retry on GraphQL 5xx | (f) | GitHub result chars -70 %, 5xx minutes to 0 | `backend/app/integrations/providers/github/actions.py`, `graphql.py`, `pulls.py`, `search.py` | backend-unit; median GitHub result under 5 k chars, 5xx retries visible in the tool histogram |
| 10 | Event-driven CI watch (a check-suite event wakes the run) instead of `wait` polling | (d) | wait share under 15 %, desk minutes per task -30 % | `backend/app/integrations/providers/github/pr_events.py`, `backend/app/services/platform_events.py` | backend-unit; idle wait over 2 min 0 |

Slices already in flight that carry part of this list: rollout resilience (P5), sandbox create reason, compact GitHub reads, the `get_work_item` window, event filter expressions, the assignment run resume streak.

## 4. Instrumentation slice (ready brief)

**Goal:** every metric marked "no data" above gets a value, and every fix in section 3 can show before and after from the trace store alone. Telemetry only: no change to agent behaviour, prompts or security boundaries.

### 4.1 Per-run rollup stored on the trace
- **Where:** `backend/app/services/langfuse_tracing.py` (trace close), `backend/app/services/builtin_tools/traces.py` (`_run_rollup`, today computed on read from truncated spans).
- **What:** at trace close, write `metadata.rollup`: iterations, generations, tool calls and errors, `tool_result_chars`, input, output, cache-read and cache-write tokens, `wait_ms` (sum of `wait` tool spans), resume count and take-back cause, build SHA.
- **Done when:** `inspect_my_traces` with `include_spans=false` shows the rollup; a run cut by a rollout carries its partial rollup.
- **Shipped (resumed runs, #8584):** an assignment run resumed after a worker stop records one trace per attempt, all with its `assignment_run_id`. Called inside the resumed attempt, `inspect_my_traces` folds the stored rollups of the earlier attempts into the in-progress one (`run_rollup.merge_segment_rollups`): iterations, model and tool calls, tokens, wall, model, tool and wait time, the per-tool table and the largest results are the run's totals, with `segments` and `resumes`. A failed read of the earlier traces keeps the segment's own figures.
- **Shipped (segments lost with their worker, #8598):** only a segment that ends on its own worker (a hand-back after a stop signal) stores its trace; a segment taken back after its worker said it was stopping, or lost without a word, leaves none, since its spans lived in that worker's memory. `resumes` is therefore read from the run's own record (`trigger_context["resumes"]`, written by `assignment_run_resume.prepare_resume`) when it says more than the stored traces: `segments` is `resumes + 1`, `untraced_segments` counts the segments with no trace, and the note says their figures are missing from the totals.

### 4.2 Generation and tool latency
- **Where:** `backend/app/llm/anthropic_provider.py` (stream path, `_usage_from_response`); the tool span writer in `langfuse_tracing.py`; `backend/app/services/platform_insights/analytics.py`.
- **What:** `ttft_ms` and cache tokens on every generation span. On every tool span: `provider` (integration id or `builtin`), `http_status`, `retries`, `result_bytes`. Analytics: p50, p90 and p95 by source (today only an average), plus a `tool_latency` metric by tool and provider with its 5xx count.
- **Done when:** the analytics answer "p90 GitHub read latency and 5xx rate, 24 h" and "p90 time to first token by model" in one query.
- **Shipped (time to first token):** every streamed generation span carries `time_to_first_token` (ms, the existing `llm_spans` column) and `metadata.ttft_ms`; the run rollup carries `first_token_ms` (trace start to the first streamed output, what a chat user waits) and `max_ttft_ms`, and `first_token_ms` joins the rollup percentiles of `list_platform_traces`. A non-streamed call records nothing. A tool-calling generation (`chat_stream_with_tools`) measures its first output at the provider's first content block, text, thinking or tool call (`StreamUsageSink.bind_first_output`, #9076): before, a generation that answered with a tool call alone, on a model whose thinking is omitted, recorded no time to first token, and `max_ttft_ms` read `null` on every fleet trace.
- **Shipped (per-generation read, #9180):** `inspect_my_traces` and `inspect_agent_traces` list on every GENERATION span `ttft_ms` (the span's `time_to_first_token`, null when the call did not stream), `cache_read_tokens` and `cache_write_tokens` (from `usage_details` `cache_read_input_tokens` and `cache_creation_input_tokens`), beside `input_tokens` and `output_tokens`; two calls of one turn compare directly, a first-call cache miss reads as cache writes with no cache reads. `inspect_my_traces(trace_id=...)` (UUID or 8+ hex prefix) opens one of the calling agent's own traces however old, with its rollup and the same span paging; another agent's trace answers not found.
- **Shipped (time window and rollup filters, #9272):** `inspect_my_traces` takes `started_from` / `started_to` (ISO 8601, a naive time read as UTC; `started_from` replaces the `period` bound, an inverted window is refused), `min_generations` / `max_generations` (the rollup's `generations`) and `streamed_only` (the rollup carries `turn_first_token_ms` or `first_token_ms`), and pages with `offset`, the answer naming `next_offset` while matches remain. The rollup filters run on the rollup stored at close, so they read the window newest first by pages of 100, at most 500 traces, and say so in `filter_note` when that cap left traces unread; a trace without a stored rollup never matches a set filter. Every trace line carries its `conversation_id` when it has one. Own traces only, as before: `inspect_my_traces(started_from=T, max_generations=3, streamed_only=true)` lists the agent's streamed 1 to 3 generation turns since T with their conversations in one call (the chat turn latency read of `dev:chat-reactivity` R5).
- **Shipped (supervision window, #740):** `inspect_agent_traces` takes the same `started_from` / `started_to` window and `offset` paging, forwarded to the same reader: a supervisor reads another agent's runs of a given hour, or pages past the newest, inside the supervision scope it already has (the window narrows the time, never the agents read).
- **Shipped (chat turn pre-generation timeline, #8505):** a chat turn records where its time goes before the first provider request (`app/services/tracing/turn_stages.py`). The web chat starts the timeline when the message arrives (`history`, `compaction`, `chat_turn_setup`), `run_agent_chat` checkpoints its setup (`setup`, `memory`, `prompts_and_skills`, `prompt_context`, `integrations_kb_handover_load`, `scope_and_bindings`, `integration_tools`, `custom_and_mcp_tools`, `tool_assembly_and_guards`) and the first `start_llm_generation` of the run closes it (`loop_to_first_request`). `end_trace` writes `rollup.turn_stages` (stages in order with ms), `rollup.pre_generation_ms` and `rollup.turn_first_token_ms` (message arrival to first streamed output); the last two join the rollup percentiles. Timings only; a nested consult or transfer records its own timeline, never into its parent's.
- **Shipped (memory stage budget, #8693):** the first production timelines put `memory` at 67% of pre-generation (1458.5 of 2188.7 ms). The chat turn now bounds its persistent-memory query embedding with `CHAT_TURN_EMBED_BUDGET_SECS` (0.4 s, `app/services/persistent_memory.py`) instead of the 30 s setup cap, past which it takes the recency load; assignments keep the 30 s cap. The MEMORY span output carries `agent_memory_ms`, `persistent_memory_ms` and `user_memory_ms`, so the tier holding the stage is read from the trace.
- **Shipped (persistent tier deadline, #8757):** the first carrying build still showed `memory` at 481 to 546 ms with `persistent_memory_ms` the holder, because the 0.4 s bounded the embed alone and the provider resolution and the queries came on top. The chat turn now bounds the whole wait for the embedding from the start of the tier (`CHAT_TURN_EMBED_DEADLINE_SECS`, 0.3 s): the provider resolution runs alongside the eligibility probe, the embed gets what is left of the deadline, and below 50 ms it is skipped for the recency load. The MEMORY span splits the tier into `persistent_probe_ms`, `persistent_resolve_ms`, `persistent_embed_ms`, `persistent_vector_ms`, `persistent_recency_ms`, with `persistent_embed_timed_out` / `persistent_embed_skipped` when the semantic search gave way.
- **Shipped (loop_to_first_request split, #9043):** production turns showed `pre_generation_ms` of 195 to 287 s, all in the single `loop_to_first_request` stage. The timeline now checkpoints `run_setup` (`run_agent_chat` after the tool assembly: pipeline prepare, loop config, wait policy, approval gate, run budget), `loop_setup` (`run_agent_loop` before its first iteration), `continued_cut_wait` (a resumed turn finishing the `wait` its stopping worker cut, `_continue_cut_wait`) and `iteration_context` (steering admit, context curation and compaction of the first iteration); `loop_to_first_request` is what remains up to the provider request. A stage in `turn_stages.WAIT_STAGES` (`continued_cut_wait`) stays on the timeline but is left out of `pre_generation_ms` and reported as `pre_generation_wait_ms`: the percentiles measure preparation, not a pause the turn was asked to make. The loop marks through `turn_stages.mark_current(ctx, name)`, which finds the root timeline from any span and does nothing without one.
- **Shipped (overlapped recency, unbounded unwind, #8801):** the build carrying #8757 still showed `memory` at 619 ms: `persistent_embed_ms` 429 ms with `persistent_embed_timed_out` true (window at most 300 ms), then `persistent_recency_ms` 112 ms in series. `asyncio.wait_for` waits for the cancelled embedding to unwind (an HTTP client closing its request), and the recency query ran only after the embed. With a deadline the chat turn now reads the recency rows on the request session while the embedding is in flight, then releases the connection, and bounds the embed with `_embed_within`, which leaves a timed-out call to finish on its own. The span adds `persistent_embed_window_ms`. Assignments (no deadline) keep the sequential path.
- **Shipped (recency index, #8836):** after #8801 `persistent_recency_ms` still read 217 to 451 ms on agents with a large pool: the recency query (`agent_id`, not consolidated, active for the reader, `ORDER BY created_at DESC LIMIT n`) had only the single-column `agent_id` index, so it fetched every entry of the agent, ran the newer-state `EXISTS` on each and sorted them all. `ix_agent_memory_entries_agent_created (agent_id, created_at)` (migration `m8836memrec1`, built in place under a lock timeout) lets the planner walk the agent's entries newest first and stop after n eligible ones. Acceptance: `memory` <= 400 ms on two post-boot agent-chat traces of the carrying build.
- **Shipped (fixed context per turn, #8756):** an agent-chat trace's metadata carries `fixed_context`: what every generation of the turn sends before a message, sizes only (`app/services/prompt_budget.fixed_context`). `system_prompt_chars`, `sections` (chars per labelled prompt section, largest first: persona, skills, skills_catalog, user_memory, agent_memory, coordination_tools, workspace_sources..., the rest as `platform_scaffolding`), `tool_schema_chars` and `tool_count`, `by_source` (tool schema chars by tool source, the 15 largest, the rest as `other`), `total_chars` and `est_tokens` (chars // 3). The fixed-context cut (R4 of the chat reactivity program) is chosen from these figures.
- **Shipped (sent context under tool gating, #8777):** `fixed_context` sizes the whole tool catalogue before progressive disclosure. When the gate is active the root trace metadata also carries `sent_context` (`app/services/prompt_budget.sent_context`): the schemas the first generation is offered (`tool_schema_chars`, `tool_count`, `by_source`), `catalog_prompt_chars` for the gated-groups catalog message, `total_chars` and `est_tokens`. Sizes only.
- **Shipped (integration HTTP):** every tool span whose integration calls go through `safe_request` carries `metadata.http` (`{provider: {calls, ms, retries, 5xx, 429, network_errors, status}}`); the run rollup carries a bounded `providers` table and the totals `http_5xx` and `http_retries`, both in the rollup percentiles. Figures only, no URL or payload.

### 4.3 Sandbox timings
- **Where:** `backend/app/services/sandbox_workspace/commands.py` (`_created`, `_container`, `_wait_and_finish`), `kernel.py`, `_sandbox_selector.py`.
- **What:** on the tool span, `image`, `warm` (true or false), `create_ms`, `create_attempts`, `create_504`, `stage_bytes`, `stage_ms`, `run_ms`.
- **Done when:** a 504 shows up as a counted field, not as text in a result.
- **Shipped (stages):** `xtmone_sandbox_command_stage_seconds{stage=start|command|teardown, outcome}` next to the create histogram (`xtmone_sandbox_create_seconds`) and the upload series (`xtmone_sandbox_workspace_sync_seconds{phase="materialize"}`), which stay where they are; the `sandbox_run_command` tool span carries `sandbox.image`, `sandbox.container` (new, reused, restarted), `sandbox.create_ms`, `sandbox.upload_ms`, `sandbox.start_ms`, `sandbox.command_ms`, `sandbox.command_outcome`.

### 4.4 Event latency
- **Where:** `backend/app/services/platform_events.py` (`publish`), the event-triggered run start in `assignment_engine.py`.
- **What:** `event_published_at`, `run_queued_at`, `run_started_at` in the run's `trigger_context`, and `event_latency_ms` on its trace.
- **Done when:** the pick-up p90 is a query, not a hand-kept ledger.
- **Shipped:** the first start of a platform event or integration event run puts `event_latency` on its trace metadata (`kind`, `provider`, `event_to_trigger_ms`, `trigger_to_queue_ms`, `queue_ms`, `event_to_run_ms`) from the timestamps the run already carries (`occurred_at`, `triggered_at`, `created_at`, `started_at`), and `xtmone_event_to_run_seconds{kind}` counts it in Prometheus. A HITL resume or a resumed attempt is not measured.
- **Shipped:** the background task a one-off platform event watch starts is measured the same way at its first start: `sub_agent._with_watch_event_latency` writes `event_latency` (`kind: platform_event_watch`, `event_to_trigger_ms`, `queue_ms`, `event_to_run_ms`) in the task's metadata from the `occurred_at` / `triggered_at` the watch wrote and feeds `xtmone_event_to_run_seconds{kind="platform_event_watch"}`, once per task (a resumed attempt or a held task released is not measured again).
- **Fixed:** a provider webhook delivers an `integration_event` run with `trigger_context.type = "webhook"`, so the measure reads the assignment's `trigger_type` for the kind; `event_origin` says where `event_to_run_ms` starts (`occurred_at`, or `receipt` when the provider gives no event timestamp, as GitHub's webhooks do). A legacy `webhook` assignment run is still not measured.

### 4.5 Run start, queue and rollout counters
- **What:** `queued_at` on every assignment run and background task trace; open-since percentiles per owner in the work items analytics; an orphan and cut counter per build (`run.orphaned`, `claim_released`, graceful cancels) next to the release history; `queued_at`, `leased_at` and `finished_at` on Video Studio renders and presentation previews.
- **Shipped (rollout counters):** `xtmone_tasks_orphaned_total{version}` (a running task whose execution lost its lease) and `xtmone_run_resumes_total{entry, cause, graceful, version}` (a resumed assignment run or background task; `cause` is a bounded class of `lost_cause`: handed_back, handback_lost, taken_back, lease_lost, engine_reason, unknown), `version` being the short git SHA of the build. Queue depth stays in the scrape-time gauges `xtmone_assignment_queue_depth`, `xtmone_assignment_queue_oldest_age_seconds` and `xtmone_assignment_dispatch_lag_p95_seconds`.

### Gate for the instrumentation PR
backend-lint, backend-types, backend-unit, backend-tripwires, backend-snapshots. Overhead under 1 ms per span (unit-tested), no payload content added to traces, only sizes and timings.
