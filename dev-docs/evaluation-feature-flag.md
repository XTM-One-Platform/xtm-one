# Evaluation - Feature Flag

Evaluation (datasets, evaluations and their runs, and the persona rewrites they
measure) ships behind the `EVALUATION` feature flag, off by default. Like
`DEEP_INVESTIGATION`, the flag is **enforced by the backend**: off, the feature
is dark, not merely hidden.

## Enabling it

Off by default. Evaluation is on only where **both** hold:

- the platform runs in a mode where AI runs (`config.AI_MODES`: `xtm_one`,
  `copilot`, `dev`): evaluation is a generic AI capability, on in both
  products (`dev-docs/mode-split.md`, slice S3); `ai_disabled` never runs it,
  whatever the flag;
- `EVALUATION` is enabled:

```
ENABLED_FEATURES=EVALUATION
```

Set it on the platform and the worker, then restart both: the flag is read at
startup, and the resolved id list reaches the frontend through
`GET /auth/config`. **`ENABLED_FEATURES=*` enables it too**; list the ids
explicitly on a deployment that must stay without evaluation.

One rule, in the flag registry: `EVALUATION`'s entry in `FLAG_CONDITIONS`
(`backend/app/core/feature_flags.py`) is the mode condition, so
`is_feature_enabled("EVALUATION")` holds only in a mode where AI runs, and
`resolve_enabled_feature_flags()` (what `GET /auth/config` serves and the
startup logs print) leaves it out elsewhere. The backend gates read it through
`services/evaluation/availability.py` (`evaluation_enabled()`, the
`require_evaluation` dependency); the frontend reads the plain flag
(`useFeatureFlag("EVALUATION")`, `<FeatureFlagGate flag="EVALUATION">`) with no
mode check of its own.

## Two gates: the surfaces and the engine

Since the Platform Concierge (#8257, decision B9 of
`dev-docs/platform-concierge.md`) the rule above is the **surface gate**, and
the engine has a gate of its own:

| Gate | Function | Holds when |
| ---- | -------- | ---------- |
| Surface | `evaluation_enabled()`, `require_evaluation` | `EVALUATION` is enabled in a mode where AI runs (`xtm_one`, `copilot`, `dev`) |
| Engine | `engine_accepts(db, evaluation_id)`, `engine_accepts_runs(db, run_ids)` | The surface gate holds, **or** the Concierge is on (the `CONCIERGE` flag and Settings > Concierge, `health_active`) and built the evaluation (`Evaluation.concierge_built`, read on the row), whatever the mode |

The surface gate keeps every human surface of the table below exactly as it
was: in XTM One mode no page, route, user tool, analytics source, Admin
Objects type or seeded benchmark appears, `CONCIERGE` on or off. The engine
gate only decides whether a queued evaluation or rescore runs, so the
Concierge measures its experiments in every mode. The Concierge never reaches
the REST routes (its identity cannot log in): it launches through its own
tools, which check the engine gate before staging anything. The Concierge has
no user row, so its evaluations have no owner; `concierge_built` is set only by
those tools, never by REST, a copy, an export or an import
(`EVALUATION_NOT_SETTINGS`).

## What the flag gates

