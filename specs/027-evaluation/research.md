# Research: Evaluation

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

The decisions behind the requirements of [spec.md](spec.md) that a reader would otherwise question. Each cites the requirements it explains. Defects are in the spec's Known gaps, not here. Paths are under `backend/app/`.

## Datasets and families

### R-01 Frozen dataset versions, pinned per run (FR-009, FR-010)

- **Decision**: every change to a dataset writes an immutable version; a run pins the current version of each linked dataset and reads only from it (`services/evaluation/dataset_versioning.py`).
- **Rationale**: editing or deleting an entry never changes a past run's results or makes two runs incomparable.
- **Alternatives considered**: scoring against the live entries.

### R-02 The answer shape belongs to the dataset (FR-002 to FR-004, FR-011, FR-021)

- **Decision**: family, label vocabulary and answer schema live on the dataset (or entry); the evaluation holds only scoring intent (field overrides, extractors). Any JSON deliverable is `STRUCTURED_JSON`.
- **Rationale**: the shape is part of the ground truth, so every evaluation using a dataset scores it the same way.
- **Alternatives considered**: an evaluation-level output schema; one family per JSON output type (`DETECTION_RULE`, `JSON_ANALYSIS`).

## Configuration

### R-03 Prompt guards and tool approval off by default (FR-022, FR-029, FR-094)

- **Decision**: two per-evaluation switches, off by default, recorded on each run and folded into the fingerprint when on.
- **Rationale**: nobody can approve a tool call during an evaluation, and guard checks add model calls that distort performance.
- **Alternatives considered**: applying the agent's chat gates (guards and approval on).

### R-04 Two execution modes, recorded on the run (FR-019, FR-029, FR-030)

- **Decision**: Full Loop or LLM Only, any other value refused on write, the mode recorded when the run starts; a failed Full Loop entry is a failed entry, never retried in LLM Only (`services/evaluation/run_mode.py`).
- **Rationale**: a run's label must not follow a later edit of the evaluation, and a silent fallback would score a different system.
- **Alternatives considered**: reading the mode from the evaluation at display time; falling back to LLM Only.

### R-05 Closed, declarative extractor catalog (FR-046 to FR-048)

- **Decision**: an extractor is 1 to 8 first-order operations from a fixed set over a fixed source; its output is stored on the result; preview, run and rescore share one pipeline (`services/evaluation/extraction/`).
- **Rationale**: every extractor can be type-checked against the family on save, and a stored extraction tells an agent error from an extractor error.
- **Alternatives considered**: arbitrary code; a higher-order `map_values` operation.

## Running and orchestration

### R-06 Runs execute on the durable orchestration engine (FR-025, FR-037, FR-040)

- **Decision**: a launch is an orchestration execution staged in the same transaction that makes its runs active (`services/orchestration_adapters/evaluation.py`).
- **Rationale**: a run is never committed active without the execution that can finish it.
- **Alternatives considered**: commit then schedule (crash window); a periodic evaluation sweep.

### R-07 A run belongs to the launch that created it (FR-026, FR-039)

- **Decision**: `owner_execution_id` is set with the active status; cleanup, dispatch and failure act only on owned runs, through one module (`services/evaluation/run_ownership.py`).
- **Rationale**: overlapping launches are allowed, so one launch's failure must not fail another's runs.
- **Alternatives considered**: status-only cleanup, which fails a later attempt's runs.

### R-08 A dead worker's run is failed, never re-run; a late completion stands (FR-039)

- **Decision**: the next attempt fails a run a dead attempt left running, with a re-run message; if the stalled worker completes it anyway, the completion wins.
- **Rationale**: partial results are already committed, so re-running would double-count them; a completion is proof, a lease expiry only inference.
- **Alternatives considered**: re-running the run; fencing the stalled worker out.

### R-09 At most 5 runs in flight (FR-033)

- **Decision**: parallel mode is a bounded pool (`EVALUATION_MAX_CONCURRENT_RUNS`, 5); sequential mode is the same pool with a cap of 1.
- **Rationale**: an unbounded fan-out trips provider rate limits, whose retries inflate every run's measured duration.
- **Alternatives considered**: unbounded `gather`; a separate sequential code path.

### R-10 Autonomous mode, never re-prompted (FR-031, FR-036)

- **Decision**: entries carry a directive that no human can answer; an answer that still asks is flagged confirmation-seeking, not re-prompted (`services/evaluation/framing.py`).
- **Rationale**: the run measures what the agent delivers alone; the directive is added on evaluation paths only, so no other prompt grows (invariant 16).
- **Alternatives considered**: none recorded.

### R-11 Two anomaly kinds, treated differently (FR-036, FR-075, FR-097)

- **Decision**: a transient provider error is excluded from every headline and not judged; a confirmation-seeking answer is judged and counted, but never reused.
- **Rationale**: a provider outage says nothing about the agent, while asking instead of delivering is the agent's own failure.
- **Alternatives considered**: excluding both kinds; counting both.

## Quality scoring and the judge

### R-12 Ill-formed and edge answers are scored as answers (FR-049, FR-050, FR-053)

