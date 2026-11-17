/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * What a concept of the evaluation pages means, said once for every page that shows it:
 * a tooltip reads its sentence from here, so two pages cannot explain the same
 * thing differently. A concept shown on one page only keeps its text on that page.
 *
 * Vocabulary, used the same way in every sentence:
 * - a **launch** starts an evaluation once; it creates one **run** per agent and model
 *   (and per arm and trial in a placebo comparison);
 * - a run answers every **entry** of the linked datasets; the agent's **answer** is
 *   compared with the entry's **expected answer**;
 * - the **output family** is the shape every expected answer takes, and decides the
 *   deterministic quality metrics;
 * - the **LLM judge** scores answers on five sub-scores; **judge instructions** guide it;
 * - Judge Quality, Overall Quality and Overall Performance are the three overall scores
 *   (`HEADLINE_NOTES` in `evaluation-headlines.ts`).
 *
 * Read by `t_i18n`: every value is a catalog key. @i18n-keys
 */
export const EVALUATION_TERMS = {
  // The baseline: a marker, one agent per evaluation (`EvaluationAgent.is_baseline`).
  baselineAgent:
    "The evaluation's reference agent, marked so the other agents can be compared with it. One agent per evaluation is the baseline; it changes no score.",
  baselineRun: "A run of the evaluation's baseline agent, its reference agent for comparisons.",

  // Prediction reuse (`services/evaluation/fingerprint.py`).
  predictionReuse:
    "Copy a stored answer instead of running the agent again when nothing that shapes the answer changed: the agent, model, execution mode, persona, prompt and setting versions, the entry and its files, and the knowledge bases. Only successful answers are copied; scores are always recomputed.",
  reusedEntry:
    "Copied from a prior run with a matching input fingerprint: the agent was not re-executed and only scoring was recomputed. Its performance metrics are the source run's; the judge's time and cost are this run's.",
  reusedEntries:
    "{reused} of {total} answers were copied from earlier runs instead of running the agent again; only scoring was recomputed for them.",

  // Acting on a finished run (`replay_run`, `rescore_evaluation_run`).
  rerunFull:
    "Starts a new run that replays this one: the same agent, model, persona, prompt and setting versions and pinned dataset versions, with prediction reuse off. This run is not changed.",
  rerunFullSelected:
    "Starts a new run for each selected run, replaying its own versions and pinned dataset versions with prediction reuse off. The selected runs are not changed; placebo comparison runs are skipped.",
  replay:
    "Starts a new run with this run's versions and pinned dataset versions. Prediction reuse follows the evaluation's setting, so earlier answers may be copied.",
  rescore:
    "Scores this run's stored answers again with the evaluation's current scoring settings, and replaces its scores. The agent does not run again; the LLM judge, when enabled, calls its model again.",
  rescoreSelected:
    "Scores the selected runs' stored answers again with the evaluation's current scoring settings, and replaces their scores. The agents do not run again; the LLM judge, when enabled, calls its model again.",

  // Judge instructions: the entry's replace the evaluation's, which replace the dataset's
  // (`evaluation_engine.py`, `entry.evaluation_prompt or evaluation_instructions`).
  judgeInstructionsDataset:
    "What the LLM judge is told for this dataset's entries. An evaluation's own judge instructions replace them, and an entry's replace both: they are never combined.",
  judgeInstructionsEvaluation:
    "What the LLM judge is told for every entry of this evaluation. They replace the datasets' default judge instructions, and an entry's own replace them: they are never combined.",
  judgeInstructionsEntry:
    "What the LLM judge is told for this entry only. They replace the evaluation's and the dataset's judge instructions: they are never combined.",

  // The expected answer and the output family (`datasets.py` checks it on save).
  expectedAnswer:
    "The answer the agent should give. The output family's metrics and the LLM judge compare the agent's answer with it, so it takes the family's shape: a STIX bundle, a JSON object matching the schema, labels from the vocabulary, or prose.",
  needsFamily:
    "This dataset has no output family. An evaluation linked to it cannot run, replay or rescore until a family is set in the dataset's settings.",
  familyConflict:
    "This dataset's output family, {dataset}, differs from the evaluation's, {evaluation}, so its expected answers are not shaped for the metrics that score them. Set the same family on both.",
  datasetReadOnly:
    "Its entries, settings and output family cannot be changed, and it cannot be deleted, until read-only is turned off in its settings.",
  datasetEvaluationCount:
    "The number of evaluations linked to this dataset. Each of their runs answers its entries.",
  datasetVersion:
    "The dataset's latest snapshot. A new version is saved when its entries, files, JSON schema or label vocabulary change; a run pins the version current at its launch and is scored against it.",

  // Models.
  agentDefaultModel: "The agent's own configured model, not one added to the evaluation.",
  // `unavailable_model_ids`.
  unavailableModel:
    "No provider the evaluation's owner can use offers this model, so a launch that includes it is refused. Remove it or configure a provider that serves it; while it is disabled, launches leave it out.",
  modelIndependent:
    "This agent skips the LLM, so its answer does not depend on a model: it runs once per launch, not once per model.",

  // Placebo comparisons (`dev-docs/agent-placebo-evaluation.md`).
  placeboTrial: "Trial {n}: the n-th repetition of this arm on every entry of the placebo comparison.",

  // Durations (`evaluation_engine.py`, `services/evaluation/metrics.py`).
  runDuration:
    "The agent's working time summed over every entry of the run; the LLM judge and the waits between steps are left out, unlike the Run Duration metric. A run cancelled or stopped by an error shows the time from its start to its end instead.",
  entryDuration:
    "The agent's working time on this entry, summed over its steps; the LLM judge is left out. A reused entry shows its source's time.",
  agentTime: "The agent's working time, summed over its steps.",
  stepStart:
    "When this step started, counted by adding up the durations of the steps before it, not read from a clock. Tools that ran in parallel show one after another.",
  entryJudgeScore:
    "The LLM judge's score for this entry, 0 to 100: its five sub-scores weighted into one. The run's Judge Quality averages the counted entries' scores and lowers the result by the consistency penalty.",
  judgeSubScores:
    "The LLM judge's five grades for this entry, each from 0 to 100. Weighted into one, they make the entry's judge score.",
  newDatasetNoFamily:
    "A new dataset has no output family. Set one in its settings before an evaluation can run on it.",
  judgeTime: "The time the LLM judge took to score.",
  totalTime: "The agent's and the LLM judge's time added together, not the time from start to end.",
  ttft: "Time to first token: how long the model took to start answering.",
  // The Run menu, on the evaluation page and the evaluations list.
  runEvaluation: "Runs every agent on every enabled model, and on each agent's default model when the evaluation uses it.",
  runAll: "The same as Run: every agent on every enabled model, and on each agent's default model when the evaluation uses it.",
  defaultModelInSelection: "Run Selected uses it only when it is ticked.",
  availableModels:
    "Platform models not configured on this evaluation. Running a selection that holds one adds it to the evaluation's enabled models.",
  enabledModel:
    "An enabled model runs at every launch. A disabled one is skipped but stays on the evaluation, ready to be enabled again.",

  // Who can see and change an evaluation.
  companyManagedEvaluation:
    "This evaluation is managed by your organization: everyone can see it, and only administrators can change it.",
  sharedEvaluation: "This evaluation is shared with you by its owner, directly or through a group.",

  // Per-run values.
  runTags:
    "A tag of this run, merged from the evaluation's, the agent's and the run's own tags; the run's win over the agent's, which win over the evaluation's.",
  judgeOutputBudget:
    "Maximum tokens the LLM judge may use for its verdict. Raise this if complex outputs produce truncated (unscored) verdicts.",
  placeboArm:
    "An arm is one version of the agent in a placebo comparison: real, placebo or no target. The no-target arm runs the agent with the target removed.",
} as const;

