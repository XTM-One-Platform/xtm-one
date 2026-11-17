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

import React, { Fragment, useState, useRef, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import { useColorMode } from "@/hooks/useColorMode";
import {
  useEvaluation,
  useEvaluationAvailableModels,
  useEvaluationChart,
  useEvaluationMetricCatalog,
  useEvaluationRuns,
  useEvaluationRunsCount,
  useTriggerEvaluation,
  useTriggerEvaluationFromRuns,
  useRescoreRunsFromRuns,
  useDeleteEvaluation,
  useUpdateEvaluation,
  useAddEvaluationAgents,
  useRemoveEvaluationAgent,
  useSetAgentBaseline,
  useSetAgentTags,
  useEvaluationFamilies,
  useCancelEvaluationRun,
  useDeleteEvaluationRun,
  useDeleteAllEvaluationRuns,
  exportEvaluation,
} from "@/hooks/api/useEvaluationApi";
import {
  useDatasets,
  useEvaluationDatasets,
  useLinkDataset,
  useUnlinkDataset,
} from "@/hooks/api/useDatasetApi";
import { useAuth } from "@/hooks/useAuth";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { NotFoundState } from "@/components/ui/not-found-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { RunVersionChips } from "@/components/evaluation/run-version-chips";
import { RunMenuModelRow } from "@/components/evaluation/unavailable-model";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { HeldBadge } from "@/components/ui/held-badge";
import { AutomaticRunsPausedBadge } from "@/components/evaluation/automatic-runs-badge";
import { configSavePayload } from "@/components/evaluation/config-save-payload";
import { EvaluationExtractorCard } from "@/components/evaluation/evaluation-extractor-card";
import { OverallScoreComposition } from "@/components/evaluation/metric-selector";
import { MetricName } from "@/components/evaluation/metric-name";
import { EvaluationAgentPickerCard } from "@/components/evaluation/evaluation-agent-picker-card";
import { EvaluationModelPickerCard, ModelActiveLabel } from "@/components/evaluation/evaluation-model-picker-card";
import { EvaluationSettingsCard } from "@/components/evaluation/evaluation-settings-card";
import { RunModeBadge } from "@/components/evaluation/run-mode-badge";
import { PromptGuardBadge } from "@/components/evaluation/prompt-guard-badge";
import { PlatformErrorBadge } from "@/components/evaluation/platform-error-badge";
import { ToolApprovalBadge } from "@/components/evaluation/tool-approval-badge";
import { evaluationExecutionMode } from "@/components/evaluation/run-mode";
import { EvaluationScoringCard } from "@/components/evaluation/evaluation-scoring-card";
import { PlaceboArmTag } from "@/components/evaluation/PlaceboArmTag";
import { PlaceboComparisonsCard } from "@/components/evaluation/PlaceboComparisonsCard";
import { PlaceboConfigCard } from "@/components/evaluation/PlaceboConfigCard";
import { PlaceboRunDialog } from "@/components/evaluation/PlaceboRunDialog";
import { useTriggerFailedToast } from "@/components/evaluation/use-trigger-failed-toast";
import { isComparisonRun } from "@/lib/placebo";
import { EVALUATION_TERMS, FIELD_MODE_DESCRIPTIONS, FIELD_MODE_LABELS, fieldModeCopy, triggerNote } from "@/lib/evaluation-glossary";
import { familyLabel } from "@/lib/evaluation-families";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SharedBadge } from "@/components/ui/shared-badge";
import { Switch } from "@/components/ui/switch";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useDebounce } from "@/hooks/useDebounce";
import {
  FlaskConical,
  BarChart3,
  Database,
  Settings2,
  Trash2,
  Play,
  ChevronDown,
  Bot,
  Activity,
  Pencil,
  X,
  Check,
  Plus,
  Rows3,
  ExternalLink,
  Download,
  Repeat,
  AlertTriangle,
  Gavel,
  Star,
  Copy,
  Tag,
  Trophy,
  History,
  Zap,
} from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { HEADLINE_LABELS, HEADLINE_NOTES, aggregateNaReason, isLegacyRun, judgeHeadline, judgeNaNote, legacyOverallNaNote, naReason, performanceHeadline, qualityHeadline } from "@/lib/evaluation-headlines";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { NeedsFamilyBadge } from "@/components/ui/needs-family-badge";
import { headlineTip } from "@/components/evaluation/headline-tip";
import { cn, formatShortDate, formatModelLabel, formatShortDateTime } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import { evaluationSubjectKey, evaluationSubjectPath } from "@/lib/evaluation-subject";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { canManageEntity } from "@/lib/entity-permissions";
import { useFormatter } from "@/i18n";
import {
  EVALUATION_STATUS_CONFIG,
  EVALUATION_TRIGGER_LABELS,
  REFETCH_INTERVAL_FAST,
  ICON_MAP,
  PROVIDER_DISPLAY_NAMES,
  RUN_TAG_BASELINE_KEY,
  REUSE_BADGE_CONFIG,
  COPY_FEEDBACK_DELAY,
  HEADLINES,
  CHART_RANGES,
  DEFAULT_CHART_RANGE,
  headlineColorClass,
  headlineDef,
  seriesEncoding,
} from "@/lib/constants";
import type { HeadlineDef, SeriesEncoding } from "@/lib/constants";
import type { CorrelationKey, EvaluationFamily, EvaluationPlaceboConfig, ExtractorSpec, FamilyInfo, FieldScoringOverrides, HeadlineTrendKey, LinkedDataset } from "@/types";
import { FIELD_SCORING_MODES, type FieldScoringMode } from "@/types";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ScatterChart,
  Scatter,
  Label,
} from "recharts";
import { formatTimeAgo } from "@/lib/relative-time";
type Tab = "overview" | "activity" | "dataset" | "config";

// ---------------------------------------------------------------------------
// Per-field scoring overrides
// ---------------------------------------------------------------------------

/** One editable row: a JSON Pointer plus the treatment and/or weight declared for it. */
interface FieldOverrideRow {
  pointer: string;
  /** A `FieldScoringMode`, or `"derived"` for "let the schema decide" — which is the
   *  absence of an override, not a mode the backend accepts. */
  mode: FieldScoringMode | "derived";
  weight: string;
}

/**
 * Flatten the two stored maps into rows keyed by pointer.
 *
 * Stored as `{field_modes, field_weights}`, the scorer's policy shape, but an author
 * edits a field. Reading the two together also exposes the one configuration the server
 * refuses: a weight on an excluded field.
 */
function overridesToRows(overrides: FieldScoringOverrides | null | undefined): FieldOverrideRow[] {
  const modes = overrides?.field_modes ?? {};
  const weights = overrides?.field_weights ?? {};
  const pointers = Array.from(new Set([...Object.keys(modes), ...Object.keys(weights)])).sort();
  return pointers.map((pointer) => ({
    pointer,
    mode: modes[pointer] ?? "derived",
    weight: weights[pointer] != null ? String(weights[pointer]) : "",
  }));
}

/**
 * Fold rows back into the stored shape, dropping anything that says nothing.
 *
 * A row with no pointer, a "derived" mode and a blank weight is omitted: an unset key
 * means "derive from the schema".
 */
function rowsToOverrides(rows: FieldOverrideRow[]): FieldScoringOverrides | null {
  const modes: Record<string, FieldScoringMode> = {};
  const weights: Record<string, number> = {};
  for (const row of rows) {
    const pointer = row.pointer.trim();
    if (!pointer) continue;
    if (row.mode && row.mode !== "derived") modes[pointer] = row.mode;
    const weight = Number(row.weight);
    if (row.weight.trim() !== "" && Number.isFinite(weight)) weights[pointer] = weight;
  }
  const hasModes = Object.keys(modes).length > 0;
  const hasWeights = Object.keys(weights).length > 0;
  if (!hasModes && !hasWeights) return null;
  return {
    field_modes: hasModes ? modes : null,
    field_weights: hasWeights ? weights : null,
  };
}


// ---------------------------------------------------------------------------
// Overview Tab
// ---------------------------------------------------------------------------

/** The point shapes Recharts draws, as small inline legend swatches. */
const LEGEND_SHAPE_PATH: Record<string, string> = {
  circle: "M6 2.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z",
  square: "M2.8 2.8h6.4v6.4H2.8z",
  triangle: "M6 2 10 9.6H2z",
  diamond: "M6 1.8 10.2 6 6 10.2 1.8 6z",
  star: "M6 1.6 7.3 4.7l3.3.2-2.6 2.1.9 3.2L6 8.5 3.1 10.2l.9-3.2-2.6-2.1 3.3-.2z",
  cross: "M5 1.8H7V5H10.2V7H7V10.2H5V7H1.8V5H5Z",
  wye: "M5.3 6.2H6.7V10.2H5.3Z M5.3 6.8L6 6.1L2.6 1.9L1.9 2.6Z M6.7 6.8L6 6.1L9.4 1.9L10.1 2.6Z",
};

/**
 * A series key is `${agentLabel}/${model_id}`, built identically in the trend payload
 * and the quadrant. The legend reads it back as the agent/model pair, with the same
 * `formatModelLabel` treatment as the Leader card and Top 10 table.
 *
 * `parts` is the split taken from the run that produced the key, the only exact
 * answer: model ids routinely contain the separator (`anthropic/claude-sonnet-5`),
 * so no split of the string is correct in general.
 *
 * The fallback covers keys whose run is outside the loaded page and splits on the
 * first separator: wrong only for a slash-bearing agent name, where splitting on the
 * last is wrong for every OpenRouter-style model.
 */
function seriesLabel(key: string, t_i18n: (id: string) => string, parts?: { agent: string; model: string }): string {
  if (parts) return `${parts.agent} · ${formatModelLabel(parts.model, t_i18n)}`;
  const idx = key.indexOf("/");
  if (idx < 0) return key;
  return `${key.slice(0, idx)} · ${formatModelLabel(key.slice(idx + 1), t_i18n)}`;
}

/**
 * Chart legend rendering the same combined encoding the plot uses: colour,
 * dash pattern and point shape. When there are more series than distinct encodings,
 * the repetition is stated rather than shown as a unique identity.
 *
 * `encodingFor` is passed in, never derived here: a trend chart's keys are
 * only the pairs with a value on that headline in the selected range, so indexing
 * its own `keys` would disagree with the plot.
 */
function SeriesLegend({
  keys,
  mode,
  encodingFor,
  labelFor,
}: {
  keys: string[];
  mode: "line" | "scatter";
  encodingFor: (key: string) => SeriesEncoding;
  labelFor: (key: string) => string;
}) {
  const { t_i18n } = useFormatter();
  const reused = keys.filter((key) => encodingFor(key).encodingReused).length;
  return (
    <div className="mt-3 pt-2 border-t border-elevation-subtle-soft space-y-1">
      <div className="flex flex-wrap gap-2">
        {keys.map((key) => {
          const enc = encodingFor(key);
          return (
            <div key={key} className="flex items-center gap-1">
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                {mode === "line" ? (
                  <line x1="0" y1="6" x2="12" y2="6" stroke={enc.color} strokeWidth="2" strokeDasharray={enc.dash || undefined} />
                ) : (
                  <path d={LEGEND_SHAPE_PATH[enc.shape]} fill={enc.color} />
                )}
              </svg>
              <span className="content-caption text-default-secondary">{labelFor(key)}</span>
            </div>
          );
        })}
      </div>
      {reused > 0 && (
        <p className="content-caption text-default-secondary">
          {t_i18n("{count, plural, one {# series reuses} other {# series reuse}} an earlier line style: read them by colour and label", {
            count: reused,
          })}
        </p>
      )}
    </div>
  );
}

/**
 * The evaluation page's own explanations outside its Configuration tab, read by
 * `t_i18n`. The run menu's sentences are the evaluation list's, word for word. @i18n-keys
 */
const PAGE_NOTES = {
  leader:
    "The best value on this score among each agent and model's latest scored run, from the evaluation's 100 most recent runs; placebo comparison runs are left out. It ignores the chart range.",
  top10:
    "The ten best agent and model pairs on this score, each by its latest scored run among the evaluation's 100 most recent runs; placebo comparison runs are left out. It ignores the chart range.",
  chartRange:
    "Applies to the three trend charts together. The correlation charts always use each pair's latest run, and the Leader and Top 10 cards ignore the range.",
  shownRuns: "The completed runs launched in the selected range, out of all completed runs; placebo comparison runs are left out.",
  runOptions: "Choose the models to run on, or run a placebo comparison.",
  runInProgress: "A launch of this evaluation is still running: wait for it to finish, or cancel it in the Activity tab.",
  familyMissing: "Set an output family in the Configuration tab before running this evaluation.",
  export:
    "Downloads a ZIP bundle with the evaluation's settings, its agents, its datasets and its runs with their results, to import elsewhere. Placebo comparison runs are left out.",
  selectAll:
    "Selects these completed runs: only a completed run can be selected. Placebo comparison runs can be compared, but Re-run full and Re-score skip them.",
  linkDataset:
    "Lists the datasets of this evaluation's output family and those without one. A dataset of another family cannot be linked.",
} as const;

/** How the Activity tab lists runs together, read by `t_i18n`. @i18n-keys */
const BATCH_GROUPING_NOTE = {
  grouping: "Runs started in the same minute for the same reason are listed together.",
} as const;

/** What an Activity card's status says about its runs, by the status shown, read by `t_i18n`. @i18n-keys */
const BATCH_STATUS_NOTES = {
  running: "At least one of these runs is still pending or running.",
  completed: "Every one of these runs completed.",
  failed: "None of these runs is still running, and at least one failed.",
  cancelled: "None of these runs is still running, and at least one was cancelled.",
} as const;

/** Why the runs of an Activity card started, then how the card groups them. */
function batchTriggerNote(reason: string, t_i18n: (id: string) => string): string {
  return `${t_i18n(triggerNote(reason))} ${t_i18n(BATCH_GROUPING_NOTE.grouping)}`;
}

