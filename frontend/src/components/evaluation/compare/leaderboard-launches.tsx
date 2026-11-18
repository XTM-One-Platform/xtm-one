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
 * One evaluation's launches on its Leaderboard (#2): how a launch reads (its tag, its date,
 * what changed since the launch before) and the launch bar chart, which picks the launches
 * the board shows as columns.
 */

import { useMemo } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import {
  formatBoardValue,
  launchKind,
  launchNotes,
  zoomedAxis,
  type Launch,
  type LaunchNotePart,
  type LaunchOrder,
  type LaunchPick,
  type LaunchStats,
  type SetupChange,
} from "@/lib/evaluation-leaderboard";
import { cn } from "@/lib/utils";
import type { ComparedRun } from "@/types";
import { LAUNCH_KIND_SWATCHES } from "./compare-colors";

/** The launch picks and column orders, read by `t_i18n`. @i18n-keys */
const PICK_LABELS = {
  latest: "Latest",
  best: "Best",
  experiment: "Experiments",
  scheduled: "Scheduled reruns",
  all: "All",
} as const satisfies Record<Exclude<LaunchPick, "custom">, string>;
const ORDER_LABELS = {
  chrono: "Oldest first",
  newest: "Newest first",
  best: "Best run first",
  mean: "Best average first",
} as const satisfies Record<LaunchOrder, string>;

/** A launch's tag: its rank, oldest first. */
export const launchTag = (index: number) => `#${index + 1}`;

