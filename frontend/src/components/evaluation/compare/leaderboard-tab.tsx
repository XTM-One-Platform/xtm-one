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
 * One evaluation's Leaderboard (#2): every completed run by launch. A click on a run opens
 * its review (shift+click sets a reference first); the metric may be a pooled one, the
 * judge score, the cost or the duration. The best run of each launch picks the launches
 * shown; then either a configuration × launch grid (each cell a run, its figure on its own
 * entries, sortable on any column, launch columns six at a time) or every run of the
 * launches shown, ranked, on the shared records table.
 */

import { Fragment, useMemo, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useFormatter } from "@/i18n";
import {
  BOARD_METRIC_LABELS,
  boardValue,
  configRows,
  COST_METRIC,
  DURATION_METRIC,
  formatBoardValue,
  goodness,
  higherIsBetter,
  JUDGE_METRIC,
  LAUNCH_PAGE,
  launchKind,
  launchStats,
  shownLaunches,
  sortConfigRows,
  type BoardRun,
  type ConfigRow,
  type GridSort,
  type Launch,
  type LaunchOrder,
  type LaunchPick,
} from "@/lib/evaluation-leaderboard";
import { cn } from "@/lib/utils";
import type { ComparedMetric, ComparedRun } from "@/types";
import { launchTag, LaunchPanel, useLaunchText } from "./leaderboard-launches";
import { ReviewHead, useBoardReview } from "./leaderboard-review";

type Rows = "configs" | "runs";
/** The two readings of the board, read by `t_i18n`. @i18n-keys */
const ROW_LABELS = { configs: "By configuration", runs: "Every run, ranked" } as const satisfies Record<Rows, string>;

interface Props {
  board: { launches: Launch[]; runs: BoardRun[] };
  metric: ComparedMetric;
  metrics: ComparedMetric[];
  /** A run's name, by what differs between the runs. */
  label: (run: ComparedRun) => string;
  /** Told when a pooled metric is picked here, so the page's other tabs follow. */
  onMetricChange?: (key: string) => void;
}

interface BoardMetric {
  key: string;
  label: string;
}

/** Who a cell is in the current review: A, the reference; B, the run reviewed. */
type Roles = { a: string | null; b: string | null };

export function LeaderboardTab({ board, metric, metrics, label, onMetricChange }: Props) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [rows, setRows] = useState<Rows>("configs");
  const [pick, setPick] = useState<LaunchPick>("latest");
  const [order, setOrder] = useState<LaunchOrder>("chrono");
  const [custom, setCustom] = useState<Set<number>>(new Set());
  // The board's metric follows the page's until one is picked here.
  const [picked, setPicked] = useState<{ key: string; from: string } | null>(null);
  const key = picked && picked.from === metric.key ? picked.key : metric.key;

  const boardMetrics: BoardMetric[] = [
    ...metrics.map((m) => ({ key: m.key, label: m.label })),
    ...[JUDGE_METRIC, COST_METRIC, DURATION_METRIC].map((k) => ({ key: k, label: BOARD_METRIC_LABELS[k as keyof typeof BOARD_METRIC_LABELS] })),
  ];
  const current = boardMetrics.find((m) => m.key === key) ?? boardMetrics[0];
  const pickMetric = (next: string) => {
    if (onMetricChange && metrics.some((m) => m.key === next)) {
      onMetricChange(next);
      setPicked(null);
    } else setPicked({ key: next, from: metric.key });
  };

  const stats = useMemo(() => launchStats(board, key), [board, key]);
  const shown = useMemo(() => shownLaunches(board.launches, stats, key, pick, order, custom), [board.launches, stats, key, pick, order, custom]);

  const byId = useMemo(() => new Map(board.runs.map((r) => [r.run.id, r])), [board.runs]);
  const nameOf = (runId: string) => {
    const r = byId.get(runId);
    return r ? `${label(r.run)} · ${launchTag(r.launch)}` : runId;
  };
  const evaluationId = board.runs[0]?.run.evaluation_id ?? "";
  const review = useBoardReview(evaluationId, nameOf);
  // A records-table row opens on its own click: the modifier is read on the way down.
  const shift = useRef(false);

  const toggle = (index: number) => {
    const next = new Set(shown);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    if (!next.size) {
      toast({ title: t_i18n("Keep at least one launch."), tag: "board-launches" });
      return;
    }
    setCustom(next);
    setPick("custom");
  };

  return (
    <div
      className="space-y-4"
      onClickCapture={(e) => (shift.current = e.shiftKey || e.altKey)}
      onKeyDownCapture={(e) => (shift.current = e.shiftKey || e.altKey)}
    >
      <ReviewHead reference={review.reference} current={review.current} nameOf={nameOf} onClear={review.clearReference} />
      <div className="flex flex-wrap items-center gap-3">
        <SegmentedFilter
          options={boardMetrics.map((m) => ({ key: m.key, label: m.label }))}
          value={key}
          onChange={(next: string) => pickMetric(next)}
          ariaLabel={t_i18n("Metric")}
          selection="radio"
        />
        <SegmentedFilter
          options={(Object.keys(ROW_LABELS) as Rows[]).map((k) => ({ key: k, label: ROW_LABELS[k] }))}
          value={rows}
          onChange={(k: Rows) => setRows(k)}
          ariaLabel={t_i18n("Rows")}
          selection="radio"
        />
      </div>

      <LaunchPanel
        launches={board.launches}
        stats={stats}
        metricKey={key}
        metricLabel={t_i18n(current.label)}
        runCount={board.runs.length}
        shown={shown}
        pick={pick}
        onPick={setPick}
        order={order}
        onOrder={setOrder}
        showOrder={rows === "configs"}
        onToggle={toggle}
        label={label}
      />

      {rows === "configs" ? (
        <ConfigGrid
          board={board}
          metricKey={key}
          shown={shown}
          label={label}
          roles={review.roles}
          onRun={(r, asReference) => review.open(r.run.id, asReference)}
        />
      ) : (
        <RankedRuns
          board={board}
          metric={current}
          metrics={boardMetrics}
          shown={shown}
          label={label}
          roles={review.roles}
          onRun={(r) => review.open(r.run.id, shift.current)}
        />
      )}
    </div>
  );
}

