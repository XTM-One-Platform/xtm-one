---

description: "Tasks for the evaluation vertical (written after the fact: all done)"
---

# Tasks: Evaluation

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md)

**Tests**: Required (constitution VI). Every task is done; each phase lists the test files that pin its behaviour before the code they cover. Known gaps (spec.md, Known gaps) are open issues and have no task here.

## Format: `[ID] [P?] [Story] Description`

Paths are repo-relative. Abbreviations: `API` = `backend/app/api/v1/evaluations.py`, `DS` = `backend/app/api/v1/datasets.py`, `SCH` = `backend/app/api/v1/schemas/evaluation.py`, `ENG` = `backend/app/services/evaluation_engine.py`, `EV/` = `backend/app/services/evaluation/`, `MP/` = `EV/metric_package/`, `EM/` = `backend/app/evaluation_metrics/`, `T/` = `backend/tests/`, `FE/` = `frontend/src/`.

## Phase 1: Setup

- [X] T001 Register the evaluation and dataset routers in `backend/app/main.py` and the admin-only `/evaluations/*` routes in `FE/App.tsx`
- [X] T002 [P] Lint, type and test support: ruff `TID251` banned-api on `app.evaluation_metrics` (waived for `MP/` and `EM/`) and mypy scoped to `EV/extraction` in `backend/pyproject.toml`; anchors in `T/evaluation_helpers.py`; PostgreSQL harness `T/scratch_database.py` pinned by `T/test_evaluation_guards.py`

---

## Phase 2: Foundational (blocks all stories)

**Purpose**: the schema, the vendored metric suite behind its one adapter, and durable execution.

- [X] T003 [P] Boundary and seam tests: `T/test_evaluation_guards.py`, `T/test_metric_suite_contracts.py`, `T/test_evaluation_metric_{package,catalog}.py`, `T/test_evaluation_families.py`
- [X] T004 [P] Execution and migration tests: `T/test_evaluation_guards.py`, the evaluation cases of `T/test_orchestration_adapters.py`, `T/test_orchestration_evaluation_adapter.py` and `T/test_orchestration_reconciler.py`, `T/test_evaluation_migrations.py`
- [X] T005 Models `Evaluation`, `EvaluationAgent`, `EvaluationRun`, `EvaluationResult`, `EvaluationResultTrace`, `EvaluationEpisode` in `backend/app/models/evaluation.py`, imported in `backend/app/models/__init__.py`
- [X] T006 [P] Models `Dataset`, `DatasetEntry`, `EvaluationDataset` in `backend/app/models/dataset.py`, `DatasetVersion` in `dataset_version.py`, `EvaluationMetric` in `evaluation_metric.py`, `EvaluationRunParam` in `evaluation_param.py` (all under `backend/app/models/`)
- [X] T007 One lock-safe, idempotent Alembic revision `e1780evp001`, single head, in `backend/alembic/versions/`
- [X] T008 Vendor the suite core (`ScoreResult` contract, errors, canonical serialization, one suite version) in `EM/core/`, `EM/__init__.py`, `EM/README.md`
- [X] T009 Adapter seam, the only importer of `EM/`: one metadata walk in `MP/registry.py` and `MP/traversal.py` feeding `MP/directions.py`, `MP/units.py`, `MP/rollups.py`, `MP/catalog.py`, `MP/families.py`; `MetricOutcome` in `MP/results.py`; fail-closed availability in `MP/availability.py` (FR-062); public surface in `MP/__init__.py`
- [X] T010 Orchestration adapter `EvaluationAdapter` (launch, rescore and resume payloads, `on_dead_letter`, `periodic`, lease-loss opt-in) in `backend/app/services/orchestration_adapters/evaluation.py`, registered in `orchestration_adapters/__init__.py`; staging in the caller's transaction (`stage_evaluation_execution`, `enqueue_evaluation`, `stage_rescore_execution`) in `ENG`; ownership predicates on `owner_execution_id` in `EV/run_ownership.py`
- [X] T011 [P] Request and response models in `SCH`; types in `FE/types/index.ts`; TanStack Query hooks in `FE/hooks/api/useEvaluationApi.ts` and `FE/hooks/api/useDatasetApi.ts`

**Checkpoint**: tripwires and seam tests pass; migrations upgrade to one head.

---

## Phase 3: User Story 1 - Prepare a dataset (P1)

**Goal**: ground truth in the shape the scorers expect, frozen per version.

