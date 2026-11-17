# Contract: Metric package boundary

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

The seam between the platform and the vendored metric suite: what crosses it, how the platform calls it, and what happens when it is missing.

## Encapsulation rule

| Side | Path | Rule |
|---|---|---|
| Suite | `backend/app/evaluation_metrics/` (`core/`, `quality/`, `performance/`) | Pure: no I/O, no clock, imports no platform code (`backend/tests/test_metric_suite_contracts.py`) |
| Adapter | `backend/app/services/evaluation/metric_package/` | The only importer of `app.evaluation_metrics`: ruff `TID251` (`backend/pyproject.toml`) and `backend/tests/test_evaluation_guards.py` |
| Platform | everything else | Imports only `metric_package/__init__.py` `__all__`; package types (`Family`, `ScoreResult`, `PerformanceResult`, telemetry, `PriceTable`) never appear in an engine, API or serializer signature |

## Result shape

Every metric of every entry yields one result. Its serialized form is stored verbatim in `execution_details.metric_results[key]` ([stored-results.md](stored-results.md)):

```
{ "status":   "scored" | "invalid" | "skipped" | "unavailable",
  "value":    float | null,
  "detail":   { ... },                       // metric-specific, see below
  "error":    { "code", "message", "locations": [str], "details": {} } | null,
  "metadata": { "key", "label", "kind", "primary_measure",
                "direction": "higher_is_better" | "lower_is_better",
                "rollup":    "mean" | "pooled" | "macro",
                "unit"?:     "milliseconds" | "count" | "tokens" | "currency" | "ratio" } }  // performance only
```

| Status | Quality `value` | Performance `value` | `error` | FR |
|---|---|---|---|---|
| `scored` | 0–100 | finite | none | FR-049, FR-064 |
| `invalid` | exactly 0 | null | required | FR-049, FR-064 |
| `skipped` | null | null | none | FR-049 |
| `unavailable` | null | null, `detail.reason` required | quality may carry one; performance none | FR-049, FR-064 |

