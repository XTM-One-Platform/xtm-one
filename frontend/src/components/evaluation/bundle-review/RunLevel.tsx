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

// The run level of a run review: the run's figures pooled over every report it answered,
// each with its change against what it is compared with, and where its entity points go
// (the near-miss kinds, the missing, spurious and duplicated objects) over those reports.

import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { formatScore } from "@/lib/evaluation-bundle-review";
import {
  BUNDLE_F1,
  ENTITY_F1,
  RELATIONSHIP_F1,
  precision,
  recall,
  type CauseKey,
  type CauseSummary,
  type RunFigures,
  type RunMetric,
} from "@/lib/evaluation-run-review";
import { cn } from "@/lib/utils";
import { Bar, Delta, Tile } from "./figures";
import { STATUS_COLOR, causeLabel } from "./labels";

/** The run figures' labels, read by `t_i18n`. @i18n-keys */
const METRIC_LABELS = {
  [BUNDLE_F1]: "Bundle F1",
  [ENTITY_F1]: "Entity F1",
  [RELATIONSHIP_F1]: "Relationship F1",
} as const satisfies Record<RunMetric, string>;

/** A cost and a duration: "$0.42 · 12 min". */
function useCostTime() {
  const { t_i18n } = useFormatter();
  const { money } = useNumberFormat();
  return (f: Pick<RunFigures, "cost" | "seconds">) => {
    const cost = f.cost ? money(f.cost) : t_i18n("no cost reported");
    const s = f.seconds ?? 0;
    const time = s < 120 ? t_i18n("{count} s", { count: Math.round(s) }) : t_i18n("{count} min", { count: Math.round(s / 60) });
    return `${cost} · ${time}`;
  };
}

export interface RunComparatorFigures {
  name: string;
  /** Its figures on the reports both runs count. */
  figures: RunFigures;
  /** The reviewed run's figures on those same reports. */
  mine: RunFigures;
  /** Fewer reports than the reviewed run counts: the change is measured on these. */
  partial: boolean;
}

export function RunHead({ name, sub, figures, comparator }: { name: string; sub: string; figures: RunFigures; comparator: RunComparatorFigures | null }) {
  const { t_i18n } = useFormatter();
  const vs = comparator ? (comparator.partial ? t_i18n("{name} on the {count} shared reports", { name: comparator.name, count: comparator.mine.counted }) : comparator.name) : null;
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="content-base-bold">
        <span className="mr-2 content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Run")}</span>
        {name}
        <span className="font-normal text-default-secondary"> · {sub}</span>
      </h2>
      <p className="content-caption text-default-secondary">
        {vs
          ? t_i18n("Pooled over {count, plural, =0 {no report} one {the # report} other {the # reports}}, change vs {vs}", { count: figures.counted, vs })
          : t_i18n("Pooled over {count, plural, =0 {no report} one {the # report} other {the # reports}}", { count: figures.counted })}
        {figures.unreadable > 0 && (
          <span className="text-feedback-error-primary">
            {" · "}
            {t_i18n("{count, plural, one {# report without a readable answer} other {# reports without a readable answer}}", { count: figures.unreadable })}
          </span>
        )}
        {figures.leftOut > 0 && (
          <>
            {" · "}
            {t_i18n("{count, plural, one {# report left out (platform error or judge failure)} other {# reports left out (platform error or judge failure)}}", { count: figures.leftOut })}
          </>
        )}
      </p>
    </div>
  );
}