- **Decision**: unparseable JSON, a schema violation, a malformed STIX bundle or an out-of-vocabulary label is `scored` 0 with a diagnostic (an unknown label counts as a false positive); an empty expected label set answered empty is correct.
- **Rationale**: only scored values aggregate, so an answer the agent gave must move the score, never drop out of the run and inflate quality.
- **Alternatives considered**: `invalid` with value 0; withholding the classification metrics on an unknown label.

### R-13 JSON scored per field, by schema-derived modes (FR-054, FR-055)

- **Decision**: each field gets a mode from the answer schema; schema conformance is a separate metric and does not gate field scores; a nested object and each entry of a list of objects count as their leaves, and list entries are paired by optimal assignment, bounded at 10,000 pairs per list and 100,000 per answer (`evaluation_metrics/quality/families/structured_json/`).
- **Rationale**: partial credit per field, and one stray key does not zero an otherwise correct answer.
- **Alternatives considered**: flattened TP/FP/FN over the whole object; greedy pairing, which made the score depend on the entries' order; a list counted as a field and through its entries' fields, so each entry twice.

### R-14 One platform-wide prose normalisation (FR-056)

- **Decision**: NFC, case-folded, whitespace collapsed, and the marks that present a token trimmed (sentence punctuation at its end, brackets and quotes, markdown emphasis), for every PROSE metric and `free_text` field.
- **Rationale**: scores stay comparable across evaluations. A trailing period took token F1 from 100 to 66.7, while `C#`, `-5`, `.NET` and `*.evil.com` are identified by their punctuation: trimming every mark at the edges merged them with `C`, `5`, `NET`.
- **Alternatives considered**: per-evaluation normalisation settings.

### R-15 A judge failure is explicit (FR-060, FR-075)

- **Decision**: a verdict that stays unusable after repair is a judge failure, with no retry and no substitute score, left out of every headline.
- **Rationale**: any substitute reads as a measured score and moves the headline.
- **Alternatives considered**: filling 50; filling 0; retrying.

### R-16 One verdict schema for enforcement, fallback and repair (FR-057, FR-060)

- **Decision**: the same schema drives the provider's native structured output, the prompt fallback and repair-aware parsing; the prompt describes it only when native enforcement is missing.
- **Rationale**: one definition cannot drift between paths, and the judge prompt does not grow where the provider enforces the schema (invariant 16).
- **Alternatives considered**: describing the schema in every judge prompt.

## Performance measurement

### R-17 Nine performance metrics (FR-063)

- **Decision**: the suite keeps only the nine metrics that tell agents apart.
- **Rationale**: any other candidate is never computed, duplicates a kept metric (iterations vs model calls) or moves with completion rate.
- **Alternatives considered**: keeping the full set of 19.

### R-18 Work counted along the whole chain, all or nothing (FR-068 to FR-070)

- **Decision**: tokens, cost, calls and retries come from each run's own trace spans across handovers and delegated tasks, each model at its own price; an incomplete chain gives unavailable.
- **Rationale**: progress events miss helper agents' work, and a partial sum reads as a cheap agent.
- **Alternatives considered**: rebuilding totals from progress events; reporting the partial sum.

## Headlines and aggregation

### R-19 Three separate headlines (FR-073)

- **Decision**: Judge Quality, Overall Quality and Overall Performance are never blended into one score or one winner.
- **Rationale**: a blend of an absolute quality and a comparative performance has no meaning a reader can act on.
- **Alternatives considered**: a weighted overall score; a single ranking.

### R-20 Unmeasured is N/A, never 0 (FR-049, FR-064, FR-074)

- **Decision**: a missing value is `None` end to end and left out of rankings and charts.
- **Rationale**: 0 reads as the worst real result.
- **Alternatives considered**: defaulting missing values to 0.

### R-21 Quality absolute, performance comparative (FR-080 to FR-083, FR-089)

- **Decision**: Overall Quality uses the package's 0–100 values of one run; Overall Performance normalises each metric across all completed runs of the evaluation and refreshes every run on any change (`services/evaluation/aggregation.py`).
- **Rationale**: milliseconds or dollars only mean something next to sibling runs; a basis narrower than the evaluation gives false 100s.
- **Alternatives considered**: a fixed rescale of performance units; a per-launch basis.

### R-22 Shared metrics only, with abstention (FR-081, FR-086)

- **Decision**: Overall Performance uses the metrics every measuring run has; a run that measured none abstains instead of shrinking the set.
- **Rationale**: a run with one flattering metric must not outscore a fully measured one, and old runs without telemetry must not veto measured ones.
- **Alternatives considered**: per-run metric sets; letting every run constrain the intersection.

### R-23 Tolerance band relative to the best value (FR-082)

- **Decision**: values within 5 % of the best score 100; when the best is 0 or less, the band is taken from the span of values.
- **Rationale**: noise-level differences do not split runs, and a best value of 0 does not collapse the band.
- **Alternatives considered**: a span-based band everywhere (re-ranks stored evaluations).

### R-24 Direction-aware spread penalty for averaged metrics (FR-077)

