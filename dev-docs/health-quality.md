# Quality Analyst and Experimenter (Platform Health, level 6)

Issue #8259, epic #1783. PRD: `dev-docs/platform-concierge.md` sections 6.5 and 6.6.

The Quality Analyst reads the quality signals of the agents in Platform Health's supervision scope and records what it finds; the Experimenter measures a variant on sandbox copies and files the proposal (level 3) the measurement earned. Neither writes an agent, a skill or an assignment: a variant lives in its experiment row and in sandbox copies, and the only path to production is the proposal its managers approve.

## Findings

`concierge_findings` (`models/concierge_finding.py`, migration `c6cnc8254061`), `services/concierge/findings.py`.

- A finding is one pattern on one target (`agent`, `assignment`, or `flow`, read through the rule level 7 registers): its `fingerprint` is `sha256` of the target, the kind and the normalized key, never of the evidence. Recording it again merges the evidence (ids newest first, 20 per kind), bumps `occurrences` and `last_seen_at`, and answers `new=false`; a `resolved` finding that recurs reopens, a `dismissed` one stays silent.
- `content_evidence`: the finding was recorded with content access (the agent opted in), whatever it cites. It is sticky: a revoked opt-in redacts the hypothesis, the suggested experiment and the trace and rating ids at the next read (`access.redact_finding`).
- `window_counts` gives a cycle's new and known findings (level 8's digest).

## Signals and evidence

`services/concierge/diagnosis.py`, no model call:

- `quality_signals`: the agent's window and the one before, from the stored run rollups read through the trace read store (PostgreSQL or ClickHouse; `SAMPLE_RUNS` per window, `sampled` when cut): failure rate and causes, tool calls and errors per tool, cost and tokens per run, p50/p90; the runs that ended failed or cancelled before their loop started (no trace: read from `assignment_runs`, `started_at` and `llm_trace_id` unset, a run a listed trace names counted once) are added to `runs` and `failed` and served apart as `failed_before_start` with `failed_before_start_causes` (`deadline_before_start`, `other`) (#1416); its ratings (users and platform reactions apart), criteria, the latest negatives; its open findings. Failed trace ids, the run ids of the runs failed before start (`failed_before_start_run_ids`) and rating comments only with content access, never from an opted-out user, each comment audited through level 1's `require_content_read` before it reaches the model.
- `verify_evidence`: the refs the rows confirm for the agent, never the argument: its ratings (`annotations.agent_rating_raters`), its assignment runs, its traces with content access, nothing of an opted-out user.

## Experiments

`concierge_experiments` (`models/concierge_experiment.py`, migration `c6cnc8254062`), `services/concierge/experiments.py`.

- Each change type registers an `ExperimentKind` (`register_experiment_kind`): `prepare_draft`, `check_stale`, `dataset_target`, `subjects`, `proposal_content`. Level 6 registers `persona`, `skill` and `assignment_prompt` (`text_kinds.py`, the #1044 rules of `variants.py`); level 7 its flow types. The run, settle and file steps are the same for every kind.
- Draft: one open experiment per finding, change and target. A skill variant only on a skill whose managers all read the agent's conversations (a proposal on the skill goes to them), never a skill synced from its source or with bundled files.
- Run (`experiments.run`): refused outside a health cycle (nothing could book its spend); the base read again (a human edit makes it `stale`); no consent, no measurement (`skipped`, `no_consent`); the budget of the run's health cycle read first and the cases sized to it (`size_to_budget`: two arms, the judge's quarter on top; a cost cap with no measured cost per run launches nothing); a dataset of the target's consented production cases (level 4); two sandbox copies (level 2) evaluated on the same cases. The evaluation reserves the sizing on the cycle and holds it past the cycle's close until it charges what its runs measured (`evaluations.settle_concierge_evaluations`, which also stops its runs past a cap). A refused or failed launch reserves nothing and is `skipped` with its reason.
- Settle (`settle`, on Platform Health's tick or when the Experimenter reads the experiment): once both arms ended, the comparison (quality with its 2-point margin, cost, latency) and the writes the sandbox answered per case (`result["simulated_writes"]`, level 8's digest). An arm with no verdict states why: stopped at a cap (`incomplete`, `budget_reached_mid_run`), entries withdrawn (`incomplete`, `no_consent`), otherwise `failed`, `evaluation_unavailable`.
- File (`propose`): `progression`, or `neutral` with a shorter text or a cost per run 5 % lower; a regression or a neutral result with no gain files nothing; `skipped`, `incomplete` or `failed` with a known reason files `unmeasured`, stating it. Never over a stale target. A finding drawn from conversations files only while they are still readable, its refs as the consent rows allow them now.
- `status_counts` counts every status as itself: `skipped` and `incomplete` are never `completed`.
- `quality_deps.py` is the one adapter on levels 1 to 4. It states what level 4 does not offer yet: one run per arm (`MAX_TRIALS = 1`) and no per-arm evaluation overlay (`SUBJECT_OVERLAYS_AVAILABLE = False`), so skill and assignment-prompt variants are skipped as `evaluation_unavailable` for now.

## Access

`services/concierge/access.py`, read on the rows at every call, on every surface:

- Platform Health: a call is Platform Health's only when the agent calling the tool is the bound principal's own (level 3's `calling_health_principal`); an agent its run executes inline, a sandbox copy's turn, reads as its runner. It reads the agents of its supervision scope (level 1's consent scope, no admin bypass), their content only while opted in. Its run's cycle is read from the run (level 5's `run_cycle_id`).
- A person: the findings, signals and experiments of an agent whose feedback they may read (its managers), their content only with the chats right (`may_read_agent_traces`).
- Writes are Platform Health's, each held by one role: `record_finding` and `draft_variant` the `quality_analyst`'s, `run_experiment`, `propose_from_experiment` and settling the `experimenter`'s.
- An agent out of reach answers the same refusal whether it exists or not.

## Tools

`builtin_tools/health_quality.py` (`concierge_diagnosis`: `get_quality_signals`, `list_findings`, `record_finding`, `draft_variant`) and `builtin_tools/health_experiments.py` (`concierge_experiments`: `list_experiments`, `get_experiment`, `run_experiment`, `propose_from_experiment`), gated on `CONCIERGE`, wired in agent chat and assignment runs (`add_concierge_quality_tools`, with the calling agent and the run). `/mcp/xtm` serves the four reads to the agents' managers. Every value that came from a trace, a rating or a model is returned inside an untrusted-data block; errors are generic, reasons in logs. Seeded skills `concierge-quality-analyst` and `concierge-experimenter` (unbound: level 8 binds them to the fleet).
