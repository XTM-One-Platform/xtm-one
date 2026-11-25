# Evaluations

The root `AGENTS.md` keeps one line per entry; this file holds the Evaluation module's full contract: the rules other code and integrators rely on. How the module is built and runs (the pipeline, an evaluated turn versus a chat turn, background execution, the scoring stages) is in `dev-docs/evaluations.md`; the metric suite's own contract is in `backend/app/evaluation_metrics/README.md`; placebo comparisons are in `dev-docs/agent-placebo-evaluation.md`. `specs/027-evaluation/` is the frozen design record of the 1780 delivery, not maintained.

Paths are relative to `backend/app/` for the backend and `frontend/src/` for the frontend.

## 1. Entities and data model

Models: `models/dataset.py`, `dataset_version.py`, `evaluation.py`, `evaluation_metric.py`, `evaluation_param.py`. A child row cascades with its parent unless stated.

```
datasets ─┬─ dataset_entries
          └─ dataset_versions ── dataset_version_entries
evaluations ─┬─ evaluation_agents ──> agents
             ├─ evaluation_datasets ──> datasets
             ├─ evaluation_placebo_comparisons
             └─ evaluation_runs ─┬─ evaluation_results ─┬─ evaluation_metrics
                                 │                     └─ evaluation_result_traces ··> LLM traces
                                 ├─ evaluation_episodes
                                 └─ evaluation_run_params
evaluation_runs.owner_execution_id ··> orchestration_executions (adapter "evaluation")
evaluation_runs.placebo_comparison_id ··> evaluation_placebo_comparisons
```

`──` is a foreign key, `··` a reference without one.

### Tables

| Table | Rules other code relies on |
|---|---|
| `datasets` | Visibility column set. `family` NULL is `needs_family`. `label_vocabulary`, `output_schema`: §2. `content_hash` unique: the Hugging Face dedup key, cleared by any entry write. `is_read_only`. `current_version` (0 = never versioned). |
| `dataset_entries` | `expected_answer` NOT NULL. `position` unique per dataset (`uq_dataset_entries_position`): an append takes `max + 1` under a row lock on the dataset. `output_schema` overrides the dataset's. |
| `dataset_versions` | Unique `(dataset_id, version_number)`. `content_hash`, `entry_count`, frozen `output_schema` and `label_vocabulary`. |
| `dataset_version_entries` | Frozen copy of one entry. `entry_id` FK SET NULL: the snapshot outlives the entry. |
| `evaluations` | Visibility column set. `family` NULL: saved, never run. `timeout_seconds` CHECK ≥ 60. `reuse_default` CHECK `off`/`on` (ORM default `on`, server default `off`). `scorer_selection`: NULL or a list holding `"judge"` = judge on, `[]` = off. `overall_quality_metrics` / `overall_performance_metrics`: NULL inherits all, `[]` selects none. `enabled` pauses the schedule and on-agent-change launches only. `placebo_config`: [placebo](../agent-placebo-evaluation.md). |
| `evaluation_agents` | The evaluation's subjects: an agent or a flow (`flow_id`, #8257), exactly one (`ck_evaluation_agents_one_subject`). Unique `(evaluation_id, agent_id)`, partial unique `(evaluation_id, flow_id)`. `is_baseline`, at most one per evaluation (partial unique `uq_eval_agent_baseline`). `output_extractor`, `tags`. A flow subject: `dev-docs/health-evaluation.md`. |
| `evaluation_datasets` | Unique `(evaluation_id, dataset_id)`. `position` unique per evaluation, appended under a lock on the evaluation row. |
| `evaluation_result_traces` | `trace_id` without FK. `trace_role` CHECK `agent`/`judge`/`auxiliary`. Unique `(result_id, trace_id)` and `(result_id, trace_order)`. |
| `evaluation_episodes` | One per run and entry, unique `(run_id, entry_id)`. `correlation_id` unique, `eval:{run_id}:{entry_id}`. `entry_id` FK CASCADE on `dataset_entries`. `first_turn_details` (JSONB): `version_entry_id` and the first turn's `execution_details` without judge and quality, from which a resume builds the result (§12). |
| `evaluation_run_params` | Unique `(run_id, key)`; `source` CHECK `auto`/`evaluation`/`manual` (§13). |

