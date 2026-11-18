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

// The STIX bundle review (#12282): one dataset entry of a STIX_BUNDLE evaluation, its
// expected bundle against one to four answers (`?results=id1,id2`), object by object, in
// five views: coverage, annotate, diff, graph and knowledge graph. With one answer it is
// checked against the expected bundle; with two or more, the second is compared with the
// first, each judged against the expected bundle.

import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Icon, Tabs, TabsContent, TabsList, TabsTrigger } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { PageSkeleton } from "@/components/ui/skeleton";
import { AnnotateView, type AnnotateLens } from "@/components/evaluation/bundle-review/AnnotateView";
import { CoverageView } from "@/components/evaluation/bundle-review/CoverageView";
import { DiffView, type DiffLayout } from "@/components/evaluation/bundle-review/DiffView";
import { GraphView } from "@/components/evaluation/bundle-review/GraphView";
import { KnowledgeGraphView, type GraphSide } from "@/components/evaluation/bundle-review/KnowledgeGraphView";
import { ReviewInspector } from "@/components/evaluation/bundle-review/ReviewInspector";
import { ReviewRail } from "@/components/evaluation/bundle-review/ReviewRail";
import { AnswerCards, PairCounts, PairPicker } from "@/components/evaluation/bundle-review/ReviewSummary";
import type { ReviewContext, ReviewSelection } from "@/components/evaluation/bundle-review/context";
import { useBundleReview } from "@/hooks/api/useEvaluationApi";
import { useFormatter } from "@/i18n";
import {
  EXPECTED,
  answerName,
  FILTER_KEYS,
  buildModel,
  derive,
  forceLayout,
  locateMentions,
  modeOf,
  type ReviewPair,
} from "@/lib/evaluation-bundle-review";
import { formatModelLabel } from "@/lib/utils";
import type { BundleReview } from "@/types";

/** The most answers one review lays side by side (the backend's limit). */
const MAX_ANSWERS = 4;
const LETTERS = ["A", "B", "C", "D"];

type ReviewTab = "coverage" | "annotate" | "diff" | "graph" | "kg";
/** The view tabs, read by `t_i18n`. @i18n-keys */
const TAB_LABELS = {
  coverage: "Coverage",
  annotate: "Annotate",
  diff: "Diff",
  graph: "Graph",
  kg: "Knowledge graph",
} as const satisfies Record<ReviewTab, string>;

const ReviewGlyph = ({ className }: { className?: string }) => <Icon name="file-search" size={24} className={className} />;

/** One answer: checked against the expected bundle; two or more: the second against the first. */
function defaultPair(review: BundleReview): ReviewPair {
  const [first, second] = review.answers;
  return second ? { ref: first.key, under: second.key } : { ref: EXPECTED, under: first.key };
}

function validPair(review: BundleReview, pair: ReviewPair | null): pair is ReviewPair {
  const keys = new Set(review.answers.map((a) => a.key));
  return !!pair && keys.has(pair.under) && (pair.ref === EXPECTED || pair.ref === "none" || keys.has(pair.ref)) && pair.ref !== pair.under;
}