**Independent Test**: Create a CLASSIFICATION dataset with a vocabulary and a STRUCTURED_JSON dataset with an answer schema, add valid and invalid entries, edit one after a run, and check the refusals and the pinned version.

- [X] T012 [P] [US1] Tests: `T/test_datasets.py`, `T/test_dataset_versioning.py`, `T/test_dataset_delete_file_scope.py`, `T/test_dataset_entry_file_removal.py`, `T/test_dataset_file_references_postgres.py`; Playwright `frontend/tests_e2e/dataset-label-vocabulary.spec.ts`
- [X] T013 [US1] Dataset and entry CRUD, attached files, family, read-only lock and family-change clean-up in `DS`; schema seeding from an agent's output schema, `GET /datasets/{id}/seed-ground-truth` (FR-001, FR-002, FR-006, FR-007, FR-008)
- [X] T014 [P] [US1] Write-time label guard in `EV/scorers/classification_vocabulary.py` and answer-schema guard (2020-12, ReDoS fence) in `EV/scorers/structured_output.py` and `EV/dataset_conformance.py` (FR-003, FR-004, FR-005)
- [X] T015 [US1] Immutable versions and run pinning in `EV/dataset_versioning.py` (`bump_version`, `pin_evaluation_versions`, `load_pinned_entries`), called from `DS` (FR-009, FR-010)
- [X] T016 [US1] Family compatibility of links in `EV/dataset_family_compatibility.py` and the link routes in `DS` (FR-011)
- [X] T017 [US1] ZIP export/import (500 MB) and Hugging Face import (500,000 rows) in `DS` (FR-012)
- [X] T018 [P] [US1] Pages `FE/pages/Datasets.tsx` and `FE/pages/DatasetDetail.tsx`, badges `FE/components/ui/family-badge.tsx` and `needs-family-badge.tsx` (FR-002 to FR-008)

**Checkpoint**: US1 Independent Test passes.

---

## Phase 4: User Story 2 - Create and configure an evaluation (P1)

**Goal**: the configuration that decides what every run measures.

**Independent Test**: Create an evaluation with two agents, an extra model, a STRUCTURED_JSON family, the judge off and an extractor; reopen it and check every setting, then copy and delete it.

- [X] T019 [P] [US2] Tests: `T/test_evaluation_families.py`, `T/test_evaluation_extraction_api.py` (save-time validation), `T/test_evaluation_framing.py`, the settings and copy cases of `T/test_evaluation_export_import.py` and the settings lookups of `T/test_evaluations.py`, repo tripwires listing evaluation routes (`T/test_admin_reach.py`, `T/test_ai_disabled_read_surface.py`, `T/test_name_length_guards_postgres.py`); Playwright `frontend/tests_e2e/evaluation-extractor-ui.spec.ts`
- [X] T020 [US2] Admin-only access and AI / agentic-licence gates on every route in `API` and `DS` (FR-014, FR-015)
- [X] T021 [US2] Create, read, update and list with model sources, disabled extra models, family gate (`_validate_family_and_gate`), execution mode, run mode, timeout, reuse default and judge switch in `API` (FR-016 to FR-020)
- [X] T022 [US2] Per-agent extractor validation against the family in `backend/app/api/v1/evaluation_extractors.py`, stored through `EV/extractor_repository.py` (FR-021)
- [X] T023 [US2] Prompt-guard and tool-approval switches: `GET /evaluations/always-ask-tools` and `/prompt-guard` in `API`, detection in `EV/always_ask.py` (FR-022)
- [X] T024 [US2] Copy and delete in `API` (FR-023, FR-024)
- [X] T025 [P] [US2] Pages `FE/pages/Evaluations.tsx`, `FE/pages/NewEvaluation.tsx`, the Configuration tab of `FE/pages/EvaluationDetail.tsx`, `FE/pages/agent-detail/EvaluationTab.tsx`; cards `FE/components/evaluation/evaluation-{settings,scoring,agent-picker,model-picker,extractor}-card.tsx` and `extractor-catalog.ts` (FR-016 to FR-022)

**Checkpoint**: US2 Independent Test passes.

---

## Phase 5: User Story 3 - Run an evaluation (P1)

**Goal**: runs that record what they ran with and always reach a terminal state.

**Independent Test**: Run an evaluation with two LLM agents, two models and one non-LLM agent: five runs start, show progress, end completed and record their versions.

