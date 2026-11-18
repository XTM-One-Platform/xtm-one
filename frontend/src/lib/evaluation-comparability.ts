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
 * The evaluation comparison's readings that are not statistics (#12282), pure: the rows
 * of "What can be compared" (per dimension, each evaluation's value and what the
 * difference means), the entries only one evaluation has, the answers that reached their
 * time limit, the metrics the page leads with, and whether the shared entries are a fair
 * sample of each dataset.
 */

import { entriesOf, sharedEntries } from "./evaluation-compare.ts";
import type { ComparedCell, ComparedEvaluation, EvaluationComparison } from "../types/index.ts";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** What a dimension's difference means for the comparison. */
export type Verdict = "same" | "tested" | "confound" | "aligned" | "scope" | "excluded";

const allEqual = (values: string[]) => new Set(values).size <= 1;

/** How an evaluation's entries reach its agent: text, an attached file, or both. */
function inputMode(comparison: EvaluationComparison, evaluationId: string): "text" | "file" | "mixed" {
  const files = entriesOf(comparison.entries, evaluationId).map(
    (entry) => entry.members.find((m) => m.evaluation_id === evaluationId)?.has_file ?? false,
  );
  if (files.every(Boolean)) return "file";
  if (files.some(Boolean)) return "mixed";
  return "text";
}

/** How an evaluation's entries reach its agent, read by `t_i18n`. @i18n-keys */
const INPUT_LABEL = { text: "Text in the prompt", file: "Attached file", mixed: "Text and attached files" } as const;

interface ComparabilityRow {
  dimension: string;
  values: string[];
  details?: string[];
  verdict: Verdict;
  /** The pill's own words when the verdict's name says less ("12 shared", "Different"). */
  pill?: string;
  note: string;
}

/** The metrics a family's comparison leads with (switch, paired tiles), in this order;
 *  a family without an entry leads with every metric it serves. */
const HEADLINE_METRICS: Record<string, string[]> = {
  STIX_BUNDLE: ["stix.bundle_f1", "stix.entity_prf", "stix.relationship_prf"],
};

/** The comparison's headline metrics, in the family's order. */
export function headlineMetrics(comparison: Pick<EvaluationComparison, "family" | "metrics">): EvaluationComparison["metrics"] {
  const keys = HEADLINE_METRICS[comparison.family] ?? [];
  const picked = keys
    .map((key) => comparison.metrics.find((m) => m.key === key))
    .filter((m): m is EvaluationComparison["metrics"][number] => !!m);
  return picked.length ? picked : comparison.metrics;
}

/** Per evaluation, how many of its entries the comparison leaves out (not shared by all). */
export function onlyInCounts(comparison: Pick<EvaluationComparison, "entries" | "evaluations">): number[] {
  const shared = sharedEntries(comparison.entries, comparison.evaluations.map((e) => e.id)).length;
  return comparison.evaluations.map((e) => entriesOf(comparison.entries, e.id).length - shared);
}

/** The answers on `keys` of the compared runs that reached their evaluation's time limit
 *  (98% of it or more: the agent was stopped, or finished on the wire). */
export function limitHits(
  comparison: Pick<EvaluationComparison, "evaluations">,
  cells: Map<string, Map<string, ComparedCell>>,
  keys: readonly string[],
): number {
  let n = 0;
  for (const evaluation of comparison.evaluations) {
    const limit = evaluation.timeout_seconds;
    if (!limit) continue;
    for (const run of evaluation.runs) {
      for (const key of keys) {
        const seconds = cells.get(run.id)?.get(key)?.seconds;
        if (typeof seconds === "number" && seconds >= limit * 0.98) n++;
      }
    }
  }
  return n;
}

/** How many answers of a run on `keys` the scorer could not read (they count as everything missing). */
export function unreadableCount(cells: Map<string, Map<string, ComparedCell>>, runId: string, keys: readonly string[]): number {
  let n = 0;
  for (const key of keys) {
    const cell = cells.get(runId)?.get(key);
    if (cell && cell.counted !== false && cell.unreadable) n++;
  }
  return n;
}