- **Decision**: lower-is-better quality values are flipped to 100 − v before averaging; the penalty k × σ always moves toward worse.
- **Rationale**: a consistent agent beats an erratic one with the same mean, and an unflipped lower-is-better metric would invert.
- **Alternatives considered**: a plain mean; penalising before orienting the values.

### R-25 Rates counted inside entries are pooled (FR-078, FR-091)

- **Decision**: completion rate and tool error rate sum numerators and denominators over entries, with no penalty, and give no value when an entry lacks its counts.
- **Rationale**: averaging per-entry rates gives 0.375 where 5 of 11 calls failed.
- **Alternatives considered**: a penalised mean of rates; pooling only the entries that have counts.

### R-26 Default contributors are an explicit list of answer measures (FR-084)

- **Decision**: an allowlist of measures of the answer (FR-084); every other metric, and `performance.latency.ttft`, counts only when selected.
- **Rationale**: a validity check repeats the 0 an invalid answer already scores (an empty valid STIX bundle read 40); a stricter or redundant view doubles a default's weight; TTFT on a short answer double-weights latency. An allowlist keeps a new metric from silently changing every unconfigured headline.
- **Alternatives considered**: every computed metric contributes unless excluded (the earlier rule).

### R-27 Every exclusion recorded with its reason (FR-085 to FR-088)

- **Decision**: each run stores per headline its contributors and its excluded metrics with a closed reason, including `not_a_default_contributor` and `missing_from_other_runs`; surfaces read this record.
- **Rationale**: an unexplained N/A or a silently dropped metric misleads; deriving it at read time lets surfaces disagree.
- **Alternatives considered**: disclosing only under an explicit selection; recomputing provenance at read time.

## Reuse and rescore

### R-28 Fingerprint of answer-shaping inputs only; scores always recomputed (FR-094, FR-095, FR-098)

- **Decision**: reuse matches a SHA-256 of the inputs that shape the answer; scoring settings are left out, and a new setting enters only when on (`services/evaluation/fingerprint.py`).
- **Rationale**: a scoring-only change reuses every answer, and adding a setting keeps existing fingerprints matching.
- **Alternatives considered**: caching scores; always including every setting key.

### R-29 Reuse candidates re-checked, not trusted (FR-096, FR-097)

- **Decision**: a source must be a fresh, non-anomalous, non-error result of a completed run, and its stored answer is run through the detectors again.
- **Rationale**: older rows carry no flag, and asking instead of delivering is stochastic, so one bad answer must not be locked in.
- **Alternatives considered**: trusting the stored flags.

### R-30 A reused entry keeps its performance metrics (FR-072, FR-099)

- **Decision**: performance describes the source execution and counts in Overall Performance; Run Duration covers only the judge.
- **Rationale**: stripping them would drop reused runs from the comparison, while the agent time belongs to the source run.
- **Alternatives considered**: stripping performance from reused results; omitting Run Duration.

## Comparison

### R-31 Tags merged at read time; baseline is an agent flag (FR-104 to FR-106)

- **Decision**: effective tags merge run over agent over evaluation when read; the `baseline` key is reserved and baseline is a flag on the evaluation's agent (`services/evaluation/tags.py`).
- **Rationale**: a tag edit applies to past runs, and baseline has one source of truth.
- **Alternatives considered**: copying tags onto the run at creation; a `baseline` run tag.

### R-32 Agent-tool comparison is arithmetic with a 2-point margin (FR-111)

- **Decision**: per score, the candidate minus the baseline, neutral within ±2 points (`services/evaluation_comparison.py`).
- **Rationale**: the self-improvement loop needs a deterministic verdict, and a judge re-scoring the same answers moves about that much.
- **Alternatives considered**: letting a model read the two runs' scores.

## Export and import

### R-33 A package is history, not work (FR-113, FR-115, FR-117)

- **Decision**: results carry flags and `is_reused` but no fingerprint or reuse source; import turns a non-terminal run into failed and derives metric rows from `execution_details`; imported runs cannot be replayed.
- **Rationale**: an import must not create in-flight runs, reuse sources or a second source of truth for scores on the target instance.
- **Alternatives considered**: keeping pending or running statuses; exporting metric rows.

## Package boundary

### R-34 Vendored metric suite behind one adapter, failing closed (FR-051, FR-061, FR-062)

- **Decision**: `evaluation_metrics/` ships with the backend, computes per-entry metrics only, and only `services/evaluation/metric_package/` imports it (`tests/test_evaluation_guards.py`); absence or drift fails scoring.
- **Rationale**: one owner for metric semantics, versioned per run, and never a partial metric set.
- **Alternatives considered**: an editable install of an external package; aggregating inside the package.

### R-35 Direction, unit and roll-up read from package metadata (FR-063, FR-077 to FR-079)

- **Decision**: one walk of the package registry feeds directions, units, roll-ups and the catalog; a metric with an unknown or conflicting declaration composes no headline (`metric_package/registry.py`).
- **Rationale**: a guessed orientation silently inverts a headline.
- **Alternatives considered**: a local direction list; inferring from the key prefix; defaulting to higher-is-better.
