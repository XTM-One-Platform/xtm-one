# Contract: Evaluation export package

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

The ZIP that `GET /evaluations/{id}/export` writes and `POST /evaluations/import` reads (FR-112 to FR-117). The writer is `_build_evaluation_export_bundle` and the reader is `import_evaluation`, both in `backend/app/api/v1/evaluations.py`.

## Layout

```
evaluation-<slug>.zip
├── evaluation.json                         definition, dependencies, run index
├── dependencies/agents/agent-<i>.json      agent export format (ImportAgentRequest), in link order
├── dependencies/datasets/dataset-<i>.zip   dataset package (manifest format "filigran-dataset-v1"), in link position order
└── evaluation_runs/run-<i>.json            one per run, any status, in creation order
```

## `evaluation.json`

```
{ "version": 1,
  "export_type": "evaluation_package",
  "exported_at": iso8601,
  "evaluation": { name, <every setting of services/evaluation/settings.py EVALUATION_SETTINGS>,
                  enabled, last_triggered_at },
  "dependencies": { "agents":   [{ index, path, tags, is_baseline, output_extractor }],
                    "datasets": [{ index, position, path }] },
  "evaluation_runs": [{ index, results_path, agent_index, model_id, is_default_model, status, trigger_reason,
                        execution_mode, apply_prompt_guards, require_tool_approval,
                        persona_version_number, prompt_versions, agent_config_versions, skill_versions,
                        metric_suite_version, scorer_selection, score, speed_score, overall_score, f1_score,
                        duration_seconds, completed_entries, total_entries, error,
                        started_at, completed_at, created_at }] }
```

## `evaluation_runs/run-<i>.json`

```
{ "version": 1, "run_index": int,
  "results": [{ entry_ref: { dataset_index, entry_position } | null,
                agent_output, score, sub_scores, evaluator_reasoning, duration_seconds,
                speed_score, overall_score, f1_score,
                is_judge_failure, execution_anomaly_kind, is_reused,
                trace_ids, execution_details, created_at }] }
```

## Rules

- **Format version**:
  - `version` is pinned to exactly `1`. A new key is added without a bump, and readers on both sides treat it as optional.
  - A bump would make every deployed server refuse every package this one writes.
  - The dataset package has its own version (2) and its own reader, which accepts a newer number.
- **Entry references**: a result points to its entry by dataset index and position (`entry_ref`), never by id. A result whose entry was deleted or is no longer in a linked dataset still counts toward its run's headlines, so it is exported with `entry_ref: null` and imported without an entry. A position is unique in its dataset (FR-112), enforced by a unique index; the ties that appends left earlier are renumbered by the migration `e1780evp001`, which builds the index. An embedded dataset whose entries share a position a result names is refused (422): which entry that result answered is lost, so the dataset is exported again from an up-to-date platform. Any other tie loses nothing: the entries are renumbered 0..n-1 in manifest order, and each result still finds its entry by the position the package gives it. A standalone dataset import renumbers the same way.
- **Never exported**: `input_fingerprint`, `reused_from_result_id` (FR-113), `version_entry_id`, `evaluation_metrics` rows, a run's headlines, run params, episodes, the bodies of LLM traces and the resume secret. The metrics and the headlines are derived again on import.
- **Every setting**: the definition carries every setting of `EVALUATION_SETTINGS` and each agent's settings of `EVALUATION_AGENT_SETTINGS`; a test fails when a model column is in neither list. The resume secret is left out of `trigger_config`.
- **The resume secret** is never exported, and an import drops one an older package carries: the imported evaluation takes no callback until it is given a new secret, and in callback mode it cannot be run until then (400).

## Import

`POST /evaluations/import`, multipart field `file`.

### Refused

Nothing is created when the import is refused. The import is one transaction: the whole package is read and checked before anything is written, and a failure after that, a cancellation included, rolls back everything, embedded agents and datasets included, and deletes the files the datasets stored. The import commits once, at its end.

| Status | Cause |
|---|---|
| 413 | the upload exceeds 500 MB, or its members declare more than 2 GB uncompressed: the import holds the package whole while it checks it |
| 400 | not a ZIP |
| 400 | no `evaluation.json`. It is matched exactly, `./`-stripped, or by the shallowest suffix, so a wrapping folder is accepted. |
| 400 | `evaluation.json` is not JSON |
| 400 | `version` ≠ 1 ("Unsupported export version") |
| 400 | `export_type` ≠ `evaluation_package` |
| 400 | a value of the wrong type or range, or too long for its column, in `evaluation.json` or a run's results file; a results file that is not a JSON object; two agents or two datasets on one `index`, or two datasets on one `position`. The detail names the file and the field: `Invalid evaluation.json: evaluation_runs.0.score: Input should be a finite number`. |
| 400 | an embedded agent bundle is missing, is not JSON, or does not match `ImportAgentRequest` |
| 400 | an embedded dataset bundle is missing |
| 413 and other dataset-import errors | passed through from the dataset import, prefixed with the embedded dataset's path (`dependencies/datasets/dataset-0.zip: ...`): format other than `filigran-dataset-v1`, ZIP over 500 MB or whose members declare more than 2 GB uncompressed, a tie on a position a result names |
| 422 | `evaluation.execution_mode` other than `llm_only` or `full_loop`. This is checked before anything is created. |

### Accepted legacy keys

| Legacy key | Read as |
|---|---|
| `runs` | `evaluation_runs` |
| `speed_tolerance` | `performance_tolerance` |
| `quality_weight` | ignored: retired, no setting |
| `results` inline on a run | the run's results file |

### What is created (FR-115)

- **Evaluation**:
  - Private to the importer, named after the package. The name gets a `-<8 hex>` suffix when it is taken (case-insensitive, across all evaluations).
  - Every setting is read, a missing key taking create's default (an older package names fewer), and refused as create refuses it (family, judge selection, field overrides, extractors) before anything is imported. Only the first agent marked baseline is.
  - `enabled` is not read: automatic runs arrive paused, so the source's schedule never fires here until an admin turns it on.
  - Agents and datasets are imported as new objects, by the code of their own imports, in the import's transaction, and re-linked in bundle order.
- **Run**:
  - A run names its agent by the agent's `index` in `dependencies.agents`; a run naming no embedded agent is skipped.
  - Status: absent → `completed`; `completed`, `failed` or `cancelled` kept; anything else → `failed`.
  - Defaults: `trigger_reason` `"import"`, `model_id` `"unknown-model"`.
  - Version snapshots are copied verbatim. They name the source instance's versions, so replay is refused (FR-117).
  - `scorer_selection` is kept: the judge headline is computed from it. Absent (an older package), the judge counts as on.
  - Headlines: once every result and metric row is written, the headlines of every completed run are computed as a run's completion computes them (FR-116), so an imported run shows the headlines of its source.
- **Result**:
  - Skipped when it has no `score`. Imported without an entry when its `entry_ref` is `null` or does not resolve, so it counts toward its run's headlines as on the source.
  - `is_reused` is true only for JSON `true`. `trace_ids` are ignored.
  - Its `evaluation_metrics` rows are derived from `execution_details` by `extract_metrics`, under the rules of [stored-results.md](stored-results.md). That derivation runs in one savepoint per run.
- **Audit**: one `import` entry with the agent, dataset and run counts.