/** A time per entry: seconds below two minutes, minutes above. */
export function formatSeconds(v: number | null | undefined): string {
  if (v == null) return "—";
  return v < 120 ? `${Math.round(v)} s` : `${Math.round(v / 60)} min`;
}

/** The figure's change with its sign; ± within a tenth of a point. */
export function signed(v: number): string {
  return `${v > 0.05 ? "+" : v < -0.05 ? "−" : "±"}${Math.abs(v).toFixed(1)}`;
}

/** Points under which the shared entries and the others count as alike. */
const FAIR_GAP = 4;

/** Whether the shared entries stand for an evaluation's whole dataset: the gap, shared
 *  minus others, of largest size over its runs (null when no run has both figures). */
export function largestGap(rows: ReadonlyArray<{ shared: number | null; other: number | null }>): number | null {
  let gap: number | null = null;
  for (const row of rows) {
    if (row.shared == null || row.other == null) continue;
    const g = row.shared - row.other;
    if (gap === null || Math.abs(g) > Math.abs(gap)) gap = g;
  }
  return gap;
}

export const isFairSample = (gap: number) => Math.abs(gap) < FAIR_GAP;

/** The names of the configurations that can be first, the first six then an ellipsis. */
export function tiedNames(names: readonly string[], max = 6): string {
  return names.slice(0, max).join(", ") + (names.length > max ? "…" : "");
}