export function OverviewTab({ evaluationId }: { evaluationId: string }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  // Kept on TanStack Query (range is part of the query key) rather than local
  // state, so each window caches separately instead of evicting the last.
  const [chartRange, setChartRange] = useState(DEFAULT_CHART_RANGE);
  const { data: chartData } = useEvaluationChart(evaluationId, chartRange);
  // A placebo comparison's runs rank no configuration (FR-28), and its
  // trials would fill the page: the server leaves them out.
  const { data: latestRuns = [] } = useEvaluationRuns(evaluationId, 100, 0, { excludeComparisons: true });

  // Theme-aware chart colors: Recharts stroke/fill are SVG presentation
  // attributes that cannot resolve CSS `var()` tokens, so concrete per-mode
  // values are derived (the tooltip's style does resolve var tokens).
  const isDark = useColorMode() === "dark";
  const gridStroke = isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.08)";
  const axisStroke = isDark ? "rgba(255,255,255,0.3)" : "rgba(0,0,0,0.35)";
  const tickFill = isDark ? "rgba(255,255,255,0.5)" : "rgba(0,0,0,0.55)";
  // Axis titles: one step firmer than the ticks, and not tinted with the
  // headline colour, which on a scatter already means "which series".
  const axisTitleFill = isDark ? "rgba(255,255,255,0.7)" : "rgba(0,0,0,0.7)";
  const axisTitleStyle = { fontSize: 11, fontWeight: 500, fill: axisTitleFill, textAnchor: "middle" as const };
  const cursorStroke = isDark ? "rgba(255,255,255,0.2)" : "rgba(0,0,0,0.2)";
  const activeDotStroke = isDark ? "rgba(255,255,255,0.3)" : "rgba(0,0,0,0.25)";

  // One transform per headline trend, all from the same windowed payload, so the
  // three are never read over different periods.
  //
  // `idx` (chronological position) is the axis key, not `timestamp`: an ordinal
  // axis spaces points evenly, and `idx` is unique per point, so same-day runs do
  // not collide on one tick. `timestamp` stays on each row for labels.
  const trendFor = (id: HeadlineTrendKey) => {
    const points = chartData?.trends?.[id] ?? [];
    const data = points
      .map((point) => {
        const base: Record<string, string | number> = {
          timestamp: new Date(point.triggered_at).getTime(),
        };
        Object.entries(point.scores).forEach(([key, value]) => { base[key] = value; });
        return base;
      })
      .sort((a, b) => (a.timestamp as number) - (b.timestamp as number))
      .map((row, idx) => ({ ...row, idx })) as (Record<string, string | number> & { idx: number })[];
    const keys = Array.from(new Set(points.flatMap((p) => Object.keys(p.scores))));
    return { data, keys };
  };

  // Every series key across all six charts, so one agent/model pair keeps the
  // same visual identity everywhere.
  const allSeriesKeys = Array.from(
    new Set([
      ...Object.values(chartData?.trends ?? {}).flatMap((pts) =>
        pts.flatMap((p) => Object.keys(p.scores)),
      ),
      ...Object.values(chartData?.correlations ?? {}).flatMap((pts) =>
        pts.map((p) => p.series),
      ),
    ]),
  );

  // Model-independent (skip_llm) runs are included: these surfaces rank
  // an agent, and an agent that runs without an LLM is still an agent.
  //
  // A run qualifies on any headline, so a judge-off run with measured
  // quality metrics still appears.
  const completedRuns = latestRuns.filter(
    (r) =>
      r.status === "completed" &&
      (r.score !== null
        || (r.quality_score ?? null) !== null
        || (r.performance_score ?? null) !== null),
  );
  // Headline values stay nullable: `?? 0` would rank an unmeasured headline as a
  // measured zero. Every consumer below excludes `null`.
  const runsForQuadrant = completedRuns.map((r) => {
    const agentLabel = r.agent_name || r.agent_slug || evaluationSubjectKey(r);
    return {
      runId: r.id,
      key: `${evaluationSubjectKey(r)}/${r.model_id}`,
      name: `${agentLabel}/${r.model_id}`,
      agent: agentLabel,
      model: r.model_id,
      score: r.score ?? null,
      quality: r.quality_score ?? null,
      // Overall Performance via the shared resolver, which also supplies the
      // legacy `speed_score` fallback for older runs; a run shows one or the
      // other, never both.
      performance: performanceHeadline(r),
      isBaseline: !!r.is_baseline,
      // What the run recorded: a persona-only score ranked among agents must say so.
      execution_mode: r.execution_mode,
      fallback_entries: r.fallback_entries,
      // And whether the prompt guard checked it.
      apply_prompt_guards: r.apply_prompt_guards,
      // And whether it held tool calls needing approval.
      require_tool_approval: r.require_tool_approval,
      // And how many entries the platform could not run, left out of its scores.
      platform_error_count: r.platform_error_count,
    };
  });
  // Dedupe: keep LATEST (first in list, since runs are sorted newest-first)
  const durationMap = new Map<string, typeof runsForQuadrant[0]>();
  for (const d of runsForQuadrant) { if (!durationMap.has(d.key)) durationMap.set(d.key, d); }
  const quadrantDeduped = Array.from(durationMap.values());

  // One visual identity per series, resolved from its position in ONE ordered list:
  // the deduped runs first (they appear in the leaders and top 10), then any chart
  // key not yet covered, so a pair reads the same across all six charts.
  const seriesOrder = Array.from(new Set([...quadrantDeduped.map((d) => d.name), ...allSeriesKeys]));
  const encodingFor = (key: string) => {
    const idx = seriesOrder.indexOf(key);
    return seriesEncoding(idx >= 0 ? idx : 0);
  };

  // The agent/model split, taken from the run that produced the key rather
  // than parsed back out of it. `latestRuns` is one page, so a key
  // from an older run falls back to `seriesLabel`'s split.
  const seriesParts = new Map<string, { agent: string; model: string }>();
  for (const r of latestRuns) {
    const agent = r.agent_name || r.agent_slug || evaluationSubjectKey(r);
    seriesParts.set(`${agent}/${r.model_id}`, { agent, model: r.model_id });
  }
  const labelFor = (key: string) => seriesLabel(key, t_i18n, seriesParts.get(key));

  // Attach each run's series colour.
  const quadrantUnique = quadrantDeduped.map((d) => ({
    ...d,
    color: encodingFor(d.name).color,
  }));

  // One leader per headline, none outranking the others, and no overall winner:
  // the three measurements are decoupled.
  //
  // A run with no value on a headline is excluded from that headline's selection,
  // not ranked at zero, and a tie shows joint leaders: an arbitrary pick would be
  // indistinguishable from a real result.
  const valueOn = (d: typeof quadrantUnique[0], id: HeadlineTrendKey): number | null =>
    id === "quality" ? d.quality
      // The legacy `speed_score` fallback is display-only. A run with a null
      // `performance_score` is excluded from ranking, leader selection and the
      // correlation views: the two values are on different scales.
      : id === "performance" ? (d.performance.legacy ? null : d.performance.value)
      : d.score;

  const leaders = HEADLINES.map((h) => {
    const pool = quadrantUnique.filter((d) => valueOn(d, h.id) !== null);
    if (pool.length === 0) return { headline: h, tied: [] as typeof pool };
    const best = Math.max(...pool.map((d) => valueOn(d, h.id)!));
    return { headline: h, tied: pool.filter((d) => valueOn(d, h.id) === best) };
  });

  const measuredHeadlines = leaders.filter((l) => l.tied.length > 0);

  if (quadrantUnique.length === 0) {
    const ghostWidths = [85, 70, 62, 55, 46];
    return (
      <div className="space-y-6">
        <PlaceboComparisonsCard evaluationId={evaluationId} />
        <div className="flex flex-col items-center gap-2 py-6 text-center">
          <BarChart3 className="h-9 w-9 text-default-disabled mb-1" />
          <p className="content-base font-medium text-default-secondary">{t_i18n("No results yet")}</p>
          <p className="content-compact text-default-secondary">{t_i18n("Trigger an evaluation run to populate this overview")}</p>
        </div>
        <div className="animate-pulse space-y-4 pointer-events-none select-none opacity-40">
          {/* Best Performer ghost */}
          <div className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default overflow-hidden">
            <div className="flex items-stretch h-[88px]">
              <div className="w-16 shrink-0 bg-elevation-hover" />
              <div className="flex-1 px-5 py-4 space-y-2">
                <div className="h-2 w-20 rounded-full bg-elevation-hover" />
                <div className="h-4 w-36 rounded-full bg-elevation-hover" />
                <div className="h-2.5 w-28 rounded-full bg-elevation-hover" />
              </div>
              {[0, 1, 2].map((i) => (
                <div key={i} className="w-[120px] border-l border-elevation-subtle-soft flex flex-col items-center justify-center gap-1.5 py-4">
                  <div className="h-2 w-16 rounded-full bg-elevation-hover" />
                  <div className="h-5 w-10 rounded-full bg-elevation-hover" />
                </div>
              ))}
            </div>
          </div>
          {/* Top 10 tables ghost */}
          <div className="grid gap-4 lg:grid-cols-2">
            {[0, 1].map((tableIdx) => (
              <div key={tableIdx} className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default overflow-hidden">
                <div className="px-4 pt-4 pb-3">
                  <div className="h-3.5 w-28 rounded-full bg-elevation-hover" />
                </div>
                <div className="border-t border-elevation-subtle-soft">
                  {ghostWidths.map((w, i) => (
                    <div key={i} className="flex items-center gap-3 px-4 py-2.5 border-b border-elevation-subtle-soft last:border-0">
                      <div className="h-3 w-3 rounded-full bg-elevation-hover" />
                      <div className="flex-1 space-y-1.5">
                        <div className="h-2.5 rounded-full bg-elevation-hover" style={{ width: `${w}%` }} />
                        <div className="h-2 rounded-full bg-elevation-hover" style={{ width: `${w * 0.65}%` }} />
                      </div>
                      <div className="h-3 w-7 rounded-full bg-elevation-hover" />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {/* Score Evolution chart ghost */}
          <div className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default">
            <div className="px-4 pt-4 pb-3">
              <div className="h-3.5 w-36 rounded-full bg-elevation-hover" />
            </div>
            <div className="px-4 pb-4">
              <div className="h-[220px] rounded-sm bg-elevation-hover relative overflow-hidden">
                {[20, 40, 60, 80].map((pct) => (
                  <div key={pct} className="absolute w-full border-t border-elevation-subtle-soft" style={{ top: `${pct}%` }} />
                ))}
                <svg className="absolute inset-0 w-full h-full" viewBox="0 0 400 220" preserveAspectRatio="none">
                  <polyline points="0,160 80,120 160,145 240,88 320,108 400,68" fill="none" stroke={isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.08)"} strokeWidth="2" />
                  <polyline points="0,180 80,155 160,172 240,132 320,148 400,105" fill="none" stroke={isDark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.05)"} strokeWidth="2" />
                </svg>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PlaceboComparisonsCard evaluationId={evaluationId} />
      {/* One leader per headline, side by side and never ordered: there
          is only a best agent per measurement. */}
      {measuredHeadlines.length === 0 ? (
        /* One statement instead of nine empty surfaces, which would read as a
           rendering failure rather than a measurement outcome. */
        <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
          <CardContent className="p-6 text-center space-y-1.5">
            <BarChart3 className="h-8 w-8 mx-auto text-default-disabled" />
            <p className="content-base font-medium">{t_i18n("No overall score could be computed")}</p>
            <p className="content-compact text-default-secondary max-w-lg mx-auto">
              {t_i18n(
                "These runs completed, but none of the three overall scores has a value: the LLM judge did not score them, no selected quality metric produced a value, and Overall Performance needs at least two runs sharing a selected performance metric. Each run still shows its individual metrics.",
              )}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-3">
          {leaders.map(({ headline, tied }) => (
            <Card key={headline.id} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft overflow-hidden">
              <CardHeader className="pb-2">
                <CardTitle className="text-base font-medium flex items-center gap-1.5">
                  <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: headline.color }} />
                  {t_i18n(headline.label)}
                  <InfoTip content={t_i18n(HEADLINE_NOTES[headline.id])} label={t_i18n("What {score} is", { score: t_i18n(headline.label) })} />
                  {/* The ranked list below repeats this headline label; the
                      right-hand qualifier tells the two cards apart. */}
                  <InfoLabel content={t_i18n(PAGE_NOTES.leader)} className="ml-auto content-caption font-normal text-default-secondary uppercase tracking-wider">
                    {t_i18n("Leader")}
                  </InfoLabel>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {tied.length === 0 ? (
                  <p className="content-compact text-default-secondary italic py-2">
                    {/* Aggregate surface: no single run is the subject, so no run is
                        passed and the generic wording stands. */}
                    {t_i18n(aggregateNaReason(headline.id, latestRuns))}
                  </p>
                ) : (
                  <>
                    {/* Joint leaders on a tie — never one picked arbitrarily. */}
                    {tied.map((d) => (
                      <button
                        key={d.key}
                        onClick={() => navigate(`/evaluations/runs/${d.runId}`)}
                        className="w-full text-left flex items-center justify-between gap-2 rounded-sm px-2 py-1.5 hover:bg-elevation-hover transition-colors"
                      >
                        <div className="min-w-0 flex items-center gap-1.5">
                          <Trophy className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                          <div className="min-w-0">
                            <p className="content-base font-medium truncate">{d.agent}</p>
                            <p className="content-caption text-default-secondary truncate">{formatModelLabel(d.model, t_i18n)}</p>
                          </div>
                        </div>
                        <span className={cn("text-lg font-bold font-sans-geologica shrink-0", headlineColorClass(valueOn(d, headline.id)))}>
                          {valueOn(d, headline.id)}
                        </span>
                      </button>
                    ))}
                    {tied.length > 1 && (
                      <p className="content-caption text-default-secondary">
                        {t_i18n("{count} joint leaders: tied on this overall score", { count: tied.length })}
                      </p>
                    )}
                  </>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Three ranked lists + six charts. Each headline is ranked on its own;
          a run with no value on it is excluded, not sorted in at zero. */}
      {quadrantUnique.length > 0 && (() => {
        const rankBy = (id: HeadlineTrendKey) =>
          quadrantUnique
            .filter((d) => valueOn(d, id) !== null)
            .sort((a, b) => valueOn(b, id)! - valueOn(a, id)!)
            .slice(0, 10);
        const omittedFrom = (id: HeadlineTrendKey) =>
          quadrantUnique.filter((d) => valueOn(d, id) === null).length;

        const rankTable = (headline: HeadlineDef) => {
          const data = rankBy(headline.id);
          const omitted = omittedFrom(headline.id);
          return (
            <Card key={headline.id} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
              <CardHeader className="pb-2">
                <CardTitle className="text-base font-medium flex items-center gap-1.5">
                  <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: headline.color }} />
                  {t_i18n(headline.label)}
                  <InfoTip content={t_i18n(HEADLINE_NOTES[headline.id])} label={t_i18n("What {score} is", { score: t_i18n(headline.label) })} />
                  {/* Distinguishes this ranked list from the leader card above,
                      which carries the identical headline label. */}
                  <InfoLabel content={t_i18n(PAGE_NOTES.top10)} className="ml-auto content-caption font-normal text-default-secondary uppercase tracking-wider">
                    {t_i18n("Top 10")}
                  </InfoLabel>
                </CardTitle>
              </CardHeader>
              <CardContent>
                {/* An unmeasured headline renders the leader card's statement,
                    not an empty table that would read as a failed load. */}
                {data.length === 0 ? (
                  <p className="content-compact text-default-secondary italic py-2">
                    {/* Aggregate surface: see the leader card above. */}
                    {t_i18n(aggregateNaReason(headline.id, latestRuns))}
                  </p>
                ) : (
                <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden max-h-[284px] overflow-y-auto">
                  <table className="w-full content-base table-fixed">
                    <colgroup>
                      <col style={{ width: "28px" }} />
                      <col />
                      <col style={{ width: "56px" }} />
                    </colgroup>
                    <thead className="sticky top-0 z-10">
                      <tr className="border-b border-elevation-subtle-soft layer-1 bg-elevation-default">
                        <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">#</th>
                        <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Agent / Model")}</th>
                        <th className="text-right content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Score")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.map((d, idx) => {
                        const val = valueOn(d, headline.id)!;
                        return (
                          <tr key={d.key} className="border-b border-elevation-subtle-soft last:border-0 hover:bg-elevation-hover cursor-pointer transition-colors" onClick={() => navigate(`/evaluations/runs/${d.runId}`)}>
                            <td className="px-3 py-2">
                              <span className={cn("content-compact font-bold", idx === 0 ? "text-feedback-warning-primary" : idx === 1 ? "text-default-secondary" : idx === 2 ? "text-feedback-warning-primary" : "text-default-secondary")}>{idx + 1}</span>
                            </td>
                            <td className="px-3 py-2">
                              <div className="flex items-start gap-1.5">
                                <span className="inline-block w-2 h-2 rounded-full shrink-0 mt-[5px]" style={{ backgroundColor: d.color }} />
                                <div className="min-w-0">
                                  <div className="flex items-center gap-1">
                                    <p className="content-compact font-medium truncate">{d.agent}</p>
                                    {d.isBaseline && (
                                      <InfoLabel className="inline-flex shrink-0" content={t_i18n(EVALUATION_TERMS.baselineRun)}>
                                        <Star role="img" aria-label={t_i18n("Baseline")} className="h-3 w-3 text-feedback-warning-primary fill-feedback-alert-primary" />
                                      </InfoLabel>
                                    )}
                                    <RunModeBadge run={d} />
                                    <PromptGuardBadge run={d} />
                                    <ToolApprovalBadge run={d} />
                                    <PlatformErrorBadge run={d} />
                                  </div>
                                  <p className="content-caption text-default-secondary truncate">{formatModelLabel(d.model, t_i18n)}</p>
                                </div>
                              </div>
                            </td>
                            <td className="px-3 py-2 text-right">
                              <span className={cn("content-compact font-bold", headlineColorClass(val))}>{val}</span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                )}
                {/* Disclose omissions: a shorter list must not read as fewer runs. */}
                {omitted > 0 && data.length > 0 && (
                  <p className="mt-2 content-caption text-default-secondary">
                    {t_i18n("{count, plural, one {# agent and model pair} other {# agent and model pairs}} omitted: their latest run has no value for this overall score", {
                      count: omitted,
                    })}
                  </p>
                )}
              </CardContent>
            </Card>
          );
        };

        const trendChart = (headline: HeadlineDef) => {
          const { data, keys } = trendFor(headline.id);
          return (
            <Card key={headline.id} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
              <CardHeader className="pb-2">
                <CardTitle className="text-base font-medium flex items-center gap-1.5">
                  <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: headline.color }} />
                  {t_i18n(headline.label)}
                  <InfoTip content={t_i18n(HEADLINE_NOTES[headline.id])} label={t_i18n("What {score} is", { score: t_i18n(headline.label) })} />
                </CardTitle>
              </CardHeader>
              <CardContent>
                {data.length === 0 ? (
                  /* Three distinct empty states, each calling for a different
                     action. A single "no data" would make a regression that
                     predates the window look like one that never happened. */
                  <div className="flex items-center justify-center h-40 text-default-secondary">
                    <div className="text-center max-w-xs">
                      <BarChart3 className="h-6 w-6 mx-auto mb-2 opacity-50" />
                      <p className="content-compact">
                        {(chartData?.total_runs ?? 0) === 0
                          ? t_i18n("No runs yet")
                          : (chartData?.runs_in_range ?? 0) === 0
                            ? t_i18n("No runs in the selected range: widen it to see earlier history")
                            /* Aggregate surface: see the leader card above. */
                            : t_i18n(aggregateNaReason(headline.id, latestRuns))}
                      </p>
                    </div>
                  </div>
                ) : (
                  <>
                    <ResponsiveContainer width="100%" height={200}>
                      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} />
                        <XAxis
                          dataKey="idx"
                          type="number"
                          domain={[0, data.length - 1]}
                          allowDecimals={false}
                          tickFormatter={(idx: number) => {
                            const row = data[Math.round(idx)];
                            return row ? formatShortDate(new Date(row.timestamp as number)) : "";
                          }}
                          stroke={axisStroke}
                          tick={{ fontSize: 10, fill: tickFill }}
                          tickLine={false}
                          axisLine={{ stroke: gridStroke }}
                        />
                        <YAxis domain={[0, 100]} stroke={axisStroke} tick={{ fontSize: 10, fill: tickFill }} tickLine={false} axisLine={{ stroke: gridStroke }} />
                        <Tooltip
                          {...CHART_TOOLTIP_PROPS}
                          cursor={{ stroke: cursorStroke, strokeWidth: 1 }}
                          labelFormatter={(label, payload) => {
                            // Prefer the hovered point's own row from the
                            // payload, which carries `timestamp`; `data[idx]` is
                            // the fallback when Recharts has no payload yet.
                            const row = (payload?.[0]?.payload as { timestamp?: number } | undefined) ?? data[Math.round(Number(label))];
                            return row?.timestamp ? formatShortDateTime(new Date(row.timestamp)) : "";
                          }}
                        />
                        {keys.map((key) => {
                          const enc = encodingFor(key);
                          return (
                            <Line
                              key={key}
                              type="monotone"
                              dataKey={key}
                              stroke={enc.color}
                              /* Colour alone cannot carry series identity: many
                                 pairs get close hues, and some readers cannot
                                 see colour. */
                              strokeDasharray={enc.dash || undefined}
                              strokeWidth={2}
                              dot={{ r: 2, fill: enc.color, strokeWidth: 0 }}
                              activeDot={{ r: 4, fill: enc.color, stroke: activeDotStroke, strokeWidth: 2 }}
                              name={labelFor(key)}
                            />
                          );
                        })}
                      </LineChart>
                    </ResponsiveContainer>
                    <SeriesLegend keys={keys} mode="line" encodingFor={encodingFor} labelFor={labelFor} />
                    <p className="mt-1 content-caption text-default-secondary">
                      {t_i18n(CHART_RANGES.find((r) => r.value === chartRange)?.label ?? chartRange)}
                    </p>
                  </>
                )}
              </CardContent>
            </Card>
          );
        };

        const correlationChart = (
          id: CorrelationKey,
          xHeadline: HeadlineDef,
          yHeadline: HeadlineDef,
        ) => {
          const points = chartData?.correlations?.[id] ?? [];
          const keys = Array.from(new Set(points.map((pt) => pt.series)));
          return (
            <Card key={id} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
              <CardHeader className="pb-2">
                <CardTitle className="text-base font-medium flex items-center gap-1.5">
                  {t_i18n("{x} × {y}", { x: t_i18n(xHeadline.label), y: t_i18n(yHeadline.label) })}
                  {/* A comparison aid, not a ranking: the two axes are never
                      combined into an order. */}
                  <InfoTip
                    content={t_i18n("A comparison aid only: the two axes are never combined into a ranking. Each point is one agent/model pair's latest run, with both values from that same run.")}
                    label={t_i18n("What this chart compares")}
                  />
                </CardTitle>
              </CardHeader>
              <CardContent>
                {points.length === 0 ? (
                  <div className="flex items-center justify-center h-40 text-default-secondary">
                    <div className="text-center max-w-xs">
                      <BarChart3 className="h-6 w-6 mx-auto mb-2 opacity-50" />
                      <p className="content-compact">
                        {(chartData?.total_runs ?? 0) === 0
                          ? t_i18n("No runs yet")
                          : t_i18n("No run has a value on both axes, so there is nothing to plot")}
                      </p>
                    </div>
                  </div>
                ) : (
                  <>
                    <ResponsiveContainer width="100%" height={210}>
                      {/* Margins leave room for the axis titles, which name the
                          measurement each axis carries. */}
                      <ScatterChart margin={{ top: 8, right: 12, bottom: 14, left: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} />
                        <XAxis type="number" dataKey="x" name={t_i18n(xHeadline.label)} domain={[0, 100]} stroke={axisStroke} tick={{ fontSize: 10, fill: tickFill }} tickLine={false} axisLine={{ stroke: gridStroke }}>
                          <Label value={t_i18n(xHeadline.label)} position="insideBottom" offset={-12} style={axisTitleStyle} />
                        </XAxis>
                        <YAxis type="number" dataKey="y" name={t_i18n(yHeadline.label)} domain={[0, 100]} stroke={axisStroke} tick={{ fontSize: 10, fill: tickFill }} tickLine={false} axisLine={{ stroke: gridStroke }}>
                          <Label value={t_i18n(yHeadline.label)} angle={-90} position="insideLeft" offset={10} style={axisTitleStyle} />
                        </YAxis>
                        <Tooltip {...CHART_TOOLTIP_PROPS} cursor={{ stroke: cursorStroke, strokeWidth: 1 }} />
                        {keys.map((key) => {
                          const enc = encodingFor(key);
                          return (
                            <Scatter
                              key={key}
                              name={labelFor(key)}
                              data={points.filter((pt) => pt.series === key)}
                              fill={enc.color}
                              shape={enc.shape}
                            />
                          );
                        })}
                      </ScatterChart>
                    </ResponsiveContainer>
                    <SeriesLegend keys={keys} mode="scatter" encodingFor={encodingFor} labelFor={labelFor} />
                    {(chartData?.correlations_omitted?.[id] ?? 0) > 0 && (
                      <p className="mt-1 content-caption text-default-secondary">
                        {t_i18n("{count, plural, one {# pair} other {# pairs}} omitted: no value on one of the two axes", {
                          count: chartData!.correlations_omitted[id],
                        })}
                      </p>
                    )}
                  </>
                )}
              </CardContent>
            </Card>
          );
        };

        return (
          <div className="space-y-4">
            <div className="grid gap-4 lg:grid-cols-3">
              {HEADLINES.map(rankTable)}
            </div>

            {/* One range control for all three trends — never one each, which
                would let the headlines be read over different windows. */}
            <div className="flex items-center justify-between gap-3">
              <p className="content-compact text-default-secondary">
                <InfoLabel content={t_i18n(PAGE_NOTES.shownRuns)}>
                  {t_i18n("Showing {shown} of {total, plural, one {# run} other {# runs}}", {
                    shown: chartData?.runs_in_range ?? 0,
                    total: chartData?.total_runs ?? 0,
                  })}
                </InfoLabel>
              </p>
              <Tip content={t_i18n(PAGE_NOTES.chartRange)}>
                <span>
                  <Select value={chartRange} onValueChange={setChartRange}>
                    <SelectTrigger className="w-[150px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {CHART_RANGES.map((r) => (
                        <SelectItem key={r.value} value={r.value}>{t_i18n(r.label)}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </span>
              </Tip>
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              {HEADLINES.map(trendChart)}
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              {correlationChart("quality_performance", headlineDef("quality"), headlineDef("performance"))}
              {correlationChart("judge_performance", headlineDef("judge"), headlineDef("performance"))}
              {correlationChart("quality_judge", headlineDef("quality"), headlineDef("judge"))}
            </div>
          </div>
        );
      })()}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Activity Tab
// ---------------------------------------------------------------------------

const ACTIVITY_PAGE_SIZE = 20;

/** Turn a `rescore-from-runs` machine error code into a translated reason. */
function describeRescoreError(
  code: string | null | undefined,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
): string {
  switch (code) {
    case "run_not_found":
      return t_i18n("not found");
    case "no_predictions":
      return t_i18n("no stored predictions");
    case "run_in_progress":
      return t_i18n("already in progress");
    case "placebo_comparison_run":
      return t_i18n("part of a placebo comparison");
    default:
      return code || t_i18n("unknown");
  }
}

function RunIdBadge({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);
  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!navigator.clipboard?.writeText) return;
    navigator.clipboard
      .writeText(id)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
      })
      .catch(() => undefined);
  };
  return (
    <Tip content={id}>
      <button
        type="button"
        onClick={handleCopy}
        className="inline-flex items-center gap-1 font-mono-plex content-caption text-default-secondary hover:text-default-primary transition-colors"
        aria-label={t_i18n("Copy run ID {id}", { id })}
      >
        {id.slice(0, 8)}
        {copied ? <Check className="h-2.5 w-2.5 text-feedback-success-primary" /> : <Copy className="h-2.5 w-2.5 opacity-70" />}
      </button>
    </Tip>
  );
}

function ActivityTab({ evaluationId }: { evaluationId: string }) {
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [page, setPage] = useState(0);
  // Selected run id -> whether it belongs to a placebo comparison: those
  // compare, but are never re-run or re-scored alone - a re-scored trial
  // would leave its comparison's statistics on the scores it settled with.
  const [selectedRuns, setSelectedRuns] = useState<Map<string, boolean>>(new Map());
  const selectedRunIds = new Set(selectedRuns.keys());
  const reRunnableRunIds = [...selectedRuns].filter(([, comparison]) => !comparison).map(([runId]) => runId);
  const [ConfirmEl, confirm] = useConfirm();
  const { data: totalRuns = 0 } = useEvaluationRunsCount(evaluationId, {
    refetchInterval: REFETCH_INTERVAL_FAST,
  });
  const totalPages = Math.max(1, Math.ceil(totalRuns / ACTIVITY_PAGE_SIZE));
  // Clamp the page before it drives the runs query, so a shrunk total (e.g. after
  // a delete) never renders an out-of-range page or fetches an invalid offset.
  // ``totalPages`` is always >= 1, so deleting every run returns to page 0.
  const clampedPage = Math.min(page, totalPages - 1);
  // Sync the persisted page state to the clamped value (effects cannot run during
  // render); the UI already uses ``clampedPage``, so there is no invalid frame.
  React.useEffect(() => {
    if (page !== clampedPage) {
      setPage(clampedPage);
    }
  }, [page, clampedPage]);
  const { data: runs = [] } = useEvaluationRuns(
    evaluationId,
    ACTIVITY_PAGE_SIZE,
    clampedPage * ACTIVITY_PAGE_SIZE,
    { refetchInterval: REFETCH_INTERVAL_FAST },
  );
  const cancelRun = useCancelEvaluationRun();
  const deleteRun = useDeleteEvaluationRun();
  const deleteAll = useDeleteAllEvaluationRuns();
  const triggerFromRuns = useTriggerEvaluationFromRuns();
  const rescoreFromRuns = useRescoreRunsFromRuns();

  const handleDeleteAll = async () => {
    if (await confirm({ title: "Delete all runs?", description: "This will permanently delete all evaluation runs and results. This cannot be undone.", confirmLabel: "Delete All" })) {
      deleteAll.mutate(evaluationId, {
        onSuccess: () => { toast({ title: t_i18n("All runs deleted"), variant: "success" }); setPage(0); },
      });
    }
  };

  const handleReRunFullSelected = async () => {
    const runIds = reRunnableRunIds;
    if (runIds.length === 0) return;

    const confirmed = await confirm({
      title: t_i18n("Re-run full for selected runs?"),
      description: t_i18n(
        "A new run starts for each selected run, replaying its own versions and pinned dataset versions with prediction reuse off. The selected runs are not changed.",
      ),
      confirmLabel: t_i18n("Re-run full"),
    });
    if (!confirmed) return;

    try {
      const created = await triggerFromRuns.mutateAsync({
        evaluationId,
        runIds,
      });
      toast({
        title: t_i18n("Re-run full started"),
        description: t_i18n("{count, plural, one {# run} other {# runs}} queued.", { count: created.length }),
        variant: "success",
      });
      setSelectedRuns(new Map());
    } catch (error) {
      toast({
        title: t_i18n("Failed to re-run full selected runs"),
        description: error instanceof Error ? error.message : t_i18n("Unable to re-run full selected runs"),
        variant: "error",
        durationMs: 10000,
      });
    }
  };

  const handleRescoreSelected = async () => {
    const runIds = reRunnableRunIds;
    if (runIds.length === 0) return;

    const confirmed = await confirm({
      title: t_i18n("Re-score selected runs?"),
      description: t_i18n(
        "The selected runs' stored answers are scored again with the evaluation's current scoring settings, and their scores are replaced.",
      ),
      confirmLabel: t_i18n("Re-score"),
    });
    if (!confirmed) return;

    try {
      const { results } = await rescoreFromRuns.mutateAsync({
        evaluationId,
        runIds,
      });
      const failedResults = results.filter((r) => r.status === "failed");
      const queued = results.length - failedResults.length;
      const failed = failedResults.length;
      const FAILED_PREVIEW_LIMIT = 3;
      const failedPreview = failedResults
        .slice(0, FAILED_PREVIEW_LIMIT)
        .map((r) => `${r.run_id.slice(0, 8)} (${describeRescoreError(r.error, t_i18n)})`)
        .join(", ");
      const failedRemainder = failed - Math.min(failed, FAILED_PREVIEW_LIMIT);
      const failedSummary =
        failedRemainder > 0
          ? t_i18n("{preview}, +{remainder} more", { preview: failedPreview, remainder: failedRemainder })
          : failedPreview;
      toast({
        title:
          queued > 0
            ? t_i18n("Re-score started")
            : t_i18n("Failed to re-score selected runs"),
        description:
          failed > 0
            ? t_i18n(
                "{queued, plural, one {# run} other {# runs}} queued, {failed, plural, one {# run} other {# runs}} failed to queue: {summary}.",
                { queued, failed, summary: failedSummary },
              )
            : t_i18n("{count, plural, one {# run} other {# runs}} queued for re-scoring.", { count: queued }),
        variant: queued === 0 ? "error" : failed > 0 ? "info" : "success",
        durationMs: failed > 0 ? 10000 : undefined,
      });
      // Cleared on any successful request, even an all-rejected batch: the toast
      // says which runs failed.
      setSelectedRuns(new Map());
    } catch (error) {
      toast({
        title: t_i18n("Failed to re-score selected runs"),
        description: error instanceof Error ? error.message : t_i18n("Unable to re-score selected runs"),
        variant: "error",
        durationMs: 10000,
      });
    }
  };

  const getStatusBadge = (status: string) => {
    const config = EVALUATION_STATUS_CONFIG[status];
    if (!config) return <Badge variant="outline">{status}</Badge>;
    const Icon = config.icon;
    return (
      <Badge variant={config.badge}>
        <Icon className={cn("h-3 w-3 mr-1", status === "running" && "animate-spin")} />
        {t_i18n(config.label)}
      </Badge>
    );
  };

  // A card's status sums up its runs: the rule behind it is its tooltip.
  const batchStatusBadge = (status: keyof typeof BATCH_STATUS_NOTES) => (
    <InfoLabel className="inline-flex" content={t_i18n(BATCH_STATUS_NOTES[status])}>
      {getStatusBadge(status)}
    </InfoLabel>
  );

  if (runs.length === 0 && clampedPage === 0) {
    return (
      <Card className="border-dashed border-elevation-subtle-soft">
        <CardContent className="flex flex-col items-center justify-center py-12 text-center">
          <Activity className="h-10 w-10 text-default-secondary mb-3" />
          <p className="content-base text-default-secondary">{t_i18n("No evaluation runs yet")}</p>
          <p className="content-compact text-default-secondary mt-1">
            {t_i18n("Trigger an evaluation to see run history here")}
          </p>
        </CardContent>
      </Card>
    );
  }

  // Group runs by trigger batch (same created_at minute + trigger_reason)
  const batches: { key: string; trigger_reason: string; time: string; runs: typeof runs }[] = [];
  const batchMap = new Map<string, typeof runs>();
  for (const run of runs) {
    const batchKey = run.created_at.slice(0, 16) + "|" + run.trigger_reason;
    if (!batchMap.has(batchKey)) batchMap.set(batchKey, []);
    batchMap.get(batchKey)!.push(run);
  }
  for (const [key, batchRuns] of batchMap) {
    batches.push({ key, trigger_reason: key.split("|")[1], time: batchRuns[0].created_at, runs: batchRuns });
  }

  return (
    <div className="space-y-4">
      {/* Top bar: the bulk actions and the total, once; the pager sits in the footer and never repeats it (#10215 criterion 6, #11191). */}
      <div className="flex items-center justify-end">
        {runs.length > 0 && (
          <div className="flex items-center gap-2">
            {totalRuns > 0 && <TotalChip total={totalRuns} />}
            {reRunnableRunIds.length >= 1 && (
              <Tip content={aiOff ? aiReason : t_i18n(EVALUATION_TERMS.rerunFullSelected)} wrap="auto">
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  onClick={handleReRunFullSelected}
                  disabled={triggerFromRuns.isPending || aiOff}
                  aria-busy={triggerFromRuns.isPending || undefined}
                >
                  {triggerFromRuns.isPending && <Spinner size="sm" tone="inherit" />}
                  {!triggerFromRuns.isPending && <Zap className="h-3.5 w-3.5" />}
                  {t_i18n("Re-run full ({count})", { count: reRunnableRunIds.length })}
                </Button>
              </Tip>
            )}
            {reRunnableRunIds.length >= 1 && (
              <Tip content={aiOff ? aiReason : t_i18n(EVALUATION_TERMS.rescoreSelected)} wrap="auto">
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  onClick={handleRescoreSelected}
                  disabled={rescoreFromRuns.isPending || aiOff}
                  aria-busy={rescoreFromRuns.isPending || undefined}
                >
                  {rescoreFromRuns.isPending && <Spinner size="sm" tone="inherit" />}
                  {!rescoreFromRuns.isPending && <Repeat className="h-3.5 w-3.5" />}
                  {t_i18n("Re-score ({count})", { count: reRunnableRunIds.length })}
                </Button>
              </Tip>
            )}
            {selectedRunIds.size >= 2 && (
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5"
                onClick={() => navigate(`/evaluations/${evaluationId}/compare?runs=${[...selectedRunIds].join(",")}`)}
              >
                <Rows3 className="h-3.5 w-3.5" /> {t_i18n("Compare ({count})", { count: selectedRunIds.size })}
              </Button>
            )}
            <Button variant="ghost" size="sm" className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDeleteAll} disabled={deleteAll.isPending} aria-busy={deleteAll.isPending || undefined}>
              {deleteAll.isPending && <Spinner size="sm" tone="inherit" />}
              {!deleteAll.isPending && <Trash2 className="h-3.5 w-3.5" />} {t_i18n("Delete All")}
            </Button>
          </div>
        )}
      </div>

      {batches.map((batch) => {
        const allCompleted = batch.runs.every((r) => r.status === "completed");
        const anyRunning = batch.runs.some((r) => r.status === "running" || r.status === "pending");
        const totalProgress = batch.runs.reduce((s, r) => s + r.completed_entries, 0);
        const totalEntries = batch.runs.reduce((s, r) => s + r.total_entries, 0);
        // The legacy blended score is not written for new runs: render its
        // column only where a stored value exists.
        const batchHasLegacyOverall = batch.runs.some((r) => r.overall_score !== null);
        // When every run in the batch is legacy, Overall Quality has no value on any
        // row, so its column is hidden and Overall Performance's header names what it
        // shows. A mixed batch keeps both columns for its newer runs.
        const batchFullyLegacy = batch.runs.length > 0 && batch.runs.every((r) => r.overall_score !== null);

        return (
          <Card key={batch.key} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
            <CardContent className="p-4">
              {/* Batch header */}
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2 content-base">
                  <InfoLabel className="inline-flex" content={batchTriggerNote(batch.trigger_reason, t_i18n)}>
                    <Badge variant="outline" className="content-caption">
                      {t_i18n(EVALUATION_TRIGGER_LABELS[batch.trigger_reason] || batch.trigger_reason)}
                    </Badge>
                  </InfoLabel>
                  <span className="text-default-secondary">{formatTimeAgo(intl, batch.time)}</span>
                  {anyRunning && batchStatusBadge("running")}
                  {allCompleted && batchStatusBadge("completed")}
                  {!anyRunning && !allCompleted && batch.runs.some((r) => r.status === "failed") && batchStatusBadge("failed")}
                  {!anyRunning && !allCompleted && batch.runs.some((r) => r.status === "cancelled") && batchStatusBadge("cancelled")}
                </div>
                <div className="flex items-center gap-2">
                  {anyRunning && (
                    <div className="flex items-center gap-2 content-compact text-default-secondary">
                      <span>{totalProgress} / {totalEntries}</span>
                      <div className="w-20 h-1.5 bg-elevation-hover rounded-full overflow-hidden">
                        <div
                          className="h-full bg-accent rounded-full transition-all"
                          style={{ width: `${totalEntries > 0 ? (totalProgress / totalEntries) * 100 : 0}%` }}
                        />
                      </div>
                    </div>
                  )}
                  {anyRunning && (
                    <Tip content={t_i18n("Cancel all runs in this batch")}>
                      <button aria-label={t_i18n("Cancel all runs in this batch")}
                        className="h-6 w-6 flex items-center justify-center rounded-sm text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10 transition-colors"
                        onClick={() => { batch.runs.filter((r) => r.status === "running" || r.status === "pending").forEach((r) => cancelRun.mutate(r.id)); toast({ title: t_i18n("Cancelling batch"), variant: "success" }); }}
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </Tip>
                  )}
                  <Tip content={t_i18n("Delete all runs in this batch")}>
                    <button aria-label={t_i18n("Delete all runs in this batch")}
                      className="h-6 w-6 flex items-center justify-center rounded-sm text-feedback-error-primary hover:bg-feedback-error-primary/10 transition-colors"
                      onClick={async () => {
                        const ok = await confirm({ title: "Delete batch", description: t_i18n("Delete {count, plural, one {the only run} other {all # runs}} from this batch?", { count: batch.runs.length }), confirmLabel: "Delete", variant: "danger" });
                        if (!ok) return;
                        const results = await Promise.allSettled(batch.runs.map((r) => deleteRun.mutateAsync(r.id)));
                        const failed = results.filter((r) => r.status === "rejected").length;
                        if (failed === 0) {
                          toast({ title: t_i18n("Batch deleted"), variant: "success" });
                        } else {
                          toast({ title: t_i18n("Batch delete incomplete"), description: t_i18n("{failed, plural, one {# run} other {# runs}} of {total} could not be deleted.", { failed, total: results.length }), variant: "error" });
                        }
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </Tip>
                </div>
              </div>

              {/* Compact results table */}
              <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
                <table className="w-full content-base">
                  <thead>
                    <tr className="border-b border-elevation-subtle-soft bg-elevation-hover">
                      <th className="px-2 py-2 w-8">
                        <Tip content={t_i18n(PAGE_NOTES.selectAll)}>
                        <Switch
                          aria-label={t_i18n("Select all completed runs")}
                          className="scale-75"
                          checked={batch.runs.filter((r) => r.status === "completed").length > 0 && batch.runs.filter((r) => r.status === "completed").every((r) => selectedRunIds.has(r.id))}
                          onCheckedChange={(checked) => {
                            setSelectedRuns((prev) => {
                              const next = new Map(prev);
                              batch.runs.filter((r) => r.status === "completed").forEach((r) => checked ? next.set(r.id, isComparisonRun(r)) : next.delete(r.id));
                              return next;
                            });
                          }}
                        />
                        </Tip>
                      </th>
                      <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Agent")}</th>
                      <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Model")}</th>
                      <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Versions")}</th>
                      <th className="text-center content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">
                        <InfoLabel content={t_i18n(HEADLINE_NOTES.judge)}>{t_i18n(HEADLINE_LABELS.judge)}</InfoLabel>
                      </th>
                      {!batchFullyLegacy && (
                        <th className="text-center content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">
                          <InfoLabel content={t_i18n(HEADLINE_NOTES.quality)}>{t_i18n(HEADLINE_LABELS.quality)}</InfoLabel>
                        </th>
                      )}
                      <th className="text-center content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">
                        {batchFullyLegacy ? (
                          <InfoLabel content={t_i18n(HEADLINE_NOTES.legacySpeed)}>{t_i18n("Speed (legacy)")}</InfoLabel>
                        ) : (
                          <InfoLabel content={t_i18n(HEADLINE_NOTES.performance)}>{t_i18n(HEADLINE_LABELS.performance)}</InfoLabel>
                        )}
                      </th>
                      {/* Legacy blended score. Not written for new runs, so shown
                          only where a stored value exists. */}
                      {batchHasLegacyOverall && (
                        <th className="text-center content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">
                          <InfoLabel content={t_i18n(HEADLINE_NOTES.legacyOverall)}>{t_i18n("Overall (legacy)")}</InfoLabel>
                        </th>
                      )}
                      <th className="text-center content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Status")}</th>
                      <th className="text-center content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2">{t_i18n("Progress")}</th>
                      <th className="text-right content-caption font-medium text-default-secondary uppercase tracking-wider px-3 py-2 w-16"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {batch.runs.map((run) => {
                      // A placebo comparison's run is never re-run alone, by design:
                      // its arm tag says so, not the "No replay" alarm of a broken run.
                      const replayBlocked = !run.can_replay && !isComparisonRun(run);
                      return (
                        <tr
                          key={run.id}
                          className={cn(
                            "border-b border-elevation-subtle-soft last:border-0 cursor-pointer transition-colors",
                            replayBlocked
                              ? "bg-feedback-error-primary/[0.08] hover:bg-feedback-error-primary/[0.12] ring-1 ring-inset ring-feedback-error-primary/30"
                              : "hover:bg-elevation-hover"
                          )}
                          onClick={() => navigate(`/evaluations/runs/${run.id}`)}
                        >
                        <td className="px-2 py-2 w-8" onClick={(e) => e.stopPropagation()}>
                          {run.status === "completed" && (
                            <Switch
                              aria-label={t_i18n("Select run")}
                              className="scale-75"
                              checked={selectedRunIds.has(run.id)}
                              onCheckedChange={() => {
                                setSelectedRuns((prev) => {
                                  const next = new Map(prev);
                                  if (next.has(run.id)) { next.delete(run.id); } else { next.set(run.id, isComparisonRun(run)); }
                                  return next;
                                });
                              }}
                            />
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-1.5">
                            <Bot className="h-3 w-3 text-default-secondary shrink-0" />
                            {!!run.is_baseline && (
                              <InfoLabel className="inline-flex shrink-0" content={t_i18n(EVALUATION_TERMS.baselineRun)}>
                                <Star role="img" aria-label={t_i18n("Baseline")} className="h-3 w-3 text-feedback-warning-primary fill-feedback-alert-primary" />
                              </InfoLabel>
                            )}
                            {(run.anomaly_count ?? 0) > 0 && (
                              <InfoLabel
                                className="inline-flex shrink-0"
                                content={t_i18n("Execution anomalies flagged: {count}. Confirmation-seeking results still count toward scores; transient execution errors remain excluded.", { count: run.anomaly_count ?? 0 })}
                              >
                                <AlertTriangle role="img" aria-label={t_i18n("Execution anomalies")} className="h-3 w-3 text-feedback-warning-primary" />
                              </InfoLabel>
                            )}
                            {!!run.has_dataset_drift && (
                              <InfoLabel
                                className="inline-flex shrink-0"
                                content={t_i18n("Dataset updated since this run: results reflect the pinned historical snapshot, not the current dataset")}
                              >
                                <History role="img" aria-label={t_i18n("Dataset updated")} className="h-3 w-3 text-feedback-warning-primary" />
                              </InfoLabel>
                            )}
                            {(run.judge_failure_count ?? 0) > 0 && (
                              <InfoLabel
                                className="inline-flex shrink-0"
                                content={t_i18n("Judge failures excluded from scores: {count}", { count: run.judge_failure_count ?? 0 })}
                              >
                                <Gavel role="img" aria-label={t_i18n("Judge failures")} className="h-3 w-3 text-feedback-error-primary" />
                              </InfoLabel>
                            )}
                            <RunModeBadge run={run} />
                            <PromptGuardBadge run={run} />
                            <ToolApprovalBadge run={run} />
                            <PlatformErrorBadge run={run} />
                            {(run.reused_entries ?? 0) > 0 && (
                              <InfoLabel
                                className="inline-flex shrink-0"
                                content={t_i18n(EVALUATION_TERMS.reusedEntries, { reused: run.reused_entries ?? 0, total: run.total_entries })}
                              >
                                <Badge className={cn("shrink-0 gap-1 text-[9px]", REUSE_BADGE_CONFIG.reused.color, REUSE_BADGE_CONFIG.reused.bg)}>
                                  <Repeat className="h-2.5 w-2.5" />
                                  {t_i18n("Reused")}
                                </Badge>
                              </InfoLabel>
                            )}
                            <span className="truncate">{run.agent_name || run.agent_slug}</span>
                            {evaluationSubjectPath(run) && (
                              <a
                                href={withBasePath(evaluationSubjectPath(run) ?? "")}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center hover:text-default-primary transition-colors"
                                onClick={(e) => e.stopPropagation()}
                                aria-label={t_i18n(run.flow_id ? "Open flow {name} in a new tab" : "Open agent {name} in a new tab", { name: run.agent_name || run.agent_slug })}
                              >
                                <ExternalLink className="h-2.5 w-2.5 opacity-70" />
                              </a>
                            )}
                          </div>
                          <div className="mt-0.5">
                            <RunIdBadge id={run.id} />
                          </div>
                        </td>
                        <td className="px-3 py-2">
                          <span className="content-compact text-default-secondary">{formatModelLabel(run.model_id, t_i18n)}</span>
                          {run.is_default_model && (
                            <InfoLabel content={t_i18n(EVALUATION_TERMS.agentDefaultModel)} className="content-caption text-accent ml-1">
                              {t_i18n("(default)")}
                            </InfoLabel>
                          )}
                          <PlaceboArmTag run={run} className="ml-1.5" />
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex flex-wrap gap-1">
                            <RunVersionChips run={run} fallback={<span className="content-caption text-default-secondary">—</span>} />
                          </div>
                        </td>
                        <td className="px-3 py-2 text-center">
                          {(() => {
                            if (run.status === "running") return <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                            const val = run.score;
                            if (val !== null && val !== undefined) {
                              const color = headlineColorClass(val);
                              return (
                                <InfoLabel
                                  content={headlineTip(judgeHeadline(run), t_i18n)}
                                  className={cn("font-medium content-base", color)}
                                >
                                  {val}
                                </InfoLabel>
                              );
                            }
                            if (run.status === "failed" || run.status === "cancelled") {
                              return <span className="content-compact text-default-secondary">—</span>;
                            }
                            if (run.status === "completed") {
                              return (
                                <InfoLabel
                                  content={t_i18n(...judgeNaNote(run))}
                                  className="content-compact text-default-secondary italic"
                                >
                                  {t_i18n("N/A")}
                                </InfoLabel>
                              );
                            }
                            return <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                          })()}
                        </td>
                        {/* Overall Quality: the persisted value, never
                            recomputed. `null` renders "N/A", never 0. Hidden when
                            the whole batch is legacy (batchFullyLegacy). */}
                        {!batchFullyLegacy && (
                        <td className="px-3 py-2 text-center">
                          {(() => {
                            if (run.status === "running") return <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                            const q = qualityHeadline(run);
                            if (q.value !== null) {
                              return (
                                <InfoLabel
                                  content={headlineTip(q, t_i18n)}
                                  className={cn("font-medium content-base", headlineColorClass(q.value))}
                                >
                                  {q.value}
                                </InfoLabel>
                              );
                            }
                            if (run.status === "failed" || run.status === "cancelled" || isLegacyRun(run)) {
                              return <span className="content-compact text-default-secondary">—</span>;
                            }
                            if (run.status === "completed") {
                              return (
                                <InfoLabel content={t_i18n(naReason("quality", run.status))} className="content-compact text-default-secondary italic">
                                  {t_i18n("N/A")}
                                </InfoLabel>
                              );
                            }
                            return <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                          })()}
                        </td>
                        )}
                        {/* Overall Performance: comparative within this
                            evaluation, as the tooltip says. An older run falls back
                            to its legacy `speed_score`, labelled as such, never both. */}
                        <td className="px-3 py-2 text-center">
                          {(() => {
                            if (run.status === "running") return <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                            const perf = performanceHeadline(run);
                            if (perf.value !== null) {
                              return (
                                <span className="inline-flex items-baseline justify-center gap-1">
                                  <InfoLabel
                                    content={headlineTip(perf, t_i18n)}
                                    className={cn("font-medium content-base", perf.legacy ? "text-default-secondary" : headlineColorClass(perf.value))}
                                  >
                                    {perf.value}
                                    {perf.legacy && !batchFullyLegacy && (
                                      <span className="content-caption text-default-secondary ml-1 uppercase tracking-wide">{t_i18n("legacy")}</span>
                                    )}
                                  </InfoLabel>
                                  {/* The run's own duration, not a unit of the comparative score beside it. */}
                                  {!perf.legacy && run.duration_seconds !== null && (
                                    <InfoLabel content={t_i18n(EVALUATION_TERMS.runDuration)} className="content-caption text-default-secondary">
                                      ({run.duration_seconds}s)
                                    </InfoLabel>
                                  )}
                                </span>
                              );
                            }
                            if (run.status === "failed" || run.status === "cancelled") {
                              return <span className="content-compact text-default-secondary">—</span>;
                            }
                            if (run.status === "completed") {
                              return (
                                <InfoLabel content={t_i18n(naReason("performance", run.status, run))} className="content-compact text-default-secondary italic">
                                  {t_i18n("N/A")}
                                </InfoLabel>
                              );
                            }
                            return <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                          })()}
                        </td>
                        {batchHasLegacyOverall && (
                        <td className="px-3 py-2 text-center">
                          {run.status === "running" ? (
                            <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>
                          ) : run.overall_score !== null ? (
                            <InfoLabel content={t_i18n(HEADLINE_NOTES.legacyOverall)} className="font-medium content-base text-default-secondary">
                              {run.overall_score}
                            </InfoLabel>
                          ) : (run.status === "failed" || run.status === "cancelled") ? (
                            <span className="content-compact text-default-secondary">—</span>
                          ) : run.status === "completed" ? (
                            <InfoLabel content={t_i18n(...legacyOverallNaNote(run))} className="content-compact text-default-secondary italic">
                              {t_i18n("N/A")}
                            </InfoLabel>
                          ) : (
                            <div className="flex justify-center text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>
                          )}
                        </td>
                        )}
                        <td className="px-3 py-2 text-center">
                          <div className="flex items-center justify-center gap-1.5">
                            {getStatusBadge(run.status)}
                            {run.held_reason && <HeldBadge reason={run.held_reason} className="content-caption" />}
                            {replayBlocked && (
                              <InfoLabel
                                content={run.replay_block_reason || t_i18n("Replay impossible: missing version snapshots.")}
                                className="inline-flex items-center gap-1 rounded-sm border border-feedback-error-primary/40 bg-feedback-error-primary/20 px-1.5 py-0.5 content-caption font-medium text-feedback-error-primary"
                              >
                                <AlertTriangle className="h-2.5 w-2.5" />
                                {t_i18n("No replay")}
                              </InfoLabel>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-center">
                          <span className="content-compact text-default-secondary">
                            {run.completed_entries}/{run.total_entries}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right">
                          <div className="flex items-center justify-end gap-1">
                            {(run.status === "running" || run.status === "pending") && (
                              <Tip content={t_i18n("Cancel")}>
                                <button aria-label={t_i18n("Cancel")}
                                  className="h-6 w-6 flex items-center justify-center rounded-sm text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10 transition-colors"
                                  onClick={(e) => { e.stopPropagation(); cancelRun.mutate(run.id, { onSuccess: () => toast({ title: t_i18n("Run cancelled"), variant: "success" }) }); }}
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              </Tip>
                            )}
                          </div>
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
      })}

      {/* Pagination — bottom */}
      <Pagination
        page={clampedPage}
        pageSize={ACTIVITY_PAGE_SIZE}
        total={totalRuns}
        onChange={setPage}
      />
      {ConfirmEl}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Datasets Tab
// ---------------------------------------------------------------------------

/**
 * A linked dataset's output family on the Dataset tab: its name, or what is
 * wrong with it - none, which blocks a launch, or one that conflicts with the
 * evaluation's, which the server reports and never fixes.
 */
function LinkedDatasetFamily({
  dataset,
  evaluationFamily,
  familyCatalog,
}: {
  dataset: LinkedDataset;
  evaluationFamily: EvaluationFamily | null;
  familyCatalog?: FamilyInfo[];
}) {
  const { t_i18n } = useFormatter();
  if (dataset.needs_family || !dataset.family) return <NeedsFamilyBadge size="sm" />;
  const label = familyLabel(dataset.family, familyCatalog, t_i18n);
  return (
    <>
      <Badge variant="outline" className="content-caption shrink-0">{label}</Badge>
      {dataset.family_conflict && (
        <InfoLabel
          className="inline-flex shrink-0"
          content={t_i18n(EVALUATION_TERMS.familyConflict, {
            dataset: label,
            evaluation: evaluationFamily ? familyLabel(evaluationFamily, familyCatalog, t_i18n) : "",
          })}
        >
          <Badge variant="warning" className="content-caption">{t_i18n("Family conflict")}</Badge>
        </InfoLabel>
      )}
    </>
  );
}

function DatasetsTab({ evaluationId, canManage, evaluationFamily }: { evaluationId: string; canManage: boolean; evaluationFamily: EvaluationFamily | null }) {
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { data: linkedDatasets = [], isLoading } = useEvaluationDatasets(evaluationId);
  const { data: familyCatalog } = useEvaluationFamilies();
  const linkDataset = useLinkDataset();
  const unlinkDataset = useUnlinkDataset();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");
  const pickerRef = useRef<HTMLDivElement>(null);
  const debouncedSearch = useDebounce(search, 300);
  const { data: allDatasets } = useDatasets(debouncedSearch || undefined, evaluationFamily || undefined);
  const linkedIds = linkedDatasets.map((d) => d.dataset_id);
  const availableToLink = (allDatasets ?? []).filter((d) => !linkedIds.includes(d.id));

  useClickOutside(pickerRef, () => setPickerOpen(false), pickerOpen);

  const handleLink = (datasetId: string) => {
    linkDataset.mutate(
      { datasetId, evaluationId },
      {
        onSuccess: () => { toast({ title: t_i18n("Dataset linked"), variant: "success" }); setPickerOpen(false); setSearch(""); },
        onError: () => toast({ title: t_i18n("Failed to link dataset"), variant: "error" }),
      },
    );
  };

  const handleUnlink = (datasetId: string) => {
    unlinkDataset.mutate(
      { datasetId, evaluationId },
      {
        onSuccess: () => toast({ title: t_i18n("Dataset unlinked"), variant: "success" }),
        onError: () => toast({ title: t_i18n("Failed to unlink dataset"), variant: "error" }),
      },
    );
  };

  if (isLoading) return <div className="py-8 text-center content-base text-default-secondary">{t_i18n("Loading datasets...")}</div>;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="content-base text-default-secondary">
          {t_i18n("{count, plural, one {# linked dataset} other {# linked datasets}}", { count: linkedDatasets.length })}
        </p>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" className="gap-1.5" onClick={() => navigate("/evaluations/datasets")}>
            <Database className="h-3.5 w-3.5" /> {t_i18n("Manage Datasets")}
          </Button>
          {canManage && (
            <div className="relative" ref={pickerRef}>
              <Tip content={t_i18n(PAGE_NOTES.linkDataset)}>
                <Button size="sm" variant="outline" className="gap-1.5" onClick={() => setPickerOpen((v) => !v)} aria-expanded={pickerOpen}>
                  <Plus className="h-3.5 w-3.5" /> {t_i18n("Link Dataset")}
                </Button>
              </Tip>
              {pickerOpen && (
                <div className="absolute right-0 z-50 mt-1 w-72 rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay">
                  <div className="p-2 border-b border-elevation-subtle-soft">
                    <SearchInput
                      className="max-w-none"
                      placeholder={t_i18n("Search datasets...")}
                      value={search}
                      onChange={setSearch}
                      autoFocus
                    />
                  </div>
                  <div className="max-h-52 overflow-y-auto">
                    {availableToLink.length > 0 ? (
                      availableToLink.map((ds) => (
                        // A dataset with no output family links, but blocks Run: the row says so.
                        <Tip key={ds.id} content={ds.needs_family ? t_i18n(EVALUATION_TERMS.needsFamily) : undefined}>
                          <button
                            type="button"
                            className="flex items-center gap-2.5 w-full px-3 py-2 text-left content-compact transition-colors hover:bg-elevation-hover"
                            onClick={() => handleLink(ds.id)}
                            disabled={linkDataset.isPending}
                          >
                            <Database className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                            <span className="flex-1 truncate">{ds.name}</span>
                            {/* An icon leaves the name its room; the row's tooltip says what it means. */}
                            {ds.needs_family && (
                              <AlertTriangle role="img" aria-label={t_i18n("Needs family")} className="h-3 w-3 shrink-0 text-feedback-warning-primary" />
                            )}
                            <span className="text-default-secondary shrink-0">{t_i18n("{count, plural, one {# entry} other {# entries}}", { count: ds.entry_count })}</span>
                          </button>
                        </Tip>
                      ))
                    ) : (
                      <p className="px-3 py-3 text-[11px] text-default-secondary text-center">
                        {search
                          ? t_i18n("No matching datasets.")
                          : evaluationFamily
                            ? t_i18n("Every dataset of this output family, or without one, is already linked.")
                            : t_i18n("All datasets are linked.")}
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {linkedDatasets.length === 0 ? (
        <Card className="border-dashed border-elevation-subtle-soft">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Database className="h-10 w-10 text-default-secondary mb-3" />
            <p className="content-base text-default-secondary">{t_i18n("No datasets linked")}</p>
            <p className="content-compact text-default-secondary mt-1">{t_i18n("Link a dataset to provide test cases for this evaluation.")}</p>
            {canManage && (
              <div className="flex gap-2 mt-3">
                <Button size="sm" variant="outline" className="gap-1.5" onClick={() => navigate("/evaluations/datasets")}>
                  <Database className="h-3.5 w-3.5" /> {t_i18n("Browse Datasets")}
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
        linkedDatasets.map((ld) => (
          <Card key={ld.id} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
            <CardContent className="p-4">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
                  <Database className="h-4 w-4 text-accent" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <p className="content-base font-medium truncate">{ld.dataset_name}</p>
                    <LinkedDatasetFamily dataset={ld} evaluationFamily={evaluationFamily} familyCatalog={familyCatalog} />
                  </div>
                  {ld.dataset_description && (
                    <p className="content-compact text-default-secondary truncate">{ld.dataset_description}</p>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Badge variant="outline" className="content-caption">{t_i18n("{count, plural, one {# entry} other {# entries}}", { count: ld.entry_count })}</Badge>
                  <Tip content={t_i18n("Open dataset")}>
                    <Button aria-label={t_i18n("Open dataset")}
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 text-default-secondary hover:text-accent"
                      onClick={() => navigate(`/evaluations/datasets/${ld.dataset_id}`)}
                    >
                      <ExternalLink className="h-3 w-3" />
                    </Button>
                  </Tip>
                  {canManage && (
                    <Tip content={aiOff ? aiReason : t_i18n("Unlink dataset")} wrap="auto">
                      <Button aria-label={t_i18n("Unlink dataset")}
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0 text-default-secondary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                        onClick={() => handleUnlink(ld.dataset_id)}
                        disabled={unlinkDataset.isPending || aiOff}
                      >
                        <X className="h-3 w-3" />
                      </Button>
                    </Tip>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Configuration Tab
// ---------------------------------------------------------------------------

/** A Configuration-tab field heading, with room for its "i". */
const CONFIG_HEADING = "flex items-center gap-1 content-caption font-medium text-default-secondary uppercase tracking-wider";

/**
 * What the Configuration tab's own fields mean: the judge's model and budget
 * (`evaluation_engine.py`, `_get_evaluator_llm`, judge truncation), per-field scoring
 * (`ComparisonPolicy`, default weight 1), tags (`services/evaluation/tags.py`, filtered
 * by run search) and the composition read view. @i18n-keys
 */
const CONFIG_NOTES = {
  evaluatorModel:
    "The model the LLM judge runs on, through the providers the evaluation's owner may use. Platform default is the platform's default model.",
  fieldPointer: "The field's path in the answer, as a JSON Pointer such as /findings/0/severity.",
  fieldWeight: "How much this field counts in Field Accuracy, relative to the others. Empty means 1; a container's weight multiplies its fields'.",
  tags: "Labels on this evaluation's runs, which the run search can filter on. A run's tags are read from the evaluation, its agent and itself, so a change here reaches existing runs too.",
  tagValue: "Left empty, the tag is saved with the value true.",
  composition: "Which metrics compose Overall Quality and Overall Performance. Every metric is measured and stored either way.",
  defaultMetrics: "This output family's default metrics, including any added later. A metric marked not counted by default is left out.",
} as const;

/** The saved per-field scoring overrides, read-only: the rows the edit view lists. */
function SavedFieldScoring({ overrides }: { overrides: FieldScoringOverrides | null }) {
  const { t_i18n } = useFormatter();
  const rows = overridesToRows(overrides);
  return (
    <div className="pt-3 border-t border-elevation-subtle-soft">
      <p className={cn(CONFIG_HEADING, "mb-1.5")}>{t_i18n("Per-Field Scoring")}</p>
      {rows.length === 0 ? (
        <p className="content-compact text-default-secondary">{t_i18n("No override: every field is scored according to its schema declaration.")}</p>
      ) : (
        <ul className="space-y-1">
          {rows.map((row) => {
            // A mode this app does not know is shown as stored.
            const mode = fieldModeCopy(row.mode);
            return (
              <li key={row.pointer} className="flex items-center gap-3 content-compact">
                <code className="min-w-0 flex-1 truncate font-mono-plex">{row.pointer}</code>
                {mode ? (
                  <InfoLabel className="whitespace-nowrap" content={t_i18n(mode.description)}>{t_i18n(mode.label)}</InfoLabel>
                ) : (
                  <span>{row.mode}</span>
                )}
                {row.weight !== "" && (
                  <InfoLabel content={t_i18n(CONFIG_NOTES.fieldWeight)}>
                    <span className="font-mono-plex tabular-nums">{t_i18n("weight {value}", { value: row.weight })}</span>
                  </InfoLabel>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// Agent-scoped config: baseline designation + cascading agent tags, one compact
// row per agent.
function AgentTagsBaselinePanel({
  agents,
  evaluationId,
  canManage,
}: {
  agents: {
    id: string;
    name: string;
    slug: string | null;
    icon?: string | null;
    tags?: Record<string, string> | null;
    is_baseline?: boolean;
  }[];
  evaluationId: string;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  if (agents.length === 0) return null;
  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <Tag className="h-3.5 w-3.5 text-default-secondary" />
          <CardTitle className="text-base font-medium">{t_i18n("Agent Tags & Baseline")}</CardTitle>
        </div>
        <p className="content-compact text-default-secondary pt-1">
          {t_i18n("Scoped to this evaluation. An agent's tags apply to all of its runs and override evaluation-level tags on key conflict. One agent can be the baseline that comparisons are measured against.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-1.5">
        {agents.map((agent) => (
          <AgentTagRow
            key={agent.id}
            agent={agent}
            evaluationId={evaluationId}
            canManage={canManage}
          />
        ))}
      </CardContent>
    </Card>
  );
}

// Single agent's baseline toggle + cascading tag editor, rendered inside
// AgentTagsBaselinePanel.
function AgentTagRow({
  agent,
  evaluationId,
  canManage,
}: {
  agent: {
    id: string;
    name: string;
    slug: string | null;
    icon?: string | null;
    tags?: Record<string, string> | null;
    is_baseline?: boolean;
  };
  evaluationId: string;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const setBaseline = useSetAgentBaseline();
  const setTags = useSetAgentTags();
  const aiOff = useAiActionDisabled();
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  const IconComp = ICON_MAP[(agent.icon ?? "Bot") as keyof typeof ICON_MAP] ?? Bot;
  const tags = agent.tags || {};
  const tagEntries = Object.entries(tags);
  const isBaseline = !!agent.is_baseline;
  const newKeyReserved = newKey.trim() === RUN_TAG_BASELINE_KEY;

  const toggleBaseline = () => {
    setBaseline.mutate(
      { evaluationId, agentId: isBaseline ? null : agent.id },
      {
        onError: (e) =>
          toast({ title: t_i18n("Failed to set baseline"), description: e.message, variant: "error" }),
      },
    );
  };

  const saveTags = (next: Record<string, string>) => {
    setTags.mutate(
      { evaluationId, agentId: agent.id, tags: next },
      {
        onError: (e) =>
          toast({ title: t_i18n("Failed to update tags"), description: e.message, variant: "error" }),
      },
    );
  };

  const addTag = () => {
    const key = newKey.trim();
    if (!key || newKeyReserved) return;
    saveTags({ ...tags, [key]: newValue.trim() || "true" });
    setNewKey("");
    setNewValue("");
  };

  const removeTag = (key: string) => {
    const next = { ...tags };
    delete next[key];
    saveTags(next);
  };

  return (
    <div className="rounded-sm bg-elevation-default px-4 py-3 space-y-2">
      <div className="flex items-center gap-3">
        <IconComp className="h-4 w-4 text-accent shrink-0" />
        <p className="content-base font-medium truncate flex-1 min-w-0">{agent.name}</p>
        {agent.slug && <code className="content-caption text-default-secondary shrink-0">{agent.slug}</code>}
        {canManage ? (
          <div className="flex items-center gap-1.5 shrink-0">
            <Star className={cn("h-3.5 w-3.5", isBaseline ? "text-feedback-warning-primary fill-feedback-alert-primary" : "text-default-secondary")} />
            <InfoLabel content={t_i18n(EVALUATION_TERMS.baselineAgent)} className="content-caption text-default-secondary">
              {t_i18n("Baseline")}
            </InfoLabel>
            <AiDisabledTip>
              <Switch aria-label={t_i18n("Baseline: {name}", { name: agent.name })} checked={isBaseline} onCheckedChange={toggleBaseline} disabled={setBaseline.isPending || aiOff} />
            </AiDisabledTip>
          </div>
        ) : (
          isBaseline && (
            <InfoLabel content={t_i18n(EVALUATION_TERMS.baselineAgent)} className="shrink-0">
              <Badge variant="outline" className="gap-1 content-caption border-feedback-alert-primary/30 text-feedback-warning-primary">
                <Star className="h-2.5 w-2.5 fill-feedback-alert-primary text-feedback-warning-primary" />
                {t_i18n("Baseline")}
              </Badge>
            </InfoLabel>
          )
        )}
      </div>
      {(tagEntries.length > 0 || canManage) && (
        <div className="pl-7 space-y-1.5">
          {tagEntries.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {tagEntries.map(([key, value]) => (
                <Badge key={key} variant="outline" className="gap-1 content-caption font-mono-plex">
                  {`${key}: ${value}`}
                  {canManage && (
                    <AiDisabledTip>
                      <button
                        onClick={() => removeTag(key)}
                        disabled={setTags.isPending || aiOff}
                        className="hover:text-feedback-error-primary transition-colors"
                        aria-label={`Remove tag ${key}`}
                      >
                        <X className="h-2.5 w-2.5" />
                      </button>
                    </AiDisabledTip>
                  )}
                </Badge>
              ))}
            </div>
          )}
          {canManage && (
            <div className="flex items-center gap-2">
              <Tip
                content={
                  newKeyReserved
                    ? t_i18n('"baseline" is reserved: use the baseline toggle instead.')
                    : undefined
                }
              >
                <Input aria-label={t_i18n("key")}
                  value={newKey}
                  onChange={(e) => setNewKey(e.target.value)}
                  placeholder={t_i18n("key")}
                  className="h-7 text-[11px]"
                  invalidTone="alert"
                  aria-invalid={newKeyReserved || undefined}
                  onKeyDown={(e) => e.key === "Enter" && !aiOff && addTag()}
                />
              </Tip>
              <Tip content={t_i18n(CONFIG_NOTES.tagValue)}>
                <Input aria-label={t_i18n("value (optional)")}
                  value={newValue}
                  onChange={(e) => setNewValue(e.target.value)}
                  placeholder={t_i18n("value (optional)")}
                  className="h-7 text-[11px]"
                  onKeyDown={(e) => e.key === "Enter" && !aiOff && addTag()}
                />
              </Tip>
              <AiDisabledTip>
                <Button aria-label={t_i18n("Add")}
                  size="sm"
                  variant="outline"
                  onClick={addTag}
                  disabled={!newKey.trim() || newKeyReserved || setTags.isPending || aiOff}
                  className="h-7 shrink-0"
                >
                  <Plus className="h-3 w-3" />
                </Button>
              </AiDisabledTip>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Evaluation-level cascading tag editor. The reserved `baseline` key is blocked:
// baseline is set per agent, not via tags.
function EvaluationTagsPanel({
  evaluationId,
  tags,
  canManage,
}: {
  evaluationId: string;
  tags: Record<string, string> | null | undefined;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateEvaluation();
  const aiOff = useAiActionDisabled();
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  const current = tags || {};
  const entries = Object.entries(current);
  const newKeyReserved = newKey.trim() === RUN_TAG_BASELINE_KEY;

  const save = (next: Record<string, string>) => {
    update.mutate(
      { id: evaluationId, tags: next },
      {
        onError: (e) =>
          toast({ title: t_i18n("Failed to update tags"), description: e.message, variant: "error" }),
      },
    );
  };

  const addTag = () => {
    const key = newKey.trim();
    if (!key || newKeyReserved) return;
    save({ ...current, [key]: newValue.trim() || "true" });
    setNewKey("");
    setNewValue("");
  };

  const removeTag = (key: string) => {
    const next = { ...current };
    delete next[key];
    save(next);
  };

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <Tag className="h-3.5 w-3.5 text-default-secondary" />
          <CardTitle className="flex items-center gap-1 text-base font-medium">
            {t_i18n("Evaluation Tags")}
            <InfoTip content={t_i18n(CONFIG_NOTES.tags)} label={t_i18n("What evaluation tags are for")} />
          </CardTitle>
        </div>
        <p className="content-compact text-default-secondary pt-1">
          {t_i18n("Applied to every run of this evaluation. Agent- and run-level tags override on key conflict.")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {entries.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {entries.map(([key, value]) => (
              <Badge key={key} variant="outline" className="gap-1 content-caption font-mono-plex">
                {`${key}: ${value}`}
                {canManage && (
                  <AiDisabledTip>
                    <button
                      onClick={() => removeTag(key)}
                      disabled={update.isPending || aiOff}
                      className="hover:text-feedback-error-primary transition-colors"
                      aria-label={`Remove tag ${key}`}
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </AiDisabledTip>
                )}
              </Badge>
            ))}
          </div>
        ) : (
          <p className="content-compact text-default-secondary">{t_i18n("No tags.")}</p>
        )}
        {canManage && (
          <div className="flex items-center gap-2">
            <Tip
              content={
                newKeyReserved
                  ? t_i18n('"baseline" is reserved: designate a baseline agent instead.')
                  : undefined
              }
            >
              <Input aria-label={t_i18n("key")}
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder={t_i18n("key")}
                className="h-8 content-compact"
                invalidTone="alert"
                aria-invalid={newKeyReserved || undefined}
                onKeyDown={(e) => e.key === "Enter" && !aiOff && addTag()}
              />
            </Tip>
            <Tip content={t_i18n(CONFIG_NOTES.tagValue)}>
              <Input aria-label={t_i18n("value (optional)")}
                value={newValue}
                onChange={(e) => setNewValue(e.target.value)}
                placeholder={t_i18n("value (optional)")}
                className="h-8 content-compact"
                onKeyDown={(e) => e.key === "Enter" && !aiOff && addTag()}
              />
            </Tip>
            <AiDisabledTip>
              <Button aria-label={t_i18n("Add")}
                size="sm"
                variant="outline"
                onClick={addTag}
                disabled={!newKey.trim() || newKeyReserved || update.isPending || aiOff}
                className="h-8 shrink-0"
              >
                <Plus className="h-3.5 w-3.5" />
              </Button>
            </AiDisabledTip>
          </div>
        )}
        {newKeyReserved && (
          <p className="content-caption text-feedback-warning-primary">
            {t_i18n('"baseline" is reserved: designate a baseline agent instead.')}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** Read-only echo of a headline's composition, keeping `[]` distinct from `null`. */
function SelectionSummary({ label, note, value }: { label: string; note: string; value: string[] | null }) {
  const { t_i18n } = useFormatter();
  return (
    <p className="content-compact">
      <span className="text-default-secondary">
        <InfoLabel content={t_i18n(note)}>{label}</InfoLabel>:{" "}
      </span>
      {value === null ? (
        <InfoLabel content={t_i18n(CONFIG_NOTES.defaultMetrics)} className="font-medium">
          {t_i18n("default metrics")}
        </InfoLabel>
      ) : value.length === 0 ? (
        <span className="font-medium text-feedback-warning-primary">{t_i18n("none: reads N/A")}</span>
      ) : (
        <span className="font-medium">
          {value.map((key, i) => (
            <Fragment key={key}>
              {i > 0 && ", "}
              <MetricName metricKey={key} />
            </Fragment>
          ))}
        </span>
      )}
    </p>
  );
}

function ConfigTab({
  evaluation,
  canManage,
}: {
  evaluation: {
    id: string;
    name: string;
    description: string | null;
    family: EvaluationFamily | null;
    execution_mode: string;
    run_mode: string;
    evaluator_model: string | null;
    evaluator_prompt: string | null;
    evaluation_instructions: string | null;
    performance_tolerance: number;
    sub_score_weights: Record<string, number> | null;
    quality_source: "judge" | "f1";
    penalty_factor: number;
    timeout_seconds: number;
    judge_max_tokens: number;
    field_scoring_overrides: FieldScoringOverrides | null;
    agent_extractors?: Record<string, ExtractorSpec> | null;
    scorer_selection: string[] | null;
    reuse_default: "off" | "on";
    apply_prompt_guards: boolean;
    require_tool_approval: boolean;
    overall_quality_metrics: string[] | null;
    overall_performance_metrics: string[] | null;
    tags?: Record<string, string> | null;
    trigger_config: { schedule: string | null; on_agent_change: boolean };
    enabled: boolean;
    use_agent_default_model: boolean;
    agents: Array<{
      id: string;
      name: string;
      slug: string | null;
      icon?: string | null;
      tags?: Record<string, string> | null;
      is_baseline?: boolean;
    }>;
    model_ids: string[];
    disabled_model_ids: string[];
    unavailable_model_ids?: string[];
    agent_ids: string[];
    placebo_config?: EvaluationPlaceboConfig | null;
  };
  canManage: boolean;
}) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [editing, setEditing] = useState(false);
  const update = useUpdateEvaluation();
  const addAgents = useAddEvaluationAgents();
  const removeAgent = useRemoveEvaluationAgent();
  // The owner's models, which its save and its judge resolve among, not the viewer's.
  const { data: availableModels } = useEvaluationAvailableModels(evaluation.id);
  const { data: familyCatalog } = useEvaluationFamilies();
  // Edit form state
  const [name, setName] = useState(evaluation.name);
  const [description, setDescription] = useState(evaluation.description || "");
  const [family, setFamily] = useState<EvaluationFamily | "">(evaluation.family ?? "");
  const [evalInstructions, setEvalInstructions] = useState(evaluation.evaluation_instructions || "");
  const [executionMode, setExecutionMode] = useState<string>(evaluationExecutionMode(evaluation.execution_mode));
  const [runMode, setRunMode] = useState<"parallel" | "sequential">((evaluation.run_mode as "parallel" | "sequential") || "parallel");
  const [evaluatorModel, setEvaluatorModel] = useState(evaluation.evaluator_model || "");
  const [scheduleEnabled, setScheduleEnabled] = useState(!!evaluation.trigger_config.schedule);
  const [schedule, setSchedule] = useState(evaluation.trigger_config.schedule || "*/5 * * * *");
  // Not offered while it starts no run; an evaluation that turned it on keeps a
  // switch to turn it off, shown while the stored value is on.
  const [onAgentChange, setOnAgentChange] = useState(evaluation.trigger_config.on_agent_change);
  const [automaticRuns, setAutomaticRuns] = useState(evaluation.enabled);
  const [useAgentDefaultModel, setUseAgentDefaultModel] = useState(evaluation.use_agent_default_model);
  const [modelIds, setModelIds] = useState<string[]>(evaluation.model_ids);
  const [disabledModelIds, setDisabledModelIds] = useState<string[]>(evaluation.disabled_model_ids || []);
  const canDisableDefaultModel = modelIds.length > 0;
  const [performanceTolerance, setPerformanceTolerance] = useState(evaluation.performance_tolerance);
  const [penaltyFactor, setPenaltyFactor] = useState(evaluation.penalty_factor ?? 0.5);
  const [timeoutSeconds, setTimeoutSeconds] = useState(evaluation.timeout_seconds);
  const [judgeMaxTokens, setJudgeMaxTokens] = useState(evaluation.judge_max_tokens ?? 8192);
  // Headline composition. `null` means "inherit every metric the family
  // measures" and is never conflated with `[]`, which composes from none (N/A).
  const { data: metricCatalog } = useEvaluationMetricCatalog(evaluation.id);
  const [qualitySelection, setQualitySelection] = useState<string[] | null>(
    evaluation.overall_quality_metrics ?? null,
  );
  const [performanceSelection, setPerformanceSelection] = useState<string[] | null>(
    evaluation.overall_performance_metrics ?? null,
  );
  // LLM Judge toggle.
  const [judgeEnabled, setJudgeEnabled] = useState(
    evaluation.scorer_selection == null ? true : evaluation.scorer_selection.includes("judge"),
  );
  const familyMeta = useMemo(
    () => (family ? familyCatalog?.find((f) => f.id === family) : undefined),
    [family, familyCatalog],
  );
  const structuredFamily = familyMeta?.structured_output ?? false;
  // The read view follows the saved family, not the draft one.
  const savedFamilyStructured = familyCatalog?.find((f) => f.id === evaluation.family)?.structured_output ?? false;
  const [reuseDefault, setReuseDefault] = useState<boolean>(evaluation.reuse_default === "on");
  const [applyPromptGuards, setApplyPromptGuards] = useState<boolean>(evaluation.apply_prompt_guards);
  const [requireToolApproval, setRequireToolApproval] = useState<boolean>(evaluation.require_tool_approval);
  // Per-field scoring intent, edited as rows rather than raw JSON: the mode
  // vocabulary is closed, so a dropdown prevents modes the server would reject.
  const [fieldOverrides, setFieldOverrides] = useState<FieldOverrideRow[]>(() =>
    overridesToRows(evaluation.field_scoring_overrides),
  );
  // Per-agent extractors, drafted like the field overrides and sent by the
  // same save. `null` for an agent clears its extractor server-side.
  const [agentExtractors, setAgentExtractors] = useState<
    Record<string, ExtractorSpec | null>
  >(() => ({ ...(evaluation.agent_extractors ?? {}) }));
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const currentModelIds = editing ? modelIds : evaluation.model_ids;
  const modelAvailableToAdd = useMemo(
    () => (availableModels ?? []).filter((m) => !currentModelIds.includes(m.model)),
    [availableModels, currentModelIds],
  );
  const selectedModels = useMemo(
    () => currentModelIds.map((mid) => {
      const av = (availableModels ?? []).find((m) => m.model === mid);
      const isDisabled = (editing ? disabledModelIds : (evaluation.disabled_model_ids || [])).includes(mid);
      const unavailable = (evaluation.unavailable_model_ids ?? []).includes(mid);
      return { model: mid, provider: av?.provider ?? "", disabled: isDisabled, unavailable };
    }),
    [availableModels, currentModelIds, editing, disabledModelIds, evaluation.disabled_model_ids, evaluation.unavailable_model_ids],
  );

  const startEditing = () => {
    setName(evaluation.name);
    setDescription(evaluation.description || "");
    setFamily(evaluation.family ?? "");
    setEvalInstructions(evaluation.evaluation_instructions || "");
    setExecutionMode(evaluationExecutionMode(evaluation.execution_mode));
    setRunMode((evaluation.run_mode as "parallel" | "sequential") || "parallel");
    setEvaluatorModel(evaluation.evaluator_model || "");
    setScheduleEnabled(!!evaluation.trigger_config.schedule);
    setSchedule(evaluation.trigger_config.schedule || "*/5 * * * *");
    setAutomaticRuns(evaluation.enabled);
    setOnAgentChange(evaluation.trigger_config.on_agent_change);
    setUseAgentDefaultModel(evaluation.use_agent_default_model);
    setModelIds([...evaluation.model_ids]);
    setDisabledModelIds([...(evaluation.disabled_model_ids || [])]);
    setPerformanceTolerance(evaluation.performance_tolerance);
    setPenaltyFactor(evaluation.penalty_factor ?? 0.5);
    setJudgeMaxTokens(evaluation.judge_max_tokens ?? 8192);
    setQualitySelection(evaluation.overall_quality_metrics ?? null);
    setPerformanceSelection(evaluation.overall_performance_metrics ?? null);
    setJudgeEnabled(evaluation.scorer_selection == null ? true : evaluation.scorer_selection.includes("judge"));
    setReuseDefault(evaluation.reuse_default === "on");
    setApplyPromptGuards(evaluation.apply_prompt_guards);
    setRequireToolApproval(evaluation.require_tool_approval);
    setFieldOverrides(overridesToRows(evaluation.field_scoring_overrides));
    // Re-seeded from the server like every other draft: agents are added and removed
    // by their own mutations, so a stale draft can name an agent that left the
    // evaluation, which the save refuses.
    setAgentExtractors({ ...(evaluation.agent_extractors ?? {}) });
    setOverrideError(null);
    setEditing(true);
  };

  const handleSave = async () => {
    try {
      await update.mutateAsync({
        id: evaluation.id,
        ...configSavePayload({
          name,
          description,
          family,
          evalInstructions,
          executionMode,
          runMode,
          evaluatorModel,
          performanceTolerance,
          penaltyFactor,
          qualitySelection,
          performanceSelection,
          timeoutSeconds,
          judgeMaxTokens,
          fieldScoringOverrides: rowsToOverrides(fieldOverrides),
          agentExtractors,
          judgeEnabled,
          reuseDefault,
          applyPromptGuards,
          requireToolApproval,
          scheduleEnabled,
          schedule,
          onAgentChange,
          automaticRuns,
          useAgentDefaultModel,
          modelIds,
          disabledModelIds,
        }),
      });
      setOverrideError(null);
      toast({ title: t_i18n("Evaluation updated"), variant: "success" });
      setEditing(false);
    } catch (err) {
      // A contradictory per-field configuration is refused with a message naming the
      // offending pointer; show it rather than a generic "Failed to update".
      const message = err instanceof Error ? err.message : "";
      if (message.toLowerCase().includes("field_")) {
        setOverrideError(message);
      }
      toast({ title: message || t_i18n("Failed to update"), variant: "error" });
    }
  };

  const handleAddAgent = async (agent: { id: string }) => {
    try {
      await addAgents.mutateAsync({ evaluationId: evaluation.id, agentIds: [agent.id] });
      toast({ title: t_i18n("Agent added"), variant: "success" });
    } catch {
      toast({ title: t_i18n("Failed to add agent"), variant: "error" });
    }
  };

  const handleRemoveAgent = async (agentId: string) => {
    try {
      await removeAgent.mutateAsync({ evaluationId: evaluation.id, agentId });
      // Drop the departing agent's draft extractor. Removal is its own mutation, so a
      // draft left behind would make the next save fail on a removed agent.
      setAgentExtractors((prev) => {
        if (!(agentId in prev)) return prev;
        const next = { ...prev };
        delete next[agentId];
        return next;
      });
      toast({ title: t_i18n("Agent removed"), variant: "success" });
    } catch {
      toast({ title: t_i18n("Failed to remove agent"), variant: "error" });
    }
  };

  // One edit control for the whole tab, outside every card: `editing` drives
  // Settings, Scoring, Agents and Models alike, so a control in one card header
  // would claim a narrower scope than it has. The save stays atomic.
  const configActions = canManage ? (
    <div className="flex items-center justify-end gap-2">
      {!editing ? (
        <Button variant="ghost" size="sm" className="gap-1.5 text-default-secondary" onClick={startEditing}>
          <Pencil className="h-3.5 w-3.5" /> {t_i18n("Edit configuration")}
        </Button>
      ) : (
        <>
          <Button variant="ghost" size="sm" className="gap-1 text-default-secondary" onClick={() => setEditing(false)}>
            <X className="h-3.5 w-3.5" /> {t_i18n("Cancel")}
          </Button>
          <Button size="sm" className="gap-1" onClick={handleSave} disabled={update.isPending || aiOff} aria-busy={update.isPending || undefined}>
            {update.isPending && <Spinner size="sm" tone="inherit" />}
            {!update.isPending && <Check className="h-3.5 w-3.5" />} {t_i18n("Save")}
          </Button>
        </>
      )}
    </div>
  ) : null;

  // Judge state as stored, for the read-only view. `null` means "never narrowed"
  // (judge on); `[]` is an explicit opt-out and reads as Disabled.
  const judgeConfigured =
    evaluation.scorer_selection == null ? true : evaluation.scorer_selection.includes("judge");

  return (
    <div className="space-y-6">
      {configActions}

      {/* Settings — what a run does. */}
      <EvaluationSettingsCard
        editing={editing}
        name={name}
        onNameChange={setName}
        description={description}
        onDescriptionChange={setDescription}
        executionMode={executionMode}
        onExecutionModeChange={setExecutionMode}
        runMode={runMode}
        onRunModeChange={setRunMode}
        timeoutSeconds={timeoutSeconds}
        onTimeoutSecondsChange={setTimeoutSeconds}
        scheduleEnabled={scheduleEnabled}
        onScheduleEnabledChange={setScheduleEnabled}
        schedule={schedule}
        onScheduleChange={setSchedule}
        automaticRuns={automaticRuns}
        // Shown while the draft or the stored value is on: the save sends the draft,
        // so a value it would write is never hidden (another user may turn it off meanwhile).
        onAgentChangeStored={onAgentChange || evaluation.trigger_config.on_agent_change ? onAgentChange : undefined}
        onOnAgentChangeStoredChange={onAgentChange || evaluation.trigger_config.on_agent_change ? setOnAgentChange : undefined}
        onAutomaticRunsChange={setAutomaticRuns}
        reuseDefault={reuseDefault}
        onReuseDefaultChange={setReuseDefault}
        applyPromptGuards={applyPromptGuards}
        requireToolApproval={requireToolApproval}
        agentIds={evaluation.agents.map((a) => a.id)}
        onRequireToolApprovalChange={setRequireToolApproval}
        onApplyPromptGuardsChange={setApplyPromptGuards}
        evaluationSnapshot={{
          execution_mode: evaluation.execution_mode,
          run_mode: evaluation.run_mode,
          timeout_seconds: evaluation.timeout_seconds,
          schedule: evaluation.trigger_config.schedule,
          automatic_runs: evaluation.enabled,
          reuse_default: evaluation.reuse_default,
          apply_prompt_guards: evaluation.apply_prompt_guards,
          require_tool_approval: evaluation.require_tool_approval,
        }}
      />

      {/* Scoring — what is measured, then how it is reduced into headlines. */}
      <EvaluationScoringCard
        editing={editing}
        familyCatalog={familyCatalog}
        family={family}
        onFamilyChange={setFamily}
        judgeEnabled={judgeEnabled}
        onJudgeEnabledChange={setJudgeEnabled}
        performanceTolerance={performanceTolerance}
        onPerformanceToleranceChange={setPerformanceTolerance}
        penaltyFactor={penaltyFactor}
        onPenaltyFactorChange={setPenaltyFactor}
        judgeExtra={
          <div className="space-y-4 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className={cn(CONFIG_HEADING, "mb-1")}>
                  {t_i18n("Evaluator Model")}
                  <InfoTip content={t_i18n(CONFIG_NOTES.evaluatorModel)} label={t_i18n("Which model the LLM judge runs on")} />
                </p>
                <SearchableSelect
                  aria-label={t_i18n("Evaluator Model")}
                  options={(availableModels ?? []).map((m) => ({ value: m.model, label: m.model, keywords: [m.provider] }))}
                  pinned={[{ value: "__default__", label: t_i18n("Platform default") }]}
                  value={evaluatorModel || "__default__"}
                  onValueChange={(v) => setEvaluatorModel(v === "__default__" ? "" : v)}
                  placeholder={t_i18n("Search models...")}
                />
              </div>
              <div>
                <p className={cn(CONFIG_HEADING, "mb-1")}>
                  {t_i18n("Judge Output Budget (tokens)")}
                  <InfoTip content={t_i18n(EVALUATION_TERMS.judgeOutputBudget)} label={t_i18n("What the judge output budget limits")} />
                </p>
                <Input aria-label={t_i18n("Judge Output Budget (tokens)")}
                  type="number"
                  min={256}
                  max={131072}
                  step={256}
                  value={judgeMaxTokens}
                  onChange={(e) => {
                    const parsed = parseInt(e.target.value, 10);
                    if (!Number.isNaN(parsed)) {
                      setJudgeMaxTokens(Math.min(131072, Math.max(256, parsed)));
                    }
                  }}
                  className="h-9 font-mono-plex"
                />
              </div>
            </div>
            <div>
              <p className={cn(CONFIG_HEADING, "mb-1")}>
                {t_i18n("Judge Instructions")}
                <InfoTip content={t_i18n(EVALUATION_TERMS.judgeInstructionsEvaluation)} label={t_i18n("When the evaluation's judge instructions apply")} />
              </p>
              <textarea
                aria-label={t_i18n("Judge Instructions")}
                value={evalInstructions}
                onChange={(e) => setEvalInstructions(e.target.value)}
                rows={3}
                className={`w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base placeholder:text-default-secondary resize-y ${FIELD_FOCUS_RING}`}
                placeholder={t_i18n("Global instructions for the LLM judge. Per-entry instructions override this when set.")}
              />
            </div>
          </div>
        }
        extraContent={
          /* Per-field scoring intent. Lives on the evaluation, not the
             dataset, which owns the answer's shape: two evaluations may share a
             dataset and score its fields differently. */
          structuredFamily ? (
            <div className="space-y-2 pt-3 border-t border-elevation-subtle-soft">
              <div>
                <p className="content-base font-medium">{t_i18n("Per-Field Scoring")}</p>
                <p className="text-[11px] text-default-secondary mt-0.5">
                  {t_i18n("Each field is scored according to its own schema declaration. Add a row only for a field whose default treatment is wrong for this evaluation.")}
                </p>
              </div>
              {fieldOverrides.length > 0 && (
                <div className="space-y-2">
                  {fieldOverrides.map((row, index) => (
                    <div key={index} className="flex items-center gap-2">
                      <Tip content={t_i18n(CONFIG_NOTES.fieldPointer)}>
                        <Input aria-label={t_i18n("JSON Pointer")}
                          className="flex-1 [&_input]:font-mono-plex"
                          value={row.pointer}
                          placeholder="/findings/0/severity"
                          onChange={(e) => setFieldOverrides((rows) =>
                            rows.map((r, i) => (i === index ? { ...r, pointer: e.target.value } : r)))}
                        />
                      </Tip>
                      <Select
                        value={row.mode}
                        onValueChange={(value) => setFieldOverrides((rows) =>
                          // Radix hands back a bare string; the items below are exactly
                          // this union, so the narrowing is a fact about this Select.
                          rows.map((r, i) =>
                            (i === index ? { ...r, mode: value as FieldOverrideRow["mode"] } : r)))}
                      >
                        <SelectTrigger className="w-[240px]" aria-label={t_i18n("Field mode")}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="derived">{t_i18n(FIELD_MODE_LABELS.derived)}</SelectItem>
                          {FIELD_SCORING_MODES.map((mode) => (
                            <SelectItem key={mode} value={mode}>{t_i18n(FIELD_MODE_LABELS[mode])}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <InfoTip
                        content={t_i18n(FIELD_MODE_DESCRIPTIONS[row.mode])}
                        label={t_i18n("What this field mode does")}
                      />
                      <Tip content={t_i18n(CONFIG_NOTES.fieldWeight)}>
                        <Input aria-label={t_i18n("weight")}
                          className="w-[90px]"
                          value={row.weight}
                          placeholder={t_i18n("weight")}
                          inputMode="decimal"
                          onChange={(e) => setFieldOverrides((rows) =>
                            rows.map((r, i) => (i === index ? { ...r, weight: e.target.value } : r)))}
                        />
                      </Tip>
                      <Tip content={t_i18n("Remove row")}>
                        <Button aria-label={t_i18n("Remove row")}
                          variant="ghost"
                          size="icon"
                          onClick={() => setFieldOverrides((rows) => rows.filter((_, i) => i !== index))}
                        >
                          <X className="h-3.5 w-3.5" />
                        </Button>
                      </Tip>
                    </div>
                  ))}
                </div>
              )}
              <Button
                variant="outline"
                size="sm"
                className="h-8"
                onClick={() => setFieldOverrides((rows) => [...rows, { pointer: "", mode: "derived", weight: "" }])}
              >
                <Plus className="h-3.5 w-3.5 mr-1.5" />
                {t_i18n("Add Field Override")}
              </Button>
              {overrideError && (
                <div className="rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/[0.06] px-3 py-2">
                  <p className="content-compact text-feedback-error-primary whitespace-pre-wrap break-words">{overrideError}</p>
                </div>
              )}
            </div>
          ) : null
        }
        aggregationExtra={
          <OverallScoreComposition
            catalog={metricCatalog ?? []}
            quality={qualitySelection}
            performance={performanceSelection}
            onQualityChange={setQualitySelection}
            onPerformanceChange={setPerformanceSelection}
          />
        }
        evaluationSnapshot={{
          family: evaluation.family,
          penalty_factor: evaluation.penalty_factor,
          performance_tolerance: evaluation.performance_tolerance,
          sub_score_weights: evaluation.sub_score_weights,
        }}
        judgeConfigured={judgeConfigured}
        readOnlyJudgeExtra={
          <>
            <div>
              <p className={cn(CONFIG_HEADING, "mb-1.5")}>
                {t_i18n("Evaluator Model")}
                <InfoTip content={t_i18n(CONFIG_NOTES.evaluatorModel)} label={t_i18n("Which model the LLM judge runs on")} />
              </p>
              <p className="content-base font-medium">{evaluation.evaluator_model || t_i18n("Platform default")}</p>
            </div>
            <div>
              <p className={cn(CONFIG_HEADING, "mb-1.5")}>
                {t_i18n("Judge Output Budget")}
                <InfoTip content={t_i18n(EVALUATION_TERMS.judgeOutputBudget)} label={t_i18n("What the judge output budget limits")} />
              </p>
              <p className="content-base font-medium font-mono-plex">{evaluation.judge_max_tokens ?? 8192} {t_i18n("tokens")}</p>
            </div>
          </>
        }
        readOnlyExtraContent={
          evaluation.evaluation_instructions ? (
            <div>
              <p className={cn(CONFIG_HEADING, "mb-1.5")}>
                {t_i18n("Judge Instructions")}
                <InfoTip content={t_i18n(EVALUATION_TERMS.judgeInstructionsEvaluation)} label={t_i18n("When the evaluation's judge instructions apply")} />
              </p>
              <p className="content-base text-default-secondary whitespace-pre-line">{evaluation.evaluation_instructions}</p>
            </div>
          ) : null
        }
        readOnlyFieldScoring={
          savedFamilyStructured ? <SavedFieldScoring overrides={evaluation.field_scoring_overrides} /> : null
        }
        readOnlyAggregationExtra={
          <div className="col-span-2">
            <p className={cn(CONFIG_HEADING, "mb-1.5")}>
              {t_i18n("Overall Score Composition")}
              <InfoTip content={t_i18n(CONFIG_NOTES.composition)} label={t_i18n("What the overall score composition sets")} />
            </p>
            <div className="space-y-1">
              <SelectionSummary label={t_i18n(HEADLINE_LABELS.quality)} note={HEADLINE_NOTES.quality} value={evaluation.overall_quality_metrics} />
              <SelectionSummary label={t_i18n(HEADLINE_LABELS.performance)} note={HEADLINE_NOTES.performance} value={evaluation.overall_performance_metrics} />
            </div>
          </div>
        }
      />

      {/* Tags */}
      <EvaluationTagsPanel
        evaluationId={evaluation.id}
        tags={evaluation.tags}
        canManage={canManage}
      />

      <PlaceboConfigCard evaluation={evaluation} canManage={canManage} />

      {/* Agents */}
      <EvaluationAgentPickerCard
        agents={evaluation.agents}
        boundIds={evaluation.agent_ids}
        canManage={canManage}
        editing={editing}
        onAdd={handleAddAgent}
        onRemove={handleRemoveAgent}
      />

      {/* Agent tags & baseline */}
      <AgentTagsBaselinePanel
        agents={evaluation.agents}
        evaluationId={evaluation.id}
        canManage={canManage}
      />

      {/* Per-agent output extraction */}
      <EvaluationExtractorCard
        target={{ evaluationId: evaluation.id }}
        outputKinds={familyCatalog?.find((entry) => entry.id === family)?.extractor_output_kinds}
        agents={evaluation.agents}
        extractors={agentExtractors}
        canManage={canManage && editing}
        onChange={(patch) =>
          setAgentExtractors((current) => ({ ...current, ...patch }))
        }
      />

      {/* Models */}
      <EvaluationModelPickerCard
        editing={editing}
        useAgentDefaultModel={useAgentDefaultModel}
        onUseAgentDefaultModelChange={setUseAgentDefaultModel}
        canDisableDefaultModel={canDisableDefaultModel}
        selectedModels={selectedModels}
        availableModelsToAdd={modelAvailableToAdd}
        onAddModel={(model) => {
          setModelIds((prev) => [...prev, model]);
          setDisabledModelIds((prev) => prev.filter((mid) => mid !== model));
        }}
        onRemoveModel={(model) => {
          setModelIds((prev) => {
            const next = prev.filter((id) => id !== model);
            if (next.length === 0) setUseAgentDefaultModel(true);
            return next;
          });
          setDisabledModelIds((prev) => prev.filter((mid) => mid !== model));
        }}
        evaluationUseAgentDefaultModel={evaluation.use_agent_default_model}
        modelRowExtra={(m) => (
          <div className="flex items-center gap-1.5 shrink-0">
            <ModelActiveLabel className="content-caption text-default-secondary">
              {t_i18n("Active")}
            </ModelActiveLabel>
            <Switch aria-label={t_i18n("Active")}
              checked={!m.disabled}
              onCheckedChange={(checked) => {
                setDisabledModelIds((prev) => {
                  if (checked) {
                    return prev.filter((mid) => mid !== m.model);
                  }
                  if (prev.includes(m.model)) {
                    return prev;
                  }
                  return [...prev, m.model];
                });
              }}
            />
          </div>
        )}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function EvaluationDetail() {
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  const { data: evaluation, isLoading } = useEvaluation(id || "");
  const deleteEvaluation = useDeleteEvaluation();
  const trigger = useTriggerEvaluation();
  const triggerFailed = useTriggerFailedToast();
  const { data: activeRuns } = useEvaluationRuns(id || "", 10, 0, { refetchInterval: REFETCH_INTERVAL_FAST });
  const isRunning = activeRuns?.some((r) => r.status === "running" || r.status === "pending") ?? false;
  /* An evaluation drives models, so it cannot start with AI off. */
  const [tab] = useTabParam<Tab>("overview");
  const [ConfirmEl, confirm] = useConfirm();
  const [runMenuOpen, setRunMenuOpen] = useState(false);
  // The owner's models, the extra ones a launch accepts: read when the menu opens.
  const { data: availableModels = [] } = useEvaluationAvailableModels(id, { enabled: runMenuOpen });
  const [pendingModelId, setPendingModelId] = useState<string | null>(null);
  const [pickedModelIds, setPickedModelIds] = useState<string[]>([]);
  // The placebo comparison dialog, with the models it runs on (all enabled ones when unset).
  const [placeboScope, setPlaceboScope] = useState<{ modelIds?: string[]; includeDefault?: boolean } | null>(null);
  const runMenuRef = useRef<HTMLDivElement>(null);

  useClickOutside(runMenuRef, () => setRunMenuOpen(false), runMenuOpen);

  if (isLoading) return <DetailPageSkeleton backTo="/evaluations" backLabel="Back to Evaluations" tabs={4} descriptionLines={1} actions={2} />;
  if (!evaluation) {
    return (
      <NotFoundState
        icon={FlaskConical}
        title="Evaluation not found"
        description="This evaluation may have been deleted or you don't have access."
        actionLabel="View All Evaluations"
        onAction={() => navigate("/evaluations")}
        backTo="/evaluations"
        backLabel="Back to Evaluations"
      />
    );
  }

  const canManage = canManageEntity(evaluation, user?.id, isAdmin);
  // Family-less experiments stay viewable but cannot be re-run until a family is
  // set; the server enforces this fail-closed, and the UI blocks Run and
  // explains why.
  const familyMissing = !evaluation.family;
  // Either principal kind counts as "shared with me" — a viewer reached
  // by a direct grant is as much a guest here as one reached through a group.
  const isShared =
    !evaluation.is_company_managed &&
    !!evaluation.user_id &&
    evaluation.user_id !== user?.id &&
    (!!evaluation.shared_group_ids?.length || !!evaluation.shared_user_ids?.length);

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "dataset", label: "Dataset", icon: Database },
    { key: "config", label: "Configuration", icon: Settings2 },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  const enabledModels = (evaluation.model_ids || []).filter(
    (mid) => !(evaluation.disabled_model_ids || []).includes(mid),
  );
  const unavailableModelIds = new Set(evaluation.unavailable_model_ids ?? []);
  // A model flagged after it was picked leaves the selection: Run Selected would be refused.
  const selectedModelIds = pickedModelIds.filter((mid) => !unavailableModelIds.has(mid));
  const extraSelectedModels = selectedModelIds.filter(
    (mid) => mid !== "__default__" && !enabledModels.includes(mid),
  );
  const runOnModels = [...enabledModels, ...extraSelectedModels];
  const configuredModelIds = new Set(evaluation.model_ids || []);
  const uniqueAvailableModels = new Map<string, { model: string; provider: string }>();
  for (const item of availableModels) {
    if (
      !configuredModelIds.has(item.model)
      && !selectedModelIds.includes(item.model)
      && !uniqueAvailableModels.has(item.model)
    ) {
      uniqueAvailableModels.set(item.model, { model: item.model, provider: item.provider });
    }
  }
  const availableModelsToAdd = Array.from(uniqueAvailableModels.values()).sort((a, b) =>
    a.model.localeCompare(b.model),
  );
  // Run's tooltip says why it is disabled, first cause first; `wrap` keeps it reachable.
  const runDisabled = trigger.isPending || isRunning || aiOff || familyMissing;
  const runTip = aiOff
    ? aiReason
    : familyMissing
      ? t_i18n(PAGE_NOTES.familyMissing)
      : isRunning
        ? t_i18n(PAGE_NOTES.runInProgress)
        : t_i18n(EVALUATION_TERMS.runEvaluation);
  const runOptionsDisabled = trigger.isPending || isRunning || familyMissing;
  const runOptionsTip = familyMissing
    ? t_i18n(PAGE_NOTES.familyMissing)
    : isRunning
      ? t_i18n(PAGE_NOTES.runInProgress)
      : t_i18n(PAGE_NOTES.runOptions);

  const toggleModelSelection = (modelId: string) => {
    setPickedModelIds((prev) =>
      prev.includes(modelId) ? prev.filter((id) => id !== modelId) : [...prev, modelId],
    );
  };
  const clearSelection = () => setPickedModelIds([]);

  const handleTrigger = async (modelIds?: string[], includeDefault?: boolean) => {
    try {
      const runs = await trigger.mutateAsync({ evaluationId: id!, modelIds, includeDefault });
      toast({ title: t_i18n("Evaluation triggered"), description: t_i18n("{count, plural, one {# run} other {# runs}} started.", { count: runs.length }), variant: "success" });
      clearSelection();
      setRunMenuOpen(false);
    } catch (error) {
      triggerFailed(error);
    }
  };

  const handleTriggerSelected = async () => {
    if (selectedModelIds.length === 0) return;
    setPendingModelId("__selected__");
    try {
      const includeDefault = selectedModelIds.includes("__default__");
      const additionalModelIds = selectedModelIds.filter((id) => id !== "__default__");
      const runs = await trigger.mutateAsync({
        evaluationId: id!,
        modelIds: additionalModelIds,
        includeDefault,
      });
      toast({
        title: t_i18n("Evaluation triggered"),
        description: t_i18n("{runs, plural, one {# run} other {# runs}} started on {models, plural, one {# model} other {# models}}.", {
          runs: runs.length,
          models: selectedModelIds.length,
        }),
        variant: "success",
      });
      clearSelection();
      setRunMenuOpen(false);
    } catch (error) {
      triggerFailed(error);
    } finally {
      setPendingModelId(null);
    }
  };

  const handleAddAvailableModelSelection = (modelId: string) => {
    setPickedModelIds((prev) => {
      const next = prev.includes(modelId) ? prev : [...prev, modelId];
      if (evaluation.use_agent_default_model && !next.includes("__default__")) {
        return ["__default__", ...next];
      }
      return next;
    });
    toast({
      title: t_i18n("Model added to selection"),
      description: evaluation.use_agent_default_model
        ? t_i18n("Default model + selected model ready. Click Run Selected.")
        : t_i18n("Selected model ready. Click Run Selected."),
      variant: "info",
    });
  };

  const handleDelete = async () => {
    if (
      !(await confirm({
        title: "Delete evaluation?",
        description: "This action cannot be undone. All runs and results will be deleted.",
        variant: "danger",
      }))
    ) {
      return;
    }

    try {
      await deleteEvaluation.mutateAsync(id!);
      toast({
        title: t_i18n("Evaluation deleted"),
        description: t_i18n("The evaluation has been deleted successfully."),
        variant: "success",
      });
      navigate("/evaluations");
    } catch (error) {
      toast({
        title: t_i18n("Failed to delete evaluation"),
        description: error instanceof Error ? error.message : t_i18n("Unknown error"),
        variant: "error",
      });
    }
  };

  const handleExport = async () => {
    try {
      await exportEvaluation(id!, evaluation.name);
      toast({
        title: t_i18n("Evaluation exported"),
        description: t_i18n("Bundle downloaded successfully."),
        variant: "success",
      });
    } catch (error) {
      toast({
        title: t_i18n("Export failed"),
        description: error instanceof Error ? error.message : t_i18n("Unknown error"),
        variant: "error",
      });
    }
  };

  return (
    <div className="space-y-6">
      {ConfirmEl}
      <EntityPageHeader
        backTo="/evaluations"
        backLabel="Back to Evaluations"
        icon={FlaskConical}
        title={evaluation.name}
        description={evaluation.description}
        badges={
          <>
            <AutomaticRunsPausedBadge evaluation={evaluation} className="content-compact" />
            {/* The badges swallow a Tip's trigger props: InfoLabel is the trigger. */}
            {evaluation.is_company_managed && (
              <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.companyManagedEvaluation)}>
                <CompanyManagedBadge />
              </InfoLabel>
            )}
            {isShared && (
              <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.sharedEvaluation)}>
                <SharedBadge />
              </InfoLabel>
            )}
          </>
        }
        actions={
          <>
            {isRunning && (
              <Badge variant="cyan" className="content-caption gap-1 animate-pulse">
                <Spinner size="sm" tone="inherit" />
                {t_i18n("Evaluating...")}
              </Badge>
            )}
            <Tip content={t_i18n(PAGE_NOTES.export)}>
              <Button aria-label={t_i18n("Export evaluation bundle")}
                variant="outline"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={handleExport}
              >
                <Download className="h-3.5 w-3.5" />
              </Button>
            </Tip>
            <div className="relative" ref={runMenuRef}>
              <div className="flex items-center overflow-hidden rounded-sm border border-accent/20">
                <Tip content={runTip} wrap="auto">
                  <button
                    className="flex h-8 items-center gap-1.5 px-3 content-base font-medium text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
                    onClick={() => handleTrigger()}
                    disabled={runDisabled}
                    aria-busy={(trigger.isPending && pendingModelId === null) || undefined}
                  >
                    {trigger.isPending && pendingModelId === null ? (
                      <><Spinner size="sm" tone="inherit" /> {t_i18n("Triggering...")}</>
                    ) : (
                      <><Play className="h-3.5 w-3.5" /> {t_i18n("Run")}</>
                    )}
                  </button>
                </Tip>
                <Tip content={runOptionsTip} wrap="auto">
                  <button
                    className="flex h-8 w-7 items-center justify-center text-accent hover:bg-accent/10 transition-colors disabled:opacity-40 border-l border-accent/20"
                    onClick={() => {
                      if (!runMenuOpen) clearSelection();
                      setRunMenuOpen((v) => !v);
                    }}
                    disabled={runOptionsDisabled}
                    aria-label={t_i18n("Open run options")}
                    aria-expanded={runMenuOpen}
                  >
                    <ChevronDown className="h-3.5 w-3.5" />
                  </button>
                </Tip>
              </div>

              {runMenuOpen && (
                <div
                  className="absolute right-0 z-50 mt-1 w-80 overflow-hidden rounded-sm border border-accent/20 layer-1 bg-elevation-default shadow-overlay backdrop-blur-sm"
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="border-b border-elevation-subtle-soft px-3 py-2.5">
                    <Tip
                      content={aiOff ? aiReason : familyMissing ? t_i18n(PAGE_NOTES.familyMissing) : t_i18n(EVALUATION_TERMS.runAll)}
                      wrap="auto"
                      wrapClassName="inline-flex w-full"
                    >
                      <button
                        className="w-full rounded-sm bg-accent/15 px-3 py-2 text-left content-compact font-medium text-accent hover:bg-accent/20 transition-colors disabled:opacity-40"
                        onClick={() => handleTrigger()}
                        disabled={trigger.isPending || aiOff || familyMissing}
                      >
                        {t_i18n("Run All")}
                      </button>
                    </Tip>
                    <Tip
                      content={
                        aiOff
                          ? aiReason
                          : familyMissing
                            ? t_i18n(PAGE_NOTES.familyMissing)
                            : evaluation.placebo_config
                              ? t_i18n("Run every agent next to its placebo, on the selected models or all of them.")
                              : t_i18n("Set up a placebo control in the Configuration tab first.")
                      }
                      wrap="auto"
                      wrapClassName="inline-flex w-full"
                    >
                      <button
                        className="mt-1.5 flex w-full items-center gap-1.5 rounded-sm border border-filigran-ia-primary/25 px-3 py-2 text-left content-compact font-medium text-filigran-ia-primary hover:bg-filigran-ia-primary/10 transition-colors disabled:opacity-40"
                        onClick={() => {
                          setPlaceboScope(
                            selectedModelIds.length > 0
                              ? {
                                  modelIds: selectedModelIds.filter((mid) => mid !== "__default__"),
                                  includeDefault: selectedModelIds.includes("__default__"),
                                }
                              : {},
                          );
                          setRunMenuOpen(false);
                        }}
                        disabled={trigger.isPending || familyMissing || aiOff || !evaluation.placebo_config}
                      >
                        <FlaskConical className="h-3.5 w-3.5" />
                        {selectedModelIds.length > 0
                          ? t_i18n("Run placebo comparison on selected ({count})", { count: selectedModelIds.length })
                          : t_i18n("Run placebo comparison")}
                      </button>
                    </Tip>
                  </div>

                  <div className="px-3 pb-3 pt-2">
                    <div className="mb-1.5 flex items-center justify-between">
                      <p className="content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Run on :")}</p>
                      <span className="content-caption text-default-secondary">{t_i18n("{count} selected", { count: selectedModelIds.length })}</span>
                    </div>
                    <div className="max-h-44 overflow-y-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default">
                      {!evaluation.use_agent_default_model && runOnModels.length === 0 ? (
                        <p className="px-2 py-2 text-[11px] text-default-secondary">{t_i18n("No enabled models.")}</p>
                      ) : (
                        <>
                          {evaluation.use_agent_default_model && (() => {
                            const isSelected = selectedModelIds.includes("__default__");
                            return (
                              <Tip content={`${t_i18n(EVALUATION_TERMS.agentDefaultModel)} ${t_i18n(EVALUATION_TERMS.defaultModelInSelection)}`}>
                              <button
                                aria-pressed={isSelected}
                                className="flex w-full items-center gap-2 border-b border-elevation-subtle-soft px-2 py-1.5 text-left content-compact hover:bg-accent/10 transition-colors"
                                onClick={() => toggleModelSelection("__default__")}
                                disabled={trigger.isPending || pendingModelId === "__selected__"}
                              >
                                <span className={isSelected ? "flex h-4 w-4 items-center justify-center rounded-sm border border-accent/50 bg-accent/20 text-accent" : "flex h-4 w-4 items-center justify-center rounded-sm border border-elevation-subtle-soft"}>
                                  {isSelected ? <Check className="h-2.5 w-2.5" /> : null}
                                </span>
                                <span className="truncate italic text-default-secondary/80">{t_i18n("Agent default model")}</span>
                              </button>
                              </Tip>
                            );
                          })()}
                          {runOnModels.map((modelId) => (
                            <RunMenuModelRow
                              key={modelId}
                              modelId={modelId}
                              selected={selectedModelIds.includes(modelId)}
                              unavailable={unavailableModelIds.has(modelId)}
                              disabled={trigger.isPending || pendingModelId === "__selected__"}
                              onToggle={() => toggleModelSelection(modelId)}
                            />
                          ))}
                        </>
                      )}
                    </div>

                    {availableModelsToAdd.length > 0 && (
                      <div className="mt-2">
                        <div className="mb-1.5 flex items-center justify-between">
                          <p className="flex items-center gap-1 content-caption font-medium uppercase tracking-wider text-default-secondary">
                            {t_i18n("Available Models")}
                            <InfoTip label={t_i18n("What adding an available model does")} content={t_i18n(EVALUATION_TERMS.availableModels)} />
                          </p>
                          <span className="content-caption text-default-secondary">{t_i18n("Click to add to selection")}</span>
                        </div>
                        <div className="max-h-36 overflow-y-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default">
                          {availableModelsToAdd.map((m) => (
                            <button
                              key={m.model}
                              className="flex w-full items-center justify-between gap-2 border-b border-elevation-subtle-soft px-2 py-1.5 text-left content-compact hover:bg-accent/10 transition-colors last:border-b-0"
                              onClick={() => handleAddAvailableModelSelection(m.model)}
                              disabled={trigger.isPending || isRunning}
                            >
                              <span className="min-w-0 flex-1">
                                <span className="block truncate font-mono-plex">{m.model}</span>
                                <span className="block truncate content-caption text-default-secondary">{PROVIDER_DISPLAY_NAMES[m.provider] || m.provider}</span>
                              </span>
                              <Plus className="h-3.5 w-3.5 text-accent" />
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    <div className="mt-2 flex items-center gap-2">
                      <AiDisabledTip wrapClassName="flex flex-1">
                        <button
                          className="flex-1 rounded-sm bg-accent px-3 py-2 content-compact font-medium text-negative-primary hover:bg-accent/90 transition-colors disabled:opacity-40"
                          onClick={handleTriggerSelected}
                          disabled={selectedModelIds.length === 0 || trigger.isPending || aiOff}
                          aria-busy={pendingModelId === "__selected__" || undefined}
                        >
                          {pendingModelId === "__selected__" ? (
                            <span className="flex items-center justify-center gap-1.5">
                              <Spinner size="sm" tone="inherit" />
                              {t_i18n("Running...")}
                            </span>
                          ) : (
                            t_i18n("Run Selected ({count})", { count: selectedModelIds.length })
                          )}
                        </button>
                      </AiDisabledTip>
                      <button
                        className="rounded-sm px-2.5 py-2 content-compact text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors disabled:opacity-40"
                        onClick={clearSelection}
                        disabled={selectedModelIds.length === 0 || trigger.isPending}
                      >
                        {t_i18n("Clear")}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
            {canManage && (
              <Tip content={aiOff ? aiReason : t_i18n("Delete evaluation")} wrap="auto">
                <Button
                  aria-label={t_i18n("Delete evaluation")}
                  variant="ghost"
                  size="sm"
                  className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                  onClick={handleDelete}
                  disabled={deleteEvaluation.isPending || aiOff}
                  aria-busy={deleteEvaluation.isPending || undefined}
                >
                  {deleteEvaluation.isPending ? (
                    <Spinner size="sm" tone="inherit" label={t_i18n("Delete evaluation")} />
                  ) : (
                    <Trash2 className="h-4 w-4" />
                  )}
                </Button>
              </Tip>
            )}
          </>
        }
      />

      {familyMissing && (
        <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-4 py-3 content-base text-feedback-warning-tertiary">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" />
          <div>
            <p className="font-medium">{t_i18n("This evaluation has no output family set")}</p>
            <p className="text-feedback-warning-primary">
              {canManage
                ? t_i18n("Select an output family in the Configuration tab before running it. Existing runs remain visible below.")
                : t_i18n("An administrator must select an output family before it can be run. Existing runs remain visible below.")}
            </p>
          </div>
        </div>
      )}

      <PlaceboRunDialog
        open={placeboScope !== null}
        onOpenChange={(open) => { if (!open) setPlaceboScope(null); }}
        evaluationId={id!}
        modelIds={placeboScope?.modelIds}
        includeDefault={placeboScope?.includeDefault}
        onTriggered={clearSelection}
      />

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {/* Tab Content */}
      <div>
        {tab === "overview" && <OverviewTab evaluationId={id!} />}
        {tab === "activity" && <ActivityTab evaluationId={id!} />}
        {tab === "dataset" && <DatasetsTab evaluationId={id!} canManage={canManage} evaluationFamily={evaluation.family} />}
        {tab === "config" && <ConfigTab evaluation={evaluation} canManage={canManage} />}
      </div>
    </div>
  );
}
