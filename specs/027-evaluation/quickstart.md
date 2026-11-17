# Quickstart: Validate Evaluation

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

## Prerequisites

- `cp .env.sample .env`, then `./dev.sh`: PostgreSQL 17 with pgvector, Redis on 6380, S3, backend, worker, frontend.
- An administrator account, at least one LLM provider with a priced model, AI enabled and agentic AI licensed (FR-015).
- Backend commands run from `backend/` with `.venv/bin/python`; frontend commands from `frontend/`.

## Automated

```bash
# Unit (no services)
SECRET_KEY=test .venv/bin/python -m pytest -m "not integration" -q \
  tests/test_evaluation*.py tests/test_dataset*.py tests/test_metric_suite_*.py \
  tests/test_scorer_registry.py tests/test_prediction_reuse.py tests/test_run_ledger.py \
  tests/test_builtin_evaluation_seed.py

# PostgreSQL integration (role with CREATEDB, pgvector installed)
DATABASE_URL=<from .env> REDIS_URL=redis://localhost:6380 SECRET_KEY=test \
  .venv/bin/python -m pytest -m integration -rs \
  tests/test_evaluation*.py tests/test_dataset*.py tests/test_builtin_evaluation_seed_postgres.py \
  tests/test_prediction_reuse.py tests/test_run_tags.py tests/test_run_ledger.py

# Lint and types
.venv/bin/ruff check app/                      # includes TID251, the encapsulation lint twin
.venv/bin/mypy --config-file pyproject.toml    # extraction package

# Frontend
yarn check                                     # tsc, eslint, i18n, unit tests
yarn test:e2e tests_e2e/evaluation-extractor.spec.ts tests_e2e/evaluation-extractor-ui.spec.ts \
  tests_e2e/dataset-label-vocabulary.spec.ts      # stack running: the setup project signs in
```

Expected: pass. Read the `-rs` summary: an unreachable PostgreSQL or a role without CREATEDB **skips** the integration tests, so a green run without them proves nothing; a missing pgvector errors. Run the files you touched, not the whole suite; `./scripts/ci-local.sh --only integration` reproduces CI's integration job.

## Test files by area (`backend/tests/`)

| Area | Files |
|---|---|
| API routes | `test_evaluations.py` (dataset-family gate, atomic staging, cancel, settings lookups), `test_evaluation_{lease,stranded_runs,callbacks,export_import}.py` (lease, sweep, callback, export/import), `test_evaluation_{run_ownership,abandoned_runs,families,extraction_api,responses,builtin_tools,feedback_tools,rescore}.py`, `test_run_tags.py`, `test_evaluations_{trigger_from_runs,runs_count}_api.py` |
| Datasets | `test_datasets.py`, `test_dataset_{versioning,delete_file_scope,entry_file_removal,file_references_postgres}.py`, `test_builtin_evaluation_seed{,_postgres}.py` |
| Engine and run lifecycle | `test_evaluation_{engine,concurrency,continuation,enqueue_deadline,framing,run_stamps,comparison,rescore}.py`, `test_prediction_reuse.py`, `test_run_tags.py` |
| Orchestration, lease, AI hold | `test_orchestration_adapters.py`, `test_orchestration_evaluation_adapter.py`, `test_orchestration_reconciler.py`, `test_panic_stop.py`, `test_ai_unavailable_hold.py`, `test_ai_hold_durability.py`, `test_background_edition_gate.py` (evaluation cases) |
| Judge | `test_scorer_registry.py`, `test_evaluation_{judge_scoring,run_stamps}.py` |
| Extraction | `test_evaluation_extraction.py`, `test_evaluation_extraction_pipeline.py` |
| Metric package and headlines | `test_evaluation_{aggregation,metrics,metric_adapters,families,metric_catalog,metric_package,run_stamps,responses}.py`, `test_run_ledger.py` |
| Metric suite | `test_metric_suite_*.py` (9 files) |
| Migrations | `test_evaluation_migrations.py` |
| Tripwires | see [plan.md](plan.md#other-tripwires) |
| Frontend | `src/lib/run-metric-format.test.ts`, `src/lib/held.test.ts`, `src/components/evaluation/run-mode.test.ts`; Playwright `tests_e2e/{evaluation-extractor,evaluation-extractor-ui,dataset-label-vocabulary}.spec.ts` |

## Manual, one per story

1. **US1**: in `/evaluations/datasets`, set a CLASSIFICATION vocabulary and add an out-of-vocabulary entry: refused, naming the label; edit an entry after a run: the run's page still shows the old version (scenarios 1, 3).
2. **US2**: create an evaluation with two agents and one extra model, disable the model, reopen: it is listed and the next run skips it; as a non-admin, `/evaluations` is refused.
3. **US3**: click Run: one run per agent and model appears, progresses and ends completed; cancel another mid-run: it ends cancelled with its results kept.
4. **US4**: on a run's page, open a STRUCTURED_JSON result with an invalid answer: `json.field_accuracy` reads 0 with the parse diagnostic; an empty extractor match shows an extraction failure.
5. **US5**: open a Full Loop result that delegated a task: its tokens and cost include the delegate's; an unpriced model shows cost N/A, never 0.
6. **US6**: with one completed run, Overall Performance reads N/A "too few comparable runs"; after a second run, both have it and the panel lists each excluded metric with its reason.
7. **US7**: re-run unchanged with reuse on: the run list reads "{n} of {n} predictions reused" and no agent call is made; Rescore leaves the performance metrics unchanged.
8. **US8**: select two runs differing only by model, open Compare: the parameter diff lists `model` as changed.
9. **US9**: export an evaluation with two completed runs, import it: a new private evaluation shows the same scores per result and the same headlines per run (SC-006).