- [X] T026 [P] [US3] Tests: `T/test_evaluations.py` (atomic staging, cancel), `T/test_evaluation_run_ownership.py` (ownership), `T/test_evaluation_abandoned_runs.py` (reclaim, dead letter), `T/test_evaluation_lease.py` (lease loss), `T/test_evaluation_stranded_runs.py` (stranded sweep), `T/test_evaluation_callbacks.py` (callback), `T/test_evaluation_continuation.py`, `T/test_evaluation_concurrency.py`, `T/test_evaluation_enqueue_deadline.py`, `T/test_evaluation_engine.py`, `T/test_evaluation_framing.py`, `T/test_evaluation_run_stamps.py`, `T/test_evaluation_families.py`
- [X] T027 [P] [US3] Tests: `T/test_evaluations_trigger_from_runs_api.py`, `T/test_evaluation_responses.py`, `T/test_evaluations_runs_count_api.py`, `T/test_ai_unavailable_hold.py`, `T/test_panic_stop.py`, `T/test_run_usage_accounting.py`; `FE/lib/held.test.ts`, `FE/components/evaluation/run-mode.test.ts`
- [X] T028 [US3] Manual trigger `POST /evaluations/{id}/trigger` in `API` with fan-out per agent and model in `EV/fanout.py` and the model-independent sentinel in `EV/constants.py`; refusals before any run (`_require_runnable_family`, `_require_dataset_families_set`, no agent, model or entry) (FR-025, FR-026, FR-027)
- [X] T029 [US3] Scheduled trigger `_process_scheduled_evaluations` in `backend/app/services/autonomous.py` (FR-025, FR-028)
- [X] T030 [US3] Version snapshot at creation and execution mode, prompt-guard and tool-approval stamps at start in `ENG`, read back by `EV/run_mode.py` (FR-029)
- [X] T031 [US3] Entry execution in `execute_evaluation_run` (`ENG`): Full Loop through `run_agent_chat(evaluation_mode=True)` in `backend/app/services/agent_chat.py`, LLM Only through `_execute_agent_llm_only` (4096 tokens, one larger retry), autonomous framing from `EV/framing.py` (FR-030, FR-031)
- [X] T032 [US3] Callback episodes and `POST /evaluations/resume` in `backend/app/services/evaluation_continuation.py` and `API` (FR-032)
- [X] T033 [US3] Batch execution `execute_evaluation` with `_gather_runs_bounded` (5 or 1 in flight), delegated-task waits, raised entries recorded as 0, anomaly flag from `classify_execution_anomaly` in `EV/framing.py` (FR-033 to FR-036)
- [X] T034 [US3] Terminal states and cancel: `POST /evaluations/runs/{id}/cancel` in `API`, `close_episodes_for_cancelled_run` in `backend/app/services/evaluation_continuation.py` (FR-037, FR-038)
- [X] T035 [US3] At-most-once execution: reclaim fails a dead attempt's runs, lease-loss stop, deadline `timeout + 120 s`, `sweep_stranded_runs` in `backend/app/services/evaluation_continuation.py`, AI-unavailable hold via `held_evaluations` in `backend/app/services/ai_hold.py` (FR-037, FR-039, FR-040)
- [X] T036 [US3] Replay `POST /evaluations/runs/{id}/replay` and bulk `POST /evaluations/{id}/trigger-from-runs` (≤ 200) with `resolve_run_snapshot_overrides` and `batch_compute_run_replayability` in `ENG` (FR-041, FR-042)
- [X] T037 [US3] Delete one run or all runs in `API` (FR-043); evaluation turns not metered (`_execution_is_recorded`) in `backend/app/services/agent_chat.py` (FR-044)
- [X] T038 [P] [US3] Run list and actions in the Activity tab of `FE/pages/EvaluationDetail.tsx`, run header, cancel and "Re-run full" in `FE/pages/EvaluationRunDetail.tsx`, polling tiers in `FE/lib/held.ts`, badges `FE/components/evaluation/{run-mode,run-mode-badge,prompt-guard-badge,tool-approval-badge}` (FR-029, FR-037, FR-041)

**Checkpoint**: US3 Independent Test passes.

---

## Phase 6: User Story 4 - Score an entry's quality (P1)

**Goal**: deterministic metrics and an optional judge, each with an explicit status.

**Independent Test**: Run one entry per family with a correct, a wrong and an ill-formed answer, and check each metric's value and status and the judge verdict.

