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
 * Compare evaluations of one family on the entries their datasets share (#12282).
 *
 * Opened from the Evaluations list with 2 to 4 evaluations selected (`?ids=`); `?runs=`
 * picks the runs compared (else each evaluation's newest completed run per agent and
 * model). Four sections, in reading order: what can be compared, the same model in
 * both evaluations, every configuration on the shared entries, and whether the shared
 * entries stand for each evaluation's whole dataset. Figures are pooled from the
 * stored per-entry counts (`lib/evaluation-compare.ts`), so each is the run's own
 * figure on those entries.
 */

import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { useEvaluationComparison } from "@/hooks/api/useEvaluationApi";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Card, CardContent } from "@/components/ui/card";
import { PageSkeleton } from "@/components/ui/skeleton";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Switch } from "@/components/ui/switch";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { ComparabilityMatrix } from "@/components/evaluation/compare/comparability-table";
import { DumbbellChart, ForestPlot } from "@/components/evaluation/compare/compare-charts";
import { EVALUATION_COLORS, EVALUATION_SWATCHES } from "@/components/evaluation/compare/compare-colors";
import {
  bootstrap,
  cellIndex,
  entriesOf,
  pairedChange,
  pairedCounts,
  perEntryMean,
  rankSpread,
  runCounts,
  seedOf,
  sharedEntries,
} from "@/lib/evaluation-compare";
import { formatTimeAgo } from "@/lib/relative-time";
import { cn, formatModelLabel } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { ComparedRun, EvaluationComparison } from "@/types";

const STIX_BUNDLE = "STIX_BUNDLE";
/** Tile names built outside a component, read by `t_i18n`. @i18n-keys */
const TILE_LABELS = { costTime: "Cost · time per entry" } as const;
const fmt = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const signed = (v: number) => `${v > 0.05 ? "+" : v < -0.05 ? "−" : "±"}${Math.abs(v).toFixed(1)}`;

