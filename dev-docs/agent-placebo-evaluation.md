# Placebo-Controlled Agent Evaluation - PRD

| | |
|---|---|
| Status | Decisions validated 2026-10-24; rebased 2026-10-25 onto the evaluation vertical v1; levels 1 to 4 implemented on top of it |
| Area | Evaluations (`dev-docs/evaluations.md`, `dev-docs/reference/evaluations.md`, `backend/app/evaluation_metrics/README.md`) |
| Inspiration | [simonether/skill-placebo](https://github.com/simonether/skill-placebo) ([METHOD.md](https://github.com/simonether/skill-placebo/blob/main/METHOD.md)) |

## 1. Problem

An evaluation tells us how well an agent scores. It does not tell us **why**. When an agent with a long persona, three attached Prompts and five skills scores 82, we cannot say whether the instructions in those texts earn their place, or whether any text of the same length would do as well: more tokens in the context change how a model behaves (attention, verbosity, cost) independently of what they say.

skill-placebo measured exactly this on coding agents: 2 of the 9 most-starred skills beat a neutral text of the same length, installed the same way; 6 did no better; 1 did worse. The same question applies to every persona, Prompt and skill authored in XTM One.

## 2. Goal

Let an evaluation answer, per agent and model: **does this persona / these Prompts / this skill do better than a neutral placebo of the same length, delivered through the same mechanism, and better than nothing at all?**

### Goals

- G1. Replace, in a controlled arm, the agent's persona, its attached Prompts, its skills (all at once) or one skill, with a neutral placebo of the same length in tokens, delivered through the same mechanism.
- G2. Compare the real agent with its placebo arm and with a no-target arm (element removed), on the same dataset, same model, same tools, paired by entry.
- G3. Report quality and token cost with confidence intervals and a verdict computed by a rule fixed in advance.
- G4. Make every comparison reproducible: the placebo text, the seed and the measured lengths are stored with the run.

### Non-goals (v1)

- A badge or score on the agent, skill or Prompt pages (decision Q24).
- Placebo arms on scheduled or agent-change triggers (decision Q21).
- Placebos for guardrails, output format instructions, memory, knowledge base instructions or tool descriptions (decision Q4).
- Cost in currency: cost is measured in tokens (decision Q14).
- An LLM-generated placebo (decision Q9).

## 3. Ground: the evaluation vertical v1

The placebo method is built on the evaluation vertical v1, not beside it. What it reuses, and the rules it follows:

| Vertical v1 | Use here |
|---|---|
| `EVALUATION` feature flag, on in every mode where AI runs (xtm_one, copilot, dev) (`dev-docs/evaluation-feature-flag.md`, `services/evaluation/availability.py`) | The placebo method is evaluation: it is on exactly when evaluation is. Its routes sit on the gated evaluation routers; its agent tools join `EVALUATION_TOOL_NAMES`; its pages sit under `FeatureFlagGate flag="EVALUATION"`. No flag of its own. |
| One way to start runs: `services/evaluation/launch.py` (`plan_launch`, `add_launch_runs`) and one `OrchestrationExecution` per launch | A placebo comparison is a launch: the plan gains arms and trials, the runs are added by `add_launch_runs`, owned by the launch's execution (`run_ownership.py`). |
| Run snapshots → `resolve_run_snapshot_overrides` → `evaluation_overrides` of `run_agent_chat` (`persona`, `prompt_fragment`, `skill_versions`, config fields) | A control run overlays its placebo on those overrides; skills gain one key, `skill_substitutions`, read where the skill catalog and `load_skill` resolve skills. |
| Metric suite (`evaluation_metrics/`, reached only through `services/evaluation/metric_package/`), `evaluation_metrics` rows per result and key, roll-ups declared per metric (`aggregation.py`) | Quality and token measures are the suite's, read from the stored rows; the statistics reuse `reduce_grouped_metrics` so pooled and macro metrics keep their declared roll-up. |
| Reuse by input fingerprint (`fingerprint.py`) | Off for every run of a comparison; the fingerprint also carries the arm, the placebo hash and the trial. |
| Overall Performance normalised across every completed run of the evaluation | Comparison runs stay out of that basis, so a comparison never moves another run's headline. |
| `EvaluationAgent.is_baseline` (the reference agent), the reserved `baseline` tag, `ImprovementProposal.baseline_*` | Taken names: the arm that removes the target is the **no-target** arm (`no_target`). |

## 4. Users and access

Administrators, with exactly the access rules of the evaluation they act on (decision Q3), only where evaluation is available (section 3). The placebo trigger goes through the same access check as `POST /evaluations/{id}/trigger`, on every surface: REST and the evaluation agent tools (workspace rule "one access rule, all paths"). The vertical exposes no evaluation surface on the platform MCP server, so neither does the placebo method.

## 5. Concepts

| Term | Definition |
|---|---|
| **Target** | What the placebo replaces: `persona`, `prompts` (all attached Prompts), `skills` (all bound skills) or `skill` (one bound skill, by id). An evaluation may hold several targets. |
| **Arm** | One variant of the agent. `real`: the agent as configured. `placebo`: the target replaced by its placebo. `no_target`: the target removed. One `real` arm is shared by all targets; each target adds one `placebo` and, unless turned off, one `no_target` arm. |
| **Trial** | One pass of the dataset by one arm. Each arm runs `trials_per_entry` trials; **each trial is one evaluation run** (an entry runs once per run in the vertical: one episode per run and entry). |
| **Comparison** | One placebo launch: every arm x trial of every target for every agent x model of the evaluation, plus the statistics computed once every run is finished. |

Example: 1 agent, 1 model, targets `persona` and `skills`, 12 entries, 5 trials = 5 arms (real, placebo-persona, no-target-persona, placebo-skills, no-target-skills) x 5 trials = 25 runs, 300 agent executions.

## 6. Functional requirements

### 6.1 Configuration

- FR-1. An evaluation gains an optional **placebo configuration**: targets, `include_no_target` (default `true`), `trials_per_entry` (1-30, default 5), optional `custom_text`.
- FR-2. Targets `prompts`, `skills` and `skill` require `execution_mode = full_loop`: in `llm_only` the engine sends the persona alone (no Prompts, no `load_skill` tool), so these targets would measure nothing. `persona` works in both modes. Saving an invalid combination returns a **422**.
- FR-3. A `skill` target must reference a skill bound to every agent of the evaluation that it applies to; an agent that lacks it gets no arm for that target, and the comparison says so. A `prompts` / `skills` target on an agent with no Prompts / skills, and every target on a model-independent agent (`skip_llm`: no model call to influence), produce no arm for that agent, with the same note.
- FR-4. Duplicate targets, an empty target list, more than 20 targets, `skill` together with `skills`, and `trials_per_entry` outside 1-30 are 422.
- FR-4b. Every write path carries the configuration: create, update (`null` removes it, leaving it out keeps it, and the FR-2 check runs against the stored row too, whenever the update sends `placebo_config` or changes `execution_mode`), copy, export and import. An import drops `skill` targets, whose ids name skills of the exporting instance, says so in its response `warnings`, and refuses an invalid configuration before importing anything. A `custom_text` holds at most 20,000 characters (the corpus repeats past its end, so a longer text adds nothing, and it is served with every evaluation).

### 6.2 Placebo construction

Common rules (decisions Q6-Q12):

- FR-5. **Same mechanism.** The placebo enters the conversation exactly where the original does:
  - `persona`: the placebo text replaces the whole persona (decision Q7). Guardrails, output format, the evaluation framing (`framing.py`) and every other system section stay as they are.
  - `prompts`: one placebo Prompt per attached Prompt, same count, each with a neutral name, a neutral description and a neutral body, injected through the same prompt header as real Prompts (`prompt_fragment`).
  - `skills` / `skill`: one placebo skill per replaced skill, with a neutral name and description (the always-visible catalog entry) and a neutral body (returned by `load_skill`). A pinned (`always_load`) skill is replaced by a pinned placebo. Real names are never reused: they would reveal the skill's intent (decision Q6).
- FR-6. **Same length, in tokens of the run's model** (decision Q8), within **±5 %**, measured separately for each part: listing (name + description), body, persona, Prompt name + description, Prompt body.
  - Measurement: a probe call on the run's model with the text as system message, minus a probe with an empty system message; the provider-reported input tokens difference is the text's length. Probes are cached by text within one placebo build and run at most 4 at a time. The launch checks the placebo in characters (the leakage refusal needs no model); each placebo run builds it in its model's tokens before its first entry and stores it (FR-30).
  - The placebo is built to the original's character length (whole sentences, then words up to the closest word boundary), then rescaled until its measured token length is within ±5 % (at most 3 rounds); the closest attempt wins. A part of a few units cannot always land within ±5 % at word granularity: it keeps the closest attempt and raises the `length_out_of_tolerance` warning.
  - When the provider reports no token usage, the fallback is characters at ±5 %; the snapshot records `measured_in: "chars"`.
- FR-7. **Source.** A versioned neutral corpus shipped with the product (`backend/app/evaluations/placebo_corpus/`, one file per language, plus neutral skill / Prompt names and descriptions). Passages are selected deterministically from a seed derived from the comparison id and the target, so a replay rebuilds the same placebo. The corpus version and the seed are stored. An administrator may supply a `custom_text` instead; it goes through the same length matching and the same checks (decision Q9).
- FR-8. **Content.** The corpus is purely descriptive (decision Q12): it restates what the agent already knows about its environment and adds no direction on method, verbosity, tool choice, output format or reply language. Any corpus change is a new corpus version, reviewed as data.
- FR-9. **Language.** The placebo is in the original's language (decision Q10): English and French at least, detected deterministically on the original text by function words. An undetermined language falls back to English and the run carries a warning.
- FR-10. **No leakage.** The placebo must contain none of the distinctive words of what it replaces (decision Q11): words of the agent name, and of the names and descriptions of the replaced Prompts / skills (letters only, case and accents folded, function words and words under 3 letters excluded; exact word forms). Every corpus passage, name and description carrying one is passed over before selection. When nothing is left to draw from, the launch is **refused** before any run is created, naming the target and the words. A `custom_text` with a hit is refused at once. A last check on the built text catches a word left distinctive by a cut.

### 6.3 Execution

- FR-11. **Manual only** (decision Q21). The Run menu offers "Run placebo comparison" next to "Run". Scheduled and agent-change triggers ignore the placebo configuration. While a comparison runs, the scheduler skips the evaluation like any evaluation with active runs.
- FR-12. **Pre-launch estimate** (decision Q13): runs, agent executions, judge calls and probe calls, and a token estimate from the mean `performance.tokens.total` per entry of the last completed non-comparison run of the same agent x model (no history: counts only).
- FR-13. **Execution cap** (decision Q19): a comparison above 2,000 agent executions is refused with a 422 stating the count and the cap. The launch's execution deadline scales with the comparison: the evaluation's timeout per wave of `EVALUATION_MAX_CONCURRENT_RUNS` runs, plus the vertical's grace.
- FR-14. **Tools that can change data** (decision Q20): in `full_loop`, an agent whose tool approval is off in the evaluation (`require_tool_approval`) and that has tool sources - integrations, MCP servers, custom tools, built-in suites - runs them up to `arms x trials` times more often. The launch is refused unless the request carries `confirm_write_tools`; the estimate and the refusal list the sources per agent.
- FR-15. **Same conditions.** Every arm uses the same model, temperature, reasoning effort, max iterations, tools, knowledge bases, extractor, metric selection, prompt-guard and tool-approval settings, framing and dataset version; only the target differs. A control run overlays its placebo on the run's snapshot overrides: `persona` and `prompt_fragment` as today, and `skill_substitutions` (placebo skills replacing bound ones in the catalog, the pinned bodies and `load_skill`) next to `skill_versions`. Replays of a control run rebuild the same overlay from its snapshot.
- FR-16. **No reuse.** Every run of a comparison runs with `reuse = off` and stores `is_reused = false`; the input fingerprint additionally hashes the arm, the placebo snapshot and the trial index, so no later reuse-on run can take a control run's answer for a real one.
- FR-17. **Interleaving** (decision Q17): the launch adds the runs trial by trial, arms interleaved in an order hashed from the comparison's seed, and the dispatcher claims them in that order, so a provider slowdown hits every arm alike. An evaluation in `sequential` run mode runs one run at a time, which the comparison's warnings say (`sequential_run_mode`).
- FR-18. **Quota** (decision Q22): a control run is accounted exactly like a real evaluation run. In the vertical, evaluation entries are not charged against the agentic quota; the placebo method changes nothing there.
- FR-19. **Excluded results.** A result the vertical leaves out of every aggregate (`counts_in_headlines`: transient and platform errors, judge failures) is left out of the comparison too; the comparison reports the excluded count per arm and warns (`unbalanced_exclusions`) when the arms' excluded shares differ by more than 10 points. A run with no counted result fails as in the vertical.
- FR-20. **Unsupported launches.** A comparison cannot run in `await_callback` mode (an external system would have to answer every arm and trial), and a control run cannot be replayed alone through `trigger-from-runs` or used as a persona-rewrite basis (422).

### 6.4 Judging and measures

- FR-21. **Blind judge** (decision Q15): the judge input is identical across arms and holds no arm, target or persona; a test pins it.
- FR-22. **Measures** (decision Q14), all read from the vertical's stored metrics, per result:
  - **quality**: the evaluation's Overall Quality selection (`overall_quality_metrics`, or the family's defaults), each metric rolled up as the suite declares it (mean, pooled, macro) with no spread penalty, then the equal-weight mean the headline uses;
  - **token cost**: `performance.tokens.total` (normalised agent tokens, reasoning excluded);
  - secondary: the judge score when the judge is on (`judge_enabled`), `performance.latency.wall_clock`, `performance.counts.tool_calls`, and for a skill target whether `load_skill` was called on the target skill or its placebo.

