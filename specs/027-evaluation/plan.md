# Implementation Plan: Evaluation

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

**Branch**: `1780-featevaluation-evaluation-pipeline-v1-on-xtm-one-without-mlflow` | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

**Input**: [spec.md](spec.md), the evaluation vertical as it behaves today.

## Summary

Administrators score agents against versioned ground-truth datasets (US1–US9). A launch is one orchestration execution that runs each agent on each model over the pinned dataset versions, through the agent's own loop (Full Loop) or one model call (LLM Only). Each entry is scored by a vendored, I/O-free metric suite reached through one adapter package and by an optional LLM judge; stored metric values are rolled up into three separate headlines, recomputed without running any agent. Decisions: [research.md](research.md); tables and columns: [data-model.md](data-model.md).

## Technical Context

**Language/Version**: Python 3.14 (backend), TypeScript strict / React 19 (frontend)

**Primary Dependencies**: FastAPI, SQLAlchemy 2.0 async, SAQ (orchestration worker), `jsonschema` (answer schemas), `stix2` + `pycti` (STIX family), TanStack Query

**Storage**: PostgreSQL 17 (+ pgvector for the full-schema tests), MinIO/S3 for entry files

**Testing**: pytest (`asyncio_mode=auto`, `integration` marker for real PostgreSQL), `node --test` + vitest, Playwright

**Target Platform**: Linux server, multi-replica backend and workers

**Project Type**: web service + web app (admin-only vertical)

**Performance Goals**: up to 5 runs in parallel per launch (FR-033); headline changes cost 0 agent calls (SC-002)

**Constraints**: launch deadline = timeout + 120 s, 3 attempts, stranded sweep after 600 s (FR-040); packages ≤ 500 MB, Hugging Face ≤ 500,000 rows (FR-012); bulk actions ≤ 200 runs; run search ≤ 500 results (FR-109)

**Scale/Scope**: 4 families, 19 quality metric keys (FR-051), 9 performance metrics (FR-063), 3 headlines; 139 backend test files

## Constitution Check

| Principle | Result | Why |
|---|---|---|
| I Assignment-Driven | Exception | A launch is an orchestration execution, not an Assignment; see Complexity Tracking. On-agent-change creates no run |
| II Fail-Closed | Pass | Missing or drifted metric package refuses scoring (FR-062); unknown direction or roll-up composes no headline (FR-079); a dead worker's run is failed, never re-run (FR-039); agentic gate on create and launch (FR-015) |
| III Security by Default | Pass | The callback secret is Fernet-encrypted, served masked and never exported. Prompt guards off by default is deliberate and warned (FR-022) |
| IV Concurrency-Safe | Pass | One owning execution per run (`run_ownership.py`), headline recompute under `pg_advisory_xact_lock`, stranded sweep `ORDER BY … SKIP LOCKED`; deleting does not stop work in flight |
| V Migrations | Pass | Single head; the rescore index is built concurrently (pinned); see Migrations |
| VI Quality Gates | Pass | 2,784 collected backend items (505 integration); Hugging Face import routes and the evaluation pages have no test |
| VII Rationale-Documented | Pass | Lease, lock, fail-closed and legacy-shape paths carry why-comments (`framing.py`, `run_ownership.py`, engine headline writer) |
| VIII Shared Utilities | Pass | `user_visible_query`, `require_admin_for_company_managed`; one headline writer (backend), one headline resolver (frontend) |
| IX TanStack Query | Pass | `useEvaluationApi.ts`, `useDatasetApi.ts`; poll tiers from `held.ts` |
| X Design System | Pass | `Switch`, `Tip`, chart legends from `masterColorMap`; untranslated text is a known gap |
| XI Minimal Writing | Pass | This folder: rules cited by FR id |
| XII No Workarounds | Exception | FR-036 flags anomalies by reading the answer text with regexes and keywords (`framing.py`); see Complexity Tracking |

## Project Structure

