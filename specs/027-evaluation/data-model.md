# Data Model: Evaluation

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

PostgreSQL, SQLAlchemy models in `backend/app/models/` (`dataset.py`, `dataset_version.py`, `evaluation.py`, `evaluation_param.py`, `evaluation_metric.py`, `orchestration.py`). All ids are UUID primary keys; every table has `created_at` (and `updated_at` where it is edited). JSON columns are `JSON` unless marked JSONB.

```
datasets ─┬─ dataset_entries
          └─ dataset_versions ── dataset_version_entries
evaluations ─┬─ evaluation_agents ──> agents
             ├─ evaluation_datasets ──> datasets
             ├─ evaluation_placebo_comparisons
             └─ evaluation_runs ─┬─ evaluation_results ─┬─ evaluation_metrics
                  │              │                     └─ evaluation_result_traces ··> traces
                  │              ├─ evaluation_episodes
                  │              └─ evaluation_run_params
                  └·· owner_execution_id ··> orchestration_executions (adapter = "evaluation")
```

`──` is a foreign key, `··` a reference without one.

Kept for legacy rows only and left out below: `evaluations.quality_weight`, `evaluations.quality_source`, and `speed_score`, `overall_score`, `f1_score` on both `evaluation_runs` and `evaluation_results`. Nothing reads `quality_weight`: create and update accept it and ignore it, and no response, copy or export carries it. The others are read only to show a pre-headline run as "(legacy)" (FR-092) and by export (FR-113); the comparison of two runs also reports `f1_score` (FR-111).

## Datasets

### `datasets`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `user_id` | FK users, CASCADE | yes | owner |
| visibility set | | | `is_company_managed`, `shared_group_ids`/`_permissions`, `shared_user_ids`/`_permissions`, `access_restricted`; access is admin-only (FR-014); responses serve `can_manage` |
| `name`, `description` | str(200), text | no, yes | |
| `slug` | str(200), indexed | yes | built-in dataset key for the startup seed |
| `family` | str(32) | yes | `CLASSIFICATION`, `STIX_BUNDLE`, `STRUCTURED_JSON`, `PROSE`; NULL reads "Needs family" (FR-002) |
| `label_vocabulary` | JSON list | yes | CLASSIFICATION only (FR-003) |
| `output_schema` | JSON | yes | answer schema wrapper `{type: "json_schema", definition}`, STRUCTURED_JSON only (FR-004) |
| `evaluation_instructions` | text | yes | dataset-level judge instructions (FR-057) |
| `content_hash` | str(64), unique | yes | Hugging Face import dedup key; cleared on any edit |
| `is_read_only` | bool | no | FR-008 |
| `current_version` | int, default 0 | no | latest version number; 0 = never versioned |

### `dataset_entries`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `dataset_id` | FK datasets, CASCADE | no | |
| `position` | int | no | order in the dataset, unique in it (`uq_dataset_entries_position`): an added entry takes the next one, under a lock on the dataset row (FR-112) |
| `description`, `input_prompt` | text | yes | |
| `expected_answer` | text | no | ground truth (FR-001) |
| `evaluation_prompt` | text | yes | entry-level judge instructions |
| `file_ids` | JSON list | yes | attached files |
| `output_schema` | JSON | yes | entry-level schema, same wrapper; wins over the dataset's at scoring (a known gap at write time) |

### `dataset_versions` and `dataset_version_entries`

The version snapshot is relational, not a JSON blob.

| Table | Columns |
|---|---|
| `dataset_versions` | `dataset_id` (FK CASCADE), `version_number` (unique with `dataset_id`), `content_hash` (SHA-256 of the entries), `entry_count`, frozen `label_vocabulary`, `output_schema` |
| `dataset_version_entries` | `version_id` (FK CASCADE), `entry_id` (FK `dataset_entries`, SET NULL, indexed), frozen `position`, `description`, `input_prompt`, `expected_answer`, `evaluation_prompt`, `file_ids`, `output_schema` |

- Rows are never updated. `services/evaluation/dataset_versioning.py` `bump_version` writes a header and one frozen entry per live entry and bumps `datasets.current_version` in the caller's transaction (FR-009).
- A run reads its entries, vocabulary and schema only from the versions in `evaluation_runs.pinned_dataset_versions` (FR-010). Deleting a live entry keeps the snapshot (`entry_id` becomes NULL).

### `evaluation_datasets`