/**
 * The LLM judge's five sub-scores, by key, as its system prompt defines them
 * (`evaluation-judge-system-prompt.md`). Each is scored 0 to 100. @i18n-keys
 */
const JUDGE_SUB_SCORE_DESCRIPTIONS = {
  tool_selection_quality:
    "Did the agent choose the right tools, with the right parameters? An agent with no tools scores 100.",
  response_completeness: "Does the answer cover every key point of the expected answer?",
  response_accuracy: "Does the answer agree with the expected answer, with no wrong claim or contradiction?",
  instruction_adherence: "Does the answer follow the intent and constraints it was given?",
  coherence: "Is the answer well structured, clear and coherent?",
} as const;

/** A sub-score's description, or `null` for a key the judge does not define. */
export function judgeSubScoreDescription(key: string): string | null {
  return Object.prototype.hasOwnProperty.call(JUDGE_SUB_SCORE_DESCRIPTIONS, key)
    ? JUDGE_SUB_SCORE_DESCRIPTIONS[key as keyof typeof JUDGE_SUB_SCORE_DESCRIPTIONS]
    : null;
}

/**
 * How a JSON field is compared (`evaluation_metrics/README.md`, field accuracy), by
 * mode, shown wherever a mode or a field's result is. @i18n-keys
 */
export const FIELD_MODE_DESCRIPTIONS = {
  derived: "The mode the schema implies for this field.",
  exact: "Scored 100 when equal to the expected value, else 0.",
  enum: "Scored 100 when it is the expected value from the allowed list, else 0.",
  enum_set: "A list compared as a set: F1 over its items, in any order.",
  free_text: "Prose compared by similarity, with partial credit.",
  resource_address: "An address (a URL or URI): it must be well formed and match the expected one once normalised.",
  object_list: "A list of objects: entries are paired with the expected ones, and each counts as its fields.",
  excluded: "Not scored.",
} as const;

/**
 * A field mode's display name, shown wherever a mode is: the stored and API value
 * stays the id. @i18n-keys
 */
