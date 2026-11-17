# Evaluations

How the evaluation vertical is built and runs: the design. The rules other code relies on are in its contract.

| For | Read |
|---|---|
| The contract: data model, datasets, API, outcomes, judge, headlines, reuse, re-run, extractors, stored results, export, callbacks, tags, the metric seam | `dev-docs/reference/evaluations.md` |
| The metric suite | `backend/app/evaluation_metrics/README.md` |
| The `EVALUATION` feature flag | `dev-docs/evaluation-feature-flag.md` |
| Placebo comparisons: does a persona, a Prompt or a skill beat neutral text of its length | `dev-docs/agent-placebo-evaluation.md` |
| User documentation | `docs/docs/administration/evaluations.md` |
| The 1780 design record: stories, requirements, rationale, as delivered (frozen, not maintained) | `specs/027-evaluation/` |

## The pipeline

```text
trigger: Run, schedule, replay, bulk re-run, rescore, callback resume
  │  runs created pending + one OrchestrationExecution (adapter "evaluation"),
  │  in one transaction; each run stamped with owner_execution_id
  ▼
reconciler leases the execution to a worker
  ▼
EvaluationAdapter.execute → execute_evaluation          (evaluation_engine.py)
  │  reclaim runs a dead attempt left running, claim pending runs, fan out (≤ 5)
  ▼
execute_evaluation_run, one per run, own DB session
  │  per entry: run_agent_chat(evaluation_mode=True)    Full Loop
  │             or one streamed model call                LLM Only
  │  → extraction → metric_package (quality, performance) → LLM judge
  │  → EvaluationResult (execution_details) + evaluation_metrics rows
  ▼
_compute_run_headlines, per run, then for the batch
  │  aggregation.py: stage 1 per metric per run, normalise performance
  │  across runs, stage 2 per headline
  ▼
evaluation_runs.quality_score / performance_score / score + headline_details
  ▼
frontend reads the stored headlines only (lib/evaluation-headlines.ts)
```

Nothing recomputes a headline outside `_compute_run_headlines`: every surface reads what it stored, so two pages never show two numbers for one run.

## How an evaluated turn differs from chat

The rule: an evaluation scores the agent as configured, run the way chat runs it, whatever the person who launches it has. Full Loop runs the chat turn itself (`run_agent_chat` with `evaluation_mode=True`), so two people running the same evaluation get the same score. The run executes as its owner for credentials and visibility. The first four rows hold for every agent the evaluation reaches: the evaluated agent and the agents it consults, transfers to or delegates to (`evaluation_chain`).

| What | In an evaluation | Why |
| --- | --- | --- |
| Who the agent is talking to | The date and time in UTC, no one's profile, the default CTEM vertical (`cyber`); no identity clause in the security policy; on a company-managed agent, the "no personal memory" notice | The owner's name, timezone or vertical would make two owners score the same agent differently |
| Account-level knowledge bases and MCP servers | Left out: the agent runs with its own configuration only | They belong to one user |
| Memory | Off, reads and writes, the memory tools included | An entry leaves nothing the next one reads, a score is reproducible, and reuse's fingerprint does not include memory |
| Scheduling tools | Withheld | A scheduled action would run as the owner after the evaluation, once per entry and per run |
| Prompt guards | A setting, off by default; a consult or transfer follows it, a delegated task checks as chat does | Guard rules cost model calls on every entry, and only the author knows whether the guard is what is measured |
| Tool approval | A setting, off by default, for the whole chain; on, a held entry records the tools held | Nobody is there to approve |
| Autonomous directive | Appended to the system prompt (`services/evaluation/framing.py`) | Nobody is there to answer a question; the confirmation-seeking detector is the backstop |
| Output format | The agent's configured format, never a channel's | An evaluated turn reproduces a web chat turn |
| `scope_headers` on pipeline tool refs | None | They come from the request a turn answers, and an evaluation answers none |