### Documentation (this feature)

```text
specs/027-evaluation/
├── spec.md          # behaviour: US1–US9, FR-001–FR-117, known gaps
├── plan.md          # this file
├── research.md      # decisions
├── data-model.md    # tables, columns, states
├── quickstart.md    # validation guide
├── contracts/       # API and package contracts
├── checklists/
└── tasks.md         # /speckit-tasks
```

`027` is the next free number after `main`'s `026`; re-check it when 1780 merges, and renumber this folder if `main` has taken it.

Outside this folder: `dev-docs/evaluations.md` (design), `docs/docs/administration/evaluations.md` (user docs), `backend/app/evaluation_metrics/README.md` (metric suite).

### Source Code

```text
backend/app/
├── models/
│   ├── evaluation.py              # Evaluation, EvaluationAgent, EvaluationRun, EvaluationResult, EvaluationResultTrace, EvaluationEpisode
│   ├── evaluation_metric.py       # one row per result × metric: status, value, pooled numerator/denominator
│   ├── evaluation_param.py        # run parameters with their source
│   ├── dataset.py                 # Dataset, DatasetEntry, EvaluationDataset link
│   └── dataset_version.py         # immutable version snapshots
├── api/v1/
│   ├── evaluations.py             # /api/v1/evaluations: CRUD, triggers, runs, rescore, replay, compare, search, export/import
│   ├── datasets.py                # /api/v1/datasets: CRUD, entries, files, versions, ZIP and HF import, links
│   ├── evaluation_extractors.py   # extractor validation against the family
│   └── schemas/evaluation.py      # request/response models, MAX_BULK_RUN_IDS
├── services/
│   ├── evaluation_engine.py       # staging, run execution, judge, the single headline writer `_compute_run_headlines`
│   ├── evaluation_continuation.py # callback episodes: resume, timeout, stranded-run sweep
│   ├── evaluation_comparison.py   # two-run comparison for the agent tools
│   ├── orchestration_adapters/evaluation.py  # EvaluationAdapter: execute, resume, dead letter, stranded-headline recovery
│   ├── autonomous.py              # scheduler step: scheduled evaluations
│   ├── builtin_tools/evaluations.py          # admin-only agent tools (FR-111)
│   └── evaluation/
│       ├── aggregation.py         # stored metrics → three headlines and their provenance
│       ├── metrics.py             # per-result metric extraction, run aggregates
│       ├── metric_package/        # the only importer of the suite (see Encapsulation)
│       │   ├── quality_adapter.py, performance_adapter.py   # suite calls → platform statuses
│       │   ├── registry.py, traversal.py                    # one walk of the suite's metric metadata
│       │   ├── catalog.py, families.py, directions.py, units.py, rollups.py  # read off that walk
│       │   ├── gating.py          # no-quality-signal gate (judge off)
│       │   ├── availability.py    # fail closed on a missing or drifted suite
│       │   └── results.py         # MetricOutcome
│       ├── extraction/            # declarative extractor: spec, engine, sources, failures, limits, record, pipeline
│       ├── extractor_repository.py  # extractor DB access
│       ├── scorers/               # judge registry and metadata; write-time schema and vocabulary guards
│       ├── fingerprint.py         # reuse fingerprint and validity
│       ├── framing.py             # autonomous directive, anomaly detectors
│       ├── run_ownership.py       # every `owner_execution_id` predicate
│       ├── launch.py              # the checks and the runs of every launch: Run, API, replay, bulk re-run, schedule, persona candidate
│       ├── placebo_arms.py, placebo_launch.py, placebo_runs.py  # placebo comparisons: arms x trials, launch refusals and estimate, each run's placebo and overrides
│       ├── placebo_stats.py, placebo_comparisons.py  # their statistics (pure: D, R, bootstrap, Holm, verdict), settlement and served shape
│       ├── settings.py            # the one list of an evaluation's and an agent link's settings: create, copy, export, import
│       ├── run_mode.py, fanout.py # executed mode; non-LLM collapse to one run
│       ├── params.py, tags.py, run_search.py  # run parameters, cascading tags, search
│       ├── dataset_versioning.py, dataset_conformance.py, dataset_family_compatibility.py
│       └── always_ask.py, constants.py
├── evaluation_metrics/            # vendored suite: core/, quality/, performance/; no I/O, no platform import
└── evaluations/                   # built-in OCR benchmark seed

frontend/src/
├── pages/
│   ├── Evaluations.tsx, NewEvaluation.tsx    # list (+ Evaluations.commands.ts), create
│   ├── EvaluationDetail.tsx       # overview, activity, datasets, configuration tabs
│   ├── EvaluationRunDetail.tsx    # headlines, metrics panel, parameters, tags, results
│   ├── EvaluationRunCompare.tsx   # side-by-side compare and parameter diff
│   ├── Datasets.tsx, DatasetDetail.tsx
│   └── agent-detail/EvaluationTab.tsx
├── components/evaluation/         # settings, scoring, agent/model pickers, extractor card + extractor-catalog.ts, run-mode, badges
├── components/ui/family-badge.tsx, needs-family-badge.tsx
├── hooks/api/useEvaluationApi.ts, useDatasetApi.ts
├── lib/evaluation-headlines.ts    # the single headline resolver
├── lib/run-metric-format.ts, held.ts, constants.ts (Evaluations block)
└── types/index.ts                 # evaluation and dataset types
```