### 6.5 Statistics and verdict

Decision Q16: the skill-placebo method, computed per agent x model x target, in pure functions next to `aggregation.py` (`services/evaluation/placebo_stats.py`).

- FR-23. **Effects.** Quality difference `D = quality (real) - quality (placebo)`, in points, each arm's quality rolled up over all its counted entries x trials as FR-22 says. Token ratio `R = mean tokens (real) / mean tokens (placebo)`; below 1, the real agent is cheaper. The same statistics are reported for real vs no-target and placebo vs no-target.
- FR-24. **Intervals.** Cluster bootstrap over dataset entries: resample entries with replacement, keep every trial of both arms for each drawn entry, recompute both arms' roll-ups, 10,000 resamples, seed stored with the comparison. 95 % percentile intervals; two-sided p-values from the same bootstrap distribution (null `D = 0`, `R = 1`).
- FR-25. **Multiple comparisons.** Holm correction across the targets of one agent x model, separately per outcome. Tables show unadjusted intervals; verdicts use the adjusted p < 0.05.
- FR-26. **Verdict** (real vs placebo). An outcome *improves* when adjusted p < 0.05 in the good direction (`D > 0`, `R < 1`), *worsens* in the bad direction.

  | Verdict | Rule |
  |---|---|
  | Beats placebo | at least one outcome improves, none worsens |
  | Trade-off | one outcome improves, the other worsens |
  | Worse than placebo | at least one outcome worsens, none improves |
  | No better than placebo | nothing improves or worsens |

  When "beats placebo" rests on tokens alone, the lower bound of `D` is shown next to it.
