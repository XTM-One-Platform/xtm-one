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
 * "What can be compared" (#12282): per dimension, each evaluation's value and what the
 * difference means for the figures below, read first so a reader knows what they measure.
 */

import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import { cn, formatModelLabel } from "@/lib/utils";
import { comparabilityRows, type Verdict } from "@/lib/evaluation-comparability";
import type { ComparedCell, EvaluationComparison } from "@/types";
import { EVALUATION_SWATCHES } from "./compare-colors";

/** What each verdict reads as, read by `t_i18n`. @i18n-keys */
const VERDICT_LABEL = {
  same: "Same",
  tested: "Under test",
  confound: "Changed too",
  aligned: "Aligned",
  scope: "Restricts the comparison",
  excluded: "Not comparable",
} as const satisfies Record<Verdict, string>;

const VERDICT_TONE: Record<Verdict, string> = {
  same: "text-feedback-success-primary border-feedback-success-primary/40",
  aligned: "text-feedback-success-primary border-feedback-success-primary/40",
  tested: "text-accent border-accent/40",
  scope: "text-accent border-accent/40",
  confound: "text-feedback-warning-primary border-feedback-warning-primary/40",
  excluded: "text-feedback-error-primary border-feedback-error-primary/40",
};

export function ComparabilityMatrix({
  comparison,
  sharedCount,
  cells,
}: {
  comparison: EvaluationComparison;
  sharedCount: number;
  /** The cells by run and entry: the answers that reached their time limit are counted from them. */
  cells?: Map<string, Map<string, ComparedCell>>;
}) {
  const { t_i18n, intl } = useFormatter();
  const rows = comparabilityRows(comparison, sharedCount, t_i18n, (m) => formatModelLabel(m, t_i18n), cells);
  // When an evaluation started: its creation, else its oldest compared run.
  const startedOf = (e: EvaluationComparison["evaluations"][number]) => {
    const at = e.created_at ?? [...e.runs.map((r) => r.created_at ?? "")].filter(Boolean).sort()[0];
    const time = at ? Date.parse(at) : Number.NaN;
    return Number.isNaN(time) ? null : intl.formatDate(time, { year: "numeric", month: "short", day: "numeric" });
  };
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[42rem] content-compact">
        <thead>
          <tr className="border-b border-elevation-subtle-soft text-left">
            <th scope="col" className="px-3 py-2 w-28" />
            {comparison.evaluations.map((e, i) => (
              <th key={e.id} scope="col" className="px-3 py-2 font-medium">
                <span className="inline-flex items-center gap-2">
                  <span aria-hidden className={cn("h-2.5 w-2.5 rounded-full", EVALUATION_SWATCHES[i % EVALUATION_SWATCHES.length])} />
                  {e.name}
                </span>
                {startedOf(e) && (
                  <span className="block content-caption font-normal text-default-secondary">
                    {t_i18n("Started {date}", { date: startedOf(e) as string })}
                  </span>
                )}
              </th>
            ))}
            <th scope="col" className="px-3 py-2 font-medium">{t_i18n("For this comparison")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.dimension} className="border-b border-elevation-subtle-soft align-top last:border-b-0">
              <th scope="row" className="px-3 py-2 text-left font-medium text-default-secondary">{row.dimension}</th>
              {row.values.map((value, i) => (
                <td key={i} className="px-3 py-2">
                  {value}
                  {row.details?.[i] && <p className="content-caption text-default-secondary">{row.details[i]}</p>}
                </td>
              ))}
              <td className="px-3 py-2">
                <Badge variant="outline" className={cn("content-caption", VERDICT_TONE[row.verdict])}>
                  {row.pill ?? t_i18n(VERDICT_LABEL[row.verdict])}
                </Badge>
                <p className="mt-1 content-caption text-default-secondary">{row.note}</p>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