The same as chat, on purpose: the iteration floor (`CHAT_MIN_LOOP_ITERATIONS`), the per-run delegation cap (`MAX_CONCURRENT_SUB_AGENT_TASKS_PER_RUN`, scoped to the evaluated turn by `cap_scope`), the security policy's core rules, and the agent's tools, knowledge bases, handovers and skills. An evaluation with no owner runs with no acting user, so its agent cannot delegate a background task.

Not as chat: a run on a saved model is answered by that model or not at all. Chat's model resolver answers a model no provider of the caller would serve with another one. Before an evaluation run calls its model, it checks the resolver's own rule (`model_served`): a chat model a provider its owner may use owns, by its catalog or its vendor prefix (`ProviderRouting.owner_for`). It is not the pickers' rule: a new model a configured vendor serves before the catalog lists it still runs. Every launch checks it before creating a run: the Run button, the API, replay, bulk re-run and a persona candidate refuse it with a 422, and a schedule creates no run and logs the reason once. A run whose model stops being offered after it was created fails before calling a model. The agent-default run and model-independent agents are not checked. A failover after the model fails at run time is another way to swap it.

## Background execution

- **Launch.** Every way of starting runs (Run, API, agent tool, replay, bulk re-run, schedule, persona candidate) checks and creates them through `services/evaluation/launch.py`: family, dataset families, callback secret, served models, the version snapshot. On agent change creates no run yet; when it does, it goes through the same module. `enabled` pauses the schedule and on agent change only. One `OrchestrationExecution` per launch, staged in the transaction that creates its runs, so a run is never active without the execution that can finish it. Payloads: a launch, `rescore_runs`, `resume_episode`.
- **Ownership.** Every run records its owning execution; `services/evaluation/run_ownership.py` is the only place that writes an ownership predicate (pinned by `test_evaluation_guards.py`). Cleanup only touches runs the execution owns, so two overlapping launches never fail each other's runs; an unowned run, which only an upgrade leaves, only while no other live execution names its evaluation.
- **Attempts.** Deadline = the evaluation's timeout + 120 s; 3 attempts. A new attempt fails, never re-runs, a run the previous one left `running`.
- **Lease.** Heartbeat every 20 s; a lost lease fires the cancel event and the worker stops. Cancelling a run whose execution owns nothing else cancels the execution (about 2 s); otherwise the worker stops at the next entry.
- **Rescore.** A `rescore_runs` payload records each run's status before the rescore (`previous_statuses`). The run reads `running` while it is re-judged and gets that status back whatever happens: success, failure, abandonment or a cancel. A failure is kept in `rescore_error`, never as the run's own; the timing stays the original run's, and a successful rescore records the suite version it scored with. Every check and write is scoped to the rescore's own execution, so one superseded by a later rescore never writes over it, and a rescore refuses (`run_not_running`) a run the endpoints did not set `running`.
- **Outcome.** A run none of whose entries counts in a headline (`aggregation.counts_in_headlines`) ends `failed`, like a callback-backed run with a failed episode.
- **Re-run and delete.** "Re-run full", single or bulk, replays each run with its own persona, prompt, config and skill versions, and refuses (409) a run whose snapshot is gone. Deleting a run goes through the cancel path; deleting an evaluation or all its runs cancels every execution working on them.
- **Held.** With agentic AI unavailable, the adapter holds the launch and its runs stay `pending`, up to `AI_UNAVAILABLE_HOLD_MAX_SECONDS`; then the pending runs fail with the reason.
- **Dead letter and sweep.** A dead-lettered launch fails its active runs and computes the headlines of the completed ones (`_recover_stranded_headlines`). The adapter's `periodic` hook times out overdue callback episodes and sweeps runs whose owner finished without completing them (a grace of at least 600 s), and runs an upgrade left with no owner once no execution of their evaluation is pending or running.
- **Callbacks.** With `await_callback`, each entry parks an episode `waiting_callback`; `POST /evaluations/resume` (HMAC-signed with `trigger_config.resume_secret`, stored encrypted, served masked, never exported, and required to run in callback mode; idempotent) stages a `resume_episode` execution; the run completes when every episode is terminal (`services/evaluation_continuation.py`).