- FR-27. **Small samples.** Fewer than 10 entries shows a "few entries, read with care" warning; fewer than 3 entries, or a run that did not complete, gives no verdict ("not computable"). A comparison is never shown as concluded while a run is pending, failed or cancelled.

### 6.6 Results display

Decision Q23: inside the evaluation, with its existing views, and a placebo tag.

- FR-28. Every run of a comparison (real arm included) carries its comparison and arm. Activity lists them, grouped by comparison and tagged `Real`, `Placebo · <target>` or `No target · <target>`; the run detail and the run comparison page show the tag. They stay out of Overall Performance's basis (they get no performance headline), out of the Leader cards, the top-10 tables and the trend charts: those rank candidate configurations, and a comparison would flood them with trials of three variants.
- FR-29. The Overview gains a **Placebo comparison** block per comparison: one row per agent x model x target with `R` [CI], `D` [CI], the verdict, the counted entries x trials per arm, and the warnings of FR-3, FR-9, FR-19 and FR-27.
- FR-30. The run detail of a control run shows the target, the placebo texts used (plain text, never markdown or HTML), the measured lengths and deviations, the corpus version, the seed and the detected language.

### 6.7 Agent tools

Decision Q25: same capability, same access rule.

- FR-31. `trigger_evaluation` gains a `placebo` option (and the write-tools confirmation of FR-14); `get_evaluation` returns the placebo configuration; a new read tool `get_placebo_comparison` returns the FR-29 table and joins `EVALUATION_TOOL_NAMES`. Each is tested as non-admin, `read_only`, `config`, group grant, revoked share and company-managed.