`evaluation_id` (FK CASCADE), `dataset_id` (FK CASCADE), `position` (display order, unique in the evaluation (`uq_evaluation_datasets_position`): a linked dataset takes the next one, under a lock on the evaluation row); unique `(evaluation_id, dataset_id)`. Family match on link: FR-011.

## `evaluations`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `user_id` | FK users, CASCADE | yes | owner |
| visibility set | | | same as `datasets` |
| `name`, `description`, `slug` | str(200), text, str(200) | no, yes, yes | `slug` = built-in seed key |
| `family` | str(32) | yes | required to run (FR-018, FR-027) |
| `trigger_config` | JSON, default `{}` | no | see [JSON shapes](#trigger_config) |
| `use_agent_default_model` | bool, default true | no | FR-016 |
| `model_ids`, `disabled_model_ids` | JSON list | no | extra models; disabled subset (FR-017) |
| `execution_mode` | str(20), default `full_loop` | no | `full_loop` or `llm_only`; any other stored value runs as `llm_only` |
| `apply_prompt_guards`, `require_tool_approval` | bool, default false | no | FR-022 |
| `run_mode` | str(20), default `parallel` | no | `parallel` or `sequential` (FR-033) |
| `timeout_seconds` | int, default 3600, CHECK ≥ 60 | no | launch budget (FR-019, FR-040) |
| `reuse_default` | str(20), CHECK `off`/`on` | no | ORM default `on`, server default `off` (FR-093) |
| `evaluator_model` | str(100) | yes | judge model; NULL = platform default |
| `evaluator_prompt` | text | yes | judge prompt; NULL = managed judge skill |
| `evaluation_instructions` | text | yes | evaluation-level judge instructions |
| `judge_max_tokens` | int, default 8192 | no | FR-059 |
| `sub_score_weights` | JSON | yes | `{sub_score_key: weight}` over the five keys of FR-058 |
| `scorer_selection` | JSON list | yes | NULL = judge on; a list = judge on iff it holds `"judge"`, `[]` = judge off (FR-020) |
| `field_scoring_overrides` | JSON | yes | see [JSON shapes](#field_scoring_overrides) |
| `penalty_factor` | float, default 0.5 | no | spread penalty k (FR-077) |
| `performance_tolerance` | float, default 5.0 | no | tolerance band, % of best (FR-082) |
| `overall_quality_metrics`, `overall_performance_metrics` | JSON list | yes | headline selection: NULL = inherit, `[]` = none, list = explicit (FR-084) |
| `custom_params` | JSON | yes | `{key: value}` logged as `evaluation` run params; key `reuse` steers reuse (FR-093, FR-107) |
| `tags` | JSONB | yes | `{key: str}`, `baseline` stripped (FR-104, FR-105) |
| `placebo_config` | JSON | yes | the placebo-controlled method (`dev-docs/agent-placebo-evaluation.md`): `targets`, `include_no_target`, `trials_per_entry`, `custom_text`; NULL = none |
| `enabled` | bool, default true | no | "Automatic runs": pauses scheduled and on-agent-change launches only, never a launch a person starts; false on import (FR-028, FR-115) |
| `last_triggered_at` | timestamptz | yes | |

## `evaluation_agents`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `evaluation_id` | FK evaluations, CASCADE | no | |
| `agent_id` | FK agents, CASCADE | no | unique with `evaluation_id` |
| `tags` | JSONB | yes | agent-level tags in this evaluation (FR-104) |
| `is_baseline` | bool, default false | no | partial unique index `uq_eval_agent_baseline`: at most one per evaluation (FR-106) |
| `output_extractor` | JSON | yes | see [JSON shapes](#output_extractor); NULL = score the raw answer (FR-046) |

## `evaluation_runs`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `evaluation_id` | FK evaluations, CASCADE | no | |
| `agent_id` | FK agents, CASCADE | no | deleting the agent deletes its runs |
| `model_id` | str(100) | no | model, or `model-independent` for a non-LLM agent (FR-026) |
| `is_default_model` | bool | no | run uses the agent's own model |
| `status` | str(20), default `pending` | no | see transitions below |
| `owner_execution_id` | UUID, no FK | yes | the launch that owns the run (FR-039); NULL only on rows older than ownership |
| `trigger_reason` | str(50) | no | `manual` (Run, API, agent tool, replay, bulk re-run), `schedule`, `persona_improvement`, `import` |
| `score` | float | yes | Judge Quality; NULL = not measured (FR-076) |
| `quality_score`, `performance_score` | float | yes | Overall Quality, Overall Performance; NULL = N/A (FR-074) |
| `headline_details` | JSON | yes | provenance, see [JSON shapes](#headline_details) |
| `metric_suite_version` | str(32) | yes | FR-061 |
| `scorer_selection` | JSON | yes | judge on/off the current scores were computed with; same reading as on `evaluations` |
| `execution_mode` | str(20) | yes | stamped at start: `full_loop`, `llm_only` or `skip_llm` (non-LLM agent) (FR-029) |
| `apply_prompt_guards`, `require_tool_approval` | bool | yes | stamped at start; false outside Full Loop |
| `persona_version_number` | int | yes | snapshot; 0 = agent had no persona |
| `prompt_versions` | JSON | yes | `{prompt_id: version}` |
| `agent_config_versions` | JSON | yes | `{field: version or null}` (guardrails, temperature, reasoning effort, max iterations) |
| `skill_versions` | JSON | yes | `{skill_id: version}` |
| `pinned_dataset_versions` | JSON | yes | `{dataset_id: version_number}` (FR-010) |
| `tags` | JSONB | yes | run-level tags (FR-104) |
| `completed_entries`, `total_entries` | int, default 0 | no | progress |
| `anomaly_count` | int, default 0 | no | flagged entries (FR-036) |
| `arm` | str(20), default `real` | no | placebo comparison arm: `real`, `placebo`, `no_target`; `real` outside a comparison |
| `trial_index` | int, default 0 | no | the trial of its arm this run is; 0 outside a comparison |
| `placebo_target`, `placebo_snapshot` | JSON | yes | what a control run replaces or removes, and the placebo it ran with |
| `placebo_comparison_id` | UUID, no FK | yes | the comparison the run belongs to; both go with their evaluation |
| `duration_seconds` | float | yes | sum of entries' agent time; wall clock on cancel, failure, sweep and callback paths |
| `error` | text | yes | failure or cancel message |
| `started_at`, `completed_at` | timestamptz | yes | |

Index `ix_evaluation_runs_stranded_candidates (id, owner_execution_id) WHERE status = 'running' AND completed_at IS NULL` serves the stranded-run sweep (FR-040). Effective tags, `is_baseline`, reused-entry count, `can_replay` and dataset drift are derived at read time, never stored.

**Status transitions** (FR-037 to FR-040):

| From | To | When |
|---|---|---|
| — | `pending` | a launch creates the run with its owner (FR-026) |
| `pending` | `running` | the owning execution starts it |
| `pending` | `failed` | agent, evaluation or a pinned version gone; held by the AI gate past the hold limit (one week by default) |
| `pending`, `running` | `cancelled` | cancel (FR-038); never rewritten afterwards |
| `running` | `completed` | every entry processed; a callback run only once every episode is terminal |
| `running` | `failed` | the run cannot finish, its worker died (FR-039), the stranded sweep, or a callback entry failed or timed out |
| `completed`, `failed`, `cancelled` | `running` → `completed` / `failed` | rescore (FR-101, FR-102; status change is a known gap) |

A callback run whose entries are still parked stays `running` and is not stranded.

## `evaluation_results`

One row per entry per run.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `run_id` | FK evaluation_runs, CASCADE | no | |
| `entry_id` | FK dataset_entries, SET NULL | yes | live entry |
| `version_entry_id` | FK dataset_version_entries, SET NULL, indexed | yes | frozen entry scored and rescored |
| `agent_output` | text | no | raw answer, or `Error: …` when execution raised (FR-035) |
| `score` | float | no | judge score; 0 when the judge was off or did not run |
| `sub_scores` | JSON, default `{}` | no | the five judge sub-scores (FR-058) |
| `evaluator_reasoning` | text | yes | judge reasoning or failure cause |
| `duration_seconds` | float | yes | agent time; for a reused row, the source's |
| `execution_details` | JSON | yes | see [JSON shapes](#execution_details); NULL on an error row |
| `input_fingerprint` | str(64), indexed | yes | reuse key (FR-094); NULL on error and imported rows |
| `is_reused` | bool, default false | no | FR-100 |
| `reused_from_result_id` | self-FK, SET NULL, indexed | yes | reuse source; NULL once the source is deleted (FR-100) |
| `execution_anomaly_kind` | str(24) | yes | `transient_error`, `confirmation_seeking`, `platform_error`, or NULL when no anomaly (FR-036); the API serves `is_execution_anomaly` from it |
| `is_judge_failure` | bool, default false | no | FR-060 |

## `evaluation_metrics`

One row per scored metric per result; the queryable copy of `execution_details.metric_results`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `result_id` | FK evaluation_results, CASCADE | no | unique with `key` |
| `run_id` | FK evaluation_runs, CASCADE | no | denormalised; index `(run_id, key)` |
| `key` | str(100) | no | package metric key (dotted, FR-051, FR-063), or platform `judge_seconds`, `run_duration_seconds` (FR-072) |
| `value` | float | yes | entry value |
| `numerator`, `denominator` | float | yes | pooled metrics only (FR-078) |
| `class_counts` | jsonb | yes | macro metrics only: `{class: {tp, fp, fn}}` (FR-078) |

Only `scored` outcomes are written; unavailable, skipped and invalid ones live only in `metric_results`. Older rows may hold retired keys that no headline reads.

## `evaluation_result_traces`

`result_id` (FK CASCADE), `trace_id` (UUID, no FK, the platform's LLM trace), `trace_role` (CHECK `agent`/`judge`/`auxiliary`), `trace_order`. Unique `(result_id, trace_id)` and `(result_id, trace_order)`.

## `evaluation_episodes`

One row per entry per run, created as the entry starts; the callback state machine (FR-032).

| Column | Type | Null | Meaning |
|---|---|---|---|
| `run_id` | FK evaluation_runs, CASCADE | no | unique with `entry_id` |
| `entry_id` | FK dataset_entries, CASCADE | no | |
| `correlation_id` | str(120), unique | no | `eval:{run_id}:{entry_id}` |
| `status` | str(32), indexed | no | see below |
| `idempotency_key` | str(120) | yes | of the accepted callback; set only by a resume |
| `waiting_since`, `callback_deadline` (indexed) | timestamptz | yes | set while parked |
| `resumed_at`, `completed_at` | timestamptz | yes | |
| `first_turn_output`, `final_output` | text | yes | |
| `first_turn_details` | JSONB | yes | what the first turn produced before pausing: `version_entry_id` and the result's `execution_details` without the judge and quality; the resume builds the result from it |
| `steps`, `participants` | JSONB list | no | timeline steps; agent ids |
| `callback_payload` | JSONB | yes | the resume body: `final_output` / `agent_output` or `error` |
| `error` | text | yes | |

The resume secret is stored Fernet-encrypted as `trigger_config.resume_secret_enc` (`services/evaluation/resume_secret.py`).

**Status transitions** (`services/evaluation_continuation.py`):

| From | To | When |
|---|---|---|
| — | `running` | the entry starts (the `pending` default is never written) |
| `running` | `completed` / `failed` | entry ends without callback mode |
| `running` | `waiting_callback` | callback mode, after the first turn; deadline = `callback_timeout_seconds` |
| `waiting_callback`, `running`, `pending` | `resumed` | a signed callback is accepted |
| `resumed`, `pending` | `running` | the `resume_episode` execution processes it |
| `waiting_callback` | `timed_out` | deadline passed ("Callback deadline exceeded") |
| any non-terminal | `failed` | run cancelled ("Cancelled by user") or stranded |

Terminal: `completed`, `failed`, `timed_out`.

## `evaluation_placebo_comparisons`

One placebo launch: `evaluation_id` (FK CASCADE), `status` (`pending` → `running` → `completed` / `failed` / `cancelled`), `seed` (str(64)), `corpus_version`, `config` (the placebo configuration it ran with, frozen), `created_by` (user id, no FK), `results` and `warnings` (JSON), `completed_at`. Its runs point at it through `evaluation_runs.placebo_comparison_id`.

## `evaluation_run_params`

`run_id` (FK CASCADE), `key` (str(100), unique with `run_id`), `value` (text, scalar or JSON-encoded), `source` (CHECK `auto`/`evaluation`/`manual`, precedence FR-107). Index `(key, value)` serves run search (FR-109).

## `orchestration_executions` (shared)

One row per launch (FR-025 to FR-040), `adapter = "evaluation"`. Columns used: `state` (`pending`, `running`, `completed`, `failed`, `dead_letter`, `cancelled`), `payload` (JSONB), `run_at`, `deadline_at` (evaluation `timeout_seconds` + `orchestration_grace_seconds`), `lease_owner`, `lease_expires_at`, `attempt_count`, `max_attempts` (3), `last_error`.

| Payload | Started by |
|---|---|
| `{evaluation_id, trigger_reason}` | a launch (`stage_evaluation_execution`), including replay and bulk re-run |
| `{kind: "rescore_runs", run_ids, trigger_reason}` | rescore (`stage_rescore_execution`) |
| `{kind: "resume_episode", episode_id, idempotency_key}` | an accepted callback |

- Runs are created in the same transaction as their execution and stamped with its id in `owner_execution_id` (FR-039). The ownership predicates in `services/evaluation/run_ownership.py` are the only readers of that column.
- Partial GIN index `ix_orchestration_exec_rescore_run_ids` on `payload -> 'run_ids'` for in-flight evaluation rows answers "rescore pending" for a run.

## JSON shapes

Top-level keys only; full shapes in `contracts/`.

### `execution_details`

On `evaluation_results` ([`contracts/stored-results.md`](contracts/stored-results.md)).

| Key | Content |
|---|---|
| `total_duration_ms`, `agent_duration_ms`, `judge_duration_ms` | agent + judge; agent; judge |
| `agent_wall_clock_ms` | wall-clock latency (FR-065) |
| `ttft_ms` | time to first token or null (FR-066) |
| `execution_mode` | mode the entry ran in |
| `traces` | timeline steps `{type, description, duration_ms, outcome?, model?, usage?, error?}`; the last is `judge` (model, prompts, raw response, usage, stop reason, truncated) or `judge_skipped` |
| `trace_ids` | the agent's stored trace ids (also linked in `evaluation_result_traces`) |
| `metric_results` | `{metric_key: serialized result}` for every quality and performance metric plus synthetic `extraction.failed`, `stix.backend` (FR-047, FR-049, FR-064); shape in [`contracts/metric-package.md`](contracts/metric-package.md) |
| `chain` | Full Loop only: ledger `{calls, usage_by_model, retries, complete, incomplete_reason?}` over the agent and its consults, transfers and delegated tasks (FR-068, FR-069); a task's own ledger is in `sub_agent_tasks.metadata_json["ledger"]` |
| `extracted_prediction` | `{prediction, output_kind, item_count}` when an extractor succeeded (FR-048); removed when a rescore's extractor does not |
| `approval_required_tools` | tool names whose calls were held (FR-022) |
| `available_tools` | tool names the agent had, what reuse, rescore and judge-only replay give the judge (FR-057, FR-059) |

A reused result carries its source's agent-side `traces` (judge steps dropped), `ttft_ms`, `execution_mode`, agent durations, `chain`, `approval_required_tools` and `available_tools`, has empty `trace_ids`, and recomputes the rest (FR-098, FR-099).

### `headline_details`

On `evaluation_runs` ([`contracts/headlines.md`](contracts/headlines.md)).

| Key | Content |
|---|---|
| `quality_metric_keys`, `quality_contributions` | contributing quality metrics and `{key: contribution}` (FR-085) |
| `excluded` | quality exclusions `[{key, reason}]` |
| `performance_metric_keys`, `performance_contributions` | same for Overall Performance, rewritten on every sibling recompute |
| `performance_excluded` | `[{key, reason, run_ids}]`; `key` may be `__run__` for a whole-run reason (FR-086) |
| `metric_entry_counts` | `{key: counted entries}` of each headline's contributing metrics, both halves |

Judge Quality has no provenance entry; its value is `evaluation_runs.score`.

### `trigger_config`

On `evaluations`: `schedule` (cron, FR-025), `on_agent_change` (bool), `await_callback` (bool), `callback_timeout_seconds` (int, default `timeout_seconds`), `resume_secret` (str, write-only: stored encrypted as `resume_secret_enc`, served as `"***"`, never exported). Callback keys are API-only (FR-032); an update merges into the stored config, so the page's save, which sends `schedule` and `on_agent_change`, keeps them (FR-023).

### `field_scoring_overrides`

On `evaluations`: `{field_modes: {json_pointer: mode}, field_weights: {json_pointer: float}}`; an absent pointer derives from the schema; validation FR-055 (`metric_package/quality_adapter.py`).

### `output_extractor`

On `evaluation_agents`: `{source: {kind, tool_name?}, operations: [{op, path?}], output_kind}`; `kind` ∈ `agent_output`, `tool_output`, `tool_input`; 1 to 8 operations from `services/evaluation/extraction/spec.py`; `output_kind` ∈ `label_set`, `object`, `array` by family (FR-021, FR-046).

## Deletion

- Evaluation: cascades to agent links, dataset links, runs, results, metrics, trace links, episodes, params and placebo comparisons (FR-024).
- Run: cascades to its results and their children; headlines recomputed (FR-043).
- Agent: cascades to its `evaluation_agents` rows and its runs.
- Dataset: cascades to its entries, versions and links; results keep their row with `entry_id` NULL, episodes of its entries are deleted.