## Scoring and aggregation

- **Per entry.** The extractor (`services/evaluation/extraction/`) produces the answer to score; `metric_package/quality_adapter.py` and `performance_adapter.py` call the suite; `score_result` in the engine calls the judge. Every outcome goes into `execution_details.metric_results`; scored values are also written as `evaluation_metrics` rows (`services/evaluation/metrics.py`), the only input of the headlines.
- **Performance telemetry.** Full Loop reads the chain ledger the agent loop records for the evaluated agent, its helpers and its delegated tasks (`services/run_ledger.py`); LLM Only reads the streamed call. The entry waits for delegated work after its wall clock stops.
- **Stage 1** (`aggregation.py`): each metric's run value over the counted entries, as the registry declares its roll-up: averaged with the spread penalty, pooled from its counts (a share of items, a pass/fail one item per entry), or macro (each class's counts summed over the run, then the classes' F1 averaged).
- **Stage 2**: Overall Quality per run; Overall Performance for all completed runs of the evaluation together, recomputed by `_recompute_performance_headlines` under a transaction-scoped advisory lock on the evaluation, so two batches finishing at once cannot score runs against different bases.
- **Provenance.** Contributions, exclusions with their reason and entry counts are stored in `headline_details` beside the values.
- **Metric names.** A metric has one name: its package label (`MetricMetadata.label`), the same string as its entry in the frontend's `METRIC_KEY_LABELS`, translated there, with a one-line description in `METRIC_KEY_DESCRIPTIONS` as its tooltip. Every page shows a metric through `components/evaluation/metric-name.tsx`, never its key; `tests/test_evaluation_metric_catalog.py` pins the two tables to the package. Legacy keys keep their original labels and units, so a stored row reads as it always did.
- **In-place explanations.** Every setting, score, metric, badge and column of the evaluation and dataset pages explains itself in a tooltip, never in more text on the page. A concept shown on more than one page is said once, in `frontend/src/lib/evaluation-glossary.ts` (`EVALUATION_TERMS`, the judge sub-scores, JSON field modes and statuses, the Metrics panel's columns, trigger notes, extraction failure names); a score's definition and its N/A reasons live in `HEADLINE_NOTES` and `naReason()` (`lib/evaluation-headlines.ts`); a page-only concept keeps its text in that page's `@i18n-keys` table. Every tooltip trigger is a tab stop: `InfoTip` beside a label, `InfoLabel` on a term or value (`components/ui/info-tip.tsx`), `Tip wrap` on a button that can be disabled. Whether the judge scored a run is read from its own `scorer_selection` (`runJudged`), never from its Judge Quality, which a failed or cancelled run lacks.

## The metric package boundary

`backend/app/evaluation_metrics/` is a vendored suite: pure functions, no I/O, no platform import. `backend/app/services/evaluation/metric_package/` is its only importer (pinned by `test_evaluation_guards.py` and ruff `TID251`). The registry walks the suite's metric metadata once; directions, units, roll-ups, the catalogue and the families are views of that walk. A missing or drifted suite fails closed (`availability.py`), and each run records the suite version that scored it.

To add a metric, follow the suite's README; the platform picks it up through the registry. A new family also needs its entry in `FAMILY_MODULES` (`metric_package/registry.py`), pinned against `families.FAMILY_IDS` by `test_evaluation_metric_catalog.py`.

## Reuse

Every result stores a fingerprint of the inputs that shape the answer (`services/evaluation/fingerprint.py`), whether or not its run reuses. A reuse-on run looks up the most recent valid fresh result with the same fingerprint in another completed run of the evaluation, copies its answer and telemetry, and scores it again; scores are never copied.

## Placebo comparisons

How placebo comparisons are built and run: `dev-docs/agent-placebo-evaluation.md` § Implementation.