/** The "A" / "B" chip of a run in the current review. */
function RoleChip({ runId, roles }: { runId: string; roles: Roles }) {
  const role = runId === roles.a ? "A" : runId === roles.b ? "B" : null;
  return role ? (
    <span className="rounded-sm bg-elevation-highlight px-1 content-caption font-semibold text-default-primary">{role}</span>
  ) : null;
}

/** The "!" of a run with entries whose answer the scorer could not read. */
function UnreadableMark({ count }: { count: number }) {
  const { t_i18n } = useFormatter();
  if (!count) return null;
  const text = t_i18n("{count, plural, one {# entry} other {# entries}} without a readable answer", { count });
  return (
    <span
      className="inline-grid size-3.5 place-items-center rounded-full border border-current content-caption leading-none text-feedback-error-primary"
      aria-label={text}
      role="img"
    >
      !
    </span>
  );
}

/** A thin bar under a figure, its share of the board's best. */
function ValueBar({ value, max, className }: { value: number; max: number; className?: string }) {
  return (
    <span className={cn("mt-1 block h-0.5 overflow-hidden rounded-full bg-elevation-hover", className)} aria-hidden>
      <span className="block h-full rounded-full bg-accent opacity-70" style={{ width: `${Math.min(100, (value / max) * 100)}%` }} />
    </span>
  );
}

