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
 * The rows of the evaluation comparison's "What can be compared" table (#12282), pure:
 * per dimension, each evaluation's value and what the difference means.
 */

import { formatModelLabel } from "@/lib/utils";
import { entriesOf } from "@/lib/evaluation-compare";
import type { Translate } from "@/lib/credential-exposure";
import type { ComparedEvaluation, EvaluationComparison } from "@/types";

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
  note: string;
}

/** The comparability rows, pure enough to test through the page. */
export function comparabilityRows(
  comparison: EvaluationComparison,
  sharedCount: number,
  t_i18n: Translate,
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
  const limits = (e: ComparedEvaluation) =>
    `${e.execution_mode ?? "—"} · ${t_i18n("timeout {minutes} min", { minutes: Math.round((e.timeout_seconds ?? 0) / 60) })}`;

  return [
    {
      dimension: t_i18n("Entries"),
      values: evals.map((e) => e.datasets.map((d) => `${d.name} (${d.entry_count})`).join(", ") || "—"),
      verdict: "scope",
      note: t_i18n(
        "Compared on the {count, plural, one {# entry} other {# entries}} whose expected answers are the same ground truth in every evaluation.",
        { count: sharedCount },
      ),
    },
    {
      dimension: t_i18n("Agent"),
      values: evals.map(subjectsOf),
      details: evals.map(subjectDetail),
      verdict: allEqual(evals.map(subjectsOf)) ? "same" : "tested",
      note: allEqual(evals.map(subjectsOf))
        ? t_i18n("The same agents in every evaluation.")
        : t_i18n("The change these figures measure."),
    },
    {
      dimension: t_i18n("Input"),
      values: inputs.map((m) => t_i18n(INPUT_LABEL[m])),
      verdict: allEqual(inputs) ? "same" : "confound",
      note: allEqual(inputs)
        ? t_i18n("Every agent read the entries the same way.")
        : t_i18n("It changed with the agent: a gap measures the whole setup, not one change alone."),
    },
    {
      dimension: t_i18n("Models"),
      values: evals.map((e) => [...models(e)].map((m) => formatModelLabel(m, t_i18n)).join(", ")),
      verdict: common.length ? "scope" : "excluded",
      note: common.length
        ? t_i18n("{models}: compared entry by entry. The others are compared as configurations.", {
            models: common.map((m) => formatModelLabel(m, t_i18n)).join(", "),
          })
        : t_i18n("No model ran in every evaluation: only configurations can be compared."),
    },
    {
      dimension: t_i18n("Metric suite"),
      values: evals.map(suites),
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
        : t_i18n("Another judge, weights or instructions: judge scores are left out of this page."),
    },
    {
      dimension: t_i18n("Run limits"),
      values: evals.map(limits),
      verdict: allEqual(evals.map(limits)) ? "same" : "confound",
      note: t_i18n("Execution mode and time limit of each evaluation."),
    },
  ];
}

