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
 * One evaluation's Results (#2): the runs compared (one experiment launch, every rerun of
 * one setup, the newest run of each configuration, every run); each configuration (agent ×
 * model) pooled over them and ranked with 95% intervals; its figure against its cost or
 * time per entry, with the Pareto frontier; how far reruns of one setup spread, and which
 * expected items they found every time; how every figure is computed, to publish next to
 * it. Exports the ranking as CSV, each chart as SVG and the methodology as Markdown.
 */

import { useMemo, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useBundleReviews } from "@/hooks/api/useEvaluationApi";
import { useFormatter } from "@/i18n";
import { paretoFront, setupNames, toCsv, type BoardRun, type Launch } from "@/lib/evaluation-leaderboard";
import {
  consistencyRows,
  consistencySetups,
  foundGroups,
  FOUND_PARTS,
  methodologyMarkdown,
  rankConfigs,
  resultSets,
  runsOfSet,
  slugOf,
  tallyFound,
  type ResultSet,
} from "@/lib/evaluation-results";
import { saveChartSvg } from "@/lib/evaluation-results-svg";
import { cn, saveBlobAs } from "@/lib/utils";
import type { ComparedCell, ComparedEvaluation, ComparedMetric, ComparedRun } from "@/types";
import { ConsistencyChart, ForestPlot, ParetoChart } from "./compare-charts";
import { FOUND_SWATCHES } from "./compare-colors";
import { launchTag, useLaunchText } from "./leaderboard-launches";
import { MethodologyList, useMethodology } from "./results-methodology";

const fmt = (v: number) => v.toFixed(1);

type Axis = "cost" | "time";
/** The axes, read by `t_i18n`. @i18n-keys */
const AXIS_LABELS = { cost: "Cost", time: "Time" } as const satisfies Record<Axis, string>;

interface Props {
  evaluationName: string;
  board: { launches: Launch[]; runs: BoardRun[] };
  cells: Map<string, Map<string, ComparedCell>>;
  metric: ComparedMetric;
  metrics: ComparedMetric[];
  entryCount: number;
  label: (run: ComparedRun) => string;
  /** The evaluation compared: its datasets, mode, timeout and judge, for the methodology. */
  evaluation?: ComparedEvaluation;
  /** Told when a metric is picked here, so the page's other tabs follow. */
  onMetricChange?: (key: string) => void;
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
          {tools && <div className="flex flex-wrap items-center gap-2">{tools}</div>}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}

function ExportButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <Button variant="ghost" size="sm" className="gap-1.5" onClick={onClick} disabled={disabled}>
      <Icon name="download" size={16} /> {label}
    </Button>
  );
}