- [X] T039 [P] [US4] Suite tests: `T/test_metric_suite_{contracts,primitives,routing}.py` and the family files `T/test_metric_suite_{classification,structured_json,stix,prose,prose_normalization}.py`, goldens in `T/fixtures/metric_suite/`
- [X] T040 [P] [US4] Platform tests: `T/test_evaluation_metric_adapters.py`, `T/test_evaluation_judge_scoring.py`, `T/test_evaluation_run_stamps.py`, `T/test_scorer_registry.py`, `T/test_evaluation_families.py`, `T/test_evaluation_extraction.py`, `T/test_evaluation_extraction_pipeline.py`, fixtures `T/fixtures/extraction/`; Playwright `frontend/tests_e2e/evaluation-extractor.spec.ts`
- [X] T041 [US4] Quality primitives (sets, P/R/F1, aggregate), normalization and `ScoreResult` in `EM/quality/primitives/`, `EM/quality/normalization/`, `EM/quality/results.py`, `EM/quality/metric.py` (FR-049)
- [X] T042 [P] [US4] STRUCTURED_JSON family: validity, schema validity, `json.field_accuracy` with schema-derived modes and deterministic pairing in `EM/quality/families/structured_json/` (FR-050, FR-051, FR-054)
- [X] T043 [P] [US4] CLASSIFICATION family (vocabulary validity, micro/macro, samples F1, subset accuracy, Hamming, empty-vs-empty correct) in `EM/quality/families/classification/` (FR-051, FR-053)
- [X] T044 [P] [US4] STIX_BUNDLE family and validators in `EM/quality/families/stix_bundle/` and `EM/quality/backends/` (FR-050, FR-051)
- [X] T045 [P] [US4] PROSE family with the one platform normalisation in `EM/quality/families/prose/` (FR-051, FR-056)
- [X] T046 [US4] Family routing in `EM/quality/families/routing.py`; adapter `run_quality`, vocabulary resolution, field-override validation, no-schema `unavailable` in `MP/quality_adapter.py`; no-signal gate in `MP/gating.py` (FR-045, FR-049, FR-050, FR-053, FR-055)
- [X] T047 [US4] Declarative extractor (spec, engine, sources, failures, limits, record, pipeline) in `EV/extraction/`, preview routes `/evaluations/{id}/extractors/preview` and `/sample-results` in `API` (FR-046, FR-047, FR-048)
- [X] T048 [US4] LLM judge `score_result` in `ENG` (verdict schema, repair, failure without retry, tool-less 100, weighted sub-scores), judge registry in `EV/scorers/registry.py` and `EV/scorers/judge.py`, managed skill `evaluation-judge-system-prompt` seeded from `backend/app/agents/builtin_agents.py`; metric suite version and scorer selection stamped on the run (FR-057 to FR-061)
- [X] T049 [P] [US4] Result view: P/R/F1 breakdown and field-accuracy lists in `FE/pages/EvaluationRunDetail.tsx`; judge and field-override settings in `FE/components/evaluation/evaluation-scoring-card.tsx` (FR-052, FR-055, FR-058, FR-059)

**Checkpoint**: US4 Independent Test passes.

---

## Phase 7: User Story 5 - Measure an entry's performance (P1)

**Goal**: nine performance metrics per entry, chain included, never a false 0.

**Independent Test**: Run one Full Loop entry that delegates a task and one LLM Only entry, and check the nine metrics, their units and statuses.

- [X] T050 [P] [US5] Suite tests: `T/test_metric_suite_performance.py` (cost, counts, latency, reliability, tokens, telemetry, validation, overhead), `T/test_metric_suite_contracts.py` (units, results, roll-up), `T/test_evaluation_metric_adapters.py`, `T/test_evaluation_metrics.py`, `T/test_run_ledger.py`, latency, TTFT, chain-ledger and price-row cases of `T/test_evaluation_engine.py`; `FE/lib/run-metric-format.test.ts`
- [X] T051 [US5] Performance suite: the nine metrics in `EM/performance/metrics/{latency,counts,tokens,cost,reliability}.py`, telemetry, pricing, units and results in `EM/performance/` (FR-063, FR-064, FR-067, FR-070, FR-071)
- [X] T052 [US5] Adapter `run_performance`, `ChainInputs` and incomplete-chain mapping in `MP/performance_adapter.py` (FR-064, FR-069)
- [X] T053 [US5] Wall-clock and TTFT capture around the agent call in `ENG` (FR-065, FR-066)
- [X] T054 [US5] Chain ledger across consults, transfers and delegated tasks in `backend/app/services/run_ledger.py` and `backend/app/services/sub_agent.py`, assembled per entry in `ENG`; per-model price rows `_build_run_price_rows` in `ENG` (FR-068, FR-069, FR-070)
- [X] T055 [US5] Per-result metric rows with pooled numerator/denominator in `EV/metrics.py` (`extract_metrics`, `log_result_metrics`); Judge Time and Run Duration on the result in `ENG` (FR-063, FR-071, FR-072)
- [X] T056 [P] [US5] Unit-aware formatting in `FE/lib/run-metric-format.ts` (FR-063)