/** How launches read: a full date, a short one, what changed. */
export function useLaunchText(launches: readonly Launch[]) {
  const { t_i18n, intl } = useFormatter();
  const notes = useMemo(() => launchNotes(launches), [launches]);
  const change = (c: SetupChange): string => {
    if (c.kind === "version") {
      return t_i18n(c.field === "persona" ? "persona v{from} → v{to}" : "flow v{from} → v{to}", { from: c.from ?? "—", to: c.to ?? "—" });
    }
    if (c.kind === "mode") return t_i18n("mode {from} → {to}", { from: c.from ?? "—", to: c.to ?? "—" });
    return t_i18n(c.kind === "prompts" ? "prompts changed" : c.kind === "skills" ? "skills changed" : "agent settings changed");
  };
  const part = (p: LaunchNotePart): string =>
    p.status === "first"
      ? t_i18n("{subject}: first launch", { subject: p.subject })
      : p.status === "same"
        ? t_i18n("{subject}: same setup", { subject: p.subject })
        : `${p.subject}: ${p.changes.map(change).join(", ")}`;
  return {
    date: (launch: Launch) =>
      launch.time ? intl.formatDate(launch.time, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—",
    short: (launch: Launch) => (launch.time ? intl.formatDate(launch.time, { month: "2-digit", day: "2-digit" }) : "—"),
    note: (index: number) => (notes[index] ?? []).map(part).join("; "),
  };
}

interface Props {
  launches: Launch[];
  stats: LaunchStats[];
  metricKey: string;
  metricLabel: string;
  runCount: number;
  shown: number[];
  pick: LaunchPick;
  onPick: (pick: LaunchPick) => void;
  order: LaunchOrder;
  onOrder: (order: LaunchOrder) => void;
  /** The column order only matters to the configuration grid. */
  showOrder: boolean;
  onToggle: (index: number) => void;
  label: (run: ComparedRun) => string;
}

/** Every launch's best run, oldest first: a bar each, the axis zoomed on the range reached. */
export function LaunchPanel({ launches, stats, metricKey, metricLabel, runCount, shown, pick, onPick, order, onOrder, showOrder, onToggle, label }: Props) {
  const { t_i18n } = useFormatter();
  const text = useLaunchText(launches);
  const on = new Set(shown);
  const { min, max } = zoomedAxis(stats.map((s) => s.best));
  const fmt = (v: number | null) => formatBoardValue(v, metricKey);
  const experiments = launches.filter((l) => launchKind(l) === "experiment").length;
  const picks: { key: LaunchPick; label: string }[] = (Object.keys(PICK_LABELS) as Exclude<LaunchPick, "custom">[]).map((key) => ({ key, label: PICK_LABELS[key] }));
  if (pick === "custom") picks.push({ key: "custom", label: t_i18n("Custom · {count}", { count: on.size }) });

  return (
    <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <p className="content-compact">
            <span className="font-medium">{t_i18n("Launches")}</span>{" "}
            <span className="text-default-secondary">
              ·{" "}
              {t_i18n("{launches, plural, one {# launch} other {# launches}}, {runs, plural, one {# run} other {# runs}} · bar = best run of the launch, {metric}, axis from {min} to {max}", {
                launches: launches.length, runs: runCount, metric: metricLabel, min: fmt(min), max: fmt(max),
              })}
            </span>
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <SegmentedFilter
              options={picks}
              value={pick}
              onChange={(key: LaunchPick) => key !== "custom" && onPick(key)}
              ariaLabel={t_i18n("Launches shown")}
              selection="radio"
              size="sm"
            />
            {showOrder && (
              <label className="inline-flex items-center gap-2 content-compact text-default-secondary">
                {t_i18n("Columns")}
                <Select value={order} onValueChange={(v) => onOrder(v as LaunchOrder)}>
                  <SelectTrigger className="w-44" aria-label={t_i18n("Column order")}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(Object.keys(ORDER_LABELS) as LaunchOrder[]).map((key) => (
                      <SelectItem key={key} value={key}>{t_i18n(ORDER_LABELS[key])}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
            )}
          </div>
        </div>

        <div
          className="grid items-end gap-1 overflow-x-auto pb-0.5"
          style={{ gridTemplateColumns: `repeat(${launches.length}, minmax(1rem, 1fr))` }}
          role="group"
          aria-label={t_i18n("Best run of each launch, {metric}", { metric: metricLabel })}
        >
          {launches.map((launch, i) => {
            const s = stats[i];
            const kind = launchKind(launch);
            const height = s.best == null ? 0 : Math.max(4, ((s.best - min) / (max - min || 1)) * 100);
            const tip = [
              launchTag(i),
              text.date(launch),
              text.note(i),
              t_i18n("{count, plural, one {# run} other {# runs}}", { count: s.runs }),
              s.bestRun
                ? t_i18n("best {value} ({run})", { value: fmt(s.best), run: label(s.bestRun.run) })
                : t_i18n("best {value}", { value: fmt(s.best) }),
              t_i18n("average {value}", { value: fmt(s.mean) }),
            ].filter(Boolean).join(" · ");
            return (
              <Tip key={launch.key} content={tip}>
                <button
                  type="button"
                  aria-pressed={on.has(i)}
                  aria-label={tip}
                  onClick={() => onToggle(i)}
                  className="group flex min-w-4 flex-col items-stretch gap-1 rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                >
                  <span
                    className={cn(
                      "flex h-16 items-end overflow-hidden rounded-sm bg-elevation-hover",
                      on.has(i) && "ring-1 ring-inset ring-accent",
                    )}
                  >
                    <span
                      className={cn(
                        "block w-full rounded-t-sm transition-opacity",
                        LAUNCH_KIND_SWATCHES[kind],
                        on.has(i) ? "opacity-100" : "opacity-30 group-hover:opacity-70",
                      )}
                      style={{ height: `${height}%` }}
                    />
                  </span>
                  <span className={cn("content-caption tabular-nums text-center", on.has(i) ? "text-default-primary" : "text-default-secondary")}>
                    {i + 1}
                  </span>
                </button>
              </Tip>
            );
          })}
        </div>

        <div className="flex flex-wrap gap-x-4 gap-y-1 content-caption text-default-secondary">
          <span className="inline-flex items-center gap-1.5">
            <span className={cn("size-2.5 rounded-sm", LAUNCH_KIND_SWATCHES.experiment)} aria-hidden />
            {t_i18n("Experiment ({count, plural, one {# launch} other {# launches}} by hand)", { count: experiments })}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className={cn("size-2.5 rounded-sm", LAUNCH_KIND_SWATCHES.scheduled)} aria-hidden />
            {t_i18n("Scheduled rerun")}
          </span>
          <span>{t_i18n("Click a bar to add or remove its column; hover for what changed.")}</span>
        </div>
      </CardContent>
    </Card>
  );
}
