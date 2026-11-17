// Overall Quality's coverage: the "computed over N of M entries" line shows only
// when the headline leaves entries out, and a reason code reads as a sentence. Why Overall
// Performance reads N/A, from the run's own disclosure.

import { describe, expect, it } from "vitest";

import { METRIC_KEY_LABELS, metricKeyLabel } from "@/lib/constants";
import type { MetricExclusion, QualityCoverage } from "@/types";
import { RUN_LEVEL_EXCLUSION_KEY } from "@/types";

import {
  aggregateNaReason,
  HEADLINE_NOTES,
  headlines,
  metricReasonLabel,
  naReason,
  partialCoverage,
  performanceHeadline,
  judgeNaNote,
  legacyOverallNaNote,
  runJudged,
} from "./evaluation-headlines";

describe("partialCoverage", () => {
  it.each<{ coverage: QualityCoverage | null | undefined; partial: boolean }>([
    { coverage: { entries: 2, counted: 1, left_out: { reference_empty: 1 } }, partial: true },
    { coverage: { entries: 2, counted: 0, left_out: { not_measured: 2 } }, partial: true },
    { coverage: { entries: 2, counted: 2, left_out: {} }, partial: false },
    { coverage: null, partial: false },
    { coverage: undefined, partial: false },
  ])("is set only when entries are left out ($partial)", ({ coverage, partial }) => {
    expect(partialCoverage({ quality_coverage: coverage }) !== null).toBe(partial);
  });
});

describe("what a headline is", () => {
  const run = { id: "run-1", score: 80, quality_score: 70, performance_score: 60 };

  it("gives each headline its own definition, Overall Performance's being its comparative scope", () => {
    expect(headlines(run).map((h) => [h.id, h.note])).toEqual([
      ["judge", HEADLINE_NOTES.judge],
      ["quality", HEADLINE_NOTES.quality],
      ["performance", HEADLINE_NOTES.performance],
    ]);
    expect(HEADLINE_NOTES.performance).toMatch(/Comparative within this evaluation/);
  });

  it("says a legacy speed score is one, never that it is comparative", () => {
    const legacy = performanceHeadline({ score: null, quality_score: null, performance_score: null, speed_score: 40 });
    expect(legacy.note).toBe(HEADLINE_NOTES.legacySpeed);
  });

  it("names every failed verdict when there were some, and nothing gradeable otherwise", () => {
    expect(judgeNaNote({ judge_failure_count: 1 })).toEqual([HEADLINE_NOTES.allVerdictsFailed, { count: 1 }]);
    expect(judgeNaNote({ judge_failure_count: 0 })).toEqual([HEADLINE_NOTES.noGradeable]);
    expect(judgeNaNote({})).toEqual([HEADLINE_NOTES.noGradeable]);
  });

  it("says the judge was off for a run whose own scorer selection left it out, failed verdicts or not", () => {
    expect(judgeNaNote({ scorer_selection: [], judge_failure_count: 2 })).toEqual(["The LLM judge was not enabled for this run."]);
    expect(runJudged({ scorer_selection: null })).toBe(true);
    expect(runJudged({ scorer_selection: ["judge"] })).toBe(true);
    expect(runJudged({ scorer_selection: [] })).toBe(false);
  });
});

describe("metricReasonLabel", () => {
  it("names a reason code, and nothing else", () => {
    expect(metricReasonLabel("reference_empty")).toBe("The expected answer is empty.");
    expect(metricReasonLabel("No answer shape declared.")).toBeNull();
    expect(metricReasonLabel("constructor")).toBeNull();
  });
});

