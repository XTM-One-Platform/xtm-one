# Contract: Headlines

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

The three run headlines as stored and served: the run columns, the `headline_details` provenance JSON, the exclusion vocabulary and the API fields that expose them.

## Writer

`_compute_run_headlines` in `backend/app/services/evaluation_engine.py` is the only writer. It works in two passes over stored values, through `backend/app/services/evaluation/aggregation.py`:

1. **Quality**: per run, from that run's `evaluation_metrics` rows.
2. **Performance**: one pass over every completed run of the evaluation, under a per-evaluation advisory lock.

Both passes read only counted entries (FR-075). Recompute triggers are FR-089 and FR-090.

## Run columns (`evaluation_runs`)

| Column | Headline | NULL means | FR |
|---|---|---|---|
| `score` | Judge Quality | the run's `scorer_selection` snapshot had the judge off, or no entry was counted | FR-076 |
| `quality_score` | Overall Quality | no metric contributed | FR-080 |
| `performance_score` | Overall Performance (comparative) | no comparable data (cause in `performance_excluded`) | FR-081–FR-083 |
| `headline_details` | provenance JSON (below) | never computed; FR-090 recovery selects these runs | FR-085 |
| `speed_score`, `overall_score`, `f1_score` | retired; never written for new runs; kept on old runs | | FR-092 |

## `headline_details`

```
{
  "quality_metric_keys":       [str],
  "quality_contributions":     {key: float},   // 0–100 value that entered the mean: flipped if lower-is-better, spread penalty applied to an averaged metric
  "excluded":                  [{"key": str, "reason": str}],                     // quality half, no run_ids
  "performance_metric_keys":   [str],
  "performance_contributions": {key: float},   // normalised 0–100 across the evaluation's runs
  "performance_excluded":      [{"key": str, "reason": str, "run_ids": [str]}],
  "metric_entry_counts":       {key: int}      // counted entries per contributing metric, both halves
}
```

- Each pass merges its own keys into the dict and leaves the other half's keys alone.
- The performance keys are rewritten wholesale on every recompute, because they only mean something relative to the current run set.
- `metric_entry_counts` holds the counts of the metrics composing each headline as it stands: each pass replaces its own half's counts and keeps the other half's, read from that half's `*_metric_keys`, so a metric that stops contributing loses its count.
- A contribution is not the raw per-metric mean the run metrics panel shows (FR-091). The two differ by design.
- `key` is a metric key or the run-level sentinel `"__run__"` (`aggregation.RUN_LEVEL_EXCLUSION_KEY`). It cannot collide with a metric key because package keys are dotted. A consumer that maps exclusions onto metrics filters it out.

## Exclusion reasons (FR-086, FR-087)

| `reason` | `key` | Half | Emitted when | Remedy |
|---|---|---|---|---|
| `not_applicable` | metric | both | selected metric whose offending runs report `unavailable` or `skipped`, or which lies outside the family's catalogue | none, a re-run cannot help |
| `failed` | metric | both | selected metric that some offending run reports `invalid`, or that is absent from `metric_results` | re-run |
| `not_a_default_contributor` | metric | both | inherited selection only: a non-default metric that produced a value | select it |
| `missing_from_other_runs` | metric | performance | this run measured it; the runs in `run_ids` lack it | |
| `no_performance_data` | `__run__` | performance | `run_ids` = runs that measured no performance metric (they abstain) | |
| `too_few_comparable_runs` | `__run__` | performance | `run_ids` = the eligible runs, fewer than two | |

- Statuses come only from `execution_details.metric_results` of counted entries. A key `scored` on any counted entry counts as scored. Offenders are the runs whose status is anything other than `scored`; `run_ids` lists them sorted.
- Each run's `performance_excluded` is rewritten from that run's point of view. A metric it measured that others lack becomes `missing_from_other_runs`. A run that measured nothing lists only the run-level entries.
- An explicit empty selection lists nothing (FR-084).
- The vocabulary is open: a consumer renders an unknown reason neutrally, never as the nearest known one.

Selections live in `evaluations.overall_quality_metrics` and `evaluations.overall_performance_metrics` (JSON): NULL means inherit, `[]` means none, a list is an explicit selection (FR-084).

## API exposure

`EvaluationRunResponse` (`backend/app/api/v1/schemas/evaluation.py`) serves every run row (list, detail, trigger results):

| Field | Source |
|---|---|
| `score`, `quality_score`, `performance_score` | columns, NULL kept as `null`, never 0 (FR-074) |
| `quality_metric_keys`, `performance_metric_keys`, `metric_entry_counts`, `quality_contributions`, `performance_contributions` | `headline_details`, never recomputed on read |
| `quality_aggregation_excluded` | `headline_details.excluded` |
| `performance_aggregation_excluded` | `headline_details.performance_excluded` |
| `speed_score`, `overall_score`, `f1_score` | retired columns, passed through (FR-092) |
| `avg_sub_scores`, `judge_failure_count`, `anomaly_count`, `metric_suite_version` | run reads |

`GET /evaluations/runs/{run_id}` also returns `rescore_pending` and `callback_pending`. When neither is pending, it reads the affected headline fields fresh.

The frontend reads the headlines through one resolver, `frontend/src/lib/evaluation-headlines.ts`:

- The N/A cause (FR-088): `performanceNaCause(run)` returns `run_unmeasured`, `too_few_runs`, `no_shared_metric` or `undisclosed`.
- The legacy display (FR-092).
