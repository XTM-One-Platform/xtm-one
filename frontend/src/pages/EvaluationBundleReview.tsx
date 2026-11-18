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

// The STIX bundle review (#12282), in two modes.
//
// The run review, `/evaluations/review/<view>?run=<runId>[&against=expected|none|<runId>][&entry=<key>]`:
// one run of a STIX_BUNDLE evaluation over every report it answered, against the expected
// bundles, nothing, or another run of the evaluation. Its figures pooled over the reports
// and where its entity points go, then one report at a time, object by object.
//
// The answers review, `?results=id1,id2`: one dataset entry's expected bundle against one
// to four answers. With one answer it is checked against the expected bundle; with two or
// more, the second is compared with the first, each judged against the expected bundle.
//
// Both show the same five views of an entry: coverage, annotate, diff, graph and knowledge graph.

import { useMemo, useState, type ComponentProps, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Alert, Icon, Tabs, TabsContent, TabsList, TabsTrigger } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { PageSkeleton } from "@/components/ui/skeleton";
import { AnnotateView, type AnnotateLens } from "@/components/evaluation/bundle-review/AnnotateView";
import { EVALUATION_PAGE_LABELS, EvaluationPageShell, evaluationPageTabs, type Crumb } from "@/components/evaluation/compare/multi-page-shell";
import { CoverageView } from "@/components/evaluation/bundle-review/CoverageView";
import { DiffView, type DiffLayout } from "@/components/evaluation/bundle-review/DiffView";
import { GraphView } from "@/components/evaluation/bundle-review/GraphView";
import { KnowledgeGraphView, type GraphSide } from "@/components/evaluation/bundle-review/KnowledgeGraphView";
import { ReportHead, ReportStrip, type StripTile } from "@/components/evaluation/bundle-review/ReportStrip";
import { ReviewInspector } from "@/components/evaluation/bundle-review/ReviewInspector";
import { ReviewRail } from "@/components/evaluation/bundle-review/ReviewRail";
import { AnswerCards, JudgePanel, PairPicker, ScoreCards } from "@/components/evaluation/bundle-review/ReviewSummary";
import { CausesPanel, RunHead, RunKpis, type RunComparatorFigures } from "@/components/evaluation/bundle-review/RunLevel";
import { causeLabel } from "@/components/evaluation/bundle-review/labels";
import { RunPicker, type RunNames } from "@/components/evaluation/bundle-review/RunPicker";
import { SuiteDriftNotice } from "@/components/evaluation/bundle-review/SuiteDriftNotice";
import type { ReviewContext, ReviewSelection } from "@/components/evaluation/bundle-review/context";
import { useBundleReview, useBundleReviews, useEvaluationComparison, useEvaluationRun } from "@/hooks/api/useEvaluationApi";
import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  NONE,
  answerName,
  FILTER_KEYS,
  buildModel,
  derive,
  forceLayout,
  locateMentions,
  modeOf,
  type ReviewPair,
} from "@/lib/evaluation-bundle-review";
import { cellIndex, entriesOf } from "@/lib/evaluation-compare";
import { launchesOf, runLabeler } from "@/lib/evaluation-leaderboard";
import {
  ENTITY_F1,
  causeSummary,
  cellValue,
  comparatorOf,
  configRuns,
  entryCauses,
  presets as presetsOf,
  reviewEntries,
  reviewResultIds,
  runFigures,
  runsOfComparison,
  sharedCounted,
  type CauseKey,
} from "@/lib/evaluation-run-review";
import { EVALUATION_TRIGGER_LABELS } from "@/lib/constants";
import { formatModelLabel } from "@/lib/utils";
import type { BundleReview } from "@/types";

/** The most answers the answers review lays side by side. */
const MAX_ANSWERS = 4;
const LETTERS = ["A", "B", "C", "D"];
/** The most entries one batch of reviews reads (the backend's limit). */
const MAX_BATCH_ENTRIES = 200;

type AnnotateArcs = NonNullable<ComponentProps<typeof AnnotateView>["arcs"]>;

type ReviewTab = "coverage" | "annotate" | "diff" | "graph" | "kg";
/** The view tabs, read by `t_i18n`. @i18n-keys */
const TAB_LABELS = {
  coverage: "Coverage",
  annotate: "Annotate",
  diff: "Diff",
  graph: "Graph",
  kg: "Knowledge graph",
} as const satisfies Record<ReviewTab, string>;
const viewOf = (v: string | null): ReviewTab => (v && v in TAB_LABELS ? (v as ReviewTab) : "coverage");