export function ResultsTab({ evaluationName, board, cells, metric, metrics, entryCount, label, evaluation, onMetricChange }: Props) {
  const { t_i18n } = useFormatter();
  const text = useLaunchText(board.launches);
  const [axis, setAxis] = useState<Axis>("cost");
  // The tab's metric follows the page's until one is picked here.
  const [picked, setPicked] = useState<{ key: string; from: string } | null>(null);
  const current = metrics.find((m) => m.key === (picked && picked.from === metric.key ? picked.key : metric.key)) ?? metric;
  const key = current.key;
  const pickMetric = (next: string) => {
    if (onMetricChange) {
      onMetricChange(next);
      setPicked(null);
    } else setPicked({ key: next, from: metric.key });
  };
  const metricLabel = t_i18n(current.label);
  const slug = slugOf(evaluationName);

  // ── Which runs are compared ──
  const names = useMemo(() => setupNames(board.runs.map((r) => r.run)), [board.runs]);
  const sets = useMemo(() => resultSets(board), [board]);
  const [setId, setSetId] = useState<string | null>(null);
  const set: ResultSet = sets.find((s) => s.id === setId) ?? sets[0];
  const setLabel = (s: ResultSet): string => {
    if (s.kind === "launch") return [launchTag(s.launch), text.date(board.launches[s.launch]), text.note(s.launch)].filter(Boolean).join(" · ");
    if (s.kind === "setup") {
      return t_i18n("Every run of “{config}” · {from}–{to} · {count, plural, one {# launch} other {# launches}}", {
        config: names.get(s.setup) ?? s.setup, from: launchTag(s.launches[0]), to: launchTag(s.launches[s.launches.length - 1]), count: s.launches.length,
      });
    }
    return s.kind === "newest" ? t_i18n("Newest run of each configuration") : t_i18n("Every run");
  };
  const setRuns = useMemo(() => runsOfSet(board, set), [board, set]);
  const setLaunches = new Set(setRuns.map((r) => r.launch)).size;

  // ── 1 · Ranking ──
  const ranking = useMemo(() => rankConfigs(setRuns, cells, key, set.id), [setRuns, cells, key, set.id]);
  const nameOf = (run: ComparedRun) => label(run);
  const firsts = ranking.filter((r) => r.best === 1);

  // ── 2 · Pareto ──
  const points = ranking
    .map((r) => ({ ...r, x: axis === "cost" ? r.cost : r.seconds }))
    .filter((p): p is typeof p & { x: number } => p.x != null)
    .map((p) => ({ ...p, y: p.value }));
  const front = paretoFront(points);
  const onFront = new Set(front.map((p) => p.id));
  const missing = ranking.filter((r) => (axis === "cost" ? r.cost : r.seconds) == null);
  const axisWord = axis === "cost" ? t_i18n("cost") : t_i18n("time");

  // ── 3 · Consistency ──
  const setups = useMemo(() => consistencySetups(board), [board]);
  const [setupId, setSetupId] = useState<string | null>(null);
  const setup = setups.find((s) => s.setup === setupId) ?? setups[0];
  const spreads = useMemo(() => (setup ? consistencyRows(board, setup.setup, key) : []), [board, setup, key]);
  const part = FOUND_PARTS[key];
  const found = useMemo(() => (part ? foundGroups(spreads, cells) : { groups: [], owners: [], skipped: [] }), [part, spreads, cells]);
  const reviews = useBundleReviews(found.groups, { enabled: !!part && found.groups.length > 0 });
  const tally = useMemo(
    () => (part && reviews.data ? tallyFound(reviews.data, found.owners, part) : null),
    [part, reviews.data, found.owners],
  );
  const stable = [...spreads].sort((a, b) => a.sd - b.sd);
  const foundWhat = part === "objects" ? t_i18n("objects") : part === "relationships" ? t_i18n("relationships") : t_i18n("objects and relationships");
  const foundHead = part === "objects" ? t_i18n("Expected objects found…") : part === "relationships" ? t_i18n("Expected relationships found…") : t_i18n("Expected items found…");

  // ── 4 · Methodology ──
  const method = useMethodology({ evaluationName, evaluation, runs: board.runs, cells, metrics, entryCount });

  const forestRef = useRef<HTMLDivElement>(null);
  const paretoRef = useRef<HTMLDivElement>(null);
  const consistencyRef = useRef<HTMLDivElement>(null);
  const exportCsv = () => {
    const csv = toCsv([
      ["rank_best", "rank_worst", "configuration", "agent", "model_id", "runs_compared", "runs", "metric", "value", "ci95_low", "ci95_high", "precision", "recall", "cost_per_entry_usd", "seconds_per_entry", "unreadable_answers", "answers"],
      ...ranking.map((r) => [
        r.best, r.worst, nameOf(r.run), r.run.subject_name, r.run.model_id, setLabel(set), r.runs.length, key,
        r.value.toFixed(2), r.low.toFixed(2), r.high.toFixed(2),
        r.precision == null ? null : r.precision.toFixed(2), r.recall == null ? null : r.recall.toFixed(2),
        r.cost == null ? null : r.cost.toFixed(5), r.seconds == null ? null : r.seconds.toFixed(1),
        r.unreadable, r.answers,
      ]),
    ]);
    saveBlobAs(new Blob([csv], { type: "text/csv;charset=utf-8" }), `${slug}-results-${slugOf(set.id)}-${slugOf(key)}.csv`);
  };
  const exportSvg = (ref: React.RefObject<HTMLDivElement | null>, chart: string) =>
    saveChartSvg(ref.current, `${slug}-results-${slugOf(set.id)}-${slugOf(key)}-${chart}.svg`);
  const exportMethod = () =>
    saveBlobAs(
      new Blob([methodologyMarkdown(t_i18n("Methodology · {name}", { name: evaluationName }), method)], { type: "text/markdown;charset=utf-8" }),
      `${slug}-methodology.md`,
    );

  return (
    <div className="space-y-4">
      <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
        <CardContent className="flex flex-wrap items-center gap-3 p-4">
          <label className="flex min-w-0 flex-1 basis-80 items-center gap-2 content-compact">
            <span className="shrink-0 text-default-secondary">{t_i18n("Runs compared")}</span>
            <SearchableSelect
              aria-label={t_i18n("Runs compared")}
              options={sets.map((s) => ({ value: s.id, label: setLabel(s) }))}
              value={set.id}
              onValueChange={setSetId}
              sort={false}
              searchable="auto"
              className="min-w-0 flex-1"
            />
          </label>
          {metrics.length > 1 && (
            <SegmentedFilter
              options={metrics.map((m) => ({ key: m.key, label: m.label }))}
              value={key}
              onChange={(next: string) => pickMetric(next)}
              ariaLabel={t_i18n("Metric")}
              selection="radio"
            />
          )}
          <Button variant="outline" size="sm" className="ml-auto gap-1.5" onClick={exportCsv} disabled={!ranking.length}>
            <Icon name="download" size={16} /> {t_i18n("Export CSV")}
          </Button>
        </CardContent>
      </Card>

      <Section
        title={t_i18n("1 · Ranking with 95% intervals")}
        lead={
          !ranking.length
            ? t_i18n("No run has a figure for this metric.")
            : firsts.length > 1
              ? t_i18n("{count} configurations can be first: {names}. Their intervals overlap, so no single winner can be claimed.", {
                  count: firsts.length, names: firsts.map((r) => nameOf(r.run)).join(", "),
                })
              : t_i18n("{label} is first, with no overlap with the next configuration.", { label: nameOf(ranking[0].run) })
        }
        tools={<ExportButton label={t_i18n("SVG")} onClick={() => exportSvg(forestRef, "ranking")} disabled={!ranking.length} />}
      >
        <div ref={forestRef}>
          {ranking.length > 0 && (
            <ForestPlot
              rows={ranking.map((r) => ({ ...r, label: nameOf(r.run), colorIndex: 0, isDefault: r.run.is_default_model, runCount: r.runs.length }))}
              metricLabel={metricLabel}
            />
          )}
        </div>
        <p className="content-caption text-default-secondary">
          {setLaunches > 1
            ? t_i18n("One line per configuration, pooled over its runs in {launches} launches and the {entries, plural, one {# entry} other {# entries}}. Rank “1–4” means it could be anywhere from 1st to 4th given the intervals.", { launches: setLaunches, entries: entryCount })
            : t_i18n("One line per configuration, pooled over the {entries, plural, one {# entry} other {# entries}}. Rank “1–4” means it could be anywhere from 1st to 4th given the intervals.", { entries: entryCount })}
        </p>
      </Section>

      <Section
        title={axis === "cost" ? t_i18n("2 · Quality against cost") : t_i18n("2 · Quality against time")}
        lead={
          points.length
            ? t_i18n("Pareto frontier: {front}. Every other configuration is beaten by one of them on both {metric} and {axis}.", {
                front: front.map((p) => nameOf(p.run)).join(" → "), metric: metricLabel, axis: axisWord,
              })
            : t_i18n("No configuration has a recorded {axis} per entry.", { axis: axisWord })
        }
        tools={
          <>
            <SegmentedFilter
              options={(Object.keys(AXIS_LABELS) as Axis[]).map((k) => ({ key: k, label: AXIS_LABELS[k] }))}
              value={axis}
              onChange={(k: Axis) => setAxis(k)}
              ariaLabel={t_i18n("Axis")}
              selection="radio"
            />
            <ExportButton label={t_i18n("SVG")} onClick={() => exportSvg(paretoRef, axis)} disabled={!points.length} />
          </>
        }
      >
        <div ref={paretoRef}>
          {points.length > 0 && (
            <ParetoChart
              points={points.map((p) => ({ ...p, label: nameOf(p.run), onFront: onFront.has(p.id), isDefault: p.run.is_default_model }))}
              axis={axis}
              metricLabel={metricLabel}
            />
          )}
        </div>
        <p className="content-caption text-default-secondary">
          {t_i18n("Green: on the frontier (dashed line, from the origin: not running an agent costs nothing and finds nothing). Vertical bars: 95% intervals.")}
          {missing.length > 0 &&
            ` ${t_i18n("Not shown, no {axis} reported: {names}.", {
              axis: axis === "cost" ? t_i18n("cost") : t_i18n("duration"),
              names: missing.map((r) => nameOf(r.run)).join(", "),
            })}`}
        </p>
      </Section>

      <Section
        title={t_i18n("3 · Consistency across reruns")}
        lead={
          stable.length
            ? t_i18n("Most stable: {best} (sd {bestSd}). Least stable: {worst} (sd {worstSd}, {min} to {max}).", {
                best: nameOf(stable[0].run), bestSd: fmt(stable[0].sd),
                worst: nameOf(stable[stable.length - 1].run), worstSd: fmt(stable[stable.length - 1].sd),
                min: fmt(stable[stable.length - 1].min), max: fmt(stable[stable.length - 1].max),
              })
            : t_i18n("No configuration ran this setup more than once.")
        }
        tools={
          <>
            {setups.length > 0 && (
              <SearchableSelect
                aria-label={t_i18n("Configuration")}
                options={setups.map((s) => ({
                  value: s.setup,
                  label: t_i18n("{config} · {count, plural, one {# launch} other {# launches}}", { config: names.get(s.setup) ?? s.setup, count: s.launches }),
                }))}
                value={setup?.setup}
                onValueChange={setSetupId}
                sort={false}
                searchable="auto"
                className="w-64"
              />
            )}
            <ExportButton label={t_i18n("SVG")} onClick={() => exportSvg(consistencyRef, "consistency")} disabled={!spreads.length} />
          </>
        }
      >
        <div ref={consistencyRef}>
          {spreads.length > 0 ? (
            <ConsistencyChart
              rows={spreads.map((r) => ({ ...r, label: nameOf(r.run), isDefault: r.run.is_default_model, found: tally?.get(r.id) }))}
              metricLabel={metricLabel}
              foundLabel={part ? foundHead : undefined}
            />
          ) : (
            <p className="content-compact text-default-secondary">{t_i18n("No configuration ran this setup more than once.")}</p>
          )}
        </div>
        {part && spreads.length > 0 && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 content-caption text-default-secondary">
            {(["every", "some", "never"] as const).map((k) => (
              <span key={k} className="inline-flex items-center gap-1.5">
                <span className={cn("size-2.5 rounded-sm", FOUND_SWATCHES[k])} aria-hidden />
                {k === "every" ? t_i18n("In every run") : k === "some" ? t_i18n("In some runs") : t_i18n("Never")}
              </span>
            ))}
            {reviews.isLoading && <span>{t_i18n("Reading which expected items each run found…")}</span>}
            {reviews.isError && <span>{t_i18n("Which expected items each run found could not be read.")}</span>}
          </div>
        )}
        <p className="content-caption text-default-secondary">
          {t_i18n("Same configuration, run again: dots are runs, the bar is the mean, the band is min–max.")}
          {part &&
            ` ${t_i18n("Right: expected {what} found in every run, in some runs, never (the strict reading, like τ-bench's pass^k, is “every run”).", { what: foundWhat })}`}
          {found.skipped.length > 0 &&
            ` ${t_i18n("Not read for {names}: too many answers at once.", {
              names: spreads.filter((r) => found.skipped.includes(r.id)).map((r) => nameOf(r.run)).join(", "),
            })}`}
        </p>
      </Section>

      <Section
        title={t_i18n("4 · Methodology")}
        lead={t_i18n("What every figure on this page means, to publish next to it.")}
        tools={<ExportButton label={t_i18n("Markdown")} onClick={exportMethod} />}
      >
        <MethodologyList items={method} />
      </Section>
    </div>
  );
}