- **Pooled**: a scored result whose `rollup` is `pooled` carries `detail.numerator` and `detail.denominator`, non-negative reals with `0 ≤ numerator ≤ denominator`, `denominator > 0` (or 0 of 0 at 100 for a quality score's empty answer), and `value = scale × numerator / denominator` (scale 100 for quality, 1 for performance). The suite refuses to build any other (FR-078).
- **Macro**: a scored result whose `rollup` is `macro` carries `detail.classes`, `{class: {tp, fp, fn}}` with non-negative int counts, for the classes the entry saw on either side; extra keys per class (`f1`) are allowed. The suite refuses to build one without it (FR-078).
- **Ill-formed answer**: scored 0 with `detail.reason`, `detail.error_code` and `detail.error_message` (FR-050), plus `detail.hint` when a JSON or STIX answer wraps valid JSON in a fence or text. It is never `invalid`. A pooled or macro result counts as the expected answer would (`quality.unreadable_answer`): 0 of the expected answer's denominator (1 when it is 0), its classes all missed.
- **Malformed telemetry**: all nine performance results `invalid` with `error.code = "malformed_telemetry"` (FR-064).
- **Detail**: P/R/F1 metrics carry `precision`, `recall`, `f1`, `tp`, `fp` and `fn`, with `classes` (each with its `f1`) for `stix.entity_prf.by_type` and `classes` for `classification.macro_prf`. `json.field_accuracy` carries `missing`, `extra`, `incorrect`, `correct`, `excluded`, `exact_object_match` and `per_field[{pointer, mode, score, weight, status}]` (FR-052). Cost carries its price basis.
- **Keys**: every package key is dotted (`json.`, `classification.`, `stix.`, `prose.`, `performance.`). The key sets are FR-051 and FR-063. The keys in `registry.HIDDEN_QUALITY_KEYS` (the two phrase metrics) are in no view and never stored.
- **Platform-made results** are absent from the registry:
  - `stix.backend`: unavailable, the STIX backend could not be built.
  - `json.field_accuracy`: unavailable, no answer schema (US4 scenario 3).
  - `performance.cost.llm`: unavailable, reason `pricing_incomplete: …`.
  - `extraction.failed`: see [extractor.md](extractor.md).

  The first three are `{status: "unavailable", value: null, detail: {reason}, metadata: {key}}`. `extraction.failed` is `{status: "invalid", value: 0, error}` with no metadata.

## What crosses the seam

`MetricOutcome` (`metric_package/results.py`) has these fields:

| Field | Type | Use |
|---|---|---|
| `key` | str | the metric key |
| `status` | `MetricStatus` (the four values above) | the engine branches only on this, `value` and `direction` |
| `value` | float \| None | |
| `detail` | str \| None | human text: `error.message`, else `detail.error_message`, else `detail.reason` |
| `serialized` | dict | the result shape above; persisted verbatim, never interpreted |
| `direction` | str \| None | `None` when the package states none |

`to_persistable(outcomes)` returns `{key: serialized}`.

## Calls

| Call | When | Returns | Rules |
|---|---|---|---|
| `build_quality_config(family, evaluation, dataset_refs, declared_vocabulary)` | once per run or rescore | `QualityConfig` (opaque) | CLASSIFICATION takes the declared vocabulary, else the labels of the references (FR-053). STIX builds the canonicaliser and validator. PROSE uses the fixed normalisation (FR-056). STRUCTURED_JSON reads field modes and weights from `field_scoring_overrides` (FR-055). Raises `QualityConfigError`. |
| `run_quality(family, reference, prediction, output_schema, config)` | per entry | `list[MetricOutcome]` | `prediction` is the extracted answer (FR-045). `output_schema` is the stored wrapper `{"type": "json_schema", "definition": {...}}`. A package `ConfigurationError` becomes `QualityConfigError`, never a score. Results of `HIDDEN_QUALITY_KEYS` are dropped. |
| `run_performance(execution_details, token_usage, retry_count, price_rows, config, chain=None)` | per entry | `list[MetricOutcome]` (nine) | Never raises on bad telemetry. When `chain` is given it replaces traces, usage and prices (FR-068). An incomplete chain rewrites the not-captured reasons to `chain_incomplete: <reason>` (FR-069). |

- `PerformanceConfig` is `{token_convention: "separate", currency: "USD"}`.
- `ChainInputs` is `{calls, token_usage_by_model: [(model, usage)], price_rows_by_model: {model: [row]}, complete, incomplete_reason}`.
- A price row is `{input_price_per_1m, output_price_per_1m, cache_read_price_per_1m, cache_write_price_per_1m, currency, resolved_model, resolved_provider}`.

Write-time helpers:

- `resolve_declared_vocabulary`
- `validate_field_scoring_overrides` and `normalize_field_scoring_overrides` (FR-055)
- `list_families` and `family_is_valid`: every declared family computes package metrics, so a known family is runnable with the judge on or off

## Registry views

`metric_package/registry.py` walks the package metadata once (one cache, one lock) and builds every view from that walk. A quality metric's family is the sub-package that declares it (`FAMILY_MODULES`), never its key prefix.

| View | Functions | Rule |
|---|---|---|
| Directions | `metric_direction`, `quality_directions`, `performance_directions`, `is_quality_key`, `is_performance_key` | Drops a metric whose direction or rollup is missing or declared inconsistently, so it composes no headline (FR-079) |
| Units | `metric_unit`, `performance_units` | Performance keys only; a metric declared with two units is dropped |
| Rollup | `is_pooled_key`, `is_macro_key` | `rollup == "pooled"`, `rollup == "macro"` |
| Catalogue | `metric_catalog(family)` = `quality_catalog(family)` + `performance_catalog()` → `[{key, label, kind, direction \| null, default_contributor, unit}]` | Keeps an unoriented metric with `direction: null`. `default_contributor` is true for `DEFAULT_QUALITY_KEYS` and false for `NON_DEFAULT_PERFORMANCE_KEYS` in `catalog.py` (FR-084). |

Every reader that needs a metric's half, unit, direction or rollup asks a view. None parses a key prefix.

## Fail-closed availability (FR-062)

- `guard_package_call(operation, call)` (`availability.py`) turns `ImportError`, `AttributeError` and metadata nested deeper than 8 levels into `MetricPackageUnavailableError`. The message names `app.evaluation_metrics` and the operation.
- Callers let the error propagate. A partial metric set is never substituted.
- Availability is judged by the observed call surface, never by the declared version.

## Suite version (FR-061)

- `__version__` in `backend/app/evaluation_metrics/__init__.py` is the only declaration (`12.1.0`).
- Bump it whenever a change alters what a metric computes, including a change to a platform input. Do not bump it for docs, tests or typing.
- `metric_suite_version()` returns that value, or `None` when it cannot be read. This is deliberately not fail-closed.
- It is stamped on `evaluation_runs.metric_suite_version` when a run starts executing. The run page shows `null` as "unknown".
- Rescore does not restamp it (see the spec's [Known gaps](../spec.md#known-gaps)).