const ReviewGlyph = ({ className }: { className?: string }) => <Icon name="file-search" size={24} className={className} />;

/** What the reviewer set on the page: kept from one report to the next. */
function useReviewUi() {
  const [whatIf, setWhatIf] = useState(false);
  const [type, setType] = useState<string | null>(null);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [selection, setSelection] = useState<{ entry: string; sel: ReviewSelection } | null>(null);
  const [diffLayout, setDiffLayout] = useState<DiffLayout>("split");
  const [hideMatched, setHideMatched] = useState(false);
  const [lens, setLens] = useState<AnnotateLens>("both");
  const [arcs, setArcs] = useState<AnnotateArcs>("selected");
  const [kgSide, setKgSide] = useState<GraphSide>("both");
  const [judgeOpen, setJudgeOpen] = useState(false);
  const [cause, setCause] = useState<CauseKey | null>(null);
  return {
    whatIf, setWhatIf, type, setType, hidden, setHidden, selection, setSelection, diffLayout, setDiffLayout,
    hideMatched, setHideMatched, lens, setLens, arcs, setArcs, kgSide, setKgSide, judgeOpen, setJudgeOpen, cause, setCause,
  };
}
type ReviewUi = ReturnType<typeof useReviewUi>;

/** Each entry model's force layout, computed on first use and kept with the model. */
const LAYOUTS = new WeakMap<ReturnType<typeof buildModel>, ReturnType<typeof forceLayout>>();
function layoutOf(model: ReturnType<typeof buildModel>): ReturnType<typeof forceLayout> {
  let layout = LAYOUTS.get(model);
  if (!layout) {
    layout = forceLayout(model);
    LAYOUTS.set(model, layout);
  }
  return layout;
}

interface EntryOptions {
  /** The answers the views draw (a run review: the pair); every loaded one otherwise. */
  only?: string[];
  letter: (s: string) => string;
  sourceName: (s: string) => string;
  comparator: ReviewContext["comparator"];
  history: ReviewContext["history"];
}

/**
 * One entry of a review: the scores of the answer under review, the judge's reasoning,
 * the rail, the five views and the inspector. `head` draws what goes above the scores.
 */
