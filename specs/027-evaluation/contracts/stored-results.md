# Contract: Stored results

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

What one entry's outcome looks like once stored: the `evaluation_results.execution_details` JSON and the `evaluation_metrics` rows derived from it. This covers the shapes other code reads. Full columns are in [data-model.md](../data-model.md).

## `evaluation_results.execution_details`

- The engine writes it for each entry (`backend/app/services/evaluation_engine.py`).
- Rescore rewrites it in part. Import copies it verbatim.
- It is NULL when the entry's execution raised (FR-035).

| Key | Type | Meaning | FR |
|---|---|---|---|
| `total_duration_ms` | int | agent + judge | FR-072 |
| `agent_duration_ms` | int | sum of the agent's timeline steps, a parallel tool group counted once | |
| `agent_wall_clock_ms` | int \| null | timed around the agent call | FR-065 |
| `judge_duration_ms` | int | timed around the judge branch, including a skipped judge | FR-072 |
| `ttft_ms` | int \| null | time to first token of the answer | FR-066 |
| `execution_mode` | `full_loop` \| `llm_only` \| `llm_only_fallback` \| `skip_llm` | mode the entry ran in. `llm_only_fallback` is a Full Loop entry answered in LLM Only (`run_mode.FALLBACK_MODE`), served as `fell_back_to_llm_only`. | |
| `traces` | list | the agent timeline followed by one judge step (below) | |
| `trace_ids` | [str] | the agent's LLM trace ids; `[]` when reused | |
| `metric_results` | {key: result} | every quality and performance result, in the shape of [metric-package.md](metric-package.md) | FR-049, FR-064 |
| `chain` | ChainLedger | Full Loop and `skip_llm` only | FR-068, FR-069 |
| `approval_required_tools` | [str] | tools whose calls were held for approval | FR-022 |
| `available_tools` | [str] | the tools the agent had (`[]` in LLM Only); a reused result copies its source's, absent when the source has none. Absent on older results | FR-057, FR-059 |
| `extracted_prediction` | `{prediction, output_kind, item_count}` | present only after a successful extraction | FR-048 |

**Trace step**: `{type, description | name, duration_ms, outcome?, model?, usage?, error?, error_type?, ...}`.

| `type` | Meaning | Read by performance |
|---|---|---|
| `llm`, `model` | a model call | model call |
| `tool` | a tool call | tool call |
| `judge` | the judge call ([judge.md](judge.md)) | never (FR-068) |
| `judge_skipped` | `{description, duration_ms, skipped: true}` | never |

- A call's outcome is its `outcome` when that is `success`, `error`, `timeout` or `cancelled`. Otherwise it is `error` when the step carries `error` or `is_error`, and unknown when it carries neither (never assumed success).
- Run reads truncate `tool_input`, `tool_output`, `system_prompt`, `user_prompt` and `raw_response` to 30,000 characters. The trace download route returns one step whole.
- `evaluation_result_traces` links a result to its LLM traces as `(result_id, trace_id, trace_role: agent | judge | auxiliary, trace_order)`.

**ChainLedger** (`backend/app/services/run_ledger.py`):

```
{ "calls":          [{ "kind": "model" | "tool", "name", "model"?, "duration_ms",
                       "outcome": "success" | "error" | "timeout" | "cancelled", "usage"? }],
  "usage_by_model": [{ "model": str | null, "usage": {...} }],
  "retries":        int,
  "complete":       bool,
  "incomplete_reason"?: "wait_bound_reached" | "run_cancelled" | "ledger_missing" | "recorded_before_chain" }
```

A delegated task keeps its own `RunLedger` `{calls, spawned_task_ids, retries, partial}` under `SubAgentTask.metadata_json["ledger"]`. `assemble_chain` sums those ledgers into the entry's chain. The chain is `complete` only when no ledger is `partial` and every task's ledger is present.

### Rules readers rely on

| Rule | FR |
|---|---|
| `metric_results` statuses are the only input to exclusion reasons ([headlines.md](headlines.md)). | FR-086 |
| A failed extraction leaves exactly one key, `extraction.failed`. The run's extraction-failure count is the number of results carrying it. | FR-047 |
| A reused entry copies the source's `traces` (judge steps removed), `ttft_ms`, `agent_wall_clock_ms`, `execution_mode`, `chain`, `approval_required_tools` and `available_tools`. A Full Loop source with no `chain` yields `incomplete_reason: "recorded_before_chain"`. | FR-098, FR-099 |
| Rescore replaces every non-`performance.*` key of `metric_results`, keeps every `performance.*` key, and adds or removes `extracted_prediction`. | FR-101 |

## `evaluation_metrics` rows

One scalar per (result, key), the input to every headline and to the run metrics panel. Model: `backend/app/models/evaluation_metric.py`.

Columns and constraints: [data-model.md](../data-model.md#evaluation_metrics).

### Write rules (`backend/app/services/evaluation/metrics.py`, `extract_metrics` + `log_result_metrics`)

| Key | Value | Written when | FR |
|---|---|---|---|
| `judge_seconds` | `judge_duration_ms / 1000`, 3 decimals | the duration exists | FR-072 |
| `run_duration_seconds` | `total_duration_ms / 1000`, or `judge_duration_ms / 1000` for a reused or rescored result | the duration exists | FR-072 |
| a package key | `metric_results[key].value` | status `scored` and the value is a finite number | FR-049, FR-074 |

- An undotted key arriving in `metric_results` is refused, so an imported package cannot restate a platform measurement.
- `numerator` and `denominator` (reals) are written only for a key that `is_pooled_key` reports, and only when the counts are consistent with the value (FR-078).
- `class_counts` (`{class: {tp, fp, fn}}`) is written only for a key that `is_macro_key` reports, and only when the classes' mean F1 is the value, or there is no class (FR-078).
- Keys that are empty, longer than 100 characters or contain NUL are dropped.
- Writes are upserts on `(result_id, key)` and never delete a row (see the spec's [Known gaps](../spec.md#known-gaps)).
- Rescore writes with `is_reused=True` and writes no judge row when the judge did not run again.
- Import writes inside one savepoint per run.
- Retired keys are never written. Their old rows stay and Stage 1 skips them: `total_seconds`, `ttft_seconds`, `agent_seconds`, `input_tokens`, `output_tokens`, `llm_iterations`, `tool_calls`, `cost_usd` and `schema_score` (FR-092).

### Readers

| Reader | Reads |
|---|---|
| Headline aggregation (`backend/app/services/evaluation/aggregation.py`) | rows of the counted entries (FR-075) |
| `GET /evaluations/runs/{run_id}/metrics/aggregate` | every row of the run, no entry filter (FR-091) → `{run_id, aggregates: [{key, half, unit, pooled, mean, p50, p95, min, max, count}]}` |
| `GET /evaluations/runs/{run_id}/results/{result_id}/metrics` | → `[{key, value, half, unit}]`; `half` is `quality`, `performance` or null for a platform key |
| `POST /evaluations/runs/search` | metric filters on `{mean, min, max, p50, p95, count}` |

`half`, `unit` and `pooled` come from the registry views, never from the key ([metric-package.md](metric-package.md)).