### 6.8 Reproducibility

- FR-32. Each control run stores a placebo snapshot: arm, target, corpus version, seed, language, and per replaced element its original reference (id + version), the placebo name / description / body, both lengths, unit and deviation, and the leakage-check result.

## 7. Data model

| Change | Purpose |
|---|---|
| `evaluations.placebo_config` JSON, nullable | FR-1 |
| `evaluation_runs.arm` String(20), server default `real` | FR-28 |
| `evaluation_runs.trial_index` Integer, server default 0 | FR-17, FR-24 |
| `evaluation_runs.placebo_target` JSON, nullable | the target of a control run |
| `evaluation_runs.placebo_snapshot` JSON, nullable | FR-32 |
| `evaluation_runs.placebo_comparison_id` UUID, nullable, no foreign key (as `owner_execution_id`) | groups the runs of one comparison |
| new `evaluation_placebo_comparisons` (id, evaluation_id `ON DELETE CASCADE`, status, seed, corpus_version, config, created_by, created_at, completed_at, results, warnings) | the launch's frozen configuration and FR-23 to FR-29 |

The per-trial measures need no column: they are the vertical's `evaluation_metrics` rows. Migrations chain on the vertical's head (`e1780evp001`), single head, idempotent, lock-safe (`dev-docs/reference/migrations-safety.md`). Existing runs read `arm = real`, `trial_index = 0`, no comparison: no behaviour change.