function Section({ title, lead, children }: { title: string; lead?: string; children: React.ReactNode }) {
  return (
    <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
      <CardContent className="space-y-3 p-4">
        <div>
          <h2 className="content-base font-medium">{title}</h2>
          {lead && <p className="mt-1 content-compact text-default-secondary max-w-prose">{lead}</p>}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}

/** "agent · model" for a run. */
function runName(run: ComparedRun, t_i18n: (id: string) => string): string {
  return [run.subject_name, formatModelLabel(run.model_id, t_i18n)].filter(Boolean).join(" · ");
}

/** Which runs of each evaluation are compared; toggling one rewrites `?runs=`. */
function RunPicker({ comparison, onChange }: { comparison: EvaluationComparison; onChange: (ids: string[]) => void }) {
  const { t_i18n, intl } = useFormatter();
  const selected = new Set(comparison.evaluations.flatMap((e) => e.runs.map((r) => r.id)));
  const toggle = (runId: string) => {
    const next = new Set(selected);
    if (next.has(runId)) next.delete(runId);
    else next.add(runId);
    // An evaluation left with no run keeps its newest ones (the API's default).
    onChange([...next].filter((id) => comparison.evaluations.some((e) => e.run_choices.some((r) => r.id === id))));
  };
  return (
    <details className="rounded-sm border border-elevation-subtle-soft">
      <DisclosureSummary size="block">{t_i18n("Runs compared")}</DisclosureSummary>
      <div className="grid gap-4 p-3 md:grid-cols-2">
        {comparison.evaluations.map((evaluation, i) => (
          <div key={evaluation.id} className="space-y-2">
            <p className="flex items-center gap-2 content-compact font-medium">
              <span aria-hidden className={cn("h-2.5 w-2.5 rounded-full", EVALUATION_SWATCHES[i % EVALUATION_SWATCHES.length])} />
              {evaluation.name}
            </p>
            {evaluation.run_choices.length === 0 && (
              <p className="content-caption text-default-secondary">{t_i18n("No completed run")}</p>
            )}
            {evaluation.run_choices.map((run) => (
              <label key={run.id} className="flex items-center gap-2 content-compact">
                <Switch
                  checked={selected.has(run.id)}
                  onCheckedChange={() => toggle(run.id)}
                  aria-label={t_i18n("Compare the run {name}", { name: runName(run, t_i18n) })}
                />
                <span className="min-w-0 truncate">{runName(run, t_i18n)}</span>
                {run.created_at && (
                  <span className="ml-auto shrink-0 content-caption text-default-secondary">{formatTimeAgo(intl, run.created_at)}</span>
                )}
              </label>
            ))}
          </div>
        ))}
      </div>
    </details>
  );
}

export default function EvaluationCompare() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const ids = useMemo(() => (params.get("ids") ?? "").split(",").filter(Boolean), [params]);
  const runIds = useMemo(() => (params.get("runs") ?? "").split(",").filter(Boolean), [params]);
  const { data: comparison, isLoading, error } = useEvaluationComparison(ids, runIds);
  const [metricKey, setMetricKey] = useState<string | null>(null);
  const [pairIndex, setPairIndex] = useState(0);

  const view = useMemo(() => (comparison ? buildView(comparison, metricKey) : null), [comparison, metricKey]);

  const setRuns = (next: string[]) => {
    const p = new URLSearchParams(params);
    if (next.length) p.set("runs", next.join(","));
    else p.delete("runs");
    setParams(p, { replace: true });
  };

  const header = (
    <EntityPageHeader
      backTo="/evaluations"
      backLabel={t_i18n("Back to Evaluations")}
      icon={<Icon name="columns-3" size={20} />}
      title={comparison ? comparison.evaluations.map((e) => e.name).join(" vs ") : t_i18n("Compare evaluations")}
      description={
        view
          ? t_i18n("{family} · {count, plural, one {# entry} other {# entries}} in common", {
              family: comparison!.family,
              count: view.shared.length,
            })
          : undefined
      }
    />
  );

  if (ids.length < 2) {
    return (
      <div className="space-y-6">
        {header}
        <RefusalAlert message={t_i18n("Select at least two evaluations to compare them.")} />
      </div>
    );
  }
  if (isLoading) return <PageSkeleton cards={4} />;
  if (error || !comparison || !view) {
    return (
      <div className="space-y-6">
        {header}
        <RefusalAlert
          message={t_i18n("These evaluations cannot be compared.")}
          detail={error instanceof Error ? error.message : null}
        />
      </div>
    );
  }

  const metric = view.metric;
  const pairs = view.pairs;
  const pair = pairs[Math.min(pairIndex, pairs.length - 1)];
  const reviewable = comparison.family === STIX_BUNDLE;
  const openReview = (resultIds: string[]) => navigate(`/evaluations/review?results=${resultIds.join(",")}`);

  return (
    <div className="space-y-6">
      {header}

      <div className="flex flex-wrap items-center gap-3">
        {comparison.metrics.length > 1 && (
          <SegmentedFilter
            options={comparison.metrics.map((m) => ({ key: m.key, label: m.label }))}
            value={metric.key}
            onChange={(key: string) => setMetricKey(key)}
            ariaLabel={t_i18n("Metric")}
            selection="radio"
          />
        )}
      </div>

      <RunPicker comparison={comparison} onChange={setRuns} />

      <Section
        title={t_i18n("1 · What can be compared")}
        lead={t_i18n("Read this first: it says what the figures below measure.")}
      >
        <ComparabilityMatrix comparison={comparison} sharedCount={view.shared.length} />
      </Section>

      <Section
        title={t_i18n("2 · Same model in both evaluations")}
        lead={
          pair?.change
            ? t_i18n(
                "On the {count, plural, one {# shared entry} other {# shared entries}}, {model} scores {after} in {afterName} against {before} in {beforeName} ({change}, 95% interval {low} to {high}). {verdict}",
                {
                  count: pair.keys.length,
                  model: formatModelLabel(pair.model, t_i18n),
                  after: fmt(pair.change.after),
                  afterName: pair.after.evaluation.name,
                  before: fmt(pair.change.before),
                  beforeName: pair.before.evaluation.name,
                  change: signed(pair.change.change),
                  low: signed(pair.change.low),
                  high: signed(pair.change.high),
                  verdict: pair.change.holds
                    ? t_i18n("The change holds across the entries.")
                    : t_i18n("This change is within the noise of so few entries: neither can be called better."),
                },
              )
            : t_i18n("No model ran in two of these evaluations on the same entries: only configurations can be compared.")
        }
      >
        {pair?.change && (
          <>
            {pairs.length > 1 && (
              <SegmentedFilter
                options={pairs.map((p, i) => ({
                  key: String(i),
                  label: `${formatModelLabel(p.model, t_i18n)} · ${p.before.evaluation.name} → ${p.after.evaluation.name}`,
                }))}
                value={String(Math.min(pairIndex, pairs.length - 1))}
                onChange={(key: string) => setPairIndex(Number(key))}
                ariaLabel={t_i18n("Model compared")}
                selection="radio"
                overflow="menu"
              />
            )}
            <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {pair.tiles.map((tile) => (
                <div key={tile.label} className="rounded-sm border border-elevation-subtle-soft p-3">
                  <dt className="content-caption text-default-secondary">{t_i18n(tile.label)}</dt>
                  <dd className="content-base font-medium tabular-nums">{tile.value}</dd>
                  {tile.sub && <dd className="content-caption text-default-secondary tabular-nums">{tile.sub}</dd>}
                </div>
              ))}
            </dl>
            <DumbbellChart
              rows={pair.rows}
              metricLabel={metric.label}
              beforeLabel={pair.before.evaluation.name}
              afterLabel={pair.after.evaluation.name}
              colors={[EVALUATION_COLORS[pair.before.index % 4], EVALUATION_COLORS[pair.after.index % 4]]}
              onOpen={reviewable ? (key) => openReview(pair.resultIds(key)) : undefined}
            />
            {reviewable && (
              <p className="content-caption text-default-secondary">
                {t_i18n("Open an entry to see both answers object by object.")}
              </p>
            )}
          </>
        )}
      </Section>

      <Section
        title={t_i18n("3 · Every configuration on the shared entries")}
        lead={
          view.ranking.length
            ? view.ranking.filter((r) => r.best === 1).length > 1
              ? t_i18n("{count} configurations can be first: their intervals overlap, so no single best can be claimed.", {
                  count: view.ranking.filter((r) => r.best === 1).length,
                })
              : t_i18n("{label} is first, with no overlap with the next one.", { label: view.ranking[0].label })
            : t_i18n("No run has a figure on the shared entries.")
        }
      >
        {view.ranking.length > 0 && <ForestPlot rows={view.ranking} metricLabel={metric.label} />}
        <p className="content-caption text-default-secondary">
          {t_i18n("One line per agent and model: its figure on the same entries, with a 95% interval (bootstrap over the entries). A rank such as 1–4 means it could be anywhere from first to fourth.")}
        </p>
      </Section>

      <Section
        title={t_i18n("4 · Do the shared entries stand for each dataset?")}
        lead={t_i18n("Each run on the shared entries against its evaluation's other entries: a large gap means the shared entries are easier or harder than the rest.")}
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] content-compact">
            <thead>
              <tr className="border-b border-elevation-subtle-soft text-left">
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Run")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("All its entries")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Shared entries")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Its other entries")}</th>
              </tr>
            </thead>
            <tbody>
              {view.representativeness.map((row) => (
                <tr key={row.id} className="border-b border-elevation-subtle-soft last:border-b-0">
                  <td className="px-3 py-2">
                    <span className="inline-flex items-center gap-2">
                      <span aria-hidden className={cn("h-2.5 w-2.5 rounded-full", EVALUATION_SWATCHES[row.colorIndex % 4])} />
                      {row.label}
                    </span>
                  </td>
                  {[row.all, row.shared, row.other].map((cell, i) => (
                    <td key={i} className="px-3 py-2 tabular-nums">
                      {cell ? (
                        <>
                          {fmt(cell.value)} <span className="content-caption text-default-secondary">{fmt(cell.low)}–{fmt(cell.high)} · {cell.n}</span>
                        </>
                      ) : "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

// ── The page's figures, derived once per payload and metric ──

function buildView(comparison: EvaluationComparison, metricKey: string | null) {
  const metric = comparison.metrics.find((m) => m.key === metricKey) ?? comparison.metrics[0] ?? { key: "", label: "", direction: null };
  const cells = cellIndex(comparison);
  const evalIds = comparison.evaluations.map((e) => e.id);
  const shared = sharedEntries(comparison.entries, evalIds).map((e) => e.key);
  const label = new Map(comparison.entries.map((e) => [e.key, e.label]));
  const runs = comparison.evaluations.flatMap((evaluation, index) => evaluation.runs.map((run) => ({ run, evaluation, index })));
  // Model first (the colour says the evaluation), without its provider route prefix.
  const name = (r: (typeof runs)[number]) =>
    [r.run.model_id.replace(/^~/, "").split("/").pop(), r.run.subject_name].filter(Boolean).join(" · ");

  const ranking = rankSpread(
    runs
      .map((r) => {
        const interval = bootstrap(runCounts(cells, r.run.id, metric.key, shared), seedOf(r.run.id));
        return interval ? { id: r.run.id, label: name(r), colorIndex: r.index, ...interval } : null;
      })
      .filter((x): x is NonNullable<typeof x> => x !== null)
      .sort((a, b) => b.value - a.value),
  );

  // The same model in two evaluations: each later evaluation against the first that ran it.
  const pairs = [];
  for (let i = 0; i < runs.length; i++) {
    for (let j = i + 1; j < runs.length; j++) {
      const a = runs[i], b = runs[j];
      if (a.evaluation.id === b.evaluation.id || a.run.model_id !== b.run.model_id) continue;
      const keys = sharedEntries(comparison.entries, [a.evaluation.id, b.evaluation.id]).map((e) => e.key);
      const counts = pairedCounts(cells, a.run.id, b.run.id, metric.key, keys);
      const change = pairedChange(counts.before, counts.after, seedOf(a.run.id + b.run.id));
      if (!change) continue;
      const cost = [perEntryMean(cells, a.run.id, "cost", counts.keys), perEntryMean(cells, b.run.id, "cost", counts.keys)];
      const time = [perEntryMean(cells, a.run.id, "seconds", counts.keys), perEntryMean(cells, b.run.id, "seconds", counts.keys)];
      const tiles = comparison.metrics.map((m) => {
        const pc = pairedCounts(cells, a.run.id, b.run.id, m.key, keys);
        const c = pairedChange(pc.before, pc.after, seedOf(a.run.id + b.run.id + m.key));
        return {
          label: m.label,
          value: c ? `${fmt(c.before)} → ${fmt(c.after)} (${signed(c.change)})` : "—",
          sub: c ? `95% ${signed(c.low)} … ${signed(c.high)}` : null,
        };
      });
      tiles.push({
        label: TILE_LABELS.costTime,
        value: `${cost[0] == null ? "—" : `$${cost[0].toFixed(2)}`} → ${cost[1] == null ? "—" : `$${cost[1].toFixed(2)}`}`,
        sub: `${time[0] == null ? "—" : `${Math.round(time[0])} s`} → ${time[1] == null ? "—" : `${Math.round(time[1])} s`}`,
      });
      pairs.push({
        model: a.run.model_id,
        before: a,
        after: b,
        keys: counts.keys,
        change,
        tiles,
        rows: counts.keys.map((key, k) => ({
          key,
          label: label.get(key) ?? key,
          // A 0-of-0 entry is a correct empty answer, 100, as `pooled` reads it.
          before: counts.before[k][1] ? (100 * counts.before[k][0]) / counts.before[k][1] : 100,
          after: counts.after[k][1] ? (100 * counts.after[k][0]) / counts.after[k][1] : 100,
        })),
        resultIds: (key: string) =>
          [cells.get(a.run.id)?.get(key)?.result_id, cells.get(b.run.id)?.get(key)?.result_id].filter((x): x is string => !!x),
      });
    }
  }

  const representativeness = runs.map((r) => {
    const own = entriesOf(comparison.entries, r.evaluation.id).map((e) => e.key);
    const other = own.filter((k) => !shared.includes(k));
    const stat = (keys: string[], salt: string) => {
      const counts = runCounts(cells, r.run.id, metric.key, keys);
      const interval = bootstrap(counts, seedOf(r.run.id + salt));
      return interval ? { ...interval, n: counts.length } : null;
    };
    return { id: r.run.id, label: name(r), colorIndex: r.index, all: stat(own, "all"), shared: stat(shared, "shared"), other: stat(other, "other") };
  });

  return { metric, shared, ranking, pairs, representativeness };
}