function Review({ review }: { review: BundleReview }) {
  const { t_i18n } = useFormatter();
  const [chosenPair, setPair] = useState<ReviewPair | null>(null);
  const pair = useMemo(() => (validPair(review, chosenPair) ? chosenPair : defaultPair(review)), [review, chosenPair]);
  const mode = modeOf(pair);
  const [whatIf, setWhatIf] = useState(false);
  const [tab, setTab] = useState<ReviewTab>("coverage");
  const [type, setType] = useState<string | null>(null);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [selection, setSelection] = useState<ReviewSelection | null>(null);
  const [diffLayout, setDiffLayout] = useState<DiffLayout>("split");
  const [hideMatched, setHideMatched] = useState(false);
  const [lens, setLens] = useState<AnnotateLens>("both");
  const [kgSide, setKgSide] = useState<GraphSide>("both");

  const model = useMemo(() => buildModel(review), [review]);
  const located = useMemo(() => locateMentions(model, review.entry.pages), [model, review.entry.pages]);
  // The force layout is quadratic per iteration: computed when the knowledge graph opens,
  // once per entry, never on load (a large bundle would block every other tab).
  const layout = tab === "kg" ? layoutOf(model) : null;
  const derived = useMemo(() => derive(model, pair, whatIf), [model, pair, whatIf]);
  const filters = useMemo(() => ({ type, hidden }), [type, hidden]);

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
  const ctx: ReviewContext = {
    review,
    model,
    pair,
    mode,
    whatIf,
    derived,
    filters,
    located,
    selection,
    select: setSelection,
    letter,
    sourceName,
    shown: [...new Set([EXPECTED, ...(mode === "runs" ? [pair.ref, pair.under] : [pair.under])])],
  };

  const choosePair = (next: ReviewPair) => {
    // Each mode has its own statuses: a filter of the old one means nothing in the new one.
    if (modeOf(next) !== mode) setHidden(new Set());
    setPair(next);
  };
  const toggleStatus = (key: string) => {
    const next = new Set(hidden);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    // Hiding every status would leave every view empty: keep the last one shown.
    if (next.size === FILTER_KEYS[mode].length) return;
    setHidden(next);
  };
  const unreadable = [...new Set(mode === "runs" ? [pair.ref, pair.under] : [pair.under])].filter((k) => review.unreadable.includes(k));

  return (
    <div className="space-y-4">
      <AnswerCards ctx={ctx} />
      <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft">
        <CardContent className="space-y-3 p-4">
          <PairPicker ctx={ctx} onPair={choosePair} onWhatIf={setWhatIf} />
          <p className="content-compact">
            {mode === "score"
              ? t_i18n("{answer} checked against the expected bundle.", { answer: `${letter(pair.under)} · ${sourceName(pair.under)}` })
              : mode === "alone"
                ? t_i18n("{answer} alone: what it found, checked against the expected bundle.", { answer: `${letter(pair.under)} · ${sourceName(pair.under)}` })
                : t_i18n("What changed from {a} to {b}, each judged against the expected bundle.", { a: letter(pair.ref), b: letter(pair.under) })}
          </p>
          <PairCounts ctx={ctx} />
        </CardContent>
      </Card>
      {unreadable.map((k) => (
        <Alert
          key={k}
          severity="warning"
          title={t_i18n("Answer {letter} is not a readable STIX bundle", { letter: letter(k) })}
          description={t_i18n("The scorer could not read it as a bundle, so every expected object counts as missing.")}
        />
      ))}
      <div className="grid gap-4 xl:grid-cols-[13rem_minmax(0,1fr)_20rem]">
        <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft self-start">
          <CardContent className="p-3">
            <ReviewRail
              ctx={ctx}
              onType={setType}
              onToggle={toggleStatus}
              onClear={() => {
                setType(null);
                setHidden(new Set());
              }}
            />
          </CardContent>
        </Card>
        <Card className="layer-1 bg-elevation-default border-elevation-subtle-soft min-w-0">
          <CardContent className="p-3">
            <Tabs value={tab} onValueChange={(v) => setTab(v as ReviewTab)}>
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
                <AnnotateView ctx={ctx} lens={lens} onLens={setLens} />
              </TabsContent>
              <TabsContent value="diff" className="pt-3">
                <DiffView ctx={ctx} layout={diffLayout} onLayout={setDiffLayout} hideMatched={hideMatched} onHideMatched={setHideMatched} />
              </TabsContent>
              <TabsContent value="graph" className="pt-3">
                <GraphView ctx={ctx} />
              </TabsContent>
              <TabsContent value="kg" className="pt-3">
                {layout && <KnowledgeGraphView ctx={ctx} layout={layout} side={kgSide} onSide={setKgSide} />}
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

export default function EvaluationBundleReview() {
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
        <Review review={data} />
      ) : null}
    </div>
  );
}