export const FIELD_MODE_LABELS = {
  derived: "Derived from schema",
  exact: "Exact",
  enum: "Enum",
  enum_set: "Enum Set",
  free_text: "Free Text",
  resource_address: "Resource Address",
  object_list: "Object List",
  excluded: "Excluded",
} as const satisfies Record<keyof typeof FIELD_MODE_DESCRIPTIONS, string>;

/**
 * A field mode's display name and description, both catalog keys, or `null` for a mode
 * this app does not know: the caller shows it as stored.
 */
export function fieldModeCopy(mode: string): { label: string; description: string } | null {
  if (!Object.prototype.hasOwnProperty.call(FIELD_MODE_LABELS, mode)) return null;
  const known = mode as keyof typeof FIELD_MODE_LABELS;
  return { label: FIELD_MODE_LABELS[known], description: FIELD_MODE_DESCRIPTIONS[known] };
}

/** What a field's result means in the per-field breakdown. @i18n-keys */
export const FIELD_STATUS_DESCRIPTIONS = {
  correct: "Matches the expected value.",
  incorrect: "Differs from the expected value, or matches it only in part.",
  missing: "In the expected answer but not in the agent's answer.",
  extra: "In the agent's answer but not in the expected answer.",
  excluded: "Left out of the comparison by its mode or the scoring overrides.",
} as const;

/** What each column of a run's Metrics panel holds. @i18n-keys */
export const METRIC_COLUMN_DESCRIPTIONS = {
  contribution:
    "The 0 to 100 value this metric adds to the overall score above. It is computed on the counted entries only, so it can differ from the mean: an averaged metric is lowered by the consistency penalty, and a performance metric is placed among the evaluation's runs.",
  mean: "The average of this metric over every entry that has a value, entries left out of the overall scores included.",
  p50: "The median over the entries: half the entries are at or below it.",
  p95: "The 95th percentile over the entries: 95% of the entries are at or below it.",
  min: "The lowest value over the entries.",
  max: "The highest value over the entries.",
  n: "The number of entries with a value for this metric.",
} as const;

/** How the run's value of a metric the package pools or macro-averages
 * relates to the Mean column: said once, on the column's header, for the
 * kinds the table holds. @i18n-keys */
export const METRIC_MEAN_BASIS_NOTES = {
  pooled:
    "For a metric marked Pooled, the run's value adds up the entries' counts before dividing, so an entry with more of them weighs more: it can differ from this mean.",
  macro:
    "For a metric marked Macro, the run's value adds up each class's counts over the entries, then averages the classes' F1, so a rare class weighs as much as a frequent one: it can differ from this mean.",
} as const;

/** The marker on the row of such a metric, which the Mean header explains. @i18n-keys */
export const METRIC_MEAN_BASIS_LABELS = {
  pooled: "Pooled",
  macro: "Macro",
} as const satisfies Record<keyof typeof METRIC_MEAN_BASIS_NOTES, string>;

/** What started a run, by its `trigger_reason`, for the trigger badge. @i18n-keys */
const TRIGGER_NOTES = {
  schedule: "Started by the evaluation's schedule.",
  on_agent_change: "Started because an agent of the evaluation changed.",
  agent_change: "Started because an agent of the evaluation changed.",
  manual: "Started on request: Run, Re-run full, Replay, a placebo comparison or the API.",
  persona_improvement:
    "Started when a persona rewrite was approved: the agent with its rewritten persona, on the model of the run the rewrite started from. Its Judge Quality, compared with that run's, decides whether the rewrite is kept.",
  import: "Imported with the evaluation bundle.",
  unknown: "What started this run.",
} as const;

/** A trigger reason's note, a catalog key; an unknown reason gets the generic one. */
export function triggerNote(reason: string): string {
  return Object.prototype.hasOwnProperty.call(TRIGGER_NOTES, reason)
    ? TRIGGER_NOTES[reason as keyof typeof TRIGGER_NOTES]
    : TRIGGER_NOTES.unknown;
}

/** Why an output extractor produced nothing to score (`extraction/failures.py`). @i18n-keys */
const EXTRACTION_FAILURE_LABELS = {
  source_not_present: "Source not found",
  payload_unparseable: "Not valid JSON",
  selection_empty: "Nothing at the path",
  type_mismatch: "Wrong value type",
  limit_exceeded: "Too large",
  label_invalid: "Invalid label",
  family_incompatible: "Not scored by this family",
} as const;

/** A failure reason's name, translated; an unknown reason shows as sent. */
export function extractionFailureLabel(reason: string | undefined, t_i18n: (id: string) => string): string {
  if (!reason) return "";
  return Object.prototype.hasOwnProperty.call(EXTRACTION_FAILURE_LABELS, reason)
    ? t_i18n(EXTRACTION_FAILURE_LABELS[reason as keyof typeof EXTRACTION_FAILURE_LABELS])
    : reason;
}
