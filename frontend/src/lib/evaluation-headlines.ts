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
 * The single resolver for a run's three headline metrics, so the
 * evaluation, run and compare pages cannot give them different meanings.
 *
 * Two rules hold here rather than at each call site:
 *
 * 1. `null` means "not measured" and renders as N/A, never `0`: an unmeasured
 *    run and a run that scored zero are different answers.
 * 2. The retired `speed_score` is a FALLBACK only, used when Overall Performance
 *    is N/A (an older run). A run never displays both: a comparative
 *    headline beside the absolute number it replaced reads as two measurements of
 *    the same thing.
 */

import type { EvaluationRun, QualityCoverage } from "@/types";
import { RUN_LEVEL_EXCLUSION_KEY } from "@/types";

export type HeadlineId = "judge" | "quality" | "performance";

export interface Headline {
  id: HeadlineId;
  label: string;
  /** `null` = N/A. Never `0` as a stand-in for "unmeasured". */
  value: number | null;
  /** Metric keys that actually composed `value`. Empty when N/A. */
  metricKeys: string[];
  /**
   * `true` when the value only means something relative to the other runs of
   * the same evaluation. Overall Performance is normalised across the run set,
   * so it carries this disclosure wherever it is shown.
   */
  comparative: boolean;
  /** Set when `value` came from the retired `speed_score` column (an older run). */
  legacy: boolean;
  /** What this headline is, for its tooltip: a `HEADLINE_NOTES` catalog key. */
  note: string;
}

/** Each headline's name, read by `t_i18n` on every page that shows it. @i18n-keys */
export const HEADLINE_LABELS = {
  quality: "Overall Quality",
  performance: "Overall Performance",
  judge: "Judge Quality",
} as const;

/**
 * What each headline is, by its id, and the disclosures shown with one, read by `t_i18n`:
 * one sentence per concept for every evaluation page. Overall Performance's is its
 * comparative-scope disclosure, required on every surface showing it. A plain string const is
 * invisible to `validate-i18n`, a tagged table is not. @i18n-keys
 */
export const HEADLINE_NOTES = {
  judge:
    "Scored by the LLM judge: each entry's sub-scores are weighted into one score, then the entries' scores are averaged and lowered by the consistency penalty.",
  quality:
    "Composed from the output family's deterministic quality metrics, independently of the LLM judge: the average of their values on a run, each shown under Contribution in the run's Metrics panel.",
  performance:
    "Comparative within this evaluation, not an absolute score: on each performance metric, runs within the tolerance of the best get 100 and the worst run gets 0, then the metrics are averaged. It changes as runs are added.",
  legacySpeed: "Legacy speed score, kept from before this evaluation used comparative performance.",
  legacyOverall: "Legacy blended score. Retained for historical runs and no longer computed: read the three overall scores instead.",
  noGradeable: "No gradeable results to score",
  allVerdictsFailed: "No gradeable results: all judge verdicts failed ({count})",
  legacyOverallNotComputed: "Not computed for this run: the blended score was retired before it ran. Read the three overall scores instead.",
} as const;

/** Whether the LLM judge scored this run: its own scorer selection, `null` reading as
 * judge-on. Never the headline, which a failed or cancelled run lacks. */
export function runJudged(run: Pick<EvaluationRun, "scorer_selection">): boolean {
  return run.scorer_selection == null || run.scorer_selection.includes("judge");
}

/**
 * Why a completed run has no Judge Quality: the judge was off for it, nothing was
 * gradeable, or every verdict failed. The arguments of `t_i18n`: `t_i18n(...judgeNaNote(run))`.
 */
export function judgeNaNote(
  run: Pick<EvaluationRun, "judge_failure_count" | "scorer_selection">,
): [key: string, values?: { count: number }] {
  if (!runJudged(run)) return [NA_REASONS.judge];
  const count = run.judge_failure_count ?? 0;
  return count > 0 ? [HEADLINE_NOTES.allVerdictsFailed, { count }] : [HEADLINE_NOTES.noGradeable];
}

/**
 * Why a run's legacy blended score is empty: its judge verdicts all failed, the
 * one case a blended score could not be computed; else it ran after the score was retired.
 */
export function legacyOverallNaNote(
  run: Pick<EvaluationRun, "judge_failure_count" | "scorer_selection" | "score">,
): [key: string, values?: { count: number }] {
  return run.score == null && (run.judge_failure_count ?? 0) > 0
    ? judgeNaNote(run)
    : [HEADLINE_NOTES.legacyOverallNotComputed];
}

/** Shape shared by the full run response and the leaner run-search item. */
type RunLike = Pick<EvaluationRun, "score" | "quality_score"> &
  Partial<Pick<EvaluationRun, "performance_score" | "speed_score" | "quality_metric_keys" | "performance_metric_keys" | "id" | "performance_aggregation_excluded" | "overall_score" | "scorer_selection" | "judge_failure_count" | "placebo_comparison_id">>;

