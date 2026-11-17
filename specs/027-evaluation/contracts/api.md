# Contract: API routes

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

Every evaluation and dataset route, with who may call it (FR-014, FR-015). Schemas are in `backend/app/api/v1/schemas/evaluation.py`; handlers are in `backend/app/api/v1/evaluations.py` and `datasets.py`. Prefixes: `/api/v1/evaluations` and `/api/v1/datasets` (`backend/app/main.py`).

**Permission**:

| Code | Requires | Dependency |
|---|---|---|
| Admin | an administrator | `require_admin` |
| +AI | AI switched on | `require_ai_enabled` |
| +EE | AI on and agentic AI licensed | `require_agentic_ai` |
| User | any authenticated user | |
| HMAC | a signature only, no JWT | |

Routes with no AI gate stay available with AI off (FR-015, FR-043).

**Left out**: `GET /evaluations/metrics/query`, `POST /evaluations/{id}/import-hf`, `GET /evaluations/scorers`, `GET` and `PATCH /evaluations/{id}/agents/{agent_id}/tags` and `PUT /evaluations/runs/{run_id}/tags`, which no client called.

## Evaluations (`/api/v1/evaluations`)

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `?agent_id&scope` | Admin | list evaluations (three-tier; `scope=all` for all) |
| POST | `` | Admin +EE | create (FR-015) |
| GET | `/{evaluation_id}` | Admin | detail |
| PUT | `/{evaluation_id}` | Admin +AI | partial update: an omitted field is kept, `null` clears a nullable setting but leaves `family` and `scorer_selection` unchanged; `trigger_config` is merged, a key sent as `null` removed and `{}` a no-op, so clearing `on_agent_change` takes `false` (FR-023); reports `family_conflicts`; may recompute headlines (FR-011, FR-089) |
| DELETE | `/{evaluation_id}` | Admin +AI | delete with runs and results (FR-024) |
| POST | `/{evaluation_id}/copy` | Admin +AI | copy with every setting and agent setting, no trigger: a callback-mode source copies without callback mode (FR-023) |
| GET | `/{evaluation_id}/export` | Admin | export package ([export-package.md](export-package.md)) |
| POST | `/import` | Admin +AI | import package |
| GET | `/families` | User | family catalogue `[{id, label, description, metric_keys, structured_output, extractor_output_kinds}]`; `extractor_output_kinds` lists the output kinds an extractor of that family may produce, sorted (CLASSIFICATION `["label_set"]`, STRUCTURED_JSON `["array", "object"]`, the others `[]`): the extraction card offers and seeds only these |
| GET | `/{evaluation_id}/metric-catalog` | Admin | selectable headline metrics for the family ([metric-package.md](metric-package.md)) |
| GET | `/metric-catalog?family` | Admin | the same catalog for a family, before the evaluation exists: the creation screen; 400 for an unknown family |
| GET | `/agents/all?search&limit&exclude_ids` | Admin | agent picker search |
| GET | `/always-ask-tools?agent_ids` | Admin | "always ask" tools of agents, for the approval warning (FR-022) |
| GET | `/prompt-guard` | Admin | whether a prompt guard is connected (FR-022) |
| POST | `/{evaluation_id}/agents` | Admin +AI | link agents |
| DELETE | `/{evaluation_id}/agents/{agent_id}` | Admin +AI | unlink an agent |
| PUT | `/{evaluation_id}/baseline` | Admin +AI | set or clear the baseline agent (FR-106) |
| PUT | `/{evaluation_id}/agents/{agent_id}/tags` | Admin +AI | replace agent tags (FR-105) |
| POST | `/{evaluation_id}/extractors/preview` | Admin | read-only extractor preview ([extractor.md](extractor.md)) |
| POST | `/extractors/preview` | Admin | the same preview of a pasted sample, from a family: the creation screen |
| GET | `/{evaluation_id}/extractors/sample-results?agent_id&limit` | Admin | stored results to preview against |
| POST | `/{evaluation_id}/trigger` | Admin +AI | manual launch: agent × model runs (FR-026, FR-027); with `placebo: true`, a placebo comparison: agent × model × arm × trial runs (`dev-docs/agent-placebo-evaluation.md`), `confirm_write_tools` when the evaluation runs tool calls unapproved, 422 on what refuses it |
| POST | `/{evaluation_id}/placebo/estimate` | Admin | what a placebo comparison would run and what would refuse it, on the models the trigger would resolve, and the `seed` the trigger takes back as `placebo_seed`; writes nothing |
| POST | `/{evaluation_id}/trigger-from-runs` | Admin +AI | bulk "Re-run full", ≤ 200 runs (FR-042); 422 on a placebo comparison's run |
| POST | `/{evaluation_id}/rescore-from-runs` | Admin +AI | bulk rescore with a per-run outcome (FR-101, FR-102); a placebo comparison's run fails with `placebo_comparison_run` |
| POST | `/{evaluation_id}/recalculate-headlines` | Admin +AI | recompose every completed run's headlines from its stored metric values; nothing is re-executed or re-measured (FR-089) |
| POST | `/resume` | HMAC | callback resume ([resume-callback.md](resume-callback.md)) |
| GET | `/{evaluation_id}/runs?agent_id&model_id&limit&offset&exclude_comparisons` | Admin | runs, newest first; each with its placebo `arm`, `trial_index`, `placebo_target` and `placebo_comparison_id`; `exclude_comparisons` leaves a placebo comparison's runs out |
| GET | `/{evaluation_id}/runs/count?agent_id&model_id` | Admin | run count for paging |
| DELETE | `/{evaluation_id}/runs` | Admin | delete every run, recompute headlines (FR-043); its placebo comparisons go with them |
| GET | `/{evaluation_id}/placebo-comparisons?limit&offset` | Admin | placebo comparisons, newest first, each with its runs and results; settles any whose runs have finished |
| GET | `/placebo-comparisons/{comparison_id}` | Admin | one placebo comparison; 404 when unknown |
| GET | `/{evaluation_id}/chart?range` | Admin | headline trends and correlations (`30d`, `90d`, `all`) |
| GET | `/runs/{run_id}` | Admin | run with results, `rescore_pending`, `callback_pending` ([headlines.md](headlines.md)) |
| DELETE | `/runs/{run_id}` | Admin | delete a run, recompute headlines (FR-043); cancels its placebo comparison |
| POST | `/runs/{run_id}/cancel` | Admin | cancel a pending or running run (FR-038) |
| POST | `/runs/{run_id}/replay` | Admin +AI | replay; "Re-run full" sends `reuse: off` (FR-041); 422 on a placebo comparison's run |
| POST | `/runs/{run_id}/rescore` | Admin +AI | rescore stored answers (FR-101); 422 on a placebo comparison's run |
| POST | `/runs/{run_id}/results/{result_id}/replay-judge` | Admin +AI | judge-only replay of one result (FR-103) |
| GET | `/runs/{run_id}/results/{result_id}/traces/{trace_index}/download` | Admin | one stored trace step as JSON |
| GET | `/runs/{run_id}/results/{result_id}/metrics` | Admin | one result's metric rows ([stored-results.md](stored-results.md)) |
| GET | `/runs/{run_id}/metrics/aggregate` | Admin | run metrics panel aggregates (FR-091) |
| GET | `/runs/{run_id}/params` | Admin | run parameters with source (FR-107) |
| GET | `/runs/diff?run_a&run_b` | Admin | parameter diff of two runs (FR-108) |
| GET | `/runs/{run_id}/tags` | Admin | the run's own tags |
| PATCH | `/runs/{run_id}/tags` | Admin +AI | merge run tags |
| POST | `/runs/search` | Admin | cross-evaluation run search (FR-109) |

