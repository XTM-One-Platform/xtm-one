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
 * model). One tab row joins the evaluation's pages: Compare evaluations, its Leaderboard,
 * the Run review and its Results (the first evaluation's, compared with the others).
 * The comparison reads in four sections: what can be compared, the same model in both
 * evaluations, every configuration on the shared entries, and whether the shared
 * entries stand for each evaluation's whole dataset. One evaluation alone (`Compare
 * models` on its page) compares its own runs: run against run entry by entry, then every
 * run ranked. Figures are pooled from the stored per-entry counts
 * (`lib/evaluation-compare.ts`), so each is the run's own figure on those entries; a
 * result left out of the run's figures is left out here, an answer the scorer could not
 * read counts (as everything missing).
 */

import { Fragment, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { useEvaluationComparison } from "@/hooks/api/useEvaluationApi";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { PageSkeleton } from "@/components/ui/skeleton";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Switch } from "@/components/ui/switch";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useToast } from "@/components/ui/toast";
import { ComparabilityMatrix } from "@/components/evaluation/compare/comparability-table";
import { PairedDumbbell, RankedForest, saveSvg } from "@/components/evaluation/compare/multi-charts";
import {
  EVALUATION_PAGE_LABELS,
  EvaluationPageShell,
  evaluationPageTabs,
  type Crumb,
  type EvaluationPage,
} from "@/components/evaluation/compare/multi-page-shell";
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
import {
  formatSeconds,
  headlineMetrics,
  isFairSample,
  largestGap,
  signed,
  tiedNames,
  unreadableCount,
} from "@/lib/evaluation-comparability";
import { csvBlob, csvFilename, toCsv } from "@/lib/csv";
import { LeaderboardTab } from "@/components/evaluation/compare/leaderboard-tab";
import { ResultsTab } from "@/components/evaluation/compare/results-tab";
import { boardOf, launchesOf, runLabeler } from "@/lib/evaluation-leaderboard";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { cn, formatModelLabel, saveBlobAs } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { ComparedEvaluation, ComparedRun, EvaluationComparison } from "@/types";

const STIX_BUNDLE = "STIX_BUNDLE";
/** Tile names built outside a component, read by `t_i18n`. @i18n-keys */
const TILE_LABELS = { costTime: "Cost · time per entry" } as const;
const fmt = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(1));
const usd = (v: number | null | undefined) => (v == null ? "—" : `$${v.toFixed(2)}`);
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "evaluation";

function Section({
  title,
  lead,
  tools,
  children,
}: {
  title: string;
  lead?: string;
  tools?: React.ReactNode;
  children: React.ReactNode;
}) {
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

/** A coloured dot naming an evaluation (or a run) by its colour. */
function Swatch({ index }: { index: number }) {
  return <span aria-hidden className={cn("inline-block h-2.5 w-2.5 shrink-0 rounded-full", EVALUATION_SWATCHES[index % EVALUATION_SWATCHES.length])} />;
}

/** The change as a coloured chip: green when it improves, red when it worsens. */
function DeltaChip({ change, title, lowerIsBetter }: { change: number; title: string; lowerIsBetter?: boolean }) {
  const better = lowerIsBetter ? change < -0.05 : change > 0.05;
  const worse = lowerIsBetter ? change > 0.05 : change < -0.05;
  return (
    <span
      title={title}
      className={cn(
        "rounded-sm px-1.5 content-caption tabular-nums",
        better
          ? "bg-feedback-success-primary/10 text-feedback-success-primary"
          : worse
            ? "bg-feedback-error-primary/10 text-feedback-error-primary"
            : "bg-elevation-hover text-default-secondary",
      )}
    >
      {signed(change)}
    </span>
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
              <Swatch index={i} />
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

/** The run of an evaluation its Run review opens: the default model's, else its first. */
function reviewRunOf(evaluation: ComparedEvaluation | undefined): string | null {
  if (!evaluation) return null;
  return (evaluation.runs.find((r) => r.is_default_model) ?? evaluation.runs[0] ?? evaluation.run_choices[0])?.id ?? null;
}

export default function EvaluationCompare() {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const ids = useMemo(() => [...new Set((params.get("ids") ?? "").split(",").filter(Boolean))], [params]);
  const runIds = useMemo(() => (params.get("runs") ?? "").split(",").filter(Boolean), [params]);
  const { data: comparison, isLoading, error } = useEvaluationComparison(ids, runIds);
  const [metricKey, setMetricKey] = useState<string | null>(null);
  const [pairIndex, setPairIndex] = useState(0);
  const pairChart = useRef<HTMLDivElement>(null);
  const forestChart = useRef<HTMLDivElement>(null);

  const view = useMemo(() => (comparison ? buildView(comparison, metricKey) : null), [comparison, metricKey]);

  const single = ids.length === 1;
  const tabParam = useParams<{ tab?: string }>().tab;
  const tab: EvaluationPage = single
    ? tabParam === "results" || tabParam === "pairs" ? tabParam : "leaderboard"
    : tabParam === "leaderboard" || tabParam === "results" ? tabParam : "pairs";
  // The page's own evaluation: the first selected. Its Leaderboard and Results read
  // every completed run of it (its 50 newest).
  const main = comparison?.evaluations[0];
  const choiceIds = useMemo(
    () => (main && (single || tab !== "pairs") ? main.run_choices.map((r) => r.id) : []),
    [main, single, tab],
  );
  const { data: everyRun, isLoading: loadingEveryRun } = useEvaluationComparison(choiceIds.length && main ? [main.id] : [], choiceIds);
  const board = useMemo(() => {
    if (!everyRun || everyRun.evaluations.length !== 1) return null;
    const runs = everyRun.evaluations[0].runs;
    return {
      ...boardOf(runs, cellIndex(everyRun), everyRun.metrics.map((m) => m.key)),
      cells: cellIndex(everyRun),
      label: runLabeler(runs),
      entryCount: everyRun.entries.length,
    };
  }, [everyRun]);

  const setRuns = (next: string[]) => {
    const p = new URLSearchParams(params);
    if (next.length) p.set("runs", next.join(","));
    else p.delete("runs");
    setParams(p, { replace: true });
  };

  const saved = (filename: string) => toast({ title: t_i18n("Saved {file}", { file: filename }), variant: "success" });

  // ── The shell: crumbs, title, subtitle, page tabs ──
  const others = comparison?.evaluations.slice(1) ?? [];
  const pageLabel = EVALUATION_PAGE_LABELS[tab === "pairs" ? (single ? "pairs" : "compare") : tab];
  const comparingPage = !single && tab === "pairs";
  const crumbs: Crumb[] = [{ label: t_i18n("Evaluations"), to: "/evaluations" }];
  if (comparingPage) crumbs.push({ label: t_i18n("Compare {count} evaluations", { count: ids.length }) });
  else if (main) crumbs.push({ label: main.name, to: `/evaluations/${main.id}` }, { label: t_i18n(pageLabel) });
  const sameScorer =
    !!comparison && new Set(comparison.evaluations.flatMap((e) => e.runs.map((r) => r.metric_suite_version ?? "?"))).size <= 1;
  const title = !comparison || !main
    ? t_i18n("Compare evaluations")
    : comparingPage
      ? comparison.evaluations.map((e) => e.name).join(" vs ")
      : main.name;
  const subtitle = !comparison || !main || !view
    ? null
    : comparingPage
      ? sameScorer
        ? t_i18n("{family} · {count, plural, one {# entry} other {# entries}} in common · same scorer", { family: comparison.family, count: view.shared.length })
        : t_i18n("{family} · {count, plural, one {# entry} other {# entries}} in common", { family: comparison.family, count: view.shared.length })
      : (() => {
          const values = {
            dataset: main.datasets.map((d) => d.name).join(", ") || "—",
            count: entriesOf(comparison.entries, main.id).length,
            models: new Set(main.run_choices.map((r) => r.model_id)).size,
            launches: launchesOf(main.run_choices).length,
            others: others.map((e) => e.name).join("”, “"),
          };
          return others.length
            ? t_i18n(
                "Dataset “{dataset}” · {count, plural, one {# entry} other {# entries}} · {models, plural, one {# model} other {# models}} · {launches, plural, one {# launch} other {# launches}} · compared with “{others}”",
                values,
              )
            : t_i18n(
                "Dataset “{dataset}” · {count, plural, one {# entry} other {# entries}} · {models, plural, one {# model} other {# models}} · {launches, plural, one {# launch} other {# launches}}",
                values,
              );
        })();
  const shell = (
    <EvaluationPageShell
      crumbs={crumbs}
      title={title}
      subtitle={subtitle}
      tabs={ids.length ? evaluationPageTabs({ ids, runs: runIds, reviewRun: comparison?.family === STIX_BUNDLE ? reviewRunOf(main) : null }) : []}
      active={tab}
    />
  );

  if (ids.length === 0) {
    return (
      <div className="space-y-6">
        {shell}
        <RefusalAlert message={t_i18n("Select two evaluations of one family in Evaluations to compare them.")} />
      </div>
    );
  }
  if (isLoading) return <PageSkeleton cards={4} />;
  if (error || !comparison || !view || !main) {
    return (
      <div className="space-y-6">
        {shell}
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
  const headline = headlineMetrics(comparison);
  const fileStem = slug(comparison.evaluations.map((e) => e.name).join(" vs "));

  // A launch of an evaluation replaces its runs in `?runs=`; the others keep theirs.
  const pickLaunch = (evaluation: ComparedEvaluation, launchKey: string) => {
    const launch = launchesOf(evaluation.run_choices).find((l) => l.key === launchKey);
    if (!launch) return;
    const kept = comparison.evaluations.filter((e) => e.id !== evaluation.id).flatMap((e) => e.runs.map((r) => r.id));
    setRuns([...kept, ...launch.runs.map((r) => r.id)]);
  };
  const launchOf = (evaluation: ComparedEvaluation) => {
    const current = new Set(evaluation.runs.map((r) => r.id));
    return (
      launchesOf(evaluation.run_choices).find((l) => l.runs.length === current.size && l.runs.every((r) => current.has(r.id)))?.key ?? null
    );
  };

  const exportCsv = () => {
    const csv = toCsv(view.ranking, [
      { header: "rank_best", value: (r) => r.best },
      { header: "rank_worst", value: (r) => r.worst },
      { header: "evaluation", value: (r) => r.evaluation.name },
      { header: "agent", value: (r) => r.run.subject_name },
      { header: "run", value: (r) => r.run.id },
      { header: "model_id", value: (r) => r.run.model_id },
      { header: "metric", value: () => metric.key },
      { header: "value", value: (r) => r.value.toFixed(2) },
      { header: "ci95_low", value: (r) => r.low.toFixed(2) },
      { header: "ci95_high", value: (r) => r.high.toFixed(2) },
      { header: "entries", value: (r) => r.entries },
      { header: "cost_per_entry_usd", value: (r) => (r.cost == null ? null : r.cost.toFixed(5)) },
      { header: "seconds_per_entry", value: (r) => (r.seconds == null ? null : r.seconds.toFixed(1)) },
      { header: "unreadable_answers", value: (r) => r.unreadable },
    ]);
    const filename = csvFilename(`${fileStem}-${metric.key}`);
    saveBlobAs(csvBlob(csv), filename);
    saved(filename);
  };
  const exportChart = (ref: React.RefObject<HTMLDivElement | null>, name: string) => {
    const filename = `${fileStem}-${name}-${slug(metric.key)}.svg`;
    if (saveSvg(ref.current, filename)) saved(filename);
  };
  const svgButton = (ref: React.RefObject<HTMLDivElement | null>, name: string) => (
    <Button variant="outline" size="sm" className="gap-1.5" onClick={() => exportChart(ref, name)}>
      <Icon name="download" size={16} /> {t_i18n("SVG")}
    </Button>
  );

  const firsts = view.ranking.filter((r) => r.best === 1);
  const verdict = (holds: boolean) =>
    holds
      ? t_i18n("The change holds across the entries.")
      : t_i18n("With {count, plural, one {# entry} other {# entries}}, this gap is within the noise: neither setup can be called better.", {
          count: pair?.keys.length ?? 0,
        });

  // Section 4: per evaluation with entries of its own, whether the shared ones stand for them.
  const representativeLead = (() => {
    const sentences = comparison.evaluations.flatMap((evaluation) => {
      const own = entriesOf(comparison.entries, evaluation.id).length;
      const otherCount = own - view.shared.length;
      if (otherCount <= 0) return [];
      const gap = largestGap(
        view.representativeness
          .filter((r) => r.evaluationId === evaluation.id)
          .map((r) => ({ shared: r.shared?.value ?? null, other: r.other?.value ?? null })),
      );
      if (gap === null) return [];
      const values = { name: evaluation.name, count: own, shared: view.shared.length, others: otherCount };
      return [
        isFairSample(gap)
          ? t_i18n(
              "{name} on its {count} entries: its models score within {gap} points on the {shared} shared entries and on the {others} others, so the shared entries are a fair sample.",
              { ...values, gap: Math.abs(gap).toFixed(1) },
            )
          : t_i18n(
              "{name} on its {count} entries: the largest gap between the {shared} shared entries and the {others} others is {gap} points, so read section 3 with that in mind.",
              { ...values, gap: signed(gap) },
            ),
      ];
    });
    return sentences.length
      ? sentences.join(" ")
      : t_i18n("Every entry of these evaluations is shared: the figures above cover each whole dataset.");
  })();

  return (
    <div className="space-y-6">
      {shell}

      {tab !== "pairs" && !board && (loadingEveryRun ? <PageSkeleton cards={2} /> : <RefusalAlert message={t_i18n("No completed run")} />)}
      {tab === "leaderboard" && board && (
        <LeaderboardTab board={board} metric={metric} metrics={comparison.metrics} label={board.label} onMetricChange={setMetricKey} />
      )}
      {tab === "results" && board && (
        <ResultsTab
          evaluationName={main.name}
          board={board}
          cells={board.cells}
          metric={metric}
          metrics={comparison.metrics}
          entryCount={board.entryCount}
          label={board.label}
          evaluation={everyRun?.evaluations[0] ?? main}
          onMetricChange={setMetricKey}
        />
      )}

      {tab === "pairs" && <>
      <div
        className="flex flex-wrap items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3"
        role="group"
        aria-label={t_i18n("Comparing")}
      >
        <span className="content-caption font-medium uppercase text-default-secondary">{t_i18n("Comparing")}</span>
        {comparison.evaluations.map((evaluation, i) => (
          <Fragment key={evaluation.id}>
            {i > 0 && <span className="content-compact text-default-secondary">{t_i18n("vs")}</span>}
            <span className="inline-flex items-center gap-2 rounded-sm border border-elevation-subtle-soft px-2 py-1 content-compact">
              <Swatch index={i} />
              {evaluation.name}
            </span>
            {launchesOf(evaluation.run_choices).length > 1 && (
              <SearchableSelect
                className="w-56"
                aria-label={t_i18n("Run of {name}", { name: evaluation.name })}
                searchable="auto"
                sort={false}
                placeholder={t_i18n("Newest run of each configuration")}
                value={launchOf(evaluation)}
                onValueChange={(key) => pickLaunch(evaluation, key)}
                options={[...launchesOf(evaluation.run_choices)].reverse().map((launch) => ({
                  value: launch.key,
                  label: t_i18n("{date} · {count, plural, one {# run} other {# runs}}", {
                    date: launch.time ? formatShortDateTimeIntl(intl, launch.time) : "—",
                    count: launch.runs.length,
                  }),
                }))}
              />
            )}
          </Fragment>
        ))}
        {headline.length > 1 && (
          <SegmentedFilter
            options={headline.map((m) => ({ key: m.key, label: m.label }))}
            value={metric.key}
            onChange={(key: string) => setMetricKey(key)}
            ariaLabel={t_i18n("Metric")}
            selection="radio"
          />
        )}
        <Button variant="outline" size="sm" className="ml-auto gap-1.5" onClick={exportCsv} disabled={!view.ranking.length}>
          <Icon name="download" size={16} /> {t_i18n("Export CSV")}
        </Button>
      </div>

      <RunPicker comparison={comparison} onChange={setRuns} />

      {!single && (
        <Section
          title={t_i18n("1 · What can be compared")}
          lead={t_i18n(
            "Opened from Evaluations with {count} {family} evaluations selected. Read this first: it says what the figures below measure.",
            { count: comparison.evaluations.length, family: comparison.family },
          )}
        >
          <ComparabilityMatrix comparison={comparison} sharedCount={view.shared.length} cells={view.cells} />
        </Section>
      )}

      <Section
        title={single ? t_i18n("1 · Run against run, entry by entry") : t_i18n("2 · Same model in both evaluations")}
        tools={
          pair?.change ? (
            <>
              {pairs.length > 1 ? (
                <SegmentedFilter
                  options={pairs.map((p, i) => ({
                    key: String(i),
                    label: single
                      ? `${p.beforeLabel} → ${p.afterLabel}`
                      : `${formatModelLabel(p.model, t_i18n)} · ${p.beforeLabel} → ${p.afterLabel}`,
                  }))}
                  value={String(Math.min(pairIndex, pairs.length - 1))}
                  onChange={(key: string) => setPairIndex(Number(key))}
                  ariaLabel={single ? t_i18n("Runs compared") : t_i18n("Model compared")}
                  selection="radio"
                  overflow="menu"
                />
              ) : (
                !single && (
                  <span className="content-compact text-default-secondary">
                    {t_i18n("{model}, the only model in both", { model: formatModelLabel(pair.model, t_i18n) })}
                  </span>
                )
              )}
              {svgButton(pairChart, "pair")}
            </>
          ) : undefined
        }
        lead={
          pair?.change && single
            ? t_i18n(
                "On the {count, plural, one {# entry} other {# entries}} both ran, {after} scores {afterValue} against {beforeValue} for {before} ({change}, 95% interval {low} to {high}). {verdict}",
                {
                  count: pair.keys.length,
                  after: pair.afterLabel,
                  afterValue: fmt(pair.change.after),
                  before: pair.beforeLabel,
                  beforeValue: fmt(pair.change.before),
                  change: signed(pair.change.change),
                  low: signed(pair.change.low),
                  high: signed(pair.change.high),
                  verdict: verdict(pair.change.holds),
                },
              )
            : pair?.change
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
                  verdict: verdict(pair.change.holds),
                },
              )
            : single
              ? t_i18n("Fewer than two runs have figures on the same entries: run the evaluation on another model to compare them.")
              : t_i18n("No model ran in two of these evaluations on the same entries: only configurations can be compared.")
        }
      >
        {pair?.change && (
          <>
            <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {pair.tiles.map((tile) => (
                <div key={tile.key} className="rounded-sm border border-elevation-subtle-soft p-3">
                  <dt className="content-caption text-default-secondary">{t_i18n(tile.label)}</dt>
                  <dd className="flex flex-wrap items-center gap-2 content-base font-medium tabular-nums">
                    {tile.change ? (
                      <>
                        <span>{fmt(tile.change.before)} → {fmt(tile.change.after)}</span>
                        <DeltaChip
                          change={tile.change.change}
                          lowerIsBetter={tile.lowerIsBetter}
                          title={t_i18n("vs {source} ({value})", { source: pair.beforeLabel, value: fmt(tile.change.before) })}
                        />
                      </>
                    ) : (
                      "—"
                    )}
                  </dd>
                  {tile.change && (
                    <dd className="content-caption text-default-secondary tabular-nums">
                      {t_i18n("95% interval of the change {low} to {high}", { low: signed(tile.change.low), high: signed(tile.change.high) })}
                    </dd>
                  )}
                </div>
              ))}
              <div className="rounded-sm border border-elevation-subtle-soft p-3">
                <dt className="content-caption text-default-secondary">{t_i18n(TILE_LABELS.costTime)}</dt>
                <dd className="content-base font-medium tabular-nums">
                  {pair.cost[0] == null && pair.cost[1] == null
                    ? t_i18n("no cost reported")
                    : `${usd(pair.cost[0])} → ${usd(pair.cost[1])}`}
                </dd>
                <dd className="content-caption text-default-secondary tabular-nums">
                  {formatSeconds(pair.time[0])} → {formatSeconds(pair.time[1])}
                </dd>
              </div>
            </dl>
            <div ref={pairChart}>
              <PairedDumbbell
                rows={pair.rows}
                metricLabel={metric.label}
                beforeLabel={pair.beforeLabel}
                afterLabel={pair.afterLabel}
                colors={[EVALUATION_COLORS[pair.before.colorIndex % 4], EVALUATION_COLORS[pair.after.colorIndex % 4]]}
                onOpen={
                  reviewable
                    ? (key) =>
                        navigate(
                          `/evaluations/review/diff?run=${pair.after.run.id}&against=${pair.before.run.id}&entry=${encodeURIComponent(key)}`,
                        )
                    : undefined
                }
              />
            </div>
            <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 content-caption text-default-secondary">
              {[pair.before, pair.after].map((side, i) => (
                <Fragment key={side.run.id}>
                  {i > 0 && <span aria-hidden>·</span>}
                  <span className="inline-flex items-center gap-1.5">
                    <Swatch index={side.colorIndex} />
                    {single ? runName(side.run, t_i18n) : `${side.evaluation.name} (${runName(side.run, t_i18n)})`}
                  </span>
                </Fragment>
              ))}
              <span>
                {t_i18n("A hollow dot is an entry without a readable answer, scored as everything missing.")}
                {reviewable && ` ${t_i18n("Click an entry to diff the two answers on it.")}`}
              </span>
            </p>
          </>
        )}
      </Section>

      <Section
        title={
          single
            ? t_i18n("2 · Every run ranked on the same entries")
            : t_i18n("3 · Every configuration on the {count, plural, one {# shared entry} other {# shared entries}}", { count: view.shared.length })
        }
        tools={view.ranking.length > 0 ? svgButton(forestChart, "ranking") : undefined}
        lead={
          view.ranking.length
            ? [
                firsts.length > 1
                  ? t_i18n("{count} configurations can be first: {names}. Their intervals overlap.", {
                      count: firsts.length,
                      names: tiedNames(firsts.map((r) => r.label)),
                    })
                  : t_i18n("{label} is first, with no overlap with the next one.", { label: view.ranking[0].label }),
                single
                  ? null
                  : t_i18n("Best of each: {list}.", {
                      list: comparison.evaluations
                        .map((e) => view.ranking.find((r) => r.evaluation.id === e.id))
                        .filter((r): r is NonNullable<typeof r> => !!r)
                        .map((r) => `${r.evaluation.name} ${r.label} ${fmt(r.value)}`)
                        .join(" · "),
                    }),
              ]
                .filter(Boolean)
                .join(" ")
            : t_i18n("No run has a figure on the shared entries.")
        }
      >
        {view.ranking.length > 0 && (
          <div ref={forestChart}>
            <RankedForest rows={view.ranking} metricLabel={metric.label} />
          </div>
        )}
        {single ? (
          <p className="content-caption text-default-secondary">
            {t_i18n("One line per agent and model: its figure on the same entries, with a 95% interval (bootstrap over the entries). A rank such as 1–4 means it could be anywhere from first to fourth.")}
          </p>
        ) : (
          <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 content-caption text-default-secondary">
            <span>{t_i18n("One line per agent and model, one run each, on the same entries and the same scorer.")}</span>
            {comparison.evaluations.map((e, i) => (
              <Fragment key={e.id}>
                {i > 0 && <span aria-hidden>·</span>}
                <span className="inline-flex items-center gap-1.5">
                  <Swatch index={i} />
                  {e.name}
                </span>
              </Fragment>
            ))}
          </p>
        )}
      </Section>

      {!single && <Section title={t_i18n("4 · Do the shared entries stand for each dataset?")} lead={representativeLead}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[48rem] content-compact">
            <thead>
              <tr className="border-b border-elevation-subtle-soft text-left">
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Model")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("All its entries")}</th>
                <th scope="col" className="px-3 py-2 font-medium">
                  {t_i18n("{count} shared", { count: view.shared.length })}
                </th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Its others")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Cost per entry")}</th>
                <th scope="col" className="px-3 py-2 font-medium">{t_i18n("Time per entry")}</th>
                {reviewable && <th scope="col" className="px-3 py-2"><span className="sr-only">{t_i18n("Review")}</span></th>}
              </tr>
            </thead>
            <tbody>
              {view.representativeness.map((row) => (
                <tr key={row.id} className="border-b border-elevation-subtle-soft last:border-b-0">
                  <th scope="row" className="px-3 py-2 text-left font-normal">
                    <span className="inline-flex items-center gap-2">
                      <Swatch index={row.colorIndex} />
                      {row.label}
                      {row.isDefault && <span className="content-caption text-default-secondary">{t_i18n("default")}</span>}
                    </span>
                  </th>
                  {[row.all, row.shared, row.other].map((cell, i) => (
                    <td key={i} className="px-3 py-2 tabular-nums">
                      {cell ? (
                        <>
                          {fmt(cell.value)} <span className="content-caption text-default-secondary">{fmt(cell.low)}–{fmt(cell.high)} · {cell.n}</span>
                        </>
                      ) : "—"}
                    </td>
                  ))}
                  <td className="px-3 py-2 tabular-nums">{usd(row.cost)}</td>
                  <td className="px-3 py-2 tabular-nums">{formatSeconds(row.seconds)}</td>
                  {reviewable && (
                    <td className="px-3 py-2 text-right">
                      <Button variant="ghost" size="sm" onClick={() => navigate(`/evaluations/review?run=${encodeURIComponent(row.id)}`)}>
                        {t_i18n("Review its {count, plural, one {# entry} other {# entries}}", { count: row.entryCount })}
                      </Button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="content-caption text-default-secondary">
          {t_i18n("{metric} pooled over the entries, with 95% bootstrap intervals.", { metric: metric.label })}
          {reviewable && ` ${t_i18n("“Review” opens the run against the expected bundles on all its entries, the ones found only in its evaluation included.")}`}
        </p>
      </Section>}
      </>}
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
  const single = comparison.evaluations.length === 1;
  const headline = headlineMetrics(comparison);
  // One evaluation: the reference first, so each pair reads "reference → other": the
  // default model's run, then the baseline agent's. The colour then tells the runs apart,
  // else the evaluations.
  const ordered = comparison.evaluations.flatMap((evaluation, index) => evaluation.runs.map((run) => ({ run, evaluation, index })));
  if (single) {
    const baselines = new Set(comparison.evaluations[0].subjects.filter((s) => s.is_baseline).map((s) => s.id));
    const reference = (r: (typeof ordered)[number]) =>
      2 * Number(r.run.is_default_model) + Number(!!r.run.subject_id && baselines.has(r.run.subject_id));
    ordered.sort((a, b) => reference(b) - reference(a));
  }
  const runs = ordered.map((r, i) => ({ ...r, colorIndex: single ? i : r.index }));
  // By what differs between the runs compared (the colour says the evaluation).
  const runLabel = runLabeler(runs.map((r) => r.run));
  const name = (r: (typeof runs)[number]) => runLabel(r.run);

  const ranking = rankSpread(
    runs
      .map((r) => {
        const counts = runCounts(cells, r.run.id, metric.key, shared);
        const interval = bootstrap(counts, seedOf(r.run.id));
        return interval
          ? {
              id: r.run.id,
              label: name(r),
              colorIndex: r.colorIndex,
              runs: 1,
              isDefault: r.run.is_default_model,
              run: r.run,
              evaluation: r.evaluation,
              entries: counts.length,
              cost: perEntryMean(cells, r.run.id, "cost", shared),
              seconds: perEntryMean(cells, r.run.id, "seconds", shared),
              unreadable: unreadableCount(cells, r.run.id, shared),
              ...interval,
            }
          : null;
      })
      .filter((x): x is NonNullable<typeof x> => x !== null)
      .sort((a, b) => b.value - a.value),
  );

  // The same model in two evaluations: each later evaluation against the first that ran it.
  // One evaluation: every two of its runs.
  const pairs = [];
  for (let i = 0; i < runs.length; i++) {
    for (let j = i + 1; j < runs.length; j++) {
      const a = runs[i], b = runs[j];
      if (!single && (a.evaluation.id === b.evaluation.id || a.run.model_id !== b.run.model_id)) continue;
      const keys = sharedEntries(comparison.entries, [a.evaluation.id, b.evaluation.id]).map((e) => e.key);
      const counts = pairedCounts(cells, a.run.id, b.run.id, metric.key, keys);
      const change = pairedChange(counts.before, counts.after, seedOf(a.run.id + b.run.id));
      if (!change) continue;
      const tiles = headline.map((m) => {
        const pc = pairedCounts(cells, a.run.id, b.run.id, m.key, keys);
        return {
          key: m.key,
          label: m.label,
          lowerIsBetter: m.direction === "lower_is_better",
          change: pairedChange(pc.before, pc.after, seedOf(a.run.id + b.run.id + m.key)),
        };
      });
      const unreadable = (runId: string, key: string) => !!cells.get(runId)?.get(key)?.unreadable;
      pairs.push({
        model: a.run.model_id,
        before: a,
        after: b,
        beforeLabel: single ? name(a) : a.evaluation.name,
        afterLabel: single ? name(b) : b.evaluation.name,
        keys: counts.keys,
        change,
        tiles,
        cost: [perEntryMean(cells, a.run.id, "cost", counts.keys), perEntryMean(cells, b.run.id, "cost", counts.keys)],
        time: [perEntryMean(cells, a.run.id, "seconds", counts.keys), perEntryMean(cells, b.run.id, "seconds", counts.keys)],
        rows: counts.keys.map((key, k) => ({
          key,
          label: label.get(key) ?? key,
          // A 0-of-0 entry is a correct empty answer, 100, as `pooled` reads it.
          before: counts.before[k][1] ? (100 * counts.before[k][0]) / counts.before[k][1] : 100,
          after: counts.after[k][1] ? (100 * counts.after[k][0]) / counts.after[k][1] : 100,
          beforeUnreadable: unreadable(a.run.id, key),
          afterUnreadable: unreadable(b.run.id, key),
        })),
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
    return {
      id: r.run.id,
      evaluationId: r.evaluation.id,
      label: name(r),
      colorIndex: r.colorIndex,
      isDefault: r.run.is_default_model,
      entryCount: [...(cells.get(r.run.id)?.keys() ?? [])].filter((k) => own.includes(k)).length,
      cost: perEntryMean(cells, r.run.id, "cost", own),
      seconds: perEntryMean(cells, r.run.id, "seconds", own),
      all: stat(own, "all"),
      shared: stat(shared, "shared"),
      other: stat(other, "other"),
    };
  });

  return { metric, cells, shared, ranking, pairs, representativeness };
}
