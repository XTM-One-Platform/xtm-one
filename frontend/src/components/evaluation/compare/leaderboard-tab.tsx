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
 * One evaluation's Leaderboard (#2): every completed run by launch. The best run of each
 * launch; then either a configuration × launch grid (each cell a run, its figure on its
 * own entries) or every run ranked on the shared records table.
 */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useFormatter } from "@/i18n";
import { headlineColorClass } from "@/lib/constants";
import type { BoardRun, Launch } from "@/lib/evaluation-leaderboard";
import { cn } from "@/lib/utils";
import type { ComparedMetric, ComparedRun } from "@/types";
import { LaunchBars } from "./compare-charts";

/** The launches the grid shows as columns, newest first. */
const GRID_LAUNCHES = 8;
const fmt = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const fmtCost = (v: number | null) => (v == null ? "—" : v < 0.1 ? `$${v.toFixed(3)}` : `$${v.toFixed(2)}`);
const fmtSeconds = (v: number | null) => (v == null ? "—" : `${Math.round(v)} s`);

type Rows = "configs" | "runs";
/** The two readings of the board, read by `t_i18n`. @i18n-keys */
const ROW_LABELS = { configs: "By configuration", runs: "Every run, ranked" } as const satisfies Record<Rows, string>;

interface Props {
  board: { launches: Launch[]; runs: BoardRun[] };
  metric: ComparedMetric;
  metrics: ComparedMetric[];
  /** A run's name, by what differs between the runs. */
  label: (run: ComparedRun) => string;
}