function EntryReview({
  review,
  pair,
  ui,
  view,
  onView,
  options,
  head,
}: {
  review: BundleReview;
  pair: ReviewPair;
  ui: ReviewUi;
  view: ReviewTab;
  onView: (view: ReviewTab) => void;
  options: EntryOptions;
  head?: (ctx: ReviewContext) => ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const mode = modeOf(pair);
  // One model per set of sources, not per render: the knowledge graph's layout is kept with it.
  const onlyKey = options.only?.join(",");
  const only = useMemo(() => (onlyKey ? onlyKey.split(",") : null), [onlyKey]);
  const fullModel = useMemo(() => buildModel(review), [review]);
  const model = useMemo(() => (only ? buildModel(review, only) : fullModel), [review, fullModel, only]);
  const located = useMemo(() => locateMentions(model, review.entry.pages), [model, review.entry.pages]);
  // The force layout is quadratic per iteration: computed when the knowledge graph opens,
  // once per entry, never on load (a large bundle would block every other tab).
  const layout = view === "kg" ? layoutOf(model) : null;
  const derived = useMemo(() => derive(model, pair, ui.whatIf), [model, pair, ui.whatIf]);
  const filters = useMemo(() => ({ type: ui.type, hidden: ui.hidden }), [ui.type, ui.hidden]);
  const entryKey = review.entry.key ?? review.entry.label;
  const selection = ui.selection?.entry === entryKey ? ui.selection.sel : null;
  const ctx: ReviewContext = {
    review,
    model,
    pair,
    mode,
    whatIf: ui.whatIf,
    derived,
    filters,
    located,
    selection,
    select: (sel) => ui.setSelection({ entry: entryKey, sel }),
    letter: options.letter,
    sourceName: options.sourceName,
    shown: [...new Set([EXPECTED, ...(mode === "runs" ? [pair.ref, pair.under] : [pair.under])])],
    columns: options.only ?? review.answers.map((a) => a.key),
    fullModel,
    comparator: options.comparator,
    history: options.history,
  };
  const toggleStatus = (key: string) => {
    const next = new Set(ui.hidden);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    // Hiding every status would leave every view empty: keep the last one shown.
    if (next.size === FILTER_KEYS[mode].length) return;
    ui.setHidden(next);
  };
  const unreadable = [...new Set(mode === "runs" ? [pair.ref, pair.under] : [pair.under])].filter((k) => review.unreadable.includes(k));

  return (
    <div className="space-y-4">
      {head?.(ctx)}
      {unreadable.map((k) => (
        <Alert
          key={k}
          severity="warning"
          title={t_i18n("Answer {letter} is not a readable STIX bundle", { letter: ctx.letter(k) })}
          description={t_i18n("The scorer could not read it as a bundle, so every expected object counts as missing.")}
        />
      ))}
      <ScoreCards ctx={ctx} onOpenJudge={() => ui.setJudgeOpen(true)} />
      {ui.judgeOpen && <JudgePanel ctx={ctx} onClose={() => ui.setJudgeOpen(false)} />}
      <div className="grid gap-4 xl:grid-cols-[13rem_minmax(0,1fr)_20rem]">
        <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft self-start">
          <CardContent className="p-3">
            <ReviewRail
              ctx={ctx}
              onType={ui.setType}
              onToggle={toggleStatus}
              onClear={() => {
                ui.setType(null);
                ui.setHidden(new Set());
              }}
            />
          </CardContent>
        </Card>
        <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft min-w-0">
          <CardContent className="p-3">
            <Tabs value={view} onValueChange={(v) => onView(v as ReviewTab)}>
              <TabsList aria-label={t_i18n("Review views")}>
                {(Object.keys(TAB_LABELS) as ReviewTab[]).map((k) => (
                  <TabsTrigger key={k} value={k}>
                    {t_i18n(TAB_LABELS[k])}
                  </TabsTrigger>
                ))}
              </TabsList>
              <TabsContent value="coverage" className="pt-3">
                <CoverageView ctx={ctx} />
              </TabsContent>
              <TabsContent value="annotate" className="pt-3">
                <AnnotateView ctx={ctx} lens={ui.lens} onLens={ui.setLens} arcs={ui.arcs} onArcs={ui.setArcs} />
              </TabsContent>
              <TabsContent value="diff" className="pt-3">
                <DiffView ctx={ctx} layout={ui.diffLayout} onLayout={ui.setDiffLayout} hideMatched={ui.hideMatched} onHideMatched={ui.setHideMatched} />
              </TabsContent>
              <TabsContent value="graph" className="pt-3">
                <GraphView ctx={ctx} />
              </TabsContent>
              <TabsContent value="kg" className="pt-3">
                {layout && <KnowledgeGraphView ctx={ctx} layout={layout} side={ui.kgSide} onSide={ui.setKgSide} />}
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
        <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft self-start xl:sticky xl:top-4">
          <CardContent className="p-3">
            <ReviewInspector ctx={ctx} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

/** The view in the path (`/evaluations/review/<view>`, path routes over `?view=`, #10203),
 *  so a link opens the review on it; the query keeps the runs and the entry. */
function useView(): [ReviewTab, (view: ReviewTab) => void] {
  const { view: segment } = useParams<{ view?: string }>();
  const { search } = useLocation();
  const navigate = useNavigate();
  const view = viewOf(segment ?? null);
  const setView = (next: ReviewTab) => navigate(`/evaluations/review/${next}${search}`, { replace: true });
  return [view, setView];
}

// ─── The answers review (`?results=`) ────────────────────────────────────────

/** One answer: checked against the expected bundle; two or more: the second against the first. */
function defaultPair(review: BundleReview): ReviewPair {
  const [first, second] = review.answers;
  return second ? { ref: first.key, under: second.key } : { ref: EXPECTED, under: first.key };
}

function validPair(review: BundleReview, pair: ReviewPair | null): pair is ReviewPair {
  const keys = new Set(review.answers.map((a) => a.key));
  return !!pair && keys.has(pair.under) && (pair.ref === EXPECTED || pair.ref === NONE || keys.has(pair.ref)) && pair.ref !== pair.under;
}

function AnswersReview({ review }: { review: BundleReview }) {
  const { t_i18n } = useFormatter();
  const ui = useReviewUi();
  const [view, setView] = useView();
  const [chosenPair, setPair] = useState<ReviewPair | null>(null);
  const pair = useMemo(() => (validPair(review, chosenPair) ? chosenPair : defaultPair(review)), [review, chosenPair]);
  const mode = modeOf(pair);
  const letter = (s: string) => {
    if (s === EXPECTED) return "E";
    const i = review.answers.findIndex((a) => a.key === s);
    return i < 0 ? "" : LETTERS[i] ?? String(i + 1);
  };
  const sourceName = (s: string) => {
    if (s === EXPECTED) return t_i18n("Expected");
    const a = review.answers.find((x) => x.key === s);
    if (!a) return s;
    const name = answerName(review.answers, a, (id) => formatModelLabel(id, t_i18n));
    return `${name.title} · ${name.detail}`;
  };
  const choosePair = (next: ReviewPair) => {
    // Each mode has its own statuses: a filter of the old one means nothing in the new one.
    if (modeOf(next) !== mode) ui.setHidden(new Set());
    setPair(next);
  };
  return (
    <div className="space-y-4">
      <SuiteDriftNotice
        runs={review.answers.map((a) => ({ id: a.run_id, name: sourceName(a.key), version: a.metric_suite_version }))}
        current={review.metric_suite_version}
      />
      <EntryReview
        review={review}
        pair={pair}
        ui={ui}
        view={view}
        onView={setView}
        options={{ letter, sourceName, comparator: null, history: null }}
        head={(ctx) => (
          <>
            <AnswerCards ctx={ctx} />
            <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
              <CardContent className="space-y-3 p-4">
                <PairPicker ctx={ctx} onPair={choosePair} onWhatIf={ui.setWhatIf} />
                <p className="content-compact">
                  {mode === "score"
                    ? t_i18n("{answer} checked against the expected bundle.", { answer: `${letter(pair.under)} · ${sourceName(pair.under)}` })
                    : mode === "alone"
                      ? t_i18n("{answer} alone: what it found, checked against the expected bundle.", { answer: `${letter(pair.under)} · ${sourceName(pair.under)}` })
                      : t_i18n("What changed from {a} to {b}, each judged against the expected bundle.", { a: letter(pair.ref), b: letter(pair.under) })}
                </p>
              </CardContent>
            </Card>
          </>
        )}
      />
    </div>
  );
}

function AnswersReviewPage() {
  const { t_i18n } = useFormatter();
  const [params] = useSearchParams();
  const resultIds = useMemo(
    () => [...new Set((params.get("results") ?? "").split(",").map((s) => s.trim()).filter(Boolean))].slice(0, MAX_ANSWERS),
    [params],
  );
  const { data, isLoading, isError, error } = useBundleReview(resultIds);
  const firstRun = data?.answers[0]?.run_id;
  return (
    <div className="space-y-4">
      <EntityPageHeader
        backTo={firstRun ? `/evaluations/runs/${firstRun}` : "/evaluations"}
        backLabel={firstRun ? "Back to the run" : "Back to Evaluations"}
        title={t_i18n("Bundle review")}
        description={data ? data.entry.label : t_i18n("The expected STIX bundle of one entry against the answers, object by object.")}
      />
      {resultIds.length === 0 ? (
        <EmptyState
          icon={ReviewGlyph}
          title="No answer to review"
          description="Open a bundle review from an entry of a STIX bundle evaluation run, or from a run comparison."
        />
      ) : isLoading ? (
        <PageSkeleton cards={3} />
      ) : isError ? (
        <Alert
          severity="error"
          title={t_i18n("This review cannot be shown")}
          description={error instanceof Error ? error.message : t_i18n("The review could not be loaded.")}
        />
      ) : data && data.answers.length > 0 ? (
        <AnswersReview review={data} />
      ) : null}
    </div>
  );
}

// ─── The run review (`?run=`) ─────────────────────────────────────────────────

const isRunRef = (against: string) => against !== EXPECTED && against !== NONE;

function RunReviewPage({ runId }: { runId: string }) {
  const { t_i18n, intl } = useFormatter();
  const [params, setParams] = useSearchParams();
  const against = params.get("against") || EXPECTED;
  const [view, setView] = useView();
  const ui = useReviewUi();
  const mode = modeOf({ ref: against === EXPECTED || against === NONE ? against : "run", under: runId });

  // The evaluation of the run, its completed runs, then every one of their results.
  const runQ = useEvaluationRun(runId);
  const evaluationId = runQ.data?.evaluation_id;
  const base = useEvaluationComparison(evaluationId ? [evaluationId] : []);
  const wanted = useMemo(() => {
    const ids = new Set(runsOfComparison(base.data).map((r) => r.id));
    ids.add(runId);
    if (isRunRef(against)) ids.add(against);
    return [...ids].sort();
  }, [base.data, runId, against]);
  const full = useEvaluationComparison(evaluationId && base.data ? [evaluationId] : [], wanted);
  const comparison = full.data;
  const runs = useMemo(() => runsOfComparison(comparison), [comparison]);
  const cells = useMemo(() => (comparison ? cellIndex(comparison) : new Map()), [comparison]);
  const under = runs.find((r) => r.id === runId);
  const ref = isRunRef(against) ? runs.find((r) => r.id === against) : undefined;
  const comparatorRun = under ? comparatorOf(runs, under, against) : null;

  // Names: by agent or model, and when the run started.
  const labeler = useMemo(() => runLabeler(runs), [runs]);
  const names: RunNames = {
    config: (run) => labeler(run),
    code: (run) => (run.created_at ? intl.formatDate(run.created_at, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"),
    name: (run) => `${labeler(run)} · ${names.code(run)}`,
    launch: (time, trigger) =>
      [time ? intl.formatDate(time, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—", trigger ? t_i18n(EVALUATION_TRIGGER_LABELS[trigger] ?? trigger) : null]
        .filter(Boolean)
        .join(" · "),
  };
  const figureOf = (id: string) => runFigures(cells.get(id)).value[ENTITY_F1];

  // The reports: those the run (and the reference run) answered, in dataset order.
  const refId = ref ? against : null;
  const hasUnder = !!under;
  const entries = useMemo(
    () => (hasUnder ? reviewEntries(comparison, cells, refId ? [runId, refId] : [runId]) : []),
    [comparison, cells, hasUnder, runId, refId],
  );
  const entryKey = entries.find((e) => e.key === params.get("entry"))?.key ?? entries[0]?.key ?? null;
  const entryIndex = entries.findIndex((e) => e.key === entryKey);
  const cellOf = (rid: string | undefined, key: string | null) => (rid && key ? cells.get(rid)?.get(key) : undefined);

  // This report's answers: the pair, the comparator, and the configuration's other runs.
  const history = under && entryKey ? configRuns(runs, under).filter((r) => cells.get(r.id)?.has(entryKey)) : [];
  const resultIds = under && entryKey
    ? reviewResultIds(
        (rid) => cellOf(rid, entryKey)?.result_id,
        { under: under.id, ref: against },
        mode === "runs" ? null : comparatorRun?.id ?? null,
        history.map((r) => r.id),
      )
    : [];
  const reviewQ = useBundleReview(resultIds);

  // Every report's answer of the run, without its text: where its entity points go.
  const batch = useMemo(() => {
    const out: { key: string; rid: string }[] = [];
    for (const e of entries) {
      const cell = cells.get(runId)?.get(e.key);
      if (cell?.counted && out.length < MAX_BATCH_ENTRIES) out.push({ key: e.key, rid: cell.result_id });
    }
    return out;
  }, [entries, cells, runId]);
  const causesQ = useBundleReviews(batch.map((b) => [b.rid]));
  const causesByEntry = useMemo(() => {
    const out = new Map<string, ReturnType<typeof entryCauses>>();
    causesQ.data?.forEach((review, i) => {
      const b = batch[i];
      if (b) out.set(b.key, entryCauses(buildModel(review), b.rid));
    });
    return out;
  }, [causesQ.data, batch]);
  const summary = useMemo(() => (causesQ.data ? causeSummary([...causesByEntry.values()]) : null), [causesQ.data, causesByEntry]);

  const setParam = (next: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(next)) {
          if (v == null) p.delete(k);
          else p.set(k, v);
        }
        return p;
      },
      { replace: true },
    );
  const choosePair = (nextRef: string, nextUnder: string) => {
    const r = nextRef === nextUnder ? EXPECTED : nextRef;
    if (modeOf({ ref: isRunRef(r) ? "run" : r, under: nextUnder }) !== mode) ui.setHidden(new Set());
    ui.setJudgeOpen(false);
    setParam({ run: nextUnder, against: r === EXPECTED ? null : r });
  };

  // The evaluation's pages: crumbs, its name, its dataset and runs, the tab row.
  const main = comparison?.evaluations[0];
  const crumbs: Crumb[] = [{ label: t_i18n("Evaluations"), to: "/evaluations" }];
  if (main) crumbs.push({ label: main.name, to: `/evaluations/${main.id}` });
  crumbs.push({ label: t_i18n(EVALUATION_PAGE_LABELS.review) });
  const header = (
    <EvaluationPageShell
      crumbs={crumbs}
      title={main?.name ?? t_i18n(EVALUATION_PAGE_LABELS.review)}
      subtitle={
        main && comparison
          ? t_i18n(
              "Dataset “{dataset}” · {count, plural, one {# entry} other {# entries}} · {models, plural, one {# model} other {# models}} · {launches, plural, one {# launch} other {# launches}}",
              {
                dataset: main.datasets.map((d) => d.name).join(", ") || "—",
                count: entriesOf(comparison.entries, main.id).length,
                models: new Set(runs.map((r) => r.model_id)).size,
                launches: launchesOf(runs).length,
              },
            )
          : t_i18n("One run against the expected bundles, report by report, object by object.")
      }
      tabs={evaluationId ? evaluationPageTabs({ ids: [evaluationId], reviewRun: runId }) : []}
      active="review"
      actions={
        <Link
          to={`/evaluations/runs/${runId}`}
          className="inline-flex items-center gap-1.5 rounded-sm content-compact font-medium text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        >
          <Icon name="external-link" size={16} />
          {t_i18n("Open the run")}
        </Link>
      }
    />
  );
  const failed = runQ.isError ? runQ.error : base.isError ? base.error : full.isError ? full.error : null;
  if (failed) {
    return (
      <div className="space-y-4">
        {header}
        <Alert severity="error" title={t_i18n("This review cannot be shown")} description={failed instanceof Error ? failed.message : t_i18n("The review could not be loaded.")} />
      </div>
    );
  }
  if (runQ.isLoading || base.isLoading || full.isLoading || !comparison) {
    return (
      <div className="space-y-4">
        {header}
        <PageSkeleton cards={3} />
      </div>
    );
  }
  if (comparison.family !== "STIX_BUNDLE") {
    return (
      <div className="space-y-4">
        {header}
        <EmptyState icon={ReviewGlyph} title="No bundle review for this run" description="The run review shows the runs of a STIX bundle evaluation." />
      </div>
    );
  }
  if (!under) {
    return (
      <div className="space-y-4">
        {header}
        <EmptyState icon={ReviewGlyph} title="This run cannot be reviewed yet" description="A run is reviewed once it has completed." />
      </div>
    );
  }

  // The run level: its figures, and their change against the comparator.
  const bFigures = runFigures(cells.get(under.id));
  let comparator: RunComparatorFigures | null = null;
  if (comparatorRun) {
    const shared = sharedCounted(cells.get(under.id), cells.get(comparatorRun.id));
    const partial = shared.size < bFigures.counted;
    comparator = {
      name: names.name(comparatorRun),
      figures: runFigures(cells.get(comparatorRun.id), partial ? shared : undefined),
      mine: partial ? runFigures(cells.get(under.id), shared) : bFigures,
      partial,
    };
  }
  const judgeOn = under.judge_score != null || bFigures.judge != null;
  const allUnreadable = bFigures.counted > 0 && bFigures.unreadable === bFigures.counted;
  const tiles: StripTile[] = entries.map((e) => {
    const cell = cellOf(under.id, e.key);
    const cmp = cellOf(comparatorRun?.id, e.key);
    const causes = causesByEntry.get(e.key);
    return {
      key: e.key,
      label: e.label,
      value: cellValue(cell, ENTITY_F1),
      was: cmp && !cmp.unreadable ? cellValue(cmp, ENTITY_F1) : null,
      state: !cell?.counted ? "left-out" : cell.unreadable ? "unreadable" : "ok",
      dimmed: !!ui.cause && !!causesQ.data && !((causes?.[ui.cause] ?? 0) > 0),
    };
  });
  const presets = presetsOf(runs, under, figureOf);

  // The report level: the pair's answers on this report, by result id.
  const review = reviewQ.data;
  const resultToRun = new Map((review?.answers ?? []).map((a) => [a.key, a.run_id]));
  const keyOf = (id: string | undefined | null) => (id ? cellOf(id, entryKey)?.result_id : undefined);
  const underKey = keyOf(under.id);
  const pair: ReviewPair | null = underKey ? { under: underKey, ref: ref ? keyOf(ref.id) ?? EXPECTED : against === NONE ? NONE : EXPECTED } : null;
  const runOfKey = (k: string) => runs.find((r) => r.id === resultToRun.get(k));
  const comparatorKey = mode === "runs" ? pair?.ref : keyOf(comparatorRun?.id);
  const options: EntryOptions | null = pair
    ? {
        only: [...new Set(mode === "runs" ? [pair.ref, pair.under] : [pair.under])],
        letter: (s) => (s === EXPECTED ? "E" : s === pair.under ? "B" : s === pair.ref ? "A" : ""),
        sourceName: (s) => {
          if (s === EXPECTED) return t_i18n("Expected");
          const run = runOfKey(s);
          return run ? names.name(run) : s;
        },
        comparator: comparatorKey && comparatorRun && review?.answers.some((a) => a.key === comparatorKey) ? { key: comparatorKey, name: names.name(comparatorRun) } : null,
        history: {
          label: names.config(under),
          runs: history
            .map((r) => ({ key: keyOf(r.id) ?? "", code: names.code(r) }))
            .filter((x) => x.key && review?.answers.some((a) => a.key === x.key)),
        },
      }
    : null;

  return (
    <div className="space-y-4">
      {header}
      <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
        <CardContent className="p-4">
          <RunPicker
            runs={runs}
            under={under}
            against={ref ? ref.id : against === NONE ? NONE : EXPECTED}
            figure={figureOf}
            presets={presets}
            names={names}
            whatIf={ui.whatIf}
            onPair={choosePair}
            onWhatIf={ui.setWhatIf}
          />
        </CardContent>
      </Card>
      <SuiteDriftNotice
        runs={[under, ref].flatMap((r) => (r ? [{ id: r.id, name: names.name(r), version: r.metric_suite_version, rescoreError: r.rescore_error }] : []))}
        current={comparison?.metric_suite_version}
      />
      <section aria-label={t_i18n("Run")} className="space-y-3">
        <RunHead name={names.name(under)} sub={names.launch(under.created_at ?? "", under.trigger_reason ?? null)} figures={bFigures} comparator={comparator} />
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_24rem]">
          <RunKpis figures={bFigures} comparator={comparator} judgeOn={judgeOn} />
          <CausesPanel
            state={causesQ.isError ? "error" : causesQ.data ? "ready" : "loading"}
            summary={summary}
            allUnreadable={allUnreadable}
            cause={ui.cause}
            onCause={ui.setCause}
          />
        </div>
      </section>
      <section aria-label={t_i18n("Report")} className="space-y-3">
        {entries.length === 0 || !entryKey ? (
          <EmptyState
            icon={ReviewGlyph}
            title="No report to review"
            description={ref ? "These two runs have no report in common." : "This run has no answer to review."}
          />
        ) : (
          <>
            <ReportHead
              index={entryIndex + 1}
              count={entries.length}
              title={entries[entryIndex]?.label ?? ""}
              runName={names.name(under)}
              comparatorName={comparatorRun ? names.name(comparatorRun) : null}
              cause={ui.cause ? t_i18n(causeLabel(ui.cause)) : null}
            />
            <ReportStrip
              tiles={tiles}
              current={entryKey}
              comparatorName={comparatorRun ? names.name(comparatorRun) : null}
              onPick={(key) => {
                ui.setJudgeOpen(false);
                setParam({ entry: key });
              }}
            />
            {reviewQ.isError ? (
              <Alert
                severity="error"
                title={t_i18n("This review cannot be shown")}
                description={reviewQ.error instanceof Error ? reviewQ.error.message : t_i18n("The review could not be loaded.")}
              />
            ) : !review || !pair || !options || reviewQ.isLoading ? (
              <PageSkeleton cards={2} />
            ) : (
              <EntryReview review={review} pair={pair} ui={ui} view={view} onView={setView} options={options} />
            )}
          </>
        )}
      </section>
    </div>
  );
}

export default function EvaluationBundleReview() {
  const [params] = useSearchParams();
  const runId = params.get("run");
  return runId ? <RunReviewPage runId={runId} /> : <AnswersReviewPage />;
}