**Legacy columns**: `evaluations.quality_weight` (ignored on write, never served); `evaluations.quality_source` (only `judge`: create and update refuse `f1`, a stored, imported or copied row may hold it, and is then read as `judge`: the pages, both run comparisons and the persona loop show and compare Judge Quality, and a new run logs `quality_source` `judge`; the agent tools' evaluation summary serves the stored setting as it is); `speed_score`, `overall_score`, `f1_score` on runs and results, never written by scoring, and `f1_score` neither shown by the pages nor served by the agent tools. A result's `f1_score` is cleared only when a resumed callback replaces the answer it scored.

### `evaluation_runs`

| Column | Rule |
|---|---|
| `agent_id`, `flow_id`, `flow_version_number` | The subject: an agent, or a flow (#8257), exactly one (`ck_evaluation_runs_one_subject`); a flow's version is stamped when the run starts. |
| `model_id` | The model, or `model-independent` for a `skip_llm` agent or a flow: one run, `is_default_model` false. |
| `owner_execution_id` | The execution that last made the run `pending` or `running`, written in that transaction. No FK. Read only through `services/evaluation/run_ownership.py`. |
| `trigger_reason` | `manual` (Run, API, agent tool, replay, bulk "Re-run full"), `schedule`, `persona_improvement`, `concierge` (`run_health_evaluation`, formerly `run_concierge_evaluation`, #8257), `import` (or the exported run's reason). `on_agent_change` is staged by an agent update but creates no run (open: #3566). |
| `metric_suite_version`, `scorer_selection` | What produced the stored scores; a rescore restamps both. |
| `execution_mode`, `apply_prompt_guards`, `require_tool_approval` | Stamped at start; NULL on older runs. |
| `persona_version_number`, `prompt_versions`, `agent_config_versions`, `skill_versions`, `pinned_dataset_versions` | Snapshot at creation. `pinned_dataset_versions` is `{dataset_id: version_number}`; NULL on an older run, rendered from live entries. |
| `rescore_error` | Why the last rescore failed; cleared by the next success. A rescore never writes `error`. |
| `anomaly_count` | Results with an `execution_anomaly_kind`. |
| `arm`, `trial_index`, `placebo_*` | Placebo only; `real`, 0, NULL outside a comparison. |

**Run states**

| From | To | When |
|---|---|---|
| none | `pending` | A launch creates the run and its execution in one transaction. |
| `pending` | `running` | Its execution starts it. |
| `running` | `pending` | Its worker stops (a deploy, a restart): paused at the next entry boundary (`_pause_for_the_stop`), or, when the stop cut an entry, put back by the next attempt taken back on the stopping worker's word (`_resume_runs_cut_by_a_stop`). A resumed run keeps its `started_at` and skips the entries it stored a result for. |
| `pending`, `running` | `failed` | From `pending`: the engine gate off (`EVALUATION` off and not a health launch); agentic AI unavailable past the hold (`services/ai_hold.py`). From either: agent or evaluation gone; model no longer served; a pinned snapshot missing; its placebo could not be built; no entry counted in any headline; a callback entry failed or timed out; the execution dead-lettered; an attempt abandoned by a worker gone without a word (crash, out-of-memory kill) reclaimed; the stranded sweep. |
| `pending`, `running` | `cancelled` | Cancel or emergency stop; never ever rewritten afterwards. |
| `running` | `completed` | Every entry processed and one counted; a callback run once every episode is terminal. |
| `completed`, `failed`, `cancelled` | `running`, then the previous status | Rescore: the payload's `previous_statuses` holds each run's status, given back on success and on failure (failure text in `rescore_error`). |

**Episode states** (`services/evaluation_continuation.py`): an entry starts `running` (the `pending` default is never written) and ends `completed` or `failed`; in callback mode it parks in `waiting_callback` after the first turn until `callback_timeout_seconds`, then `timed_out`. An accepted signed callback moves `waiting_callback`, `running` or `pending` to `resumed`; the `resume_episode` execution sets `running`, then `completed` or `failed`. Cancelling or stranding the run fails every non-terminal episode. Terminal: `completed`, `failed`, `timed_out`.

**Worker stop** (`EvaluationAdapter.resume_when_worker_gone`): a deploy neither fails a run nor spends an attempt.
- From the moment a stop is asked (`ctx.draining`, the stop signal; `ctx.stopping` where a worker has none), a run not started stays `pending` and a running one pauses at its next entry boundary (`_pause_for_the_stop`, under the lease fence; a pause that cannot be written leaves the run `running` for the next attempt to resume). Once the runs in flight return, while any is left `pending` or `running` with no parked callback (`_runs_left_unfinished`), `execute_evaluation` returns `True`, writes nothing at batch end, and the adapter hands the execution back (`defer`, `place="head"`, payload marker `evaluation_handed_back`). A callback-backed run is not paused.
- One writer per run: a run starts (`pending` to `running`) under the lease fence, so a worker whose evaluation was taken back leaves queued runs `pending`; each entry starts only while the execution's lease is this worker's (`_lease_still_ours`), finished entries skipped first, and its writes take the lease fence with the run lock (`_lock_run_before_episode_writes(lease=)`, execution then run, held to the end-of-entry commit); a worker whose evaluation was taken back writes nothing (`LeaseLostError`).
- Before a stopping worker says it stopped, it says `draining` again and the reconciler fires the cancel event of every `stop_on_lease_loss` execution still running there, waiting for those, then for the executions still committing, within one `LEASE_LOSS_STOP_WAIT_SECONDS` deadline (`_stop_lease_loss_work`). Its presence record says it does (`worker_presence.STOPS_LEASE_LOSS_WORK`): only the work of a worker that carries it and said `stopped` is taken back at once; a worker of an older release, or one whose presence lapsed, keeps it until its lease expires (`waits_for_lease_of`), then it is taken back on the same last word (both lease sweeps of a cycle read one clock, so the expired sweep never takes such a row without its holder's word).
- A run cut inside an entry is taken back on its worker's last word (`stopped_on_its_word`: `draining`, `stopped`, or replaced by a deploy), first in the queue, the attempt given back. The stop is recorded in the payload (the hand-back marker or the `lease_expired` record) and read by `pending_stop_cut`, so a retry or an AI hold in between does not lose it; the reclaim that resumes from it consumes it (`stop_resumes.consumed`), so a later attempt cut otherwise is not resumed on it. That reclaim (`resume_stopped_runs`) puts the runs it owns back to `pending`, deletes the cut entry's `running`/`pending` episode, and keeps every closed or parked episode and every result, all skipped. Its batch-end pass also covers the runs the execution finished before the stop, and runs when no run is left pending.
- A resumed run keeps its `started_at`. Once its settings were stamped (`execution_mode` set), it keeps them (mode, judge selection, prompt guards, tool approval), whatever the evaluation was edited to; a run cut during its setup reads them from the evaluation as a fresh run. A flow run resumes only on the flow version it started on (`flow_version_number`, written with the settings: the launch's stamp of a run cut in its setup is not checked); edited during the pause, it is failed ("The flow changed while the run was paused"). `metric_suite_version` is re-stamped: it names the suite of the release that scored the run's last entries; a rescore re-scores every entry under one suite.
- Bounded per run: a run's first resume is its baseline; after it, a cut with no new result since the last resume, in an entry that had run at least `STOP_RESUME_MIN_WORK_SECONDS` (300; its open episode's start to the stop, so queue and hold time never count; for a take-back, the stop is the gone worker's last word, not the take-back after the restart or the lease wait), counts as stalled (`stop_resumes.runs`); a new result resets it, a shorter one (a hop within one rolling restart) leaves it; after `MAX_STOP_RESUMES_WITHOUT_PROGRESS` (3) the run and its cut entry are failed ("Cut by 4 worker stops in a row before it finished an entry") and its count dropped. A worker gone without a word still has its runs failed as abandoned and spends the attempt; so does a take-back with no readable last word (Redis out, or a word older than its TTL).
- The pause lands at an entry boundary only when the entry in flight finishes within `WORKER_SHUTDOWN_GRACE_PERIOD`; past it the entry is redone on the next worker.

**Orchestration payloads** (`orchestration_executions`, `adapter = "evaluation"`)

| Payload | Staged by |
|---|---|
| `{evaluation_id, trigger_reason}` | Every launch (`stage_evaluation_execution`); `enqueue_evaluation` for on agent change. |
| `{kind: "rescore_runs", run_ids, previous_statuses}` | Rescore (`stage_rescore_execution`). |
| `{kind: "resume_episode", episode_id, idempotency_key}` | An accepted callback. |

### `evaluation_results`

- `entry_id` (live entry) and `version_entry_id` (the snapshot entry scored, rescored and rendered): FK SET NULL.
- `agent_output`: the answer, or the error text when the agent run raised. `score` is 0 when the judge did not run (§5). `duration_seconds`: agent time, 0 for a raised entry.
- `input_fingerprint`, `is_reused`, `reused_from_result_id` (self-FK SET NULL): §7. `execution_anomaly_kind`: `transient_error`, `platform_error`, `confirmation_seeking` or NULL (§4). `execution_details`: §10.

### JSON shapes

- **`trigger_config`** (`evaluations`): `schedule` (cron), `on_agent_change` (bool), `await_callback` (bool), `callback_timeout_seconds` (int, default `timeout_seconds`), `resume_secret_enc`. The secret is written in clear as `resume_secret`, stored Fernet-encrypted, served as `"***"`, never exported (`services/evaluation/resume_secret.py`); sending the mask or omitting it keeps it, `null` or `""` removes it. Update merge rules: §3.
- **`field_scoring_overrides`** (`evaluations`): `{field_modes: {pointer: "exact" | "free_text" | "excluded"}, field_weights: {pointer: number ≥ 0}}`, keyed by JSON Pointer; any other key, and a weight on an excluded path, is refused on write (`validate_field_scoring_overrides`, `services/evaluation/metric_package/quality_adapter.py`). An absent pointer is derived from the schema.
- **`output_extractor`** (`evaluation_agents`): `{source: {kind, tool_name?}, operations: [{op, path?}], output_kind}`. `kind` is `agent_output`, `tool_output` or `tool_input`; `tool_name` (≤ 128 characters) is required for a tool source and refused otherwise. 1 to `extraction_max_operations` (8) operations from the catalog in `services/evaluation/extraction/spec.py`. `output_kind` is `label_set` (CLASSIFICATION) or `object` / `array` (STRUCTURED_JSON); other families take no extractor. NULL scores the raw answer (§9).
- **`headline_details`** (`evaluation_runs`): `quality_metric_keys`, `quality_contributions`, `excluded` (`[{key, reason}]`), `quality_coverage` (`{entries, counted, left_out}`), `performance_metric_keys`, `performance_contributions`, `performance_excluded` (`[{key, reason, run_ids}]`), `metric_entry_counts` (`{key: n}` for both halves). Each half is merged in by its own pass. Meaning: §6.
- **`execution_details`** (`evaluation_results`): §10.

## 2. Datasets

### Family, schema and vocabulary

- `family` is `CLASSIFICATION`, `STIX_BUNDLE`, `STRUCTURED_JSON` or `PROSE` (`services/evaluation/metric_package/families.py`), or NULL (`needs_family`), never inferred. A linked dataset with no family refuses every launch and rescore (`check_dataset_families`). A Hugging Face import creates a family-less dataset.
- `label_vocabulary`: CLASSIFICATION only, non-empty; `null` clears it. Every expected answer's normalised labels must be in it, checked when an entry is written and against every entry when the vocabulary is set (`validate_reference_labels`).
- `output_schema` (dataset and entry): STRUCTURED_JSON only, `{type: "json_schema", definition}`, any other type refused. The definition is checked as JSON Schema 2020-12, its patterns screened for catastrophic backtracking, under a time bound (`validate_output_schema`, `services/evaluation/scorers/structured_output.py`). `{}` clears it.
- Conformance (`services/evaluation/dataset_conformance.py`), both ways: saving a schema checks every entry, saving an entry checks it. A non-empty expected answer must conform to its entry's schema, else the dataset's; a STIX_BUNDLE one must read as a STIX reference, also checked for every entry when the family changes to STIX_BUNDLE. An empty answer is a draft and passes. A refusal is a 422 naming the entry and the label or JSON location.
- A family change clears a vocabulary or schema the new family does not use.

### Versions (`services/evaluation/dataset_versioning.py`)

`bump_version` writes the header, one frozen row per live entry and the new `current_version`, in the writer's transaction.

| Write | Version |
|---|---|
| Create with entries | yes (none without entries) |
| Add, update or delete an entry; upload or remove an entry file | yes |
| Update changing `label_vocabulary` (compared as a set) or `output_schema` | yes, once the dataset has a version |
| Update of name, description, instructions, sharing or read-only, or a family change that clears nothing | no |
| ZIP import, Hugging Face import | yes |
| First launch on a never-versioned dataset | version 1 (`pin_evaluation_versions`) |
| Built-in seed sync | when the dataset no longer reads as its current version (`cut_version_if_changed`) |

A version holds the entries (position, description, prompts, expected answer, evaluation prompt, file ids, schema), `output_schema`, `label_vocabulary`, `entry_count` and `content_hash` (SHA-256 over prompts, answers, schemas and file ids, order-independent). The family is not frozen: a run takes it from the evaluation. The dataset's `evaluation_instructions` are read live. Version rows are never updated.

**Pinning**: every launch records each linked dataset's `current_version` in `pinned_dataset_versions`. The run reads its entries, schema and vocabulary only from those versions (`load_pinned_entries`), takes `total_entries` from the live entry count at launch (the pinned versions' `entry_count`, cut in the same transaction; a re-run takes its pins', `count_pinned_entries`), and stores `version_entry_id` on each result; rescore and the result pages read the snapshot. Replay and "Re-run full" keep the original pins (§8). A run whose pin is below the dataset's `current_version` is served with `has_dataset_drift: true`.

### Linking

- `POST /datasets/{id}/link/{evaluation_id}` returns 409 when both families are set and differ (`check_family_compatibility`, `services/evaluation/dataset_family_compatibility.py`), or when already linked. A family-less side never conflicts.
- A dataset or evaluation family change re-checks every link and returns `family_conflicts`, removing none.
- `GET /datasets/for-evaluation/{evaluation_id}` serves each link's `family`, `needs_family` and `family_conflict` (the `check_family_compatibility` message, or null), shown on the evaluation's Dataset tab.
- Creating an evaluation with `dataset_ids` applies the same check: a dataset of another family is a 409 naming it, and nothing is saved. Unknown ids are skipped.
- A dataset made private keeps its links: evaluations, their links and the evaluation agent tools are reachable only by admins, who see every dataset, and by Platform Health on the datasets it built (its evaluation tools refuse any other caller), so no link is left to a user who can no longer see the dataset. The company-managed toggle of a dataset or an evaluation (`handle_company_managed_toggle`, `agent_field=None`) sweeps no reference. Should evaluations open to non-admins, a dataset going private would need to drop the links of the evaluations whose owner loses it.

### Read-only

`is_read_only` refuses (403) every entry write, file upload and removal, the dataset's deletion, and every update unless the same request sets `is_read_only: false`. The sharing fields are not locked: `shared_group_ids`, `shared_group_permissions`, `shared_user_ids`, `shared_user_permissions`, `access_restricted` and `is_company_managed` change on a read-only dataset under their usual rules, as on a read-only knowledge base; a request that also sends any other field (an explicit `null` included) is refused whole. Linking is unaffected.

### Limits

| Input | Limit |
|---|---|
| ZIP upload | 500 MB (413); the page refuses a larger file before sending it (`useDatasetApi.ts`) |
| ZIP uncompressed | 2 GB, counted on what members really expand to |
| `manifest.json` | 256 MB |
| Entry file | 40 MB (`MAX_UPLOAD_SIZE_BYTES`); the page refuses a larger file before sending it |
| Hugging Face import | `row_limit` 1 to 500,000 (default 500,000); a `content_hash` match returns the existing dataset (`reused: true`) |

### Dataset ZIP

`manifest.json` beside `files/f<i>_<filename>`:

```
{ "format": "filigran-dataset-v1", "version": 2, "exported_at": "...",
  "dataset": {"name", "description", "evaluation_instructions", "family",
              "label_vocabulary", "output_schema", "is_read_only"?},
  "entries": [{"position", "input_prompt", "expected_answer", "evaluation_prompt",
               "description", "files": ["f0"], "output_schema"}],
  "files": [{"id": "f0", "filename", "content_type", "size", "sha256",
             "path": "files/f0_<filename>"}] }
```

- Import requires `format` = `filigran-dataset-v1`, whatever `version`. It validates every manifest value as create does (422 naming it) before storing any file.
- A file absent from the ZIP, or whose `sha256` (or legacy `md5`) differs, is skipped with a warning.
- An entry without `position` takes its list index; tied positions are renumbered `0..n-1` in manifest order, refused when an evaluation import's results name one (§11).
- Import runs in the caller's transaction; stored files are deleted if it fails.

## 3. API

Handlers: `api/v1/evaluations.py`, `api/v1/datasets.py`; schemas: `api/v1/schemas/evaluation.py`. Prefixes `/api/v1/evaluations` and `/api/v1/datasets` (`main.py`).

### Feature flag

Both routers (and `POST /api/v1/improvement-proposals/persona`, with the persona rewrites a person asked for) carry the dependency `require_evaluation` (`services/evaluation/availability.py`). While `EVALUATION` is off, every route answers 403 naming the flag, before authentication, `POST /evaluations/resume` included, and none appears in `/api/openapi.json`. Everything else the flag switches off, and the engine gate that lets a health launch run whatever the mode: `dev-docs/evaluation-feature-flag.md`.

### Gates

Admin = `require_admin`; +AI = `require_ai_enabled`; +EE = `require_agentic_ai` (AI on and agentic AI licensed); User = any authenticated user; HMAC = body signature, no JWT (§12). A route with no AI gate works with AI off; background work is held by the agentic gate (`dev-docs/evaluations.md`).

### Evaluation routes

| Purpose | Routes | Gate |
|---|---|---|
| List, detail | `GET ""` (`agent_id`, `scope`; three-tier, `scope=all` for every row), `GET /{id}` | Admin |
| Create | `POST ""` | Admin +EE |
| Update, delete, copy | `PUT /{id}`, `DELETE /{id}`, `POST /{id}/copy` | Admin +AI |
| Export | `GET /{id}/export` | Admin |
| Import | `POST /import` | Admin +AI |
| Family catalogue | `GET /families`: `[{id, label, description, metric_keys, structured_output, extractor_output_kinds}]`; open to any user, the flag still applies | User |
| Metric catalogue | `GET /{id}/metric-catalog`, `GET /metric-catalog?family` (400 for an unknown family) | Admin |
| Owner's models | `GET /{id}/available-models`: the chat models of the owner's providers, what the Configuration tab and the Run menus offer to add (§3) | Admin |
| Pickers, warnings | `GET /agents/all`, `GET /always-ask-tools?agent_ids`, `GET /prompt-guard` | Admin |
| Agents, baseline, agent tags | `POST /{id}/agents`, `DELETE /{id}/agents/{agent_id}`, `PUT /{id}/baseline`, `PUT /{id}/agents/{agent_id}/tags` | Admin +AI |
| Extractor preview (§9) | `POST /{id}/extractors/preview`, `POST /extractors/preview`, `GET /{id}/extractors/sample-results` | Admin |
| Launch (§8) | `POST /{id}/trigger`, `POST /{id}/trigger-from-runs` (≤ 200 run ids), `POST /runs/{run_id}/replay` | Admin +AI |
| Re-score (§8) | `POST /runs/{run_id}/rescore`, `POST /{id}/rescore-from-runs` (≤ 200), `POST /runs/{run_id}/results/{result_id}/replay-judge`, `POST /{id}/recalculate-headlines` | Admin +AI |
| Placebo | `POST /{id}/placebo/estimate`, `GET /{id}/placebo-comparisons`, `GET /placebo-comparisons/{comparison_id}` | Admin |
| Read runs | `GET /{id}/runs` (`limit` 20, ≤ 100), `GET /{id}/runs/count`, `GET /{id}/chart?range`, `GET /runs/{run_id}` and its `results/{result_id}/traces/{trace_index}/download`, `results/{result_id}/metrics`, `metrics/aggregate`, `params`, `tags`; `GET /runs/diff?run_a&run_b`, `POST /runs/search` | Admin |
| Compare evaluations, review a bundle (§13) | `GET /compare?ids&run_ids`, `GET /bundle-review?result_ids`, `POST /bundle-reviews` | Admin |
| Stop, delete runs | `POST /runs/{run_id}/cancel`, `DELETE /runs/{run_id}`, `DELETE /{id}/runs` | Admin |
| Write run tags | `PATCH /runs/{run_id}/tags` | Admin +AI |
| Callback | `POST /resume` | HMAC |

A placebo comparison's run is refused by `trigger-from-runs`, `replay` and `rescore` (422) and fails in `rescore-from-runs` with `placebo_comparison_run`; the placebo routes and fields: `dev-docs/agent-placebo-evaluation.md`.

### Dataset routes

| Purpose | Routes | Gate |
|---|---|---|
| No AI gate | `GET ""` (`family` also keeps family-less datasets), `GET /{id}`, `GET /{id}/versions[/{n}]`, `GET /for-evaluation/{evaluation_id}`, `GET /{id}/export`, `GET /hf-splits`, `GET /hf-preview`, `DELETE /entries/{entry_id}/files/{file_id}` | Admin |
| Create | `POST ""` | Admin +EE |
| AI gate | `PUT`/`DELETE /{id}`, `GET /{id}/seed-ground-truth` (a preview), entries (`POST /{id}/entries`, `PUT`/`DELETE /entries/{entry_id}`, `POST /entries/{entry_id}/upload`), `POST`/`DELETE /{id}/link/{evaluation_id}` (409 on a family conflict) | Admin +AI |
| Import | `POST /import` (ZIP ≤ 500 MB, ≤ 2 GB declared uncompressed), `POST /hf-import` (≤ 500,000 rows, SSE progress) | Admin +AI |

### Request bodies

**`POST /{id}/trigger`** (`TriggerEvaluationRequest`), optional, every field optional:

- `model_ids`: absent, the enabled models (plus each agent's default when `use_agent_default_model`). A model the evaluation does not enable must be served by the owner's providers (else 400) and is added to `model_ids`.
- `include_default`: with `model_ids`, also runs each agent's default model when the evaluation uses it.
- `params`: flat key/value object, stored as `manual` run parameters (§13); a nested value is a 422.
- `tags`: the runs' own tags (§13).
- `reuse`: `inherit` (default, writes nothing), `on` or `off`, stored as the `manual` `reuse` parameter, winning over a `reuse` key in `params`; else 422 (§7).
- `placebo`, `confirm_write_tools`, `placebo_seed`: `dev-docs/agent-placebo-evaluation.md`.

A launch refusal (`check_launchable`, `plan_launch` in `services/evaluation/launch.py`) carries its own status: 400, 422 for an unavailable model, 409 when the evaluation's owner is no longer an active administrator (`owner_refusal`: its runs act as its owner, whoever starts them; an ownerless evaluation is not checked). The same rule fails a run when it starts or resumes, and closes a callback episode's resume with that reason.

**Re-run and re-score** (§8):

- `POST /runs/{run_id}/replay`: `{reuse}` (`inherit` | `on` | `off`, else 422).
- `POST /{id}/trigger-from-runs`: `{run_ids}` (≤ 200); the new runs are written with `reuse` `off`.
- `POST /runs/{run_id}/rescore`: no field; `POST /{id}/rescore-from-runs`: `{run_ids}` (≤ 200). Both strict (`extra="forbid"`): any other field, the retired `scorer_selection` included, is a 422.
- `POST .../replay-judge`: `{judge_max_tokens}` (256 to 131072, default 2048).

`POST /runs/search`: §13. `POST ""` and `PUT /{id}` accept the retired `quality_weight` and ignore it.

### Update, copy, create and import

`PUT /{id}` is a partial update (`UpdateEvaluationRequest`):

- An omitted field is kept. `null` clears a nullable setting (`CLEARED_BY_NULL` in `services/evaluation/settings.py`: the nullable columns of `EVALUATION_SETTINGS`); `null` keeps a non-nullable one and keeps `family` and `scorer_selection`. Sharing fields sent as `null` are kept.
- `trigger_config` is merged into the stored one: a key left out is kept, a key sent as `null` is removed (`resume_secret: null` removes the secret), `{}` and `null` change nothing.
- `evaluation_instructions: ""` clears as `null` does; `tags: {}` or `null` clears them; `placebo_config: null` removes the method.
- A `family` change re-checks the stored extractors the request does not replace (422) and reports `family_conflicts` with linked datasets, which stay linked. Changing tolerance, penalty, family or a headline selection recomposes the stored headlines (§6).

`POST /{id}/copy` copies every `EVALUATION_SETTINGS` value, each agent link with its extractor, tags and baseline flag, and the dataset links; no run. The copy is private, named `Copy of <name>`, with `trigger_config` `{}`: no schedule, on-agent-change, callback mode or secret. A stored `execution_mode` other than `llm_only`/`full_loop` becomes `llm_only`. The settings are copied, not re-validated.

A model of `model_ids` the launch's rule rejects (`model_served`: a chat model one of the owner's providers owns, by its catalog or its vendor prefix) is refused on create, and on update when the request adds it (422, the launch's wording, `check_models_served`); a saved one is never re-checked, so an evaluation whose provider went away stays editable. Copy and import keep it and name it in `warnings` (the copier or importer is the new owner). Every evaluation response, and the `get_evaluation` agent tool, serves `unavailable_model_ids`, read from the owner's providers at each read (`unserved_model_ids`), never stored; the Run menus leave such a model out of a selection. The pickers list the owner's models (`GET /{id}/available-models`), not the editor's, so they offer only what a save accepts.

Create and import validate the same way: both call `_validated_settings`, and an import reads its settings through `CreateEvaluationRequest`, so a package value create would refuse is a 422 naming the field, except `quality_source: f1`, which create refuses and import keeps; an unknown family is a 400.

## 4. Execution outcomes

Each entry of a run stores one `EvaluationResult`. Its outcome is read from two columns: `execution_anomaly_kind` (`transient_error`, `confirmation_seeking`, `platform_error` or NULL; constants in `services/evaluation/framing.py`) and `is_judge_failure`. The one inclusion rule is `aggregation.counts_in_headlines` (SQL form `result_counts_in_headlines`): an entry counts when its anomaly kind is NULL or `confirmation_seeking` and it is not a judge failure. Every headline, the metric roll-ups, `avg_sub_scores` and coverage read through it. How the evaluated turn itself runs is in [`dev-docs/evaluations.md`](../evaluations.md#how-an-evaluated-turn-differs-from-chat).

| Outcome | How it is detected | Stored | Counts in headlines | Run counter |
|---|---|---|---|---|
| Scored answer | default | judge verdict, quality and performance `metric_results` | yes | - |
| Confirmation-seeking | output heuristic (below) | judged and scored like any answer | yes | `anomaly_count` |
| Transient provider error, surfaced as the output | output heuristic (below) | `score` 0, sub-scores 0, judge skipped (`judge_skipped` step); metrics still measured | no | `anomaly_count` |
| Transient provider error, raised | `classify_raised_entry` | `score` 0, sub-scores 0, no `metric_results` | no | `anomaly_count` |
| Configuration-shaped failure, raised | `classify_raised_entry` | `score` 0, sub-scores 0; every quality metric 0 with reason `agent_run_failed` (left out when the entry's scoring configuration is unusable); performance records one failed call | yes, as 0 | - |
| Platform error, raised | `classify_raised_entry` | `score` 0, sub-scores 0; quality metrics `unavailable` with reason `platform_error` | no, but disclosed in Overall Quality coverage | `anomaly_count`, `platform_error_count` |
| Judge failure | `score_result` (§5) | the partial verdict, `is_judge_failure` true | no | `judge_failure_count` |

### Raised entries

An entry whose run raises (`classify_raised_entry`, from `services/failure_causes.classify_run_failure`) stores the error text as `agent_output` and the cause under `execution_details.agent_run_failure`; a rescore keeps that treatment.

- **Transient**: `provider_rate_limited`, `provider_overloaded`, `provider_timeout`, `provider_connection_failed`, `all_providers_failed`. A status on the exception maps 429 to rate-limited, 408 and 504 to timeout, 500/502/503/529 and any other 5xx to overloaded.
- **Configuration-shaped** (the agent failed the entry): `context_overflow`, `request_too_large`, `model_rejected_request` (400, 404, 422 and any 4xx not otherwise mapped; 413 is `request_too_large`, 401/403 a platform error), `model_declined`, `run_budget_exhausted`.
- **Platform error**: any other cause, `unknown` included (configuration, licence, quota, a bug). Scoring it as the agent's 0 would rank agents by platform incidents.

A `skip_llm` turn whose Prepare tool returned an error or raised, or whose Finalize tool returned an error, returns that error as its text; chat shows it as the answer. `_execute_agent_full_loop` reads `run_agent_chat`'s `error_out` instead: a pipeline code (`agent_chat.PIPELINE_ERROR_CODES`) raises `PipelineToolFailedError` (`agent_chat.pipeline_tool_failure`), whose message is the tool's own (the `error` of a `{"error": ...}` result), classified `pipeline_tool_failed`: a platform error, its extractor and judge never run. The turn does not flag a pipeline tool that could not be loaded, a Finalize tool that raised (Prepare's output is returned), nor a service that failed on every file and returned its per-file errors as a result: those are still scored as answers. Assignments and delegations read the same turn and take a pipeline tool's error as an answer too.

A judge call that raises is not classified by these rules: `execute_evaluation_run` catches it apart from the agent's run (`judge_failure_from_exception`), so the entry keeps the agent's `agent_output`, its anomaly kind and its quality metrics and is recorded as a judge failure, left out of every headline until a rescore or a judge-only replay scores it.

### Output heuristics

`classify_execution_anomaly` reads the final output text, only in `full_loop`, `llm_only` and `llm_only_fallback` modes (`skip_llm` is never flagged). Transient wins when both match.

- **Transient error** (`detect_execution_error`): the trimmed output starts with `Error:` or `Error code:` and either carries `Error code: NNN` with NNN in {429, 500, 502, 503, 504, 529} or a transient keyword (overloaded, rate limit, timeout, service unavailable, bad gateway, connection error, ...).
- **Confirmation-seeking** (`detect_confirmation_seeking`): the output, stripped of trailing markdown, ends with `?`, or holds an enumerated option (`A)`, `1.`, `option B`) together with a choice cue (would you like, please choose, ...).

Detection is by text, so a genuine answer ending in a question is flagged confirmation-seeking (it still counts) and an error worded otherwise is judged as an answer. No anomaly entry is reused (`fingerprint.is_result_reusable`).

### Run outcome

- A run whose results exist and none counts ends `failed` with `NOTHING_COUNTED_ERROR` (`_nothing_counted`); its headlines read N/A. A cancel landing during scoring keeps `cancelled`.
- Otherwise the run ends `completed` and `error` is cleared. `anomaly_count` counts every result with a non-NULL anomaly kind (confirmation-seeking included); `platform_error_count` and `judge_failure_count` are computed on read (`api/v1/evaluations._get_avg_sub_scores`).

### Full Loop and LLM Only

- Full Loop and `skip_llm` call `_execute_agent_full_loop`; a failure fails the entry by the classification above, with no LLM Only fallback. `llm_only_fallback` appears only on runs recorded before that fallback was removed. `fallback_entries` (run responses, agent tools) counts only those legacy entries (`services/evaluation/run_mode.py`): it is 0 on every run recorded since, whatever model answered.
- A run measures the models it runs: no call of its chain goes to another model. Every `run_agent_chat` turn of an evaluation's chain (`_evaluation_chain`: the evaluated turn, and the agents it consults, transfers to or delegates to, each on its own model) resolves with `get_llm_for_user(..., failover=False)`, so no `FailoverLLMProvider` (no provider chain, no admin fallback model, no decline fallback), and builds its run budget with `fallback_enabled=False`, so no budget-driven downgrade. The evaluated model's own error fails the entry and is classified like any other (a 403 is `PROVIDER_AUTH_REJECTED`, a platform error left out of every headline); a run none of whose entries counts ends `failed`. LLM Only and the judge resolve through `resolve_provider_for_model`, which never wraps. Pinned by `tests/test_evaluation_model_pinned_postgres.py` (the real run) and `tests/test_agent_chat_evaluation.py` (the chain).
- LLM Only streams one model call with `max_tokens` 4096 (`_LLM_ONLY_MAX_TOKENS`). An answer cut at the budget is asked once more with the model's output ceiling (`llm/cut_answers.py`); an answer still cut, or a model whose ceiling is not larger, raises, and the entry is classified as raised. LLM Only offers no tool, so its `available_tools` is `[]`.

## 5. LLM judge

Code: `score_result` and its helpers in `services/evaluation_engine.py`.

### When it runs

- The judge runs when `scorer_selection` is NULL or contains `"judge"` (`scorers/registry.judge_enabled`; `"judge"` is the only registered scorer). An explicit list without it, `[]` included, is judge-off: entries store `score` 0, sub-scores 0, reasoning "LLM judge scorer not selected for this evaluation." and a `judge_skipped` step.
- Each run snapshots its selection in `EvaluationRun.scorer_selection`; Judge Quality and `avg_sub_scores` read the snapshot, never the evaluation's current value.
- A transient-error anomaly skips the judge; a confirmation-seeking entry is judged.

### Call

| Setting | Value |
|---|---|
| Model | `evaluator_model`, else the platform default model, resolved among the evaluation owner's providers |
| System prompt | `evaluator_prompt`, else the content of the company-managed, enabled skill tagged `slug:evaluation-judge-system-prompt`; neither is an error |
| Verdict enforcement | provider-native structured output when supported, else the schema appended to the system prompt. A `TypeError` naming `structured_output` gets one retry without it, with the prompt-side schema |
| Temperature | 0.0 |
| `max_tokens` | `judge_max_tokens`, default 8192, range 256-131,072 (schema); the judge-only replay (`POST /runs/{run_id}/results/{result_id}/replay-judge`) takes its own, default 2048, same range |

### User prompt

Sections in this order:

| Section | Content | Present when |
|---|---|---|
| `## INPUT` | the entry's input prompt (its non-interactive fallback when empty) | always |
| `## AGENT OUTPUT` | the agent's raw final answer | always |
| `## EXPECTED ANSWER` | the entry's expected answer | always |
| `## AVAILABLE TOOLS` | comma-separated names, or "None (no tools were available to the agent)" | `available_tools` is not None |
| `## TOOL CALLS MADE` | one `- **name** (Nms): description` per tool step of the agent timeline (else the linked trace spans), or "None (the agent did not use any tools)" | always on the run paths |
| `## EVALUATION INSTRUCTIONS` | the entry's `evaluation_prompt`, else the evaluation's `evaluation_instructions`, else the dataset's | one is set |

`available_tools` per path (stored as `execution_details.available_tools`; read back by `_recorded_available_tools`):

| Path | `available_tools` |
|---|---|
| Fresh Full Loop or `skip_llm` | the agent's tools for the turn |
| Fresh LLM Only | `[]` |
| Reused answer, rescore, judge-only replay | the list stored with the result (the source's for a reuse); `[]` for an LLM Only result stored without one; else None |
| Resumed callback | the first turn's list, recorded in `episode.first_turn_details` and judged through `stored_judge_inputs` |

The judge reads the raw answer while the family's quality metrics score the extracted prediction when the agent has an output extractor.

### Verdict and score

- Schema (`_build_judge_verdict_schema`, from `SUB_SCORE_WEIGHTS`): `{"sub_scores": {tool_selection_quality, response_completeness, response_accuracy, instruction_adherence, coherence: int 0-100}, "reasoning": string}`, all required, no other key.
- Parse: `structured_output.validate_and_parse(..., allow_partial=True)` extracts the verdict from fences or prose and repairs JSON slips; each sub-score is coerced to an int and clamped to 0-100. A missing or non-numeric one becomes 0 and is listed as missing.
- When `available_tools` is `[]`, `tool_selection_quality` is set to 100 before the completeness check. When it is None, the judge grades it.
- **Judge failure** = unparseable verdict OR truncated at `max_tokens` (`answer_was_cut`) OR any missing sub-score OR a judge call that raised during a run (reasoning "Judge failure: the judge call raised (...)"). No retry, no substitute: the reasoning becomes "Judge failure: ..." with a 300-character raw preview, and the entry leaves every headline. A cut verdict is remedied by raising `judge_max_tokens` or replaying the judge with a larger `judge_max_tokens`.
- `score = round(Σ sub_scores[k] × w[k], 2)` over the keys of `w`: `sub_score_weights` when set, else 0.20 each. Custom weights are used as given (no validation, no normalisation), `{}` falls back to 0.20 each; a sub-score absent from them adds nothing. The score is stored even on a judge failure.
- A replay is a failure unless the judge reported `is_judge_failure: false`; it never changes the anomaly kind.

## 6. Headlines

### Writer

`_compute_run_headlines` (`services/evaluation_engine.py`) is the only writer of the three run headlines; the arithmetic lives in `services/evaluation/aggregation.py`, which reads `evaluation_metrics` rows (scored values only) joined to the counting rule, never the result blob except for status reasons and coverage. Its performance pass, `_recompute_performance_headlines`, takes `pg_advisory_xact_lock(hashtext('evaluation_headlines:<evaluation_id>'))` and rescores every completed run of the evaluation together; `commit=False` is refused unless that pass is skipped. A headline not measured is NULL ("N/A"), never 0.

| Column | Headline | Basis |
|---|---|---|
| `score` | Judge Quality | `_penalized_mean` of counting entries' judge scores; NULL when the run's snapshot is judge-off or none counts |
| `quality_score` | Overall Quality | absolute, per run |
| `performance_score` | Overall Performance | comparative, across the evaluation's completed runs, placebo comparison runs excluded ([placebo](../agent-placebo-evaluation.md)) |

### Stage 1: per-metric roll-up

Each metric rolls up as its definition declares ([metric README, rollup](../../backend/app/evaluation_metrics/README.md#how-a-run-combines-a-metric-rollup)); `k` is `penalty_factor` (default 0.5), σ the population standard deviation.

- **Averaged quality**: values are oriented first (lower-is-better becomes 100 − v), then `mean − k·σ`, clamped to 0-100; one entry reads its value. A value outside 0-100 drops the metric.
- **Averaged performance**: native units, the penalty moves toward worse (`mean + k·σ` for lower-is-better, `mean − k·σ` otherwise), floored at 0.
- **Pooled**: Σ numerators / Σ denominators, no penalty, ×100 and oriented for quality. Every entry must carry counts. A zero total reads 100 for quality, no value for a performance rate.
- **Macro**: per-class tp/fp/fn summed over the run, then the mean F1 of the classes seen; every entry must carry its class counts; no class seen reads the entries' mean.
- A metric with no direction, or a legacy non-package key, never contributes. No value is ever 0 by absence.

### Overall Quality

Equal-weight mean of the selected quality roll-ups, rounded to 2. `overall_quality_metrics`:

- NULL: the available keys in `catalog.DEFAULT_QUALITY_KEYS` (`stix.entity_prf`, `stix.relationship_prf`, `json.field_accuracy`, `classification.micro_prf`, `classification.macro_prf`, `classification.samples_f1`, `prose.token_prf`); other scored quality metrics are disclosed `not_a_default_contributor`. None available: N/A.
- `[]`: N/A, nothing disclosed.
- A list: its keys that produced a value; the others disclosed `failed` or `not_applicable`.

**Coverage** (`quality_coverage`): `entries` = counting entries plus platform-error entries; `counted` = those where every contributing key scored (the selection, or the family's defaults, when none contributed); `left_out` = per reason, the stored reason of the first unscored metric, else `not_measured`.

### Overall Performance

1. A run with no rankable performance metric abstains (`no_performance_data`) and does not shrink the basis.
2. The basis is the intersection of the measuring runs' keys; fewer than two measuring runs is `too_few_comparable_runs`.
3. Per metric (`normalise_across_runs`): equal values all read 100. Otherwise the tolerance band (`performance_tolerance`, default 5, ≥ 0) sets a threshold: `best × (1 ± t/100)` when best > 0, else `best ± span × t/100` (span = highest − lowest). Runs inside it read 100, the rest scale linearly to 0 at the worst.
4. Selection, `overall_performance_metrics`: NULL takes the shared keys minus `NON_DEFAULT_PERFORMANCE_KEYS` (`performance.latency.ttft`, opt-in), `only_opt_in_metrics_shared` when nothing remains; `[]` reads N/A with `no_metric_selected`; a list intersects with the shared keys.
5. Equal-weight mean, rounded to 2.

### Exclusion reasons

| Reason | Level | Meaning |
|---|---|---|
| `not_applicable` | metric | unavailable or skipped, or outside the family's catalogue; re-running cannot help |
| `failed` | metric | invalid, or never reported; wins a mixed set |
| `not_a_default_contributor` | metric | scored, but the default selection leaves it out |
| `missing_from_other_runs` | metric (performance) | this run measured it, the listed runs did not |
| `no_performance_data` | run | the listed runs abstained |
| `too_few_comparable_runs` | run | fewer than two measuring runs |
| `no_metric_selected` | run | performance selection is `[]` |
| `only_opt_in_metrics_shared` | run | default selection, only opt-in metrics shared |

Run-level entries carry the key `__run__` (`RUN_LEVEL_EXCLUSION_KEY`).

### `headline_details` provenance

Persisted with the values, never re-derived: `quality_metric_keys`, `excluded` (served as `quality_aggregation_excluded`, `{key, reason}`), `quality_contributions`, `quality_coverage`, `performance_metric_keys`, `performance_excluded` (served as `performance_aggregation_excluded`, `{key, reason, run_ids}`), `performance_contributions` (rewritten on every pass), `metric_entry_counts` (contributing keys of both halves). A completed run without `quality_metric_keys` is recovered by the orchestration adapter (`_recover_stranded_headlines`).

### Recompute triggers

Run completion (that run, then the performance pass), batch end, rescore (per run, then one performance pass when a run of the batch is completed), judge-only replay, run deletion and delete-all, import, `POST /evaluations/{id}/recalculate-headlines`, and an evaluation update changing `performance_tolerance`, `penalty_factor`, `family` or a headline selection. A callback-reconciled or swept run gets its own headlines without the performance pass; the next batch, rescore or recalculate writes it. Changing `sub_score_weights` recomputes nothing.

### Frontend N/A causes

`lib/evaluation-headlines.ts` (`naReason`, `aggregateNaReason`, `judgeNaNote`): a failed or cancelled run is "did not complete", a non-terminal one "still running". Judge Quality: judge off, every verdict failed (`judge_failure_count`), or nothing gradeable. Overall Quality: one generic cause. Overall Performance, most specific first: `comparison_run`, `nothing_selected`, `run_unmeasured`, `too_few_runs`, `only_opt_in_shared`, `no_shared_metric`; a payload without the disclosure reads the generic `no_shared_metric` wording. Aggregate surfaces (`aggregateNaReason`): Judge Quality reads "off" only when every run is judge-off; Overall Quality has one sentence; Overall Performance states `nothing_selected`, `only_opt_in_shared` or `too_few_runs` when any run's disclosure carries it, else `no_shared_metric`. `partialCoverage` shows coverage when `counted < entries`.

## 7. Prediction reuse

A reuse-on run copies an earlier answer produced from the same inputs instead of running the agent. Scores are never copied.

### Whether a run reuses

The run's `reuse` param decides, else `Evaluation.reuse_default` (`on` | `off`; the API default is `on`, the column's server default `off`). Params resolve `manual` > `evaluation` > `auto` (`services/evaluation/params.py`); the `auto` param is the evaluation's `reuse_default` when the run starts.

- Trigger and replay bodies take `reuse`: `inherit` (or absent) follows the evaluation, `on` / `off` is stored as a `manual` param, anything else is 422.
- Re-run full forces `off` (§8); a schedule takes the `auto` param.

Never reuses, whatever the setting: a callback run (`trigger_config.await_callback`) and a placebo comparison run (`dev-docs/agent-placebo-evaluation.md`).

### Fingerprint

Every result stores `input_fingerprint`, whether or not its run reuses, except a callback run's: the resume writes that result from the callback's answer, with no fingerprint, so it is never a reuse source. The fingerprint is a SHA-256 over canonical JSON (`compute_input_fingerprint`, `services/evaluation/fingerprint.py`) of:

- `agent_id`, `model_id`, `execution_mode` (default `full_loop`), the entry's input and its sorted file ids;
- the run's `persona_version_number`, `prompt_versions`, `agent_config_versions`;
- `full_loop` only: `kb_state` (each bound knowledge base's id, `revision`, `embedding_model`); `floored_max_iterations` when the chat floor raised the cap; `apply_prompt_guards` and `require_tool_approval` when on;
- a JSON producer only (`output_format == "json"`): the output schema;
- a placebo control arm only: `placebo` (`{arm, trial, placebo}`, the run's snapshot).

Not inputs: scoring settings, scorer selection, the expected answer, the run's `skill_versions`. `kb_state` is read once per `full_loop` run, whether or not it reuses, so a reuse-off run's results are reused by a later reuse-on run while the bound knowledge bases are unchanged.

### Candidates

`_find_reusable_prediction` (`services/evaluation_engine.py`), per entry:

- a result of another `completed` run of the evaluation, same fingerprint, `is_reused` false, no `execution_anomaly_kind`, not an LLM Only fallback (`execution_mode` `llm_only_fallback`);
- the 5 newest, each re-checked: the anomaly detectors re-run on its text, and `is_result_reusable` wants a non-empty output not starting `Error:` / `Context overflow:`, with `execution_details`, and, for `skip_llm`, no failed Prepare or Finalize step in its timeline (a result stored before such a turn was a platform error);
- the first that passes is used; none, the agent runs.

### What a reused entry copies and recomputes

| Copied from the source | Recomputed |
|---|---|
| `agent_output`, the agent-side timeline (judge steps dropped), `ttft_ms`, `execution_mode`, available and held tools, chain ledger, wall clock, agent duration, retry count | anomaly classification, judge, extraction and quality, performance metrics (from the copied telemetry), metric rows, `judge_seconds` |

The source's `trace_ids` only supply the judge's tool calls when the timeline is empty; the result stores `trace_ids: []`. It records `is_reused` and `reused_from_result_id`.

## 8. Re-run full, Replay, Re-score

### Re-run full and Replay

One route, `POST /evaluations/runs/{run_id}/replay` (`ReplayRunRequest {reuse?}`), and its bulk form `POST /evaluations/{id}/trigger-from-runs` (`{run_ids}`, at most `MAX_BULK_RUN_IDS` = 200, deduplicated). Each creates a new `pending` run (`trigger_reason: manual`) and leaves the source unchanged:

- same agent, `model_id`, `is_default_model`, persona, prompt, config and skill versions, and `pinned_dataset_versions`; `total_entries` counts the pinned versions;
- 409 when a version snapshot is gone (`resolve_run_snapshot_overrides`), announced by the run's `can_replay` / `replay_block_reason`;
- 422 for a placebo comparison run or a model no provider serves; the launch checks of §3 apply; bulk: 400 with no id, 404 for one outside the evaluation; 400 when the evaluation has no dataset entry.

The bulk route always runs with reuse `off`. The UI's **Re-run full** (a completed run, or a selection) sends `off`; **Replay** (a failed or cancelled run) sends none, so reuse follows the evaluation. The route itself accepts any status.

### Re-score

`POST /evaluations/runs/{run_id}/rescore` (strict empty body; a `scorer_selection` is 422) and `POST /evaluations/{id}/rescore-from-runs` (`{run_ids}`, ≤ 200). The agent does not run; the stored answers are scored again with the evaluation's current scoring settings, and the judge, when selected, calls its model again.

| Check | Single | Bulk (`results[].error`) |
|---|---|---|
| run unknown | 404 | `run_not_found` |
| placebo comparison run | 422 | `placebo_comparison_run` |
| `pending` / `running` | 409 | `run_in_progress` |
| no stored result | 409 | `no_predictions` |

Both also require a runnable family and dataset families, and AI enabled. Accepted runs return `status: "running"`.

Execution (`rescore_runs` payload, `rescore_run`):

- The endpoint locks the rows, stages the execution and sets each run `running` and owned in one transaction; `previous_statuses` holds each run's prior status.
- `rescore_run` refuses (`run_not_running`) a run not `running`; every check and write is scoped to its execution, so a superseded rescore never writes.
- Per result (`score_stored_result`): the anomaly is re-classified when none is stored; an entry with an anomaly other than confirmation-seeking is left as it is. Otherwise the judge runs (never for an entry whose agent run raised), then extraction and quality, then the metric rows. Quality rows no longer produced are deleted, `performance.*` rows kept.
- `scorer_selection` and `metric_suite_version` are restamped from what was applied.
- The run gets its previous status back whatever happens, unless a concurrent cancel ends it first, with the scores' commit on success. A failure goes to `rescore_error` (≤ 500 chars), never `error`; a success clears it. Timings stay the original run's.
- Judge and quality headlines are recomputed per run, Overall Performance once per batch for the evaluation.

### Judge-only replay

`POST /evaluations/runs/{run_id}/results/{result_id}/replay-judge` (`{judge_max_tokens}`, default 2048, 256–131,072; the fresh pass uses the evaluation's `judge_max_tokens`, default 8192, §5). Completed runs only (400). Re-judges one result with the fresh pass's judge inputs, replaces its judge fields, timeline step, `judge_seconds` and trace link, then recomputes the evaluation's headlines. Anomaly flags and quality metrics are untouched. It runs whatever the judge selection: on a judge-off run the score shows on the entry, not in Judge Quality.

## 9. Output extractors

### Definition

Turns an agent's run into the prediction the family's quality metrics score. One per (evaluation, agent): `evaluation_agents.output_extractor`, NULL = the raw answer; written through `agent_extractors` (`{agent_id: spec | null}`) on create and update, `output_extractor` on import. Strict at every level.

```
{ "source":     { "kind": "agent_output" | "tool_output" | "tool_input", "tool_name"?: str },
  "operations": [ { "op": str, "path"?: [str] } ],
  "output_kind": "object" | "array" | "label_set" }
```

| Source | Reads | `tool_name` |
|---|---|---|
| `agent_output` | the final answer | refused |
| `tool_output` / `tool_input` | that field of the **last** trace step with `type: "tool"`, this `tool_name` and a successful `outcome` (`success`, `succeeded`, `ok`, case-insensitive; absent is not success), in list order | required, non-blank, ≤ 128 chars |

Operations, a closed catalog (`services/evaluation/extraction/spec.py`, admission rules in its docstring):

| Op | Accepts → produces | `path` |
|---|---|---|
| `find_json` | text → text: the first fence holding JSON, else the outermost object or array | |
| `parse_json` | text → object or array | |
| `select_path` | object → what is at the path | required |
| `object_keys` / `object_values` | object → array | |
| `collect_keys` | array → array | required |
| `flatten` | array → array | |
| `dedupe_sort` | array → label_set | |

Save-time validation (`validate_extractor_chain`, 422): 1 to `extraction_max_operations` operations; `path` non-empty strings, at most `extraction_max_path_segments`; kinds fold from `text` to `output_kind`; the family allows it:

| Family | `output_kind` |
|---|---|
| `CLASSIFICATION` | `label_set` |
| `STRUCTURED_JSON` | `object`, `array` |
| other, or none | refused |

A family change orphaning a stored extractor is refused unless the same update replaces it.

### Limits

`core/config.py`, read on every call, positive ints: `extraction_max_operations` 8, `extraction_max_path_segments` 16, `extraction_max_source_bytes` 1 MiB, `extraction_max_sample_bytes` 256 KiB, `extraction_max_parse_depth` 32, `extraction_max_values` 2,000, `extraction_max_value_chars` 200.

### Pipeline

`quality_with_extraction` (`extraction/pipeline.py`), for a fresh run, a resumed callback and a rescore: re-check `output_kind` against the family; resolve the source; apply the operations; canonicalise (sorted-key compact JSON; a `label_set` de-duplicated and sorted by code point); call `run_quality`. The expected answer is never transformed. The judge reads the raw answer, not the extracted one.

### Failure

| Reason | When |
|---|---|
| `source_not_present` | no answer; no successful call to the tool; that call recorded no input / output |
| `payload_unparseable` | `find_json` found no object or array; `parse_json` met invalid JSON |
| `selection_empty` | a path matched nothing |
| `type_mismatch` | an operation met a kind it does not accept (a `parse_json` result that is not an object or array included); an unknown source or output kind (a configuration fault) |
| `limit_exceeded` | source size, nesting depth, value count, label length |
| `label_invalid` | a `label_set` member is not a non-empty string |
| `family_incompatible` | `output_kind` no longer fits the family |

`metric_results` then holds `extraction.failed` (`{status: "invalid", value: 0, error: {reason, message, agent_id, operation_index, operation, path, expected_kind, actual_kind}}`; `message` never quotes the payload; no metric row) and the family's metrics:

- the answer caused it (every reason but `family_incompatible` and a configuration fault): each metric scored 0 with reason `prediction_not_scorable`, counted as the expected answer would be;
- otherwise: each `unavailable` with reason `extraction_failed`, so the entry is left out.

The run detail returns `extraction_failure_count`.

### Success

`execution_details.extracted_prediction = {prediction, output_kind, item_count | null}`. A pass with no extractor, or whose extraction fails, removes it.

### Preview (admin; read-only, no audit)

| Route | Body |
|---|---|
| `POST /evaluations/{id}/extractors/preview` | `{agent_id, extractor, result_id | sample}`, exactly one of the last two (422); the agent must be linked and the result its own in this evaluation (404) |
| `POST /evaluations/extractors/preview` | `{family, extractor, sample}`, before the evaluation exists; unknown family 400 |
| `GET /evaluations/{id}/extractors/sample-results?agent_id&limit` | returns `[{result_id, created_at, description, position}]`, never the output; `limit` default 20, clamped to 1–100 |

`sample` is `{agent_output?, traces?}` (`traces`: stored trace steps, the only way to preview a tool source without a result); over `extraction_max_sample_bytes`, traces included, 422. The extractor is validated as a save. Response: `{status: "extracted", prediction, output_kind, item_count}` or `{status: "failed", prediction: null, output_kind, failure}` (the error fields above, without `agent_id`).

## 10. Stored results

### `execution_details`

Built by `_execution_record` (`services/evaluation_engine.py`); import stores it as packaged.

| Key | Content |
|---|---|
| `total_duration_ms` | Agent plus judge. |
| `agent_duration_ms` | Sum of the agent's timeline steps. |
| `agent_wall_clock_ms` | Timed around the agent call; null when not measured. |
| `judge_duration_ms` | Absent when the judge did not run. |
| `ttft_ms` | Time to first token of the agent's answer; null when not measured. |
| `execution_mode` | `full_loop`, `llm_only`, `skip_llm`. Older rows may hold `llm_only_fallback` (a Full Loop entry answered in LLM Only); the result serves `fell_back_to_llm_only`, also on a reuse of such an answer. |
| `traces` | The agent timeline, then one `judge` or `judge_skipped` step. |
| `trace_ids` | The agent's LLM trace ids; `[]` when reused. |
| `metric_results` | `{key: serialized result}` in the package's shape ([metric README](../../backend/app/evaluation_metrics/README.md)), plus platform keys `extraction.failed` and `stix.backend`. |
| `chain` | ChainLedger; Full Loop and `skip_llm` only. |
| `available_tools` | The tools the agent had; what rescore and replay give the judge. |
| `approval_required_tools` | Tools whose calls were held; present when non-empty. |
| `extracted_prediction` | `{prediction, output_kind, item_count}`, only after a successful extraction; removed when a rescore extracts none. |
| `agent_run_failure` | On an entry whose agent run raised: its failure cause, beside `metric_results` alone; a rescore keeps its treatment (§4). |

**Trace step**: `{type, description, duration_ms, outcome?, model?, usage?, error?, ...}`; `type` is `llm` / `model`, `tool`, `judge` (prompts, raw response, usage, stop reason, `truncated`) or `judge_skipped`. Performance never reads judge steps. A call's outcome is its `outcome` when that is `success`, `error`, `timeout` or `cancelled`, `error` when the step carries `error` or `is_error`, unknown otherwise. Run reads truncate `tool_input`, `tool_output`, `system_prompt`, `user_prompt` and `raw_response` to 30,000 characters; `GET /evaluations/runs/{run_id}/results/{result_id}/traces/{trace_index}/download` returns one step whole.

**ChainLedger** (`services/run_ledger.py`):

```
{ "calls": [{"kind": "model" | "tool", "name", "model"?, "duration_ms",
             "outcome": "success" | "error" | "timeout" | "cancelled", "usage"?}],
  "usage_by_model": [{"model": str | null, "usage": {...}}],
  "retries": int, "complete": bool, "incomplete_reason"?: str }
```

`incomplete_reason`: `wait_bound_reached` (a delegated task still running at the wait's bound), `run_cancelled`, `ledger_missing` (a ledger missing, partial or unreadable), `recorded_before_chain` (a reused source with no chain). Each delegated task keeps its `RunLedger` in `metadata_json["ledger"]`; the chain is `complete` only when every one is present and none is `partial`.

**Failed extraction**: `extraction.failed` and the family's metrics, as §9 (Failure) states; the run's extraction-failure count is the number of results carrying the key.

### `evaluation_metrics` rows

One row per `(result_id, key)` (unique), `run_id` denormalised (index `(run_id, key)`). Written by `log_result_metrics` (`services/evaluation/metrics.py`) as upserts.

| Key | Value | Written when |
|---|---|---|
| `judge_seconds` | `judge_duration_ms / 1000`, 3 decimals | The judge ran in this pass. |
| `run_duration_seconds` | `total_duration_ms / 1000`; `judge_duration_ms / 1000` for a reused or rescored result | The source duration exists. |
| A package key: family metrics and `performance.*` | `metric_results[key].value` | Status `scored` and a finite number. |

- No row for `unavailable`, `skipped` or `invalid` outcomes (`extraction.failed` included): an unmeasured metric has no row, never a 0.
- An undotted key in `metric_results` is refused, so an imported package cannot restate a platform key; an empty key, one over 100 characters or holding NUL is dropped.
- `numerator`, `denominator` (reals): pooled keys only (`is_pooled_key`), when non-negative, numerator ≤ positive denominator (0 of 0 for a quality metric), and their ratio is the value.
- `class_counts` (`{class: {tp, fp, fn}}`): macro keys only (`is_macro_key`), non-negative integers whose mean F1 is the value. Refused counts are logged and the row stores none.
- Retired keys (`total_seconds`, `ttft_seconds`, `cost_usd`, `schema_score`, ...) are never written; old rows stay unread by headlines.

**Rescore** (`score_stored_result`): `metric_results` gets new quality keys and keeps every `performance.*` key; the judge step and `judge_duration_ms` are replaced when the judge re-ran (`with_judge_pass`). Rows are written with `replace=True` and as reused (a resumed answer excepted), then the result's dotted, non-`performance.*` rows this pass did not produce are deleted. Performance and platform rows stay; a pass without the judge writes no `judge_seconds` and leaves the old row. Judge-only replay upserts `judge_seconds` alone (`log_judge_seconds`).

**Reuse** copies from the source: the agent timeline (judge steps dropped), `ttft_ms`, `execution_mode`, `available_tools`, `approval_required_tools`, `chain` (a Full Loop or `skip_llm` source without one gives `incomplete_reason: "recorded_before_chain"`), `agent_wall_clock_ms`, and `agent_duration_ms` (the larger of the carried steps and the recorded total). `trace_ids` is `[]` and only the judge trace is linked. Judge, quality and performance are recomputed; rows are written as reused (§7).

**Served**: `GET /evaluations/runs/{run_id}/results/{result_id}/metrics` returns each row with `half`, `unit`, `shown_on_entry` and `entry_label`, read from the registry by key (§14); an entry's page leaves out a metric not shown on an entry and names it by its entry name.

**Import** derives the rows from the packaged `execution_details`, one savepoint per run: a failed write costs that run its rows, never the import.

## 11. Export/import package

Writer `_build_evaluation_export_bundle`, reader `import_evaluation` (`api/v1/evaluations.py`).

### Layout

```
evaluation-<slug>.zip
├── evaluation.json                         definition, dependencies, run index
├── dependencies/agents/agent-<i>.json      agent export (ImportAgentRequest), in link order
├── dependencies/datasets/dataset-<i>.zip   dataset package ("filigran-dataset-v1"), in link position order
└── evaluation_runs/run-<i>.json            one per run, any status, in creation order
```

### `evaluation.json`

```
{ "version": 1, "export_type": "evaluation_package", "exported_at": iso8601,
  "evaluation": { name, <every EVALUATION_SETTINGS key>, enabled, last_triggered_at },
  "dependencies": { "agents":   [{ index, path, tags, is_baseline, output_extractor }],
                    "datasets": [{ index, position, path }] },
  "evaluation_runs": [{ index, results_path, agent_index, model_id, is_default_model, status,
                        trigger_reason, execution_mode, apply_prompt_guards, require_tool_approval,
                        persona_version_number, prompt_versions, agent_config_versions, skill_versions,
                        metric_suite_version, scorer_selection, score, speed_score, overall_score,
                        f1_score, duration_seconds, completed_entries, total_entries, error,
                        started_at, completed_at, created_at }] }
```

### `evaluation_runs/run-<i>.json`

```
{ "version": 1, "run_index": int,
  "results": [{ entry_ref: { dataset_index, entry_position } | null,
                agent_output, score, sub_scores, evaluator_reasoning, duration_seconds,
                speed_score, overall_score, f1_score, is_judge_failure, execution_anomaly_kind,
                is_reused, trace_ids, execution_details, created_at }] }
```

### Rules

- **Format version**: `version` is exactly `1`. A key is added without a bump and is optional on both sides; a bump would make every deployed server refuse every package. The embedded dataset package has its own version and reader (§2).
- **`entry_ref`**: a result names its entry by dataset index and position, never by id. A result whose entry was deleted or unlinked still counts toward its run's headlines and is exported with `entry_ref: null`.
- **Never exported**: `input_fingerprint`, `reused_from_result_id`, `version_entry_id`, `evaluation_metrics` rows, `quality_score`, `performance_score` and `headline_details` (an exported `score` is recomputed on import, §6), run parameters, run tags, `pinned_dataset_versions`, episodes, LLM trace bodies (only `trace_ids`), the resume secret, and a placebo comparison's runs (the package carries no arm).
- **Every setting**: `EVALUATION_SETTINGS` and `EVALUATION_AGENT_SETTINGS` (`services/evaluation/settings.py`) are the lists create, copy, export and import read; `tests/test_evaluation_settings.py` fails on a model column in no list.

### Import refusals

`POST /evaluations/import`, multipart `file`. The shape, settings and members are checked before anything is written; a dataset refusal or an unknown `entry_ref` comes later and rolls everything back. Either way nothing is created.

| Status | Cause |
|---|---|
| 413 | upload over 500 MB; members declaring over 2 GB uncompressed, or expanding past it; `evaluation.json` or an agent bundle over 32 MB, a results file over 256 MB uncompressed |
| 400 | not a ZIP; no `evaluation.json` (exact, `./`-stripped or shallowest suffix match, so a wrapping folder is accepted) or not JSON; `version` ≠ 1; `export_type` ≠ `evaluation_package` |
| 400 | a wrong type, range or column length in `evaluation.json` or a results file (the detail names file and field); two agents or datasets on one `index`, two datasets on one `position`; an embedded agent bundle missing or not an `ImportAgentRequest`; an embedded dataset missing; an `entry_ref` naming no entry of the package |
| as the dataset import | its refusals, prefixed with the embedded path (`dependencies/datasets/dataset-0.zip: …`), including 422 for a position tie on a position a result names |
| 422 | `execution_mode` other than `llm_only`/`full_loop`; a setting create refuses; an agent's settings or extractor invalid for the family |

### Legacy keys

| Key | Read as |
|---|---|
| `runs` | `evaluation_runs` |
| `speed_tolerance` | `performance_tolerance` |
| `quality_weight` | ignored |
| `results` inline on a run | the run's results file |

### What is created

- **One transaction**: datasets, agents (each through its own import code), evaluation, links, runs, results, audit and headlines, committed once. A failure after the checks rolls everything back and deletes the files the datasets stored.
- **Evaluation**: private to the importer, the package name suffixed `-<8 hex>` when taken (case-insensitive). A missing setting takes create's default; absent `execution_mode` is `full_loop`; only JSON `true` turns on `apply_prompt_guards` or `require_tool_approval`. Only the first agent marked baseline keeps it. `enabled` is not read: automatic runs arrive paused until an admin turns them on.
- **Placebo**: a `skill` placebo target names a skill of the exporting instance: it is dropped before the settings are checked and listed in the response's `warnings` and the audit's `placebo_targets_dropped`.
- **Runs**: skipped when naming no embedded agent. Status: absent → `completed`; `completed`, `failed`, `cancelled` kept; anything else, `pending` and `running` included, → `failed`. Defaults: `trigger_reason` `import`, `model_id` `unknown-model`. Version snapshots are copied verbatim, so replay is refused. `scorer_selection` kept; absent, the judge counts as on.
- **Results**: skipped without `score`; entry-less when `entry_ref` is `null`. `is_reused` only for JSON `true`; `trace_ids` ignored. Metric rows are derived from `execution_details` (`extract_metrics`) in one savepoint per run, then every completed run's headlines are computed as a completion computes them (§6, §10).
- **Audit**: one `import` entry: `agent_count`, `dataset_count`, `run_count` (runs in the package), `placebo_targets_dropped`.

## 12. Resume callback

Route `resume_evaluation_episode`; episode logic `resume_from_callback`, `process_resume_episode` (`services/evaluation_continuation.py`); secret `services/evaluation/resume_secret.py`. Design: `dev-docs/evaluations.md` (Callbacks).

### Enabling

Set through the API in `trigger_config`:

| Key | Meaning |
|---|---|
| `await_callback` | `true`: every entry pauses after the agent's first turn |
| `resume_secret` | the HMAC key. Write-only: stored Fernet-encrypted as `resume_secret_enc`, served as `"***"`, never exported. Omitted or `"***"` keeps it; `null` or `""` removes it; a JSON integer is stored as its digits; any other non-string or a vault reference is a 422 |
| `callback_timeout_seconds` | how long an entry waits; default the evaluation's `timeout_seconds` |

- One episode per entry, `correlation_id` `eval:<run_id>:<entry_id>`; while paused it is `waiting_callback` with a `callback_deadline`.
- Reuse is off in callback mode (§7).
- With `await_callback` and no secret (an imported evaluation has none), a trigger, replay or re-run is refused (400).

### Request

```
POST /api/v1/evaluations/resume
X-Signature: <lowercase hex HMAC-SHA256(key = resume_secret UTF-8, message = raw request body)>

{ "correlation_id":   str (1–120),
  "idempotency_key":  str (1–120),
  "callback_payload": { "final_output"?: str, "agent_output"?: str, "error"?: str } | null }
```

No JWT, no AI gate (the episode was already dispatched). The signature is compared in constant time. `callback_payload` is an untyped object: only the three keys above are read.

### Responses, in check order

| Status | When |
|---|---|
| 403 | `EVALUATION` is off |
| 422 | the body fails validation |
| 404 | no episode has this `correlation_id`, or its run is gone |
| 401 | the stored secret cannot be read (another `SECRET_KEY`, not a string, a vault reference) |
| 400 | no `resume_secret` is set |
| 401 | `X-Signature` missing or wrong |
| 410 | the episode was closed before any callback was applied: the payload is **not** applied and this is **not** a duplicate; the detail names the episode and its status |
| 200 `{accepted: true, duplicate: true, episode_id, status}` | the key equals the episode's stored `idempotency_key`; or the episode is `completed`; or it was closed after an earlier callback was applied. Nothing is enqueued |
| 200 `{accepted: true, duplicate: false, episode_id, status}` | accepted |

### Acceptance

- In one transaction: store `idempotency_key` and `callback_payload`, set the episode `resumed`, clear its deadline, and create one orchestration execution `{adapter: "evaluation", payload: {kind: "resume_episode", episode_id, idempotency_key}}` with deadline = (`timeout_seconds`, else 3600) + `orchestration_grace_seconds`; the worker is woken after the commit.
- `idempotency_key` is the duplicate key and a generation token: a different key on an episode still `resumed` is a new generation, accepted with its own execution, and an older execution's failure handling never closes a newer generation's episode.
- Only the latest accepted key counts as a duplicate: a validly signed replay of an earlier callback, while the episode is still `resumed`, is accepted again and overwrites the stored payload.

### Processing (`process_resume_episode`)

| Payload | Effect |
|---|---|
| `error` a non-blank string | episode `failed` with that error |
| `final_output` (else `agent_output`) a non-blank string | the result is written with that output and scored; episode `completed` |
| neither | payload kept, episode stays `resumed` with no deadline until a later callback |

- Store, judge and finalize run in three transactions, the judge holding no run lock; a newer accepted callback supersedes the answer being judged, and an episode a cancel closes meanwhile stays closed.
- Once every episode of a run is terminal, the run ends `failed` if any episode failed or timed out, else `completed`.

### Timeout sweep

`timeout_waiting_episodes`, on the adapter's periodic tick, closes overdue `waiting_callback` episodes as `timed_out` ("Callback deadline exceeded"), oldest deadline first, at most `orchestration_claim_batch` per tick. It skips a run another transaction holds, before counting the batch, so that run's episodes never take the place of another run's; they are taken on the first tick after the run is released.

## 13. Tags, baseline, run search, comparison tool

### Tags (`services/evaluation/tags.py`)

- Three levels: `Evaluation.tags`, `EvaluationAgent.tags` (scoped to the evaluation), `EvaluationRun.tags`. A run's effective tags merge at read time, run over agent over evaluation; never stored, so an evaluation or agent tag edit applies to existing runs.
- Every write path (create, update, import, the trigger's `tags`, agent tags `PUT` which replaces, run tags `PATCH` which merges and removes a key sent as `null`) runs `strip_reserved_keys`: `baseline` is dropped, values are stored as strings.

### Baseline

- Only `EvaluationAgent.is_baseline` makes an agent the baseline, set by `PUT /{id}/baseline` (`{agent_id}`, `null` clears). At most one per evaluation: the route clears the previous one first, and the partial unique index `uq_eval_agent_baseline` enforces it.
- A run reads as baseline when its agent is the baseline now (`is_baseline` resolved at read time), so every run of that agent does.

### Run parameters (`services/evaluation/params.py`)

- Each run records one `{key, value, source}` row per key. Sources: `auto` (`build_auto_params`: `model`, `execution_mode`, `quality_source` (always `judge` on a new run), `evaluator_model`, `reuse`, `dataset_version:<id>`, `kb_count`, `tool_count`, `persona_version`, `config_version:<name>`, `prompt_version:<id>`), `evaluation` (`custom_params`), `manual` (the trigger's `params` and `reuse`, a replay's `reuse`, and the `reuse: off` a re-run or placebo comparison forces).
- Precedence `manual` > `evaluation` > `auto`, enforced at upsert whatever the capture order. Values are text: strings verbatim, booleans `true`/`false`, others sorted-key JSON.
- `GET /runs/diff` takes two runs of any evaluations and lists each key as `changed` (one-sided keys included) or `identical`, with both values and sources.

### Run search (`services/evaluation/run_search.py`)

`POST /runs/search` (`RunSearchRequest`), over every evaluation the caller can see:

| Filter | Match |
|---|---|
| `evaluation_id` | one evaluation |
| `params` | exact `key: value` on the stored text |
| `metrics` | `[{key, agg, op, value}]`, `agg` `mean`/`p50`/`p95`/`min`/`max`/`count` over the run's metric rows |
| `scores` | `[{field, op, value}]`, `field` `quality_score` (default), `performance_score`, `score`, `f1_score`, or the retired `speed_score`, `overall_score` |
| `tags` | effective tags: a key with `null`, `""` or `true` must exist, else the value must equal (stringified); a `baseline` key means `baseline_only` |
| `baseline_only` | runs of the baseline agent |
| `dataset_version` | any pinned dataset at that version |
| `created_after`, `created_before` | ISO dates, inclusive; an unparseable date is ignored |

- `op` is `lt`, `lte`, `gt`, `gte` or `eq`. An unknown param, metric or tag key, aggregate or operator matches no run; an unknown score `field` is a 422.
- Newest first; `limit` 100 by default, 500 at most (422 above).
- Evaluation, score and date filters run in SQL; the others filter the newest `max(5 × limit, 100)` of those runs, so an older match can be missed.
- A row (`RunSearchResultItem`) carries headlines, own and effective tags, `is_baseline`, `anomaly_count`, `platform_error_count` and, for a placebo comparison's run, `arm`, `placebo_target` and `placebo_comparison_id` (`real` and null elsewhere), not parameters or metric aggregates (the run's own routes serve them).

### Comparison tool

`compare_evaluation_runs(baseline_run_id, candidate_run_id)` (`services/builtin_tools/evaluations.py`): like every evaluation agent tool, admins only and unregistered while `EVALUATION` is off.

- Takes two distinct completed runs of the same evaluation and agent, visible to the caller; anything else is an error string.
- For `quality_score`, `performance_score` and `score` (`COMPARED_METRICS`; the retired `f1_score`, `speed_score` and `overall_score` are not compared): delta = candidate − baseline, rounded to 2; above +2 points `progression`, below −2 `regression`, within ±2 inclusive `neutral` (`NOISE_MARGIN_POINTS` in `services/evaluation_comparison.py`). A missing value gives a `null` delta and verdict.
- Warnings when the runs differ in execution mode, model or scored entry count, or when one predates the headlines.

### Evaluation comparison (#12282)

`GET /compare?ids=a,b[&run_ids=…]` (`services/evaluation/cross_comparison.py`) compares 1 to 4 evaluations visible to the caller (404 otherwise); another family, a family-less evaluation or another count is a 422. One evaluation (`Compare models` on its page) compares its own runs: every entry is shared, the page pairs every two runs (default model first) instead of one model across evaluations, and leaves out the comparability and representativeness sections. Its page (`/evaluations/compare/:tab?`) has three tabs: `leaderboard` (default) and `results` read a second comparison whose `run_ids` are every `run_choices` run; `pairs` is the run-against-run view. Each run carries `trigger_reason`: runs of the same minute and trigger are one launch, as Activity groups them. Results pool a configuration (subject × model) over its runs: counts summed, interval resampling the runs then their entries (1,000 resamples, fixed seed; `frontend/src/lib/evaluation-leaderboard.ts`).

- **Runs**: per evaluation, the newest completed `real`-arm run per subject and model, or the runs `run_ids` names for that evaluation; `run_choices` lists its 50 newest completed runs for the page's picker. Each run carries its judge run score (`judge_score`, none when its judge was off), `completed_at`, and its setup (`setup`: subject, persona or flow version, prompt, skill and config versions, execution mode) with a fingerprint (`setup_key`): runs with one key are reruns of one setup, whatever the model.
- **Entries**: each result's scored entry (snapshot first, then live); two evaluations' entries are one when `reference_key(family, expected_answer)` (seam, §14) is equal: for STIX the canonical object ids and relationship triples, else the JSON value with sorted keys or the trimmed NFC text. Two entries of one evaluation with the same key stay two (`#n` suffix).
- **Cells**: per run and entry, each pooled quality metric's `[numerator, denominator]` from `evaluation_metrics` (results that count in headlines only, `counts_in_headlines`), its `[tp, fp, fn]` (`counts`, from the stored result's detail, for precision and recall), `unreadable` (the scorer could not read the answer: a scored quality metric's detail gives a `reason`, as every `unreadable_answer` does), `judge_score` (none when the judge was off, failed or the entry was left out), `performance.cost.llm`, and the result's `duration_seconds`. These readings have one owner, `services/evaluation/answer_facts.py`. The page adds the family's derived metrics (`withDerivedMetrics`: STIX `stix.bundle_f1`, the entity and relationship counts added up, so relationships weigh by their number). The page pools them (`frontend/src/lib/evaluation-compare.ts`: micro sum, 95% percentile bootstrap over entries with a fixed seed, paired bootstrap for one model in two evaluations, best–worst ranks), so a figure is the run's own on those entries.
- **Comparability**: per evaluation, its subjects (built-in tools, skill names, `max_iterations`, live), datasets with entry counts and an instructions digest, judge model, weights and an instructions digest, timeout, execution mode, and each run's `metric_suite_version`. The page states which dimension differs; it never shows judge scores when judges differ, and points at Re-score when suite versions differ.

### Bundle review (#12282)

`GET /bundle-review?result_ids=a,b` (`services/evaluation/bundle_review.py`): 1 to 32 results of `STIX_BUNDLE` evaluations visible to the caller, all scored against the same expected bundle (by `reference_key`), else 422; an unknown result is a 404. `POST /bundle-reviews` (`{groups, include_text}`) reads up to 200 entries at once, one group of result ids per entry, without the report text and the judge's reasoning unless `include_text`: what the run review reads over every entry of a run (its figures, where its entity points go, each object's history across runs).

- Each answer carries its metric values, `counts` (`[tp, fp, fn]`), `unreadable`, `judge_score` and `judge_reasoning`, `cost` and `seconds`; the entry its reference `key` (as `/compare` names it), `position`, the dataset's `entry_count`, `dataset_name` and `dataset_description`.
- The run review (`/evaluations/review?run=<run>[&against=expected|none|<run>][&entry=<key>][&view=…]`, linked from a run's page, its entries and the Leaderboard) shows one run over all its entries against the expected bundle, nothing or another run of the evaluation: run figures pooled as its headlines, where its entity points go (near-miss kinds, missing, spurious, duplicates), the entries with their figure and change, then the entry's five views. `?results=<id>,…` keeps reviewing given answers of one entry.

- Built on `canonical_stix_bundle` (seam), the scorer's own reading: objects of the entity selection with canonical ids, relationships and sightings as canonical triples. An answer the scorer cannot read is listed in `unreadable`.
- Objects are grouped: one group per expected object plus the objects that name the same thing (name or alias key, ATT&CK id, defanged value, a type of the same family: actor or software), then the rest; past 2,500 distinct objects only exact matches line up. Presentation only: no score changes.
- Relationship rows: one per expected triple; another type on the same group pair joins it as a wrong type; each source keeps its real endpoints, so the page compares triples as the scorer does.
- `entry.pages`: the entry's prompt, else its first attached file's text layer (PDF: one string per page, parsed off the event loop), cut at 400,000 characters.

## 14. The metric seam (platform side)

The package's own contract (result shape, metrics, roll-ups, adding one) is `backend/app/evaluation_metrics/README.md`.

### One importer

`services/evaluation/metric_package/` is the only importer of `app.evaluation_metrics`: ruff `TID251` (`backend/pyproject.toml`) and `tests/test_evaluation_guards.py`. The rest of the platform imports the seam's modules, never `app.evaluation_metrics`; no package type appears in an engine, API or serializer signature.

### What crosses

- `MetricOutcome` (`results.py`): `key`, `status` (`MetricStatus`, the package's four values), `value`, `detail`, `serialized`. Only `key` and `serialized` leave the adapters: `to_persistable` gives `{key: serialized}`, stored verbatim in `metric_results`. Direction comes from the registry, never an outcome.
- `build_quality_config(...)` → opaque `QualityConfig`, once per run or rescore; raises `QualityConfigError`.
- `run_quality(family, reference, prediction, output_schema, config)` → `list[MetricOutcome]`; a package `ConfigurationError` becomes `QualityConfigError`, never a score.
- PROSE is scored under `PROSE_NORMALIZATION_POLICY` (`quality_adapter.py`): NFC, `casefold`, `edges`, `collapse`.
- `run_performance(execution_details, token_usage, retry_count, price_rows, config, chain=None)` → `list[MetricOutcome]`; never raises on bad telemetry. A `ChainInputs` (the entry's chain ledger) replaces traces, usage and prices.
- `reference_key(family, reference)` and `canonical_stix_bundle(raw, *, reference)` (`reference_view.py`): how the scorer reads an expected answer or an answer, for the comparison and the bundle review (§13); `None` when it cannot read it or the STIX backend is unavailable.

### Registry and views

`registry.py` walks the package metadata once per process; a quality metric's family is the sub-package declaring it (`FAMILY_MODULES`). Readers ask a view, never parse a key.

| View | Rule |
|---|---|
| Directions (`directions.py`) | only metrics with one valid direction and one roll-up across declarations; others compose no headline |
| Catalogue (`metric_catalog(family)`) | `[{key, label, kind, direction | null, default_contributor, unit}]`; an unoriented quality metric stays, `direction: null`; defaults: `DEFAULT_QUALITY_KEYS`, performance minus `NON_DEFAULT_PERFORMANCE_KEYS` |
| Units (`units.py`) | performance only; two declared units drop it |
| Roll-ups (`rollups.py`) | `is_pooled_key`, `is_macro_key` |
| Hidden | `HIDDEN_QUALITY_KEYS` (the two phrase metrics) are in no view and dropped by `run_quality`, never stored |
| Entry view (`entry_view.py`) | `metric_entry_view(key)` → `EntryView(shown, label)`, from the metadata's `shown_on_entry` and `entry_label`: whether one entry's value is shown on its own and the name it takes there; declarations that disagree show it under its own name, with a warning. Only the three classification F1s declare one |

### Fail-closed

`guard_package_call` (`availability.py`) turns `ImportError`, `AttributeError` and metadata nested past 8 levels into `MetricPackageUnavailableError`, naming the operation. Callers let it propagate: no partial or improvised metric set is substituted. Availability is judged by the call surface, never the declared version.

### Platform-made results

- `stix.backend` unavailable (backend cannot be built); `json.field_accuracy` unavailable (no answer schema);
- `performance.cost.llm` unavailable `pricing_incomplete: …`; chain totals rewritten to `chain_incomplete: <reason>`;
- `unscorable_outcomes`: the family's catalogue metrics `unavailable` with a reason (`extraction_failed`, `configuration_error`, `scoring_error`, `platform_error`);
- `failed_answer_outcomes`: the scorable metrics 0 with a reason (`prediction_not_scorable`, `agent_run_failed`);
- `extraction.failed` (§9).

### Suite version

`metric_suite_version()` returns the suite's `__version__`, or `None` (not fail-closed). It is stamped on `evaluation_runs.metric_suite_version` when a run starts scoring, and restamped by every successful rescore; `null` reads as unknown. The bump rule is the README's.