/** The run's figures: Bundle, Entity and Relationship F1 with P and R, the judge, cost and time. */
export function RunKpis({ figures, comparator, judgeOn }: { figures: RunFigures; comparator: RunComparatorFigures | null; judgeOn: boolean }) {
  const { t_i18n } = useFormatter();
  const costTime = useCostTime();
  const none = figures.counted > 0 && figures.unreadable === figures.counted;
  const cmpOk = !!comparator && comparator.figures.unreadable < comparator.figures.counted;
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5" aria-label={t_i18n("Run figures")} role="group">
      {([BUNDLE_F1, ENTITY_F1, RELATIONSHIP_F1] as const).map((key) => {
        const c = figures.counts[key];
        return (
          <Tile
            key={key}
            label={t_i18n(METRIC_LABELS[key])}
            value={none ? "—" : formatScore(figures.value[key])}
            delta={!none && cmpOk && comparator ? <Delta now={comparator.mine.value[key]} was={comparator.figures.value[key]} vs={comparator.name} /> : null}
            sub={c ? t_i18n("P {precision} · R {recall}", { precision: formatScore(precision(c)), recall: formatScore(recall(c)) }) : undefined}
          />
        );
      })}
      <Tile
        label={t_i18n("Judge score")}
        value={judgeOn && !none ? formatScore(figures.judge) : "—"}
        delta={judgeOn && !none && cmpOk && comparator ? <Delta now={comparator.mine.judge} was={comparator.figures.judge} vs={comparator.name} /> : null}
        sub={judgeOn ? t_i18n("LLM judge, mean of the reports") : t_i18n("No judge in this run")}
      />
      <Tile
        label={t_i18n("Cost · time")}
        value={costTime(figures)}
        sub={
          comparator
            ? comparator.partial
              ? t_i18n("{mine} vs {other} on the {count} shared reports", { mine: costTime(comparator.mine), other: costTime(comparator.figures), count: comparator.mine.counted })
              : t_i18n("{name}: {figure}", { name: comparator.name, figure: costTime(comparator.figures) })
            : t_i18n("All reports")
        }
      />
    </div>
  );
}

/** Where the entity points go: each cause with its count, a click filters the reports. */
export function CausesPanel({
  state,
  summary,
  allUnreadable,
  cause,
  onCause,
}: {
  state: "loading" | "error" | "ready";
  summary: CauseSummary | null;
  allUnreadable: boolean;
  cause: CauseKey | null;
  onCause: (cause: CauseKey | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const max = Math.max(1, ...(summary?.rows.map((r) => r.count) ?? []));
  const tone = (key: CauseKey) => (key === "missing" ? STATUS_COLOR.fn : key === "spurious" || key === "duplicates" ? STATUS_COLOR.fp : STATUS_COLOR.near);
  return (
    <section aria-label={t_i18n("Where the entity points go")} className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-default p-3">
      <h3 className="content-compact font-semibold">{t_i18n("Where the entity points go")}</h3>
      <p className="content-caption text-default-secondary">
        {allUnreadable
          ? t_i18n("No readable answer on any report.")
          : state === "loading"
            ? t_i18n("Reading the answer on every report…")
            : state === "error" || !summary
              ? t_i18n("The answers could not be read: the causes are not shown.")
              : `${t_i18n("{count, plural, =0 {No near-miss pair among the {errors} counted errors.} one {# near-miss pair makes {made} of {errors} counted errors.} other {# near-miss pairs make {made} of {errors} counted errors.}}", {
                  count: summary.nearPairs,
                  made: 2 * summary.nearPairs,
                  errors: summary.errors,
                })} ${t_i18n("Click a cause to see which reports have it.")}`}
      </p>
      {state === "ready" && summary && !allUnreadable && (
        <ul className="space-y-1">
          {summary.rows.map((row) => (
            <li key={row.key}>
              <button
                type="button"
                aria-pressed={cause === row.key}
                onClick={() => onCause(cause === row.key ? null : row.key)}
                className={cn(
                  "grid w-full grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-2 rounded-sm px-2 py-1 text-left content-compact transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
                  cause === row.key ? "bg-elevation-highlight" : "hover:bg-elevation-hover",
                )}
              >
                <span className="truncate">{t_i18n(causeLabel(row.key))}</span>
                <Bar value={(100 * row.count) / max} color={tone(row.key)} />
                <span className="tabular-nums text-default-secondary">
                  {row.unit === "pairs"
                    ? t_i18n("{count, plural, one {# pair} other {# pairs}}", { count: row.count })
                    : row.unit === "objects"
                      ? t_i18n("{count, plural, one {# object} other {# objects}}", { count: row.count })
                      : t_i18n("{count} extra", { count: row.count })}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
