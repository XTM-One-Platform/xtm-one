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
 * One evaluation's Results (#2): each configuration (agent × model) pooled over its runs
 * and entries, ranked with 95% intervals; its figure against its cost or time per entry,
 * with the Pareto frontier; how far reruns of one configuration spread; and how every
 * figure is computed, to publish next to it. Exports the ranking as CSV.
 */

import { useMemo, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import { rankSpread, seedOf, type Counts } from "@/lib/evaluation-compare";
import {
  countsOf,
  paretoFront,
  perEntry,
  poolRuns,
  spreadOf,
  toCsv,
  type BoardRun,
  type Launch,
} from "@/lib/evaluation-leaderboard";
import { saveBlobAs } from "@/lib/utils";
import type { ComparedCell, ComparedMetric, ComparedRun } from "@/types";
import { ConsistencyChart, ForestPlot, ParetoChart } from "./compare-charts";

const fmt = (v: number) => v.toFixed(1);

type RunSet = "newest" | "all";
type Axis = "cost" | "time";
/** The run sets and axes, read by `t_i18n`. @i18n-keys */
const SET_LABELS = { newest: "Newest run of each configuration", all: "Every run" } as const satisfies Record<RunSet, string>;
const AXIS_LABELS = { cost: "Cost", time: "Time" } as const satisfies Record<Axis, string>;

interface Props {
  evaluationName: string;
  board: { launches: Launch[]; runs: BoardRun[] };
  cells: Map<string, Map<string, ComparedCell>>;
  metric: ComparedMetric;
  metrics: ComparedMetric[];
  entryCount: number;
  label: (run: ComparedRun) => string;
}

function Section({ title, lead, tools, children }: { title: string; lead?: string; tools?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="content-base font-medium">{title}</h2>
            {lead && <p className="mt-1 content-compact text-default-secondary max-w-prose">{lead}</p>}
          </div>
          {tools}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}

export function ResultsTab({ evaluationName, board, cells, metric, metrics, entryCount, label }: Props) {
  const { t_i18n } = useFormatter();
  const [set, setSet] = useState<RunSet>("newest");
  const [axis, setAxis] = useState<Axis>("cost");

  const byConfig = useMemo(() => {
    const out = new Map<string, BoardRun[]>();
    for (const r of board.runs) out.set(r.config, [...(out.get(r.config) ?? []), r]);
    return out;
  }, [board.runs]);

  // Each configuration pooled over the runs of the set, then ranked.
  const ranking = useMemo(() => {
    const rows = [...byConfig.entries()]
      .map(([config, runs], i) => {
        const picked = set === "all" ? runs : [runs.reduce((a, b) => ((b.run.created_at ?? "") > (a.run.created_at ?? "") ? b : a))];
        const perRun: Counts[][] = picked.map((r) => countsOf(cells, r.run.id, metric.key));
        const interval = poolRuns(perRun, seedOf(config + metric.key + set));
        if (!interval) return null;
        const ids = picked.map((r) => r.run.id);
        return {
          id: config,
          label: label(picked[0].run),
          run: picked[0].run,
          runs: picked.length,
          colorIndex: i,
          cost: perEntry(cells, ids, "cost"),
          seconds: perEntry(cells, ids, "seconds"),
          ...interval,
        };
      })
      .filter((x): x is NonNullable<typeof x> => x !== null)
      .sort((a, b) => b.value - a.value);
    return rankSpread(rows);
  }, [byConfig, cells, metric.key, set, label]);

  const points = ranking
    .map((r) => ({ ...r, x: axis === "cost" ? r.cost : r.seconds }))
    .filter((p): p is typeof p & { x: number } => p.x != null)
    .map((p) => ({ ...p, y: p.value }));
  const front = new Set(paretoFront(points).map((p) => p.id));
  const missing = ranking.filter((r) => (axis === "cost" ? r.cost : r.seconds) == null);

  const spreads = [...byConfig.entries()]
    .map(([config, runs]) => {
      const values = runs.map((r) => r.values[metric.key]).filter((v): v is number => v != null);
      const spread = spreadOf(values);
      return spread ? { id: config, label: label(runs[0].run), values, ...spread } : null;
    })
    .filter((x): x is NonNullable<typeof x> => x !== null)
    .sort((a, b) => b.mean - a.mean);

  const firsts = ranking.filter((r) => r.best === 1);
  const stable = [...spreads].sort((a, b) => a.sd - b.sd);

  const exportCsv = () => {
    const csv = toCsv([
      ["rank_best", "rank_worst", "configuration", "agent", "model", "runs", "metric", "value", "ci95_low", "ci95_high", "cost_per_entry_usd", "seconds_per_entry"],
      ...ranking.map((r) => [
        r.best, r.worst, r.label, r.run.subject_name, r.run.model_id, r.runs, metric.key,
        r.value.toFixed(2), r.low.toFixed(2), r.high.toFixed(2),
        r.cost == null ? null : r.cost.toFixed(5), r.seconds == null ? null : r.seconds.toFixed(1),
      ]),
    ]);
    const slug = evaluationName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "evaluation";
    saveBlobAs(new Blob([csv], { type: "text/csv;charset=utf-8" }), `${slug}-results-${metric.key}.csv`);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <SegmentedFilter
          options={(Object.keys(SET_LABELS) as RunSet[]).map((key) => ({ key, label: SET_LABELS[key] }))}
          value={set}
          onChange={(key: RunSet) => setSet(key)}
          ariaLabel={t_i18n("Runs pooled")}
          selection="radio"
        />
        <Button variant="outline" size="sm" className="ml-auto gap-1.5" onClick={exportCsv} disabled={!ranking.length}>
          <Icon name="download" size={16} /> {t_i18n("Export CSV")}
        </Button>
      </div>

      <Section
        title={t_i18n("1 · Ranking with 95% intervals")}
        lead={
          !ranking.length
            ? t_i18n("No run has a figure for this metric.")
            : firsts.length > 1
              ? t_i18n("{count} configurations can be first: their intervals overlap, so no single best can be claimed.", { count: firsts.length })
              : t_i18n("{label} is first, with no overlap with the next one.", { label: ranking[0].label })
        }
      >
        {ranking.length > 0 && <ForestPlot rows={ranking} metricLabel={t_i18n(metric.label)} />}
        <p className="content-caption text-default-secondary">
          {set === "all"
            ? t_i18n("One line per agent and model, pooled over all its runs and their entries. A rank such as 1–4 means it could be anywhere from first to fourth.")
            : t_i18n("One line per agent and model, its newest run pooled over its entries. A rank such as 1–4 means it could be anywhere from first to fourth.")}
        </p>
      </Section>

      <Section
        title={axis === "cost" ? t_i18n("2 · Quality against cost") : t_i18n("2 · Quality against time")}
        lead={
          points.length
            ? t_i18n("Pareto frontier: {front}. Every other configuration is beaten by one of them on both the figure and the {axis}.", {
                front: points.filter((p) => front.has(p.id)).sort((a, b) => a.x - b.x).map((p) => p.label).join(" → "),
                axis: axis === "cost" ? t_i18n("cost") : t_i18n("time"),
              })
            : t_i18n("No configuration has a recorded {axis} per entry.", { axis: axis === "cost" ? t_i18n("cost") : t_i18n("time") })
        }
        tools={
          <SegmentedFilter
            options={(Object.keys(AXIS_LABELS) as Axis[]).map((key) => ({ key, label: AXIS_LABELS[key] }))}
            value={axis}
            onChange={(key: Axis) => setAxis(key)}
            ariaLabel={t_i18n("Axis")}
            selection="radio"
          />
        }
      >
        {points.length > 0 && (
          <ParetoChart points={points.map((p) => ({ ...p, onFront: front.has(p.id) }))} axis={axis} metricLabel={t_i18n(metric.label)} />
        )}
        {missing.length > 0 && (
          <p className="content-caption text-default-secondary">
            {t_i18n("Not shown, no {axis} recorded: {names}.", {
              axis: axis === "cost" ? t_i18n("cost") : t_i18n("time"),
              names: missing.map((r) => r.label).join(", "),
            })}
          </p>
        )}
      </Section>

      <Section
        title={t_i18n("3 · Consistency across reruns")}
        lead={
          stable.length
            ? t_i18n("Most stable: {best} (sd {bestSd}). Least stable: {worst} (sd {worstSd}, {min} to {max}).", {
                best: stable[0].label, bestSd: fmt(stable[0].sd),
                worst: stable[stable.length - 1].label, worstSd: fmt(stable[stable.length - 1].sd),
                min: fmt(stable[stable.length - 1].min), max: fmt(stable[stable.length - 1].max),
              })
            : t_i18n("No configuration ran more than once: run the evaluation again to see how far its figures move.")
        }
      >
        {spreads.length > 0 && <ConsistencyChart rows={spreads} metricLabel={t_i18n(metric.label)} />}
        <p className="content-caption text-default-secondary">
          {t_i18n("The same agent on the same model, run again: a dot per run, its figure on its own entries; the band spans the lowest to the highest, the line is the mean.")}
        </p>
      </Section>

      <Section title={t_i18n("4 · Methodology")} lead={t_i18n("What every figure on this page means, to publish next to it.")}>
        <dl className="grid gap-x-6 gap-y-2 content-compact sm:grid-cols-[12rem_1fr]">
          <dt className="font-medium">{t_i18n("Runs")}</dt>
          <dd className="text-default-secondary">
            {t_i18n("{count, plural, =0 {No completed run} one {The # completed run} other {The # completed runs}} of “{name}” (its 50 newest at most); placebo comparison runs are left out. A launch is the runs started in the same minute by the same trigger.", {
              count: board.runs.length, name: evaluationName,
            })}
          </dd>
          <dt className="font-medium">{t_i18n("Entries")}</dt>
          <dd className="text-default-secondary">
            {t_i18n("{count, plural, one {# entry} other {# entries}} of the evaluation's datasets. An entry a run's headlines leave out (a platform error, a judge failure) is left out here too.", { count: entryCount })}
          </dd>
          <dt className="font-medium">{t_i18n("Figures")}</dt>
          <dd className="text-default-secondary">
            {t_i18n("{metrics}: each pooled, its counts added up over the entries then divided once (micro), as the run's own headline; an entry with nothing expected and nothing found counts as correct.", {
              metrics: metrics.map((m) => t_i18n(m.label)).join(", "),
            })}
          </dd>
          <dt className="font-medium">{t_i18n("Uncertainty")}</dt>
          <dd className="text-default-secondary">
            {t_i18n("95% percentile bootstrap, 1,000 resamples of the runs then of their entries, with a fixed seed. Ranks read best–worst: two configurations whose intervals overlap are tied. With few entries these intervals are optimistic.")}
          </dd>
          <dt className="font-medium">{t_i18n("Cost and time")}</dt>
          <dd className="text-default-secondary">
            {t_i18n("The LLM cost the platform recorded per answer, averaged over the answers that have one; the agent's seconds per entry. A configuration with no recorded cost is left out of the cost chart.")}
          </dd>
          <dt className="font-medium">{t_i18n("Judge Score")}</dt>
          <dd className="text-default-secondary">
            {t_i18n("Not used here: every figure comes from the deterministic metrics.")}
          </dd>
        </dl>
      </Section>
    </div>
  );
}