| Surface | With evaluation off |
| ------- | ------------------- |
| `/api/v1/evaluations*`, `/api/v1/datasets*`, `POST /api/v1/improvement-proposals/persona` | Every route answers 403 naming the flag (`require_evaluation`, evaluated before authentication), the signed `POST /evaluations/resume` callback included, and the routes are absent from `/api/openapi.json`. The other `/api/v1/improvement-proposals` routes stay published, but leave out the persona rewrites a person asked for (`surface_enabled`): those are reachable with evaluation on only |
| Scheduled evaluations | Not triggered (`autonomous.py`, step 9), one the Concierge built included: the Concierge launches on its own cycle |
| Run on agent change | Not triggered (`update_agent`, `api/v1/agents.py`) |
| Queued executions (`EvaluationAdapter.execute`) | Read through the **engine gate**: an evaluation the Concierge built runs. Any other fresh evaluation does not start and its pending runs fail with the reason; a rescore hands each run back with its previous status and the reason in `rescore_error`. A resumed callback episode is work in flight and continues |
| Evaluation agent tools | `list_evaluations`, `get_evaluation`, `list_evaluation_runs`, `get_evaluation_run_values`, `trigger_evaluation`, `get_placebo_comparison` and `compare_evaluation_runs` are neither defined nor registered (`build_evaluation_tools`), in chats and assignment runs. `get_agent_feedback` reads ratings, not evaluations, and stays |
| `GET /settings/builtin-tools/catalog`, `GET /hitl-whitelist/catalog` | The evaluation tools leave the `agent_builder` list (`withhold_evaluation_tools`); `build_builtin_tool_catalog` keeps them, as the registry skill packages and the refusal classifier read. The prompt-guard scope keeps listing them: they never run while off, and an entry that read as stale would be dropped from the scope |
| Built-in OCR benchmark dataset and evaluation | Not synced: the seed calls are skipped, not run with nothing declared, since both are full syncs that would delete them (`provisioning.py`) |
| Admin Objects | No dataset or evaluation type (`admin_objects.py` arms, `AdminObjects.tsx`) |
| `/evaluations*` pages | Redirect home (`FeatureFlagGate flag="EVALUATION"` in `App.tsx`) |
| Sidebar, ⌘K | No `Evaluations` entry (`NavFlags.evaluation`), no evaluation commands (context key `evaluation`) |
| Agent page | No `Evaluation` tab; no persona rewrite card on `Versioning` |
| Analytics | No `Evaluation` source in the picker; a bookmarked `?sources=evaluation` still filters |
| Emergency stop | The platform scope's description does not name evaluations |

## What is kept

- Stored data: datasets, evaluations, runs, results, metrics and rewrites.
  Nothing is migrated; turning evaluation back on picks everything up again.
- The janitors in `EvaluationAdapter.periodic()` (callback timeouts, stranded
  runs) and the settling of applied improvement proposals (`settle_open_proposals`):
  they start nothing and make no model call, and they close what was running.
- The emergency stop still cancels evaluation runs.
- The version-pin guards refusing to delete a prompt, skill or agent version a
  pending run pins.
- The built-in `ocr-benchmark-transcriber` agent and the
  `evaluation-judge-system-prompt` skill, both seeded in every mode
  (`platform_targets: ["global"]`): the agent seed is a full sync, so
  withholding the agent would delete its evaluation runs (`CASCADE`), and the
  engine loads the skill by slug. While evaluation is on, the benchmark itself
  is seeded in copilot and dev mode only, where evaluation can run
  (`_SEED_MODES` in `app/evaluations/seed.py`).
- LLM traces with `source="evaluation"`, dataset files in the Files repository,
  the security audit's `access.ownerless_evaluations_restricted_reach` finding
  and the HITL whitelist entries (inert while the tools are not built).
- The user docs' Evaluations page is unpublished (`exclude_docs` in
  `docs/mkdocs.yml`).

## Turning it off on a live instance

Remove `EVALUATION` from `ENABLED_FEATURES` and restart. Runs in progress are
not cancelled: an episode waiting on its callback times out through the janitor
(the callback itself now answers 403), and queued work is refused as above.

## Removing the flag

Drop `EVALUATION` from `FeatureFlag` / `KNOWN_FEATURE_FLAGS`
(`backend/app/core/feature_flags.py`) and from `KNOWN_FEATURE_FLAGS` in
`frontend/src/lib/utils.ts`, and its `FLAG_CONDITIONS` entry; reduce
`evaluation_enabled()` to the mode rule (`platform_mode in AI_MODES`) and serve the frontend that
answer instead of the flag, and republish
`administration/evaluations.md` in `docs/mkdocs.yml`.

The e2e specs under `frontend/tests_e2e/evaluation/` run in an `evaluation`
Playwright project, on a stack of their own in copilot mode with the flag on
(the `evaluation` entry of `.github/workflows/e2e.yml`'s matrix): every other
e2e job runs in XTM One mode without the flag, where evaluation stays off. That project stays
when the flag goes, since the mode rule does; only its `features` entry loses
`EVALUATION`.

`backend/tests/test_evaluation_feature_flag.py` pins the gates. Removing the
flag leaves the engine gate in place: it reduces to the mode rule or a
Concierge launch.