/**
 * A run scored before the three headlines, identified by the retired
 * blended `overall_score` being set. It can never gain an Overall Quality, so
 * surfaces render a plain "—" rather than an "N/A" that suggests a fault in
 * this run.
 */
export function isLegacyRun(run: RunLike): boolean {
  return (run.overall_score ?? null) !== null;
}

/** Why one run has no Overall Performance.
 *
 * `undisclosed` is NOT a cause: the payload carries no disclosure (the leaner
 * run-search item serves `performance_score` alone), so no cause can be read off it.
 * Absence of the disclosure is not evidence of `no_shared_metric`.
 *
 * Order matters, most specific first. An author who selected nothing gets no score
 * however the runs measured. An abstaining run in an otherwise healthy
 * evaluation also fits `no_shared_metric`, so its own abstention comes next; otherwise
 * the reader is told, falsely, that the evaluation shares no metric. Runs sharing only
 * an opt-in metric (time to first token) do share one.
 */
type PerformanceNaCause =
  | "comparison_run"
  | "nothing_selected"
  | "run_unmeasured"
  | "too_few_runs"
  | "only_opt_in_shared"
  | "no_shared_metric"
  | "undisclosed";

function performanceNaCause(run: RunLike): PerformanceNaCause {
  // Out of the performance basis by design (dev-docs/agent-placebo-evaluation.md, Implementation).
  if (run.placebo_comparison_id) return "comparison_run";
  const excluded = run.performance_aggregation_excluded;
  if (!excluded) return "undisclosed";

  const runLevel = excluded.filter((e) => e.key === RUN_LEVEL_EXCLUSION_KEY);
  if (runLevel.some((e) => e.reason === "no_metric_selected")) return "nothing_selected";
  if (
    run.id !== undefined &&
    runLevel.some((e) => e.reason === "no_performance_data" && (e.run_ids ?? []).includes(run.id as string))
  ) {
    return "run_unmeasured";
  }
  if (runLevel.some((e) => e.reason === "too_few_comparable_runs")) return "too_few_runs";
  if (runLevel.some((e) => e.reason === "only_opt_in_metrics_shared")) return "only_opt_in_shared";
  return "no_shared_metric";
}

export function judgeHeadline(run: RunLike): Headline {
  return {
    id: "judge",
    label: HEADLINE_LABELS.judge,
    value: run.score ?? null,
    metricKeys: [],
    comparative: false,
    legacy: false,
    note: HEADLINE_NOTES.judge,
  };
}

export function qualityHeadline(run: RunLike): Headline {
  return {
    id: "quality",
    label: HEADLINE_LABELS.quality,
    value: run.quality_score ?? null,
    metricKeys: run.quality_metric_keys ?? [],
    comparative: false,
    legacy: false,
    note: HEADLINE_NOTES.quality,
  };
}

export function performanceHeadline(run: RunLike): Headline {
  const measured = run.performance_score ?? null;
  if (measured !== null) {
    return {
      id: "performance",
      label: HEADLINE_LABELS.performance,
      value: measured,
      metricKeys: run.performance_metric_keys ?? [],
      comparative: true,
      legacy: false,
      note: HEADLINE_NOTES.performance,
    };
  }
  // Only ever a fallback: see rule 2 above.
  const legacy = run.speed_score ?? null;
  return {
    id: "performance",
    label: legacy !== null ? "Speed (legacy)" : HEADLINE_LABELS.performance,
    value: legacy,
    metricKeys: [],
    comparative: false,
    legacy: legacy !== null,
    note: legacy !== null ? HEADLINE_NOTES.legacySpeed : HEADLINE_NOTES.performance,
  };
}

export function headlines(run: RunLike): Headline[] {
  return [judgeHeadline(run), qualityHeadline(run), performanceHeadline(run)];
}

/** Why a headline reads N/A, one sentence per cause, read by `t_i18n`. @i18n-keys */
const NA_REASONS = {
  not_completed: "The run did not complete, so nothing was measured.",
  running: "Still running: this is computed when the run completes.",
  judge: "The LLM judge was not enabled for this run.",
  judge_off_aggregate: "The LLM judge was not enabled for these runs.",
  judge_aggregate: "No latest run has a Judge Quality: the LLM judge was off for them, or none of their verdicts was gradeable.",
  quality: "No selected quality metric produced a value for this run, or none is selected under Overall Score Composition.",
  quality_aggregate: "No latest run has an Overall Quality: no selected quality metric produced a value for them, or none is selected under Overall Score Composition.",
  comparison_run: "A placebo comparison's runs get no Overall Performance: they are compared with their placebo, not ranked with the evaluation's other runs.",
  nothing_selected: "No performance metric is selected for this evaluation's Overall Performance. Select one under Overall Score Composition to produce a score.",
  run_unmeasured: "This run recorded no performance metric, so it was left out of the comparison. Re-running it would produce one.",
  too_few_runs: "Too few comparable runs in this evaluation to rank against. Adding another measured run would produce a score.",
  only_opt_in_shared: "The only performance metrics every run of this evaluation measured are not counted by default, and none is selected. Select one under Overall Score Composition to produce a score.",
  no_shared_metric: "No performance metric was measured on every run of this evaluation, so there is nothing to compare against.",
} as const;