### Request and response bodies

Bodies are documented here when the API takes more than the screens send.

- **`POST /evaluations`** and **`PUT /{evaluation_id}`** accept the retired `quality_weight` and ignore it: nothing scores with it, and the pages of earlier versions send it on every save. The schema marks it deprecated, and no response or export carries it.
- **`POST /{evaluation_id}/trigger`** takes an optional body; every field is optional:

  ```
  { model_ids, include_default, params, tags, reuse }
  ```

  - `model_ids`: the models to run. Absent, the evaluation's enabled models, plus each agent's default model when the evaluation uses it. A model the evaluation does not list must be one the owner's providers serve (else 400), and the trigger adds it to the evaluation's models.
  - `include_default`: with `model_ids`, also runs each agent's default model when the evaluation uses it; without `include_default`, an explicit `model_ids` runs those models only.
  - `params`: a flat key/value object (a nested value is refused, 422), stored as the runs' `manual` parameters. It wins over the `evaluation` and `auto` sources (FR-107).
  - `tags`: the runs' own tags. The reserved `baseline` key is dropped and values are stored as strings, as on every tag write (FR-104, FR-105).
  - `reuse`: `inherit` (default), `on` or `off`, stored as the runs' `manual` `reuse` parameter, which wins over a `reuse` key in `params` and over the evaluation's reuse default (FR-093). Anything else is refused (422).
  - The screens send `model_ids` and `include_default` only. `params`, `tags` and `reuse` are for API callers, such as a pipeline that tags its nightly run.