## 8. API

| Method | Path | Notes |
|---|---|---|
| `POST` / `PUT` | `/api/v1/evaluations`, `/api/v1/evaluations/{id}` | accept `placebo_config` (422 on FR-2 / FR-4) |
| `POST` | `/api/v1/evaluations/{id}/placebo/estimate` | FR-12 counts and estimate, on the models the trigger resolves; returns the `seed` the trigger takes back |
| `POST` | `/api/v1/evaluations/{id}/trigger` | `TriggerEvaluationRequest` gains `placebo: bool`, `confirm_write_tools: bool`, `placebo_seed` (an estimate's seed); 422 on FR-10 / FR-13 / FR-14 / FR-20 |
| `GET` | `/api/v1/evaluations/{id}/placebo-comparisons` | paginated list |
| `GET` | `/api/v1/evaluations/placebo-comparisons/{comparison_id}` | FR-29 table |

All on the evaluation router, so behind `require_evaluation`. Errors returned to the caller name the rule that failed (cap, missing confirmation, leaked word and target); no internal detail.

## 9. Edge cases

| Case | Behaviour |
|---|---|
| Agent with no persona | `persona` target: no arm for that agent; noted in the comparison |
| A skill unbound, or a Prompt detached, between configuration and launch | the launch snapshot is authoritative; a missing element drops that agent's arm with a note |
| Model-independent agent (`skip_llm`) | no arm (FR-3) |
| Very short original | the placebo is cut at the word, and the deviation is stored |
| Very long original (longer than the corpus) | passages in seed order, then repeated; the snapshot says so (`corpus_repeated`) |
| Mixed-language original | the dominant language wins; undetermined → English + warning |
| Run cancelled mid-comparison | the comparison is `cancelled`, no verdict |
| Evaluation edited during a comparison | the comparison uses its launch-time snapshots and frozen configuration |
| `include_no_target = false` | only real vs placebo is reported |
| Evaluation turned off (`EVALUATION` off or XTM One mode) | nothing placebo is reachable; stored comparisons are kept, like every evaluation row |

## 10. Test plan

- Unit: length matching (rounds, ±5 %, chars fallback); deterministic selection; language detection; leakage table; placebo skill set (count, pinned flags, neutral names); bootstrap on known distributions (seed determinism, `D = 0` and `R = 1` cases), with pooled and macro metrics rolled up by `reduce_grouped_metrics`; Holm; the verdict table.
- Integration (`pytestmark = pytest.mark.integration`): the launch creates exactly `arms x trials` runs per agent x model, owned by one execution, `reuse = off`; a spy on `run_agent_chat` asserts each arm's overrides (persona, prompt fragment, skill substitutions) and that the real arm gets none; a spy on the judge asserts identical judge inputs across arms; comparison runs leave Overall Performance of the other runs unchanged; 422 for every invalid configuration and unsupported launch; cap; write-tools confirmation; access on the real route and the agent tool for non-admin, `read_only`, `config`, group grant, revoked share and company-managed; `test_evaluation_feature_flag.py` covers the new routes and tool; `test_evaluation_guards.py` stays green (ownership, metric-package boundary).
- Frontend: `yarn check` (i18n in the four catalogs); component tests next to the components (`*.test.tsx`, `renderWithProviders`); a screenshot of each new control.

## 11. Delivery

A stack on the `1780-featevaluation-evaluation-pipeline-v1-on-xtm-one-without-mlflow` branch (pull requests based on it), each level green on its own (decision Q28). The first stack, on `main`, is kept in draft as a reference until its replacement is up.

1. PRD, configuration and its validation on every write path, placebo construction (corpus, length matching, language, leakage), schema.
2. Execution: the launch plan's arms and trials, run creation and interleaving, overrides including skill substitution, no reuse, performance-basis exclusion, estimate, cap and deadline, write-tools guard, trigger API, unsupported launches.
3. Statistics on the stored metrics, comparisons table and API, agent tools.
4. Frontend (configuration card, run dialog, arm tags, comparison block, run detail panel) and documentation: `dev-docs/evaluations.md`, user docs.

## 12. Decision log

| # | Question | Decision | After the vertical |
|---|---|---|---|
| Q1 | Arms | Real, placebo and an arm without the target | The third arm is named `no_target`: "baseline" is the vertical's reference agent |
| Q2 | Product shape | An option on the Evaluation, adding an arm dimension to the agent x model matrix | Arms and trials are runs of one launch |
| Q3 | Users | Administrators, with the evaluation's access rules | Only where evaluation is available (`EVALUATION`, every mode where AI runs) |
| Q4 | Targets | Persona, attached Prompts, skills | |
| Q5 | Granularity | Each skill separately or all skills at once; persona and Prompts are separate targets | |
| Q6 | Skill placebo | Neutral name and description, same count, same lengths | |
| Q7 | Persona placebo | The whole text | |
| Q8 | Length | Tokens of the run's model, ±5 %, chars fallback, stored with the run | |
| Q9 | Source | Versioned corpus, seeded selection; optional custom text; no LLM generation | |
| Q10 | Language | The original's language, EN and FR at least, detected automatically | |
| Q11 | Leakage | Checked; the launch is blocked on a hit | |
| Q12 | Corpus content | Purely descriptive | |
| Q13 | Trials | 5 by default, up to 30, with a pre-launch estimate | One run per trial |
| Q14 | Measures | Quality and tokens; duration, tool calls, skill fired as secondary | Read from the vertical's metric suite: Overall Quality selection, `performance.tokens.total` |
| Q15 | Judge | Blind to arm and persona | |
| Q16 | Statistics | The skill-placebo method | Roll-ups through `reduce_grouped_metrics` |
| Q17 | Bias control | Interleaved arms, same settings | Interleaved run order; no reuse |
| Q18 | Execution mode | `full_loop` required for Prompts and skills targets | |
| Q19 | Matrix size | No single-agent restriction; an execution cap is shown and enforced | Deadline scaled with the run count |
| Q20 | Tools that change data | Confirmation required | Keyed on the evaluation's tool-approval setting |
| Q21 | Triggers | Manual only | |
| Q22 | Quota | Counted like any evaluation run | The vertical charges no evaluation entry: neither real nor control |
| Q23 | Display | Inside the evaluation with its existing views, with a placebo tag | Comparison runs are out of Leaders, top-10 tables, trend charts and Overall Performance (they would flood them and move other runs' headlines); Activity groups them |
| Q24 | Badge | No | |
| Q25 | Platform tools / MCP | Yes | Agent tools only: the vertical has no evaluation MCP surface |
| Q26 | PRD location | `dev-docs/` (this file) | |
| Q27 | Existing issue | None | |
| Q28 | Delivery | A stack of 4 | On the evaluation vertical's branch |

## 13. Assumptions (validated 2026-10-24)

1. **Q15.** The judge receives neither the persona nor the arm; a test pins it.
2. **Q20.** Confirmation is required when the agent's tool calls run unapproved.
3. **Execution cap.** 2,000 agent executions per comparison, a constant for v1.
4. **Language detection.** A deterministic function-word count over each corpus, no new dependency.

## 14. Implementation

- **Configuration.** `placebo_config` is one of the evaluation's settings (`services/evaluation/settings.py`): create, copy, export and import carry it, and an update's `null` removes it. `PlaceboConfig` (`services/evaluation_placebo.py`) validates it on create and on import, through `CreateEvaluationRequest`; an update checks it against the execution mode on the row as the update leaves it, when it sends a configuration or changes the mode (Prompts and skills targets need `full_loop`). An import drops its `skill` targets, whose ids name skills of the exporting instance, and lists them in the response's `warnings` and the audit row.
- **Launch.** `POST /trigger` with `placebo: true` goes through `launch.py` like every launch: `services/evaluation/placebo_launch.py` plans one run per agent x model x arm (`real`, `placebo`, `no_target`) x trial, refuses (422) a launch with no configuration, a mode or callback mode that cannot run it, no control arm, more than 2,000 agent executions or a placebo that would carry a word of what it replaces, and asks `confirm_write_tools` when the evaluation runs tool calls unapproved. One `evaluation_placebo_comparisons` row freezes the configuration; `add_launch_runs` creates the runs trial by trial, arms in an order hashed from the comparison's seed, and the dispatcher claims them in creation order. The execution's deadline scales with the waves of runs. `POST /placebo/estimate` returns the same plan on the models the trigger resolves (`_launch_model_selection`) and writes nothing; its `seed`, sent back as the trigger's `placebo_seed`, launches the placebos it checked. A comparison's `warnings` are objects with a `code` (`sequential_run_mode`, `target_skipped`).
- **A control run** (`services/evaluation/placebo_runs.py`) builds its placebo from its own version snapshots, with the owner's variables resolved as the model reads them, measured in its model's tokens by a probe (characters when the provider reports no usage), and stores it with what the probes cost (`probe_usage`); the other trials of its arm take it. Its overrides overlay the snapshot's: `persona`, `prompt_fragment` (present and empty: no attached Prompt), `skill_substitutes` (placebo skills in the catalog, pinned bodies and `load_skill`) or `skill_ids_removed`.
- **No reuse.** A comparison's runs never reuse an answer, and a control run's fingerprint carries its arm, trial and placebo, so no later run reuses it as the agent's own.
- **Out of the headline basis.** Comparison runs get no Overall Performance and stay out of the performance basis, the chart and persona-rewrite baselines; a comparison's run is never re-run alone (`trigger-from-runs`, replay: 422) and an export leaves it out, its bundle carrying no arm.
- **Settled with its statistics** (`services/evaluation/placebo_comparisons.py`). A comparison settles once none of its runs is pending or running: `completed` when all completed, else `failed` or `cancelled`. The execution settles the comparisons of its batch when it ends, and every read (the routes, `get_placebo_comparison`) settles any it finds finished. Settling computes `results` (`services/evaluation/placebo_stats.py`, pure, off the event loop): per agent x model x target, `D` (Overall Quality, each metric rolled up as `reduce_grouped_metrics` rolls a run up, no spread penalty) and `R` (`performance.tokens.total`), real vs placebo, real vs no target and placebo vs no target, with a cluster bootstrap over entries (10,000 resamples, seeded), Holm across the targets of one agent x model and the verdict; each arm's plain means of the judge score, latency, tool calls and, for a skill target, how often `load_skill` opened it. A cell whose runs did not all complete, with fewer than 3 entries or no quality has no verdict. Deleting one of its runs cancels a comparison (`run_deleted`); deleting every run of the evaluation removes its comparisons.
- **Read.** `GET /{evaluation_id}/placebo-comparisons` (newest first, paged) and `GET /placebo-comparisons/{comparison_id}`, admins only; the agent tools `trigger_evaluation` (`placebo`, `confirm_write_tools`), `get_evaluation` (`placebo_config`) and `get_placebo_comparison` (in `EVALUATION_TOOL_NAMES`, on the HITL read-only list), which keeps to the caller's tiers in strict mode.
- **Pages** (`components/evaluation/Placebo*`, `lib/placebo.ts`). The Configuration tab's `Placebo control` card saves `placebo_config` on its own. The Run menu's `Run placebo comparison` opens the estimate dialog, which shows the refusals and warnings, asks the write-tools confirmation and sends the estimate's seed back with the trigger. The Overview shows the comparisons block above the rankings and reads its runs with `exclude_comparisons`, so a comparison's runs (real arm included) neither rank in the leaders and the top 10 nor fill its page. Activity, the run detail and Compare tag each comparison run with its arm (a single skill by its name); Activity never re-runs or re-scores one, and the run detail shows a placebo run's snapshot. A comparison run answers `can_replay: false` with the refusal as its reason, and the rescore routes refuse it (`placebo_comparison_run` in a bulk rescore): a re-scored trial would leave its comparison's statistics on the scores it settled with.