/**
 * Why a headline reads N/A, phrased for a tooltip: a catalog key, for `t_i18n`.
 *
 * A terminal run never gains a value, so it is not told "not yet computed".
 *
 * `run` is passed only on **run-scoped** surfaces. Aggregate surfaces omit it and get
 * the generic wording: no single run's abstention is their subject.
 */
export function naReason(id: HeadlineId, status: string, run?: RunLike): string {
  if (status === "failed" || status === "cancelled") return NA_REASONS.not_completed;
  if (status !== "completed") return NA_REASONS.running;
  switch (id) {
    case "judge":
      return run && runJudged(run) ? HEADLINE_NOTES.noGradeable : NA_REASONS.judge;
    case "quality":
      return NA_REASONS.quality;
    case "performance":
      switch (run ? performanceNaCause(run) : "undisclosed") {
        case "comparison_run":
          return NA_REASONS.comparison_run;
        case "nothing_selected":
          return NA_REASONS.nothing_selected;
        case "run_unmeasured":
          return NA_REASONS.run_unmeasured;
        case "too_few_runs":
          return NA_REASONS.too_few_runs;
        case "only_opt_in_shared":
          return NA_REASONS.only_opt_in_shared;
        default:
          return NA_REASONS.no_shared_metric;
      }
  }
}

/**
 * Why a headline reads N/A on an **aggregate** surface, where no single run is the subject.
 *
 * The generic wording, except for the causes that hold for every run alike, which any
 * run's disclosure states: nothing selected, or only opt-in metrics shared (the shared
 * set is the evaluation's). Without them the author who selected nothing would be told
 * the runs share no metric.
 */
export function aggregateNaReason(id: HeadlineId, runs: RunLike[]): string {
  if (id === "judge") {
    // Off for every run, or on for some that still have no verdict to show.
    return runs.length > 0 && runs.every((run) => !runJudged(run)) ? NA_REASONS.judge_off_aggregate : NA_REASONS.judge_aggregate;
  }
  if (id === "quality") return NA_REASONS.quality_aggregate;
  const causes = new Set(runs.map(performanceNaCause));
  if (causes.has("nothing_selected")) return NA_REASONS.nothing_selected;
  if (causes.has("only_opt_in_shared")) return NA_REASONS.only_opt_in_shared;
  if (causes.has("too_few_runs")) return NA_REASONS.too_few_runs;
  return NA_REASONS.no_shared_metric;
}

/** The coverage of Overall Quality when it leaves entries out, else `null`. */
export function partialCoverage(run: Pick<EvaluationRun, "quality_coverage">): QualityCoverage | null {
  const coverage = run.quality_coverage ?? null;
  return coverage && coverage.counted < coverage.entries ? coverage : null;
}

/**
 * Why a quality metric did not score, or scored 0, for the reasons the backend stores as
 * a code. Any other reason is already the backend's own sentence, shown as written.
 * @i18n-keys
 */
const METRIC_REASON_LABELS = {
  prediction_not_well_formed: "The answer is not well-formed.",
  prediction_too_many_entries: "The answer has too many entries to compare.",
  reference_empty: "The expected answer is empty.",
  no_tokens_on_either_side: "Neither the expected answer nor the answer has a word to compare.",
  rouge_l_input_too_large: "Too long to compare word order.",
  pairing_input_too_large: "The expected list is too large to compare entry by entry.",
  no_scorable_fields: "Every field is excluded from scoring.",
  empty_on_one_side: "Each field is empty in the expected answer or in the answer, so nothing compares.",
  not_measured: "No quality metric was recorded: the entry did not complete.",
  prediction_not_scorable: "The answer cannot be scored.",
  configuration_error: "The expected answer or the scoring settings cannot be used.",
  extraction_failed: "The answer could not be extracted for scoring.",
  agent_run_failed: "The agent's run failed, so it gave no answer.",
  platform_error: "The platform could not run the agent.",
  scoring_error: "Scoring failed on this entry; the answer and the judge's verdict are kept.",
} as const;

/** The translatable label of a reason code, or `null` for a reason the backend already
 * wrote as a sentence. */
export function metricReasonLabel(reason: string): string | null {
  return Object.prototype.hasOwnProperty.call(METRIC_REASON_LABELS, reason)
    ? METRIC_REASON_LABELS[reason as keyof typeof METRIC_REASON_LABELS]
    : null;
}