- **`POST /runs/{run_id}/rescore`** takes no field, and `/rescore-from-runs` takes `{ run_ids }`. A rescore always applies the evaluation's scorer selection and records it on the run. There is no per-rescore override. Both bodies are strict: any other field, the retired `scorer_selection` included, is refused (422).
- **`POST /runs/search`** filters by every field of its request (`params`, `metrics`, `scores`, `tags`, `baseline_only`, `dataset_version`, dates), including those the search panel does not send: they are the run explorer's. A result row carries the run's headlines, tags, baseline flag, anomaly and platform-error counts. A run's parameters and metric aggregates are served by `/runs/{run_id}/params` and `/runs/{run_id}/metrics/aggregate`, not per row.

## Datasets (`/api/v1/datasets`)

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `?search&scope&family` | Admin | list datasets; a family filter also keeps family-less ones |
| POST | `` | Admin +EE | create, optionally with entries (FR-015) |
| GET | `/{dataset_id}` | Admin | detail with entries |
| PUT | `/{dataset_id}` | Admin +AI | metadata, family, vocabulary, answer schema, read-only (FR-002–FR-008) |
| DELETE | `/{dataset_id}` | Admin +AI | delete |
| GET | `/{dataset_id}/seed-ground-truth?agent_id` | Admin +AI | preview an answer schema from an agent's output schema (FR-007) |
| POST | `/{dataset_id}/entries` | Admin +AI | add an entry |
| PUT | `/entries/{entry_id}` | Admin +AI | update an entry |
| DELETE | `/entries/{entry_id}` | Admin +AI | delete an entry |
| POST | `/entries/{entry_id}/upload` | Admin +AI | attach a file |
| DELETE | `/entries/{entry_id}/files/{file_id}` | Admin | detach a file |
| GET | `/{dataset_id}/versions` | Admin | version history (FR-009) |
| GET | `/{dataset_id}/versions/{n}` | Admin | one frozen version |
| GET | `/{dataset_id}/export` | Admin | ZIP export (FR-012) |
| POST | `/import` | Admin +AI | ZIP import, ≤ 500 MB and ≤ 2 GB declared uncompressed (FR-012); entries sharing a position are renumbered 0..n-1 in manifest order |
| GET | `/hf-splits?dataset` | Admin | Hugging Face configs and splits |
| GET | `/hf-preview?dataset&config&split` | Admin | Hugging Face columns and 5 rows |
| POST | `/hf-import` | Admin +AI | Hugging Face import, ≤ 500,000 rows, progress over SSE (FR-012) |
| POST | `/{dataset_id}/link/{evaluation_id}` | Admin +AI | link to an evaluation; 409 on a family conflict (FR-011) |
| DELETE | `/{dataset_id}/link/{evaluation_id}` | Admin +AI | unlink |
| GET | `/for-evaluation/{evaluation_id}` | Admin | datasets linked to an evaluation |