/** A launch's short name: its day and time in the user's locale. */
function useLaunchName() {
  const { intl } = useFormatter();
  return (launch: Launch) =>
    launch.time ? intl.formatDate(launch.time, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}

export function LeaderboardTab({ board, metric, metrics, label }: Props) {
  const { t_i18n, intl } = useFormatter();
  const [rows, setRows] = useState<Rows>("configs");
  const launchName = useLaunchName();
  const value = (r: BoardRun) => r.values[metric.key] ?? null;

  const bars = board.launches.map((launch, i) => {
    const own = board.runs.filter((r) => r.launch === i && value(r) != null);
    const best = own.reduce<BoardRun | null>((b, r) => (b && (value(b) as number) >= (value(r) as number) ? b : r), null);
    return {
      key: launch.key,
      label: launch.time ? intl.formatDate(launch.time, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—",
      value: best ? value(best) : null,
      title: best
        ? t_i18n("{launch}: best {value} by {run}, {count, plural, one {# run} other {# runs}}", {
            launch: launchName(launch), value: fmt(value(best)), run: label(best.run), count: launch.runs.length,
          })
        : launchName(launch),
    };
  });

  return (
    <div className="space-y-4">
      <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
        <CardContent className="space-y-3 p-4">
          <div>
            <h2 className="content-base font-medium">{t_i18n("Best run of each launch")}</h2>
            <p className="mt-1 content-compact text-default-secondary max-w-prose">
              {t_i18n("A launch is the runs started together. Each bar is its best run's {metric}, on that run's own entries; hover for which run.", { metric: t_i18n(metric.label) })}
            </p>
          </div>
          <LaunchBars bars={bars} metricLabel={t_i18n(metric.label)} />
        </CardContent>
      </Card>

      <SegmentedFilter
        options={(Object.keys(ROW_LABELS) as Rows[]).map((key) => ({ key, label: ROW_LABELS[key] }))}
        value={rows}
        onChange={(key: Rows) => setRows(key)}
        ariaLabel={t_i18n("Rows")}
        selection="radio"
      />
      {rows === "configs" ? (
        <ConfigGrid board={board} metric={metric} label={label} />
      ) : (
        <RankedRuns board={board} metric={metric} metrics={metrics} label={label} />
      )}
    </div>
  );
}

/** Configurations × launches: a statistics grid, each cell one run. */
function ConfigGrid({ board, metric, label }: Omit<Props, "metrics">) {
  const { t_i18n } = useFormatter();
  const launchName = useLaunchName();
  const value = (r: BoardRun) => r.values[metric.key] ?? null;
  const shown = board.launches.map((launch, index) => ({ launch, index })).slice(-GRID_LAUNCHES).reverse();
  const configs = useMemo(() => {
    const value = (r: BoardRun) => r.values[metric.key] ?? null;
    const byConfig = new Map<string, BoardRun[]>();
    for (const r of board.runs) byConfig.set(r.config, [...(byConfig.get(r.config) ?? []), r]);
    return [...byConfig.entries()]
      .map(([key, runs]) => {
        const scored = runs.filter((r) => value(r) != null).sort((a, b) => a.launch - b.launch);
        const best = scored.reduce<BoardRun | null>((b, r) => (b && (value(b) as number) >= (value(r) as number) ? b : r), null);
        const trend = scored.length > 1 ? (value(scored[scored.length - 1]) as number) - (value(scored[0]) as number) : null;
        return { key, name: label(runs[0].run), runs, best, trend, isDefault: runs[0].run.is_default_model };
      })
      .sort((a, b) => (b.best ? (value(b.best) as number) : -1) - (a.best ? (value(a.best) as number) : -1));
  }, [board, metric.key, label]);

  const cell = (r: BoardRun | undefined) =>
    r ? (
      <Link
        to={`/evaluations/runs/${r.run.id}`}
        className="inline-flex items-baseline gap-1 rounded-sm tabular-nums hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        aria-label={t_i18n("Open the run {run} of {launch}", { run: label(r.run), launch: launchName(board.launches[r.launch]) })}
      >
        <span className={cn("font-semibold", value(r) != null && headlineColorClass(value(r)))}>{fmt(value(r))}</span>
      </Link>
    ) : (
      <span className="text-default-secondary">·</span>
    );

  return (
    <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
      <CardContent className="space-y-2 p-4">
        <p className="content-caption text-default-secondary">
          {board.launches.length > GRID_LAUNCHES
            ? t_i18n("{metric} of each run on its own entries; the {shown} newest of {count} launches. Open a figure for its run.", {
                metric: t_i18n(metric.label), shown: GRID_LAUNCHES, count: board.launches.length,
              })
            : t_i18n("{metric} of each run on its own entries. Open a figure for its run.", { metric: t_i18n(metric.label) })}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] content-compact">
            <thead>
              <tr className="border-b border-elevation-subtle-soft text-left">
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Configuration")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Best run")}</th>
                {shown.map(({ launch }) => (
                  <th key={launch.key} scope="col" className="px-3 py-2 font-medium whitespace-nowrap">{launchName(launch)}</th>
                ))}
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("First → last run")}</th>
              </tr>
            </thead>
            <tbody>
              {configs.map((c) => (
                <tr key={c.key} className="border-b border-elevation-subtle-soft last:border-b-0">
                  <th scope="row" className="px-3 py-2 text-left font-normal">
                    <span className="block font-medium">{c.name}</span>
                    <span className="content-caption text-default-secondary">
                      {t_i18n("{count, plural, one {# run} other {# runs}}", { count: c.runs.length })}
                      {c.isDefault ? ` · ${t_i18n("default model")}` : ""}
                    </span>
                  </th>
                  <td className="px-3 py-2">{cell(c.best ?? undefined)}</td>
                  {shown.map(({ launch, index }) => (
                    <td key={launch.key} className="px-3 py-2">{cell(c.runs.find((r) => r.launch === index))}</td>
                  ))}
                  <td
                    className={cn(
                      "px-3 py-2 tabular-nums",
                      c.trend == null ? "text-default-secondary" : c.trend > 0.05 ? "text-feedback-success-primary" : c.trend < -0.05 ? "text-feedback-error-primary" : "text-default-secondary",
                    )}
                  >
                    {c.trend == null ? "—" : `${c.trend > 0 ? "+" : ""}${c.trend.toFixed(1)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

/** Every run, best first, on the shared records table. */
function RankedRuns({ board, metric, metrics, label }: Props) {
  const { t_i18n } = useFormatter();
  const launchName = useLaunchName();
  const ranked = useMemo(
    () => [...board.runs].sort((a, b) => (b.values[metric.key] ?? -1) - (a.values[metric.key] ?? -1)),
    [board.runs, metric.key],
  );
  const rank = new Map(ranked.map((r, i) => [r.run.id, i + 1]));
  const columns: DataTableColumn<BoardRun>[] = [
    {
      key: "run",
      header: "Run",
      cell: (r) => <TruncatedText text={label(r.run)} className="font-medium" />,
      sortValue: (r) => label(r.run).toLowerCase(),
      csv: (r) => label(r.run),
    },
    {
      key: "rank",
      header: "Rank",
      align: "right",
      cell: (r) => <span className="tabular-nums">{rank.get(r.run.id)}</span>,
      sortValue: (r) => rank.get(r.run.id) ?? 0,
      csv: (r) => rank.get(r.run.id) ?? "",
      hideable: true,
    },
    {
      key: "launch",
      header: "Launch",
      cell: (r) => <span className="whitespace-nowrap text-default-secondary">{launchName(board.launches[r.launch])}</span>,
      sortValue: (r) => board.launches[r.launch].time,
      csv: (r) => board.launches[r.launch].time,
      hideable: true,
    },
    ...[metric, ...metrics.filter((m) => m.key !== metric.key)].map<DataTableColumn<BoardRun>>((m) => ({
      key: `m:${m.key}`,
      header: m.label,
      align: "right",
      cell: (r) => (
        <span className={cn("tabular-nums", m.key === metric.key ? cn("font-semibold", headlineColorClass(r.values[m.key])) : "text-default-secondary")}>
          {fmt(r.values[m.key])}
        </span>
      ),
      sortValue: (r) => r.values[m.key] ?? -1,
      csv: (r) => (r.values[m.key] == null ? "" : (r.values[m.key] as number).toFixed(2)),
      firstSort: "desc",
      hideable: true,
    })),
    {
      key: "cost",
      header: "Cost per entry",
      align: "right",
      cell: (r) => <span className="tabular-nums text-default-secondary">{fmtCost(r.cost)}</span>,
      sortValue: (r) => r.cost ?? Number.POSITIVE_INFINITY,
      csv: (r) => (r.cost == null ? "" : r.cost.toFixed(5)),
      hideable: true,
    },
    {
      key: "seconds",
      header: "Time per entry",
      align: "right",
      cell: (r) => <span className="tabular-nums text-default-secondary">{fmtSeconds(r.seconds)}</span>,
      sortValue: (r) => r.seconds ?? Number.POSITIVE_INFINITY,
      csv: (r) => (r.seconds == null ? "" : r.seconds.toFixed(1)),
      hideable: true,
    },
  ];
  return (
    <DataTable
      rows={ranked}
      columns={columns}
      rowKey={(r) => r.run.id}
      rowHref={(r) => `/evaluations/runs/${r.run.id}`}
      defaultSort={{ key: `m:${metric.key}`, direction: "desc" }}
      defaultPageSize={25}
      searchFields={(r) => [label(r.run), r.run.model_id, r.run.subject_name]}
      searchPlaceholder={t_i18n("Search runs by model or agent")}
      filters={[
        { key: "config", label: "Configuration", values: (r) => [label(r.run)] },
        { key: "launch", label: "Launch", values: (r) => [launchName(board.launches[r.launch])] },
      ]}
      exportName="evaluation-runs"
      urlKey="lb_"
      storageKey="evaluation-leaderboard-runs"
      emptyLabel={t_i18n("No completed run")}
      rowTestId="leaderboard-run-row"
    />
  );
}