/** Configurations × launches: a statistics grid, each cell one run. */
function ConfigGrid({
  board,
  metricKey,
  shown,
  label,
  roles,
  onRun,
}: {
  board: { launches: Launch[]; runs: BoardRun[] };
  metricKey: string;
  shown: number[];
  label: (run: ComparedRun) => string;
  roles: Roles;
  onRun: (r: BoardRun, asReference: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const text = useLaunchText(board.launches);
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<GridSort>({ col: "best", dir: -1 });
  const [everyConfig, setEveryConfig] = useState(false);
  const higher = higherIsBetter(metricKey);
  const fmt = (v: number | null | undefined) => formatBoardValue(v, metricKey);

  const pages = Math.max(1, Math.ceil(shown.length / LAUNCH_PAGE));
  const at = Math.min(page, pages - 1);
  const cols = shown.slice(at * LAUNCH_PAGE, (at + 1) * LAUNCH_PAGE);

  const name = (row: ConfigRow) => label(row.runs[0].run);
  const all = useMemo(() => configRows(board.runs, metricKey, shown), [board.runs, metricKey, shown]);
  const hidden = all.filter((r) => !r.inShown).length;
  const rows = sortConfigRows(everyConfig ? all : all.filter((r) => r.inShown), sort, metricKey, name);
  const max = Math.max(1, ...all.flatMap((r) => r.scored.map((x) => boardValue(x, metricKey) as number)));

  const sortOn = (col: GridSort["col"]) =>
    setSort((cur) => ({ col, dir: cur.col === col ? (cur.dir === 1 ? -1 : 1) : col === "name" ? 1 : -1 }));
  const header = (col: GridSort["col"], title: string, sub?: string, tip?: string) => {
    const button = (
      <button
        type="button"
        onClick={() => sortOn(col)}
        className="inline-flex flex-col items-start rounded-sm text-left font-medium focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
      >
        <span className="whitespace-nowrap">
          {title}
          {sort.col === col && <span aria-hidden>{sort.dir < 0 ? " ↓" : " ↑"}</span>}
        </span>
        {sub && <span className="content-caption font-normal text-default-secondary whitespace-nowrap">{sub}</span>}
      </button>
    );
    return (
      <th
        scope="col"
        className="px-3 py-2 align-bottom"
        aria-sort={sort.col === col ? (sort.dir < 0 ? "descending" : "ascending") : undefined}
      >
        {tip ? <Tip content={tip}>{button}</Tip> : button}
      </th>
    );
  };

  const runCell = (r: BoardRun, extra?: string) => {
    const v = boardValue(r, metricKey);
    const launch = board.launches[r.launch];
    const tip = [
      label(r.run),
      `${launchTag(r.launch)} · ${text.date(launch)}`,
      r.unreadable
        ? t_i18n("{count, plural, one {# entry} other {# entries}} without a readable answer", { count: r.unreadable })
        : null,
    ].filter(Boolean).join(" · ");
    return (
      <Tip content={tip}>
        <button
          type="button"
          onClick={(e) => onRun(r, e.shiftKey || e.altKey)}
          aria-label={t_i18n("Review the run {run}: {value}", { run: tip, value: fmt(v) })}
          className={cn(
            "block w-full min-w-16 rounded-sm px-1 py-0.5 text-left hover:bg-elevation-hover focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
            (r.run.id === roles.a || r.run.id === roles.b) && "ring-1 ring-inset ring-accent",
          )}
        >
          <span className="inline-flex items-center gap-1 tabular-nums">
            <span className="font-semibold">{fmt(v)}</span>
            <RoleChip runId={r.run.id} roles={roles} />
            {v != null && <UnreadableMark count={r.unreadable} />}
            {extra && <span className="content-caption text-default-secondary">{extra}</span>}
          </span>
          {v != null && higher && <ValueBar value={v} max={max} />}
        </button>
      </Tip>
    );
  };

  const spark = (row: ConfigRow) => {
    const n = board.launches.length;
    if (row.scored.length < 2) return null;
    const step = 112 / Math.max(1, n - 1);
    const values = all.flatMap((r) => r.scored.map((x) => boardValue(x, metricKey) as number));
    const top = Math.max(...values);
    const bottom = higher ? 0 : Math.min(...values);
    const y = (v: number) => 26 - ((v - bottom) / (top - bottom || 1)) * 22;
    const pts = row.scored.map((r) => ({ x: 4 + r.launch * step, y: y(boardValue(r, metricKey) as number), on: shown.includes(r.launch) }));
    const up = (row.trend ?? 0) >= 0;
    const color = (higher ? up : !up) ? "var(--color-feedback-success-primary)" : "var(--color-feedback-error-primary)";
    return (
      <svg viewBox="0 0 120 30" className="h-6 w-28 shrink-0" aria-hidden>
        <polyline points={pts.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" strokeWidth={1.2} style={{ stroke: color, opacity: 0.85 }} />
        {pts.filter((p) => p.on).map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={2.1} style={{ fill: color }} />
        ))}
      </svg>
    );
  };

  return (
    <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="inline-flex items-center gap-2 content-compact text-default-secondary tabular-nums">
            {pages > 1 ? (
              <>
                {t_i18n("Launch columns {from}–{to} of {count}", { from: at * LAUNCH_PAGE + 1, to: at * LAUNCH_PAGE + cols.length, count: shown.length })}
                <Button variant="outline" size="icon" aria-label={t_i18n("Previous launch columns")} disabled={at === 0} onClick={() => setPage(at - 1)}>
                  <Icon name="chevron-left" size={16} />
                </Button>
                <Button variant="outline" size="icon" aria-label={t_i18n("Next launch columns")} disabled={at >= pages - 1} onClick={() => setPage(at + 1)}>
                  <Icon name="chevron-right" size={16} />
                </Button>
              </>
            ) : (
              t_i18n("{count, plural, one {# launch column} other {# launch columns}}", { count: shown.length })
            )}
          </div>
          {(hidden > 0 || everyConfig) && (
            <Button variant="ghost" size="sm" onClick={() => setEveryConfig(!everyConfig)}>
              {everyConfig
                ? t_i18n("Hide configurations without a run in these launches")
                : t_i18n("Show {count, plural, one {# more configuration} other {# more configurations}} without a run in these launches", { count: hidden })}
            </Button>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] content-compact">
            <thead>
              <tr className="border-b border-elevation-subtle-soft text-left">
                {header("name", t_i18n("Configuration"))}
                {header("best", t_i18n("Best run"), t_i18n("all {count, plural, one {# launch} other {# launches}}", { count: board.launches.length }))}
                {cols.map((index) => {
                  const launch = board.launches[index];
                  const sub = launchKind(launch) === "scheduled" ? `${text.short(launch)} · ${t_i18n("scheduled")}` : text.short(launch);
                  const count = board.runs.filter((r) => r.launch === index).length;
                  const tip = [launchTag(index), text.date(launch), text.note(index), t_i18n("{count, plural, one {# configuration} other {# configurations}}", { count })]
                    .filter(Boolean)
                    .join(" · ");
                  return <Fragment key={launch.key}>{header(index, launchTag(index), sub, tip)}</Fragment>;
                })}
                {header("trend", t_i18n("Trend"), t_i18n("all launches · first → last"))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const first = row.scored[0];
                const last = row.scored[row.scored.length - 1];
                return (
                  <tr key={row.key} className="border-b border-elevation-subtle-soft last:border-b-0">
                    <th scope="row" className="px-3 py-2 text-left font-normal">
                      <span className="flex items-center gap-2">
                        <span className="font-medium">{name(row)}</span>
                        {row.runs[0].run.is_default_model && <Badge variant="success">{t_i18n("default")}</Badge>}
                      </span>
                      <span className="content-caption text-default-secondary">
                        {row.runs[0].run.model_id} · {t_i18n("{count, plural, one {# run} other {# runs}}", { count: row.runs.length })}
                      </span>
                    </th>
                    <td className="px-2 py-1.5">{row.best ? runCell(row.best, launchTag(row.best.launch)) : null}</td>
                    {cols.map((index) => {
                      const r = row.runs.find((x) => x.launch === index);
                      return (
                        <td key={index} className="px-2 py-1.5">
                          {r ? (
                            runCell(r)
                          ) : (
                            <Tip content={t_i18n("{config} was not run in {launch}", { config: name(row), launch: launchTag(index) })}>
                              <span className="block px-1 text-default-secondary">
                                <span aria-hidden>·</span>
                                <span className="sr-only">{t_i18n("{config} was not run in {launch}", { config: name(row), launch: launchTag(index) })}</span>
                              </span>
                            </Tip>
                          )}
                        </td>
                      );
                    })}
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center gap-2">
                        {spark(row)}
                        {row.trend != null && higher && first && last && (
                          <Tip
                            content={`${launchTag(first.launch)} ${fmt(boardValue(first, metricKey))} → ${launchTag(last.launch)} ${fmt(boardValue(last, metricKey))}`}
                          >
                            <span
                              className={cn(
                                "tabular-nums",
                                row.trend > 0.05 ? "text-feedback-success-primary" : row.trend < -0.05 ? "text-feedback-error-primary" : "text-default-secondary",
                              )}
                            >
                              {row.trend > 0.05 ? "+" : row.trend < -0.05 ? "−" : "±"}
                              {Math.abs(row.trend).toFixed(1)}
                            </span>
                          </Tip>
                        )}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

/** Every run of the launches shown, best first, on the shared records table. */
function RankedRuns({
  board,
  metric,
  metrics,
  shown,
  label,
  roles,
  onRun,
}: {
  board: { launches: Launch[]; runs: BoardRun[] };
  metric: BoardMetric;
  metrics: BoardMetric[];
  shown: number[];
  label: (run: ComparedRun) => string;
  roles: Roles;
  onRun: (r: BoardRun) => void;
}) {
  const { t_i18n } = useFormatter();
  const text = useLaunchText(board.launches);
  const key = metric.key;
  const ranked = useMemo(() => {
    const on = new Set(shown);
    return board.runs
      .filter((r) => on.has(r.launch))
      .sort((a, b) => goodness(boardValue(b, key), key) - goodness(boardValue(a, key), key) || a.launch - b.launch);
  }, [board.runs, shown, key]);
  const rank = new Map(ranked.map((r, i) => [r.run.id, i + 1]));
  const max = Math.max(1, ...ranked.map((r) => boardValue(r, key) ?? 0));
  const launchLine = (r: BoardRun) => `${launchTag(r.launch)} ${text.date(board.launches[r.launch])}`;

  const columns: DataTableColumn<BoardRun>[] = [
    {
      key: "rank",
      header: "Rank",
      align: "right",
      cell: (r) => <span className="tabular-nums">{rank.get(r.run.id)}</span>,
      sortValue: (r) => rank.get(r.run.id) ?? 0,
      csv: (r) => rank.get(r.run.id) ?? "",
    },
    {
      key: "run",
      header: "Run",
      cell: (r) => (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <TruncatedText text={label(r.run)} className="font-medium" />
          <RoleChip runId={r.run.id} roles={roles} />
          <UnreadableMark count={r.unreadable} />
        </span>
      ),
      sortValue: (r) => label(r.run).toLowerCase(),
      csv: (r) => label(r.run),
      hideable: true,
    },
    {
      key: "launch",
      header: "Launch",
      cell: (r) => (
        <span className="block min-w-0">
          <span className="block whitespace-nowrap tabular-nums">{launchLine(r)}</span>
          <TruncatedText text={text.note(r.launch)} className="content-caption text-default-secondary" />
        </span>
      ),
      sortValue: (r) => board.launches[r.launch].time,
      csv: (r) => `${launchLine(r)} · ${text.note(r.launch)}`,
      hideable: true,
    },
    ...[metric, ...metrics.filter((m) => m.key !== key)].map<DataTableColumn<BoardRun>>((m) => ({
      key: `m:${m.key}`,
      header: m.label,
      align: "right",
      cell: (r) => {
        const v = boardValue(r, m.key);
        return m.key === key ? (
          <span className="inline-block min-w-20 text-right">
            <span className="font-semibold tabular-nums">{formatBoardValue(v, m.key)}</span>
            {v != null && higherIsBetter(m.key) && <ValueBar value={v} max={max} className="w-20" />}
          </span>
        ) : (
          <span className="tabular-nums text-default-secondary">{formatBoardValue(v, m.key)}</span>
        );
      },
      sortValue: (r) => goodness(boardValue(r, m.key), m.key),
      csv: (r) => {
        const v = boardValue(r, m.key);
        return v == null ? "" : m.key === COST_METRIC ? v.toFixed(5) : v.toFixed(2);
      },
      firstSort: "desc",
      hideable: true,
    })),
    {
      key: "cost",
      header: "Cost per entry",
      align: "right",
      cell: (r) => <span className="tabular-nums text-default-secondary">{r.cost == null ? "—" : `$${r.cost < 0.1 ? r.cost.toFixed(3) : r.cost.toFixed(2)}`}</span>,
      sortValue: (r) => r.cost ?? Number.POSITIVE_INFINITY,
      csv: (r) => (r.cost == null ? "" : r.cost.toFixed(5)),
      hideable: true,
      defaultHidden: true,
    },
    {
      key: "seconds",
      header: "Time per entry",
      align: "right",
      cell: (r) => <span className="tabular-nums text-default-secondary">{r.seconds == null ? "—" : `${Math.round(r.seconds)} s`}</span>,
      sortValue: (r) => r.seconds ?? Number.POSITIVE_INFINITY,
      csv: (r) => (r.seconds == null ? "" : r.seconds.toFixed(1)),
      hideable: true,
      defaultHidden: true,
    },
  ];
  return (
    <div className="space-y-2">
      <p className="content-caption text-default-secondary">
        {t_i18n("Runs in the {count, plural, one {# launch} other {# launches}} shown, best first. Click a run to review it; shift+click to set it as the reference A.", { count: shown.length })}
      </p>
      <DataTable
        rows={ranked}
        columns={columns}
        rowKey={(r) => r.run.id}
        onRowClick={onRun}
        defaultSort={{ key: `m:${key}`, direction: "desc" }}
        defaultPageSize={25}
        rowLines={2}
        searchFields={(r) => [label(r.run), r.run.model_id, r.run.subject_name]}
        searchPlaceholder={t_i18n("Search runs by model or agent")}
        filters={[
          { key: "config", label: "Configuration", values: (r) => [label(r.run)] },
          { key: "launch", label: "Launch", values: (r) => [launchTag(r.launch)] },
        ]}
        exportName="evaluation-runs"
        urlKey="lb_"
        storageKey="evaluation-leaderboard-runs"
        emptyLabel={t_i18n("No completed run")}
        rowTestId="leaderboard-run-row"
      />
    </div>
  );
}