**Structure Decision**: existing web-app layout; routes registered in `backend/app/main.py`, pages behind `AdminRoute` in `frontend/src/App.tsx`.

### Encapsulation

Only `backend/app/services/evaluation/metric_package/` imports `app.evaluation_metrics`, pinned by `backend/tests/test_evaluation_guards.py` (with an anti-vacuity check) and its lint twin, ruff `TID251` in `backend/pyproject.toml`. The reverse direction (the suite imports no platform code) is pinned by `test_metric_suite_contracts.py`.

### Other tripwires

| Test (`backend/tests/`) | Pins |
|---|---|
| `test_metric_suite_contracts.py` | the suite's public surface; no I/O or clock read in the performance suite |
| `test_evaluation_guards.py` | only `run_ownership.py` expresses `owner_execution_id` predicates; evaluation tests build PostgreSQL through `scratch_database.py` |
| `test_evaluation_callbacks.py::test_penalized_mean_has_one_caller_writing_run_headlines` | one headline writer |
| `test_evaluation_run_stamps.py` | suite version and judge selection stamped where entries are scored |
| `test_evaluation_extraction.py` | extractor catalog: Python = `frontend/tests_e2e/fixtures/extractor-catalog.json` = `extractor-catalog.ts` |
| `test_evaluation_responses.py`, `test_evaluation_metric_catalog.py`; `frontend/src/pages/EvaluationDetail.legend.test.tsx`, `EvaluationRunDetail.metric-rows.test.tsx` | exclusion reasons, metric labels and legends shared with `frontend/src` |
| `test_evaluation_families.py`, `test_evaluation_metric_catalog.py`, `test_evaluation_metric_package.py` | platform catalogs equal the suite's |
| `test_evaluation_migrations.py` | the rescore index is built concurrently |

`test_no_workarounds.py` scans `services/builtin_tools/evaluations.py` only, not the other evaluation modules.

## Migrations

One revision, `e1780evp001` on main's `v6539rret01`, creates the final schema.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| I: launches are orchestration executions, not Assignments | An evaluation measures agents; each run and result is recorded, and Full Loop entries go through the agent's own loop | An Assignment carries one trigger and prompt, not pinned dataset versions, a model per run and per-entry scoring |
| XII: FR-036 anomaly flags read the answer text | The agent loop hands back provider failures and questions as answer text, not as structured facts | A structured outcome from the loop does not exist yet |