**Checkpoint**: US5 Independent Test passes.

---

## Phase 8: User Story 6 - Read a run's headlines (P1)

**Goal**: three separate, explained headlines per run.

**Independent Test**: Run an evaluation with two agents over a dataset with one anomalous entry, and check each run's headlines, contributions and exclusion reasons against values computed by hand.

- [X] T057 [P] [US6] Tests: `T/test_evaluation_aggregation.py`, the recompute cases of `T/test_evaluation_concurrency.py`, the single-writer pin in `T/test_evaluation_callbacks.py`, stranded-headline recovery in `T/test_orchestration_evaluation_adapter.py`, `T/test_evaluation_responses.py` (headline responses and the exclusion-reason cross-language pin), the label cross-language pin in `T/test_evaluation_metric_catalog.py`; legend and metric-row behaviour in `frontend/src/pages/EvaluationDetail.legend.test.tsx` and `EvaluationRunDetail.metric-rows.test.tsx`
- [X] T058 [US6] Stage 1 reduction in `EV/aggregation.py`: counted-entry filter, spread-penalised mean with orientation, pooled ratio, unknown direction or roll-up dropped (FR-075, FR-077, FR-078, FR-079)
- [X] T059 [US6] Quality headlines `compute_quality_headline` (Judge Quality and Overall Quality) in `EV/aggregation.py` (FR-073, FR-076, FR-080)
- [X] T060 [US6] Overall Performance: shared-metric rule, tolerance-band normalisation, abstention, `compute_performance_headlines` in `EV/aggregation.py` (FR-081, FR-082, FR-083)
- [X] T061 [US6] Selections and default contributors (`NON_DEFAULT_*_KEYS` in `MP/catalog.py`, `resolve_default_quality_selection`), exclusion classification and `headline_details` provenance in `EV/aggregation.py` (FR-084 to FR-088)
- [X] T062 [US6] Single headline writer `_compute_run_headlines` in `ENG`, `POST /evaluations/{id}/recalculate-headlines` in `API`, callback-side writer in `backend/app/services/evaluation_continuation.py`; recovery `_recover_stranded_headlines` in `backend/app/services/orchestration_adapters/evaluation.py` (FR-089, FR-090)
- [X] T063 [US6] Run metrics aggregate `get_run_aggregates` in `EV/metrics.py`, `GET /evaluations/runs/{id}/metrics/aggregate`, metric catalog and chart routes in `API`; legacy-run serialization in `API` (FR-091, FR-092)
- [X] T064 [P] [US6] Headline resolver and N/A causes in `FE/lib/evaluation-headlines.ts`; headline cells and run metrics panel in `FE/pages/EvaluationRunDetail.tsx`; Overview chart and selection editing in `FE/pages/EvaluationDetail.tsx` (FR-073, FR-074, FR-083, FR-088, FR-091, FR-092)

**Checkpoint**: US6 Independent Test passes.

---

## Phase 9: User Story 7 - Reuse earlier answers and rescore (P2)

**Goal**: no agent call when only scoring changed.

**Independent Test**: Run an unchanged evaluation twice with reuse on: the second run calls no agent, marks every entry reused and computes every score again.

- [X] T065 [P] [US7] Tests: `T/test_prediction_reuse.py`, `T/test_evaluation_rescore.py`
- [X] T066 [US7] Fingerprint `compute_input_fingerprint` and `is_result_reusable` in `EV/fingerprint.py` (FR-093, FR-094, FR-095, FR-097)
- [X] T067 [US7] Source lookup (5 latest, completed, same evaluation) and reused-entry recomputation in `ENG` (FR-096, FR-098, FR-099)
- [X] T068 [US7] `reused_entries` and per-result reuse source in `SCH` and `API` (FR-100)
- [X] T069 [US7] Rescore `POST /evaluations/runs/{id}/rescore` and bulk `/rescore-from-runs` in `API`, `rescore_run` and `execute_rescore_runs` in `ENG`; judge-only replay `POST /evaluations/runs/{id}/results/{result_id}/replay-judge` (FR-101, FR-102, FR-103)
- [X] T070 [P] [US7] Reuse counts and bulk rescore in `FE/pages/EvaluationDetail.tsx`; reuse marks, rescore and judge replay in `FE/pages/EvaluationRunDetail.tsx` (FR-100 to FR-103)