describe("naReason for Overall Performance", () => {
  const RUN = "run-1";
  const runLevel = (reason: string, run_ids: string[] = []): MetricExclusion => ({
    key: RUN_LEVEL_EXCLUSION_KEY,
    reason,
    run_ids,
  });
  const reasonFor = (excluded: MetricExclusion[]) =>
    naReason("performance", "completed", {
      id: RUN,
      score: null,
      quality_score: null,
      performance_aggregation_excluded: excluded,
    });

  it("names an explicitly empty selection, not a missing shared metric", () => {
    expect(reasonFor([runLevel("no_metric_selected")])).toMatch(/No performance metric is selected/);
  });

  it("names an empty selection before this run's own abstention", () => {
    expect(reasonFor([runLevel("no_performance_data", [RUN]), runLevel("no_metric_selected")])).toMatch(
      /No performance metric is selected/,
    );
  });

  it("names a shared metric that only counts when selected", () => {
    expect(reasonFor([runLevel("only_opt_in_metrics_shared", [RUN])])).toMatch(/are not counted by default, and none is selected/);
  });

  it("says a placebo comparison's run is never ranked, whatever else it discloses", () => {
    const comparison = { id: RUN, score: null, quality_score: null, placebo_comparison_id: "cmp-1", performance_aggregation_excluded: [] };
    expect(naReason("performance", "completed", comparison)).toMatch(/placebo comparison's runs get no Overall Performance/);
  });

  it("keeps the no-shared-metric sentence for runs that share nothing", () => {
    expect(reasonFor([])).toMatch(/No performance metric was measured on every run/);
  });
});

describe("legacyOverallNaNote", () => {
  it("blames the judge only when its verdicts failed, else says the blended score is retired", () => {
    expect(legacyOverallNaNote({ score: null, judge_failure_count: 2 })).toEqual([HEADLINE_NOTES.allVerdictsFailed, { count: 2 }]);
    expect(legacyOverallNaNote({ score: null, judge_failure_count: 0, scorer_selection: [] })).toEqual([HEADLINE_NOTES.legacyOverallNotComputed]);
    expect(legacyOverallNaNote({ score: 80, judge_failure_count: 0 })).toEqual([HEADLINE_NOTES.legacyOverallNotComputed]);
  });
});

describe("naReason for Overall Quality", () => {
  it("names an empty selection as a cause, since the run cannot tell it apart from metrics that scored nothing", () => {
    expect(naReason("quality", "completed")).toMatch(/or none is selected under Overall Score Composition/);
  });
});

describe("aggregateNaReason", () => {
  const run = (id: string, excluded: MetricExclusion[]) => ({
    id,
    score: null,
    quality_score: null,
    performance_aggregation_excluded: excluded,
  });

  it("names an empty selection on the evaluation's own tiles", () => {
    const runs = [run("a", [{ key: RUN_LEVEL_EXCLUSION_KEY, reason: "no_metric_selected", run_ids: [] }])];
    expect(aggregateNaReason("performance", runs)).toMatch(/No performance metric is selected/);
  });

  it("says the judge was off only when it was off for every run", () => {
    const off = { id: "a", score: null, quality_score: null, scorer_selection: [] as string[] };
    const on = { id: "b", score: null, quality_score: null, scorer_selection: ["judge"] };
    expect(aggregateNaReason("judge", [off])).toBe("The LLM judge was not enabled for these runs.");
    expect(aggregateNaReason("judge", [off, on])).toMatch(/No latest run has a Judge Quality/);
  });

  it("says too few runs, not that nothing was measured, when the evaluation cannot rank yet", () => {
    const runs = [run("a", [{ key: RUN_LEVEL_EXCLUSION_KEY, reason: "too_few_comparable_runs", run_ids: [] }])];
    expect(aggregateNaReason("performance", runs)).toMatch(/Too few comparable runs/);
  });

  it("names no single run's abstention", () => {
    const runs = [run("a", [{ key: RUN_LEVEL_EXCLUSION_KEY, reason: "no_performance_data", run_ids: ["a"] }])];
    expect(aggregateNaReason("performance", runs)).toMatch(/No performance metric was measured on every run/);
  });
});

describe("metricKeyLabel", () => {
  it("names a performance metric by the one table both the selector and the run page read", () => {
    expect(metricKeyLabel("performance.latency.wall_clock")).toBe(METRIC_KEY_LABELS["performance.latency.wall_clock"]);
  });

  it("names nothing for an unknown key or an inherited property", () => {
    expect(metricKeyLabel("json.not_a_metric")).toBeNull();
    expect(metricKeyLabel("constructor")).toBeNull();
  });
});
