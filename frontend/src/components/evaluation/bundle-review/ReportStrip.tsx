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

// The report level of a run review: which report of the run is open ("Report 2 of 5"), and
// one tile per report with the run's Entity F1 and its change; a tile without the cause
// picked in "Where the entity points go" is dimmed.

import { useFormatter } from "@/i18n";
import { formatScore } from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { Bar, Delta } from "./figures";

export interface StripTile {
  key: string;
  label: string;
  /** The run's Entity F1 on this report. */
  value: number | null;
  /** The comparator's, when it has a readable counted answer here. */
  was: number | null;
  /** `unreadable`: no readable answer; `left-out`: the run's figures leave it out. */
  state: "ok" | "unreadable" | "left-out";
  dimmed: boolean;
}

const short = (s: string, n = 30) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export function ReportHead({ index, count, title, runName, comparatorName, cause }: {
  index: number;
  count: number;
  title: string;
  runName: string;
  comparatorName: string | null;
  cause: string | null;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="content-base-bold">
        <span className="mr-2 content-caption font-medium uppercase tracking-wider text-default-secondary">
          {t_i18n("Report {index} of {count}", { index, count })}
        </span>
        {title}
      </h2>
      <p className="content-caption text-default-secondary">
        {comparatorName
          ? t_i18n("Entity F1 of {run} on each report, change vs {comparator}", { run: runName, comparator: comparatorName })
          : t_i18n("Entity F1 of {run} on each report", { run: runName })}
        {cause && ` · ${t_i18n("dimmed: no “{cause}”", { cause })}`}
      </p>
    </div>
  );
}

export function ReportStrip({ tiles, current, comparatorName, onPick }: { tiles: StripTile[]; current: string; comparatorName: string | null; onPick: (key: string) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <div role="tablist" aria-label={t_i18n("Reports")} className="flex gap-2 overflow-x-auto pb-1">
      {tiles.map((tile, i) => (
        <button
          key={tile.key}
          type="button"
          role="tab"
          aria-selected={tile.key === current}
          title={tile.label}
          onClick={() => onPick(tile.key)}
          className={cn(
            "flex w-44 shrink-0 flex-col gap-1 rounded-sm border px-2 py-1.5 text-left transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
            tile.key === current ? "border-accent bg-elevation-highlight" : "border-elevation-subtle hover:bg-elevation-hover",
            tile.dimmed && "opacity-40",
          )}
        >
          <span className="flex min-w-0 items-center gap-1.5 content-caption">
            <span className="tabular-nums font-semibold">{i + 1}</span>
            <span className="truncate text-default-secondary">{short(tile.label)}</span>
          </span>
          <span className="flex items-baseline gap-1.5 content-compact tabular-nums">
            {tile.state === "unreadable" ? (
              <span className="text-feedback-error-primary">{t_i18n("no answer")}</span>
            ) : tile.state === "left-out" ? (
              <span className="text-default-secondary" title={t_i18n("A platform error or a judge failure: left out of the figures.")}>
                {t_i18n("left out")}
              </span>
            ) : (
              <>
                <span className="font-semibold">{formatScore(tile.value)}</span>
                {comparatorName && <Delta now={tile.value} was={tile.was} vs={comparatorName} />}
              </>
            )}
          </span>
          <Bar value={tile.state === "ok" ? tile.value : 0} />
        </button>
      ))}
    </div>
  );
}