**Checkpoint**: US7 Independent Test passes.

---

## Phase 10: User Story 8 - Compare runs (P2)

**Goal**: labelled, searchable runs compared side by side.

**Independent Test**: Select two runs of one evaluation and open Compare: both runs' headlines, each entry's score per run, their tags and a parameter diff are shown.

- [X] T071 [P] [US8] Tests: `T/test_run_tags.py`, `T/test_evaluation_responses.py`, `T/test_evaluation_comparison.py`, `T/test_evaluation_builtin_tools.py`, `T/test_evaluation_feedback_tools.py`
- [X] T072 [US8] Cascading tags with the reserved `baseline` key in `EV/tags.py`; tag and `PUT /evaluations/{id}/baseline` routes in `API` (FR-104, FR-105, FR-106)
- [X] T073 [US8] Run parameters with sources and two-run diff in `EV/params.py`; `GET /evaluations/runs/{id}/params` and `/runs/diff` in `API` (FR-107, FR-108)
- [X] T074 [US8] Run search `search_evaluation_runs` in `EV/run_search.py` and `POST /evaluations/runs/search` in `API` (FR-109)
- [X] T075 [US8] Agent tools in `backend/app/services/builtin_tools/evaluations.py` and the two-run comparison in `backend/app/services/evaluation_comparison.py` (FR-111)
- [X] T076 [P] [US8] Compare page `FE/pages/EvaluationRunCompare.tsx`; tags panel and baseline control in `FE/pages/EvaluationRunDetail.tsx` and `FE/pages/EvaluationDetail.tsx` (FR-104, FR-106, FR-110)

**Checkpoint**: US8 Independent Test passes.

---

## Phase 11: User Story 9 - Move an evaluation between instances (P3)

**Goal**: one package carries an evaluation, its dependencies and its results.

**Independent Test**: Export an evaluation with two completed runs and import it on another instance: the evaluation, its agents and datasets, both runs and every result appear with the same scores and flags.

- [X] T077 [P] [US9] Tests: the export/import round-trip and malformed-package cases of `T/test_evaluation_export_import.py`
- [X] T078 [US9] Package v1 writer `GET /evaluations/{id}/export` in `API`, embedding agent exports and dataset packages, results keyed by dataset and position (FR-112, FR-113)
- [X] T079 [US9] Reader `POST /evaluations/import` in `API`: version and type check, embedded dependencies, private copy with a unique name, terminal statuses kept, metrics derived from execution details; replay of an imported run refused (FR-114, FR-115, FR-117)
- [X] T080 [P] [US9] Export and import actions in `FE/pages/Evaluations.tsx` and `FE/pages/EvaluationDetail.tsx` (FR-112, FR-114)

**Checkpoint**: US9 Independent Test passes.

---

## Phase 12: Polish

- [X] T081 [P] Evaluation strings in `FE/i18n/lang/{en,fr,es,ja}.json`; command palette entries in `FE/pages/Evaluations.commands.ts`
- [X] T082 [P] Built-in OCR benchmark (FR-013) in `backend/app/evaluations/seed.py`, `documents.py`, `seed_data/`, tested by `T/test_builtin_evaluation_seed.py` and `T/test_builtin_evaluation_seed_postgres.py`
- [X] T083 [P] User docs `docs/docs/administration/evaluations.md`; design doc `dev-docs/evaluations.md`; suite guide `EM/README.md`
- [X] T084 Gates: `cd backend && ruff check app/ && mypy --config-file pyproject.toml`, the evaluation unit and `integration` tests, `cd frontend && yarn check`

---

## Dependencies

- Phase 1 → Phase 2 → stories → Phase 12. Phase 2 blocks every story (models, seam, adapter).
- US1 and US2 come first: a run needs a family-checked dataset and evaluation. US3 needs both; US4 and US5 score what US3 runs; US6 needs US4 and US5.
- US7 needs US3–US6 (it recomputes their outputs); US8 needs US3 and US6; US9 needs US1–US6.