/** The comparability rows. `modelLabel` names a model id (the platform default for none). */
export function comparabilityRows(
  comparison: EvaluationComparison,
  sharedCount: number,
  t_i18n: Translate,
  modelLabel: (model: string) => string = (m) => m,
  cells?: Map<string, Map<string, ComparedCell>>,
): ComparabilityRow[] {
  const evals = comparison.evaluations;
  const subjectsOf = (e: ComparedEvaluation) => e.subjects.map((s) => s.name).sort().join(", ") || "—";
  const subjectDetail = (e: ComparedEvaluation) =>
    e.subjects
      .map((s) =>
        [
          s.builtin_tools.length ? t_i18n("tools: {tools}", { tools: s.builtin_tools.join(", ") }) : t_i18n("no built-in tool"),
          s.skills.length ? t_i18n("skills: {skills}", { skills: s.skills.join(", ") }) : null,
          s.max_iterations ? t_i18n("up to {n} iterations", { n: s.max_iterations }) : null,
        ]
          .filter(Boolean)
          .join(" · "),
      )
      .join("; ");
  const suites = (e: ComparedEvaluation) => [...new Set(e.runs.map((r) => r.metric_suite_version ?? "?"))].sort().join(", ");
  const models = (e: ComparedEvaluation) => new Set(e.runs.map((r) => r.model_id));
  const common = [...models(evals[0])].filter((m) => evals.every((e) => models(e).has(m)));
  const judge = (e: ComparedEvaluation) =>
    e.judge_on
      ? `${e.evaluator_model ?? t_i18n("Platform default")} · ${Object.entries(e.judge_weights ?? {})
          .map(([k, v]) => `${k.replace(/_/g, " ")} ${Math.round(v * 100)}%`)
          .join(", ")}`
      : t_i18n("Judge off");
  const judgeKey = (e: ComparedEvaluation) =>
    JSON.stringify([e.judge_on, e.evaluator_model, e.judge_weights, e.judge_instructions_digest, e.datasets.map((d) => d.instructions_digest)]);
  const inputs = evals.map((e) => inputMode(comparison, e.id));
  const limitOf = (e: ComparedEvaluation) =>
    e.timeout_seconds ? t_i18n("timeout {minutes} min", { minutes: Math.round(e.timeout_seconds / 60) }) : t_i18n("no time limit");
  const limits = (e: ComparedEvaluation) => `${e.execution_mode ?? "—"} · ${limitOf(e)}`;
  const families = evals.map((e) => e.family ?? comparison.family);
  const onlyIn = onlyInCounts(comparison);
  const shared = sharedEntries(comparison.entries, evals.map((e) => e.id)).map((e) => e.key);
  const hits = cells ? limitHits(comparison, cells, shared) : 0;
  const runCount = evals.reduce((n, e) => n + e.runs.length, 0);
  const datasetNotes = evals.map((e) =>
    e.datasets
      .map((d) => d.description?.trim())
      .filter(Boolean)
      .join(" · "),
  );

  return [
    {
      dimension: t_i18n("Family"),
      values: families,
      verdict: allEqual(families) ? "same" : "excluded",
      pill: allEqual(families) ? undefined : t_i18n("Different"),
      note: t_i18n("Same output, same metrics."),
    },
    {
      dimension: t_i18n("Entries"),
      values: evals.map(
        (e) => `${entriesOf(comparison.entries, e.id).length} · ${e.datasets.map((d) => `“${d.name}”`).join(", ") || "—"}`,
      ),
      details: datasetNotes.some(Boolean) ? datasetNotes : undefined,
      verdict: "scope",
      pill: t_i18n("{count} shared", { count: sharedCount }),
      note:
        evals.length === 2
          ? t_i18n(
              "Compared on the {count, plural, one {# entry} other {# entries}} whose expected answers are the same ground truth in both evaluations; {first} only in the first, {second} only in the second.",
              { count: sharedCount, first: onlyIn[0], second: onlyIn[1] },
            )
          : t_i18n(
              "Compared on the {count, plural, one {# entry} other {# entries}} whose expected answers are the same ground truth in every evaluation; {others}.",
              {
                count: sharedCount,
                others: evals
                  .map((e, i) => t_i18n("{count} of “{name}” left out", { count: onlyIn[i], name: e.name }))
                  .join(", "),
              },
            ),
    },
    {
      dimension: t_i18n("Agent"),
      values: evals.map(subjectsOf),
      details: evals.map(subjectDetail),
      verdict: allEqual(evals.map(subjectsOf)) ? "same" : "tested",
      note: allEqual(evals.map(subjectsOf))
        ? t_i18n("The same agents in every evaluation.")
        : t_i18n("The change this comparison measures."),
    },
    {
      dimension: t_i18n("Input"),
      values: inputs.map((m) => t_i18n(INPUT_LABEL[m])),
      verdict: allEqual(inputs) ? "same" : "confound",
      note: allEqual(inputs)
        ? t_i18n("Every agent read the entries the same way.")
        : t_i18n("It changed with the agent: the gap is the whole setup, not the pipeline alone."),
    },
    {
      dimension: t_i18n("Models"),
      values: evals.map((e) => String(models(e).size)),
      details: evals.map((e) => [...models(e)].map(modelLabel).join(", ")),
      verdict: common.length ? "scope" : "excluded",
      pill: common.length ? t_i18n("{count} in common", { count: common.length }) : t_i18n("None in common"),
      note: common.length
        ? t_i18n("{models}: compared entry by entry. The others are compared as configurations.", {
            models: common.map(modelLabel).join(", "),
          })
        : t_i18n("Only configurations can be compared."),
    },
    {
      dimension: t_i18n("Scorer"),
      values: evals.map((e) => t_i18n("suite {versions}", { versions: suites(e) })),
      verdict: allEqual(evals.map(suites)) ? "aligned" : "confound",
      note: allEqual(evals.map(suites))
        ? t_i18n("One scorer version scored every run.")
        : t_i18n("Scored by different metric suite versions: re-score the older runs so one version measures them all."),
    },
    {
      dimension: t_i18n("Judge"),
      values: evals.map(judge),
      verdict: allEqual(evals.map(judgeKey)) ? "same" : "excluded",
      note: allEqual(evals.map(judgeKey))
        ? t_i18n("Same judge, weights and instructions.")
        : t_i18n("Other weights and other dataset instructions: judge scores are left out here."),
    },
    {
      dimension: t_i18n("Run limits"),
      values: evals.map(limits),
      verdict: allEqual(evals.map(limits)) ? "same" : "confound",
      note: hits
        ? t_i18n("{count, plural, one {# answer on the shared entries reached its limit.} other {# answers on the shared entries reached their limit.}}", { count: hits })
        : t_i18n("No answer on the shared entries reached its limit ({count, plural, one {# run} other {# runs}} checked).", { count: runCount }),
    },
  ];
}
