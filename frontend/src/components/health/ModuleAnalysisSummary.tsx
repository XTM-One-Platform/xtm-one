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
 * The outcome of a module's analysis on its Platform Health card (#961, dev-docs/platform-health.md
 * section 6): the first line of the routing model's summary, expandable to the whole text, with the
 * time it was written. A module with no open finding shows nothing, and its card no footer at all
 * (the overview asks analysisRowFor before passing one); a refused or failed analysis shows one
 * muted line saying why. The whole summary opens in a popover, never in place: a card of the grid
 * keeps its height (UX round 1 B2 on #965). Analyse now sits in the card's actions menu, disabled and
 * explained while AI or the analysis is off. Administrators only: the overview mounts it for the
 * "all" scope. One AI block per card: the Module Analyst's line joins this block, never a second one.
 */
import { useLayoutEffect, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { InfoPopover } from "@/components/ui/info-popover";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useRunHealthAnalysis } from "@/hooks/api/useHealthApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { HEALTH_MODULE_LABELS } from "@/lib/health-modules";
import type { HealthAnalysisOverview, HealthModule, HealthModuleAnalysis } from "@/types";

/** The AI glyph, rendered by the library <Icon name> (no direct lucide-react import). */
function SparklesGlyph({ className }: { className?: string }) {
  return <Icon name="sparkles" size={16} className={className} aria-hidden />;
}

/** What each refusal or failure reason means to an administrator. @i18n-keys */
const ANALYSIS_REASON_LABELS = {
  disabled: "Analysis is off.",
  ai_off: "AI is off on this platform.",
  ceiling_not_set: "Ceiling not set: a cleared ceiling refuses every analysis.",
  ceiling_reached: "Ceiling reached: this analysis would cross a daily or monthly ceiling.",
  no_model: "No routing model is configured.",
  no_price: "The routing model has no price, so its cost cannot be held under the ceiling.",
  model_error: "The model call failed. It is tried again tomorrow, or now with Analyse now.",
} as const;

function analysisReasonLabel(reason: string | null, t: (key: string) => string): string {
  return t((ANALYSIS_REASON_LABELS as Record<string, string>)[reason ?? ""] ?? ANALYSIS_REASON_LABELS.model_error);
}

/**
 * The analysis row a module card shows, or null when the card shows no AI block: no open finding,
 * the analysis off or not loaded, or no row for the module. The overview passes a card footer only
 * when this answers a row, so a card with nothing to say carries no empty divider (UX round 1 B1).
 */
export function analysisRowFor(
  module: HealthModule,
  hasOpenFindings: boolean,
  overview: HealthAnalysisOverview | undefined,
): HealthModuleAnalysis | null {
  if (!hasOpenFindings || !overview?.enabled) return null;
  return overview.modules.find((row) => row.module === module) ?? null;
}

/** The first non-empty line of a summary: its headline, the first fix. */
export function summaryHeadline(summary: string): string {
  return summary.split("\n").map((line) => line.trim()).find(Boolean) ?? "";
}

/**
 * Whether the clamped headline is cut: measured after layout and again on every resize, so a
 * one-line summary too long for two lines still opens the whole text (UX round 3 B4 on #965).
 */
function useClampCut(text: string) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => setCut(node.scrollHeight > node.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [text]);
  return { ref, cut };
}

/** Why Analyse now is not offered, in plain words; null when it may run. */
function runBlockedReason(overview: HealthAnalysisOverview | undefined, aiOff: boolean, t: (key: string) => string): string | null {
  if (aiOff) return t(ANALYSIS_REASON_LABELS.ai_off);
  if (!overview?.enabled) return t(ANALYSIS_REASON_LABELS.disabled);
  if (!overview.ceilings_set) return t(ANALYSIS_REASON_LABELS.ceiling_not_set);
  return null;
}

/** The "Analyse now" item of a module card's AI block menu, with its pending and failed state. */
export function useAnalyseNowItem(module: HealthModule, overview: HealthAnalysisOverview | undefined) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const run = useRunHealthAnalysis();
  const blocked = runBlockedReason(overview, aiOff, t_i18n);
  const item: OverflowMenuItem = {
    id: `health-analysis-run-${module}`,
    label: t_i18n("Analyse now"),
    icon: SparklesGlyph,
    onSelect: () => run.mutate(module),
    disabled: blocked !== null,
    disabledReason: blocked ?? undefined,
  };
  return { item, busy: run.isPending, failed: run.isError };
}

/**
 * The one header of a module card's AI block, whatever its body (UX round 6 B1 on #953): the AI glyph,
 * the caption in the caption style and the card's AI actions menu. The analysis summary and the Module
 * Analyst's investigation line both render it, so the block keeps one anatomy across a row.
 */
export function AiBlockHeader({
  module,
  caption,
  items,
  busy,
}: {
  module: HealthModule;
  caption: string;
  items: OverflowMenuItem[];
  busy: boolean;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center justify-between gap-2" data-testid={`health-ai-block-header-${module}`}>
      <span className="inline-flex items-center gap-1.5 content-caption font-medium uppercase tracking-wider text-default-secondary">
        <SparklesGlyph className="shrink-0" />
        {caption}
      </span>
      <OverflowMenu label={t_i18n("{module} actions", { module: t_i18n(HEALTH_MODULE_LABELS[module]) })} busy={busy} side="auto" items={items} />
    </div>
  );
}

/** The line an AI block shows when its last action could not be started. */
export function AnalysisStartFailed() {
  const { t_i18n } = useFormatter();
  return (
    <p role="alert" className="content-compact text-feedback-error-primary">
      {t_i18n("The analysis could not be started.")}
    </p>
  );
}

function Outcome({ module, analysis }: { module: HealthModule; analysis: HealthModuleAnalysis }) {
  const { t_i18n } = useFormatter();
  // Hooks before the early returns: the headline is empty unless the analysis completed.
  const headline = analysis.status === "completed" ? summaryHeadline(analysis.summary) : "";
  const { ref, cut } = useClampCut(headline);
  if (!analysis.status) {
    return (
      <p className="content-compact text-default-secondary" data-testid="health-analysis-never">
        {t_i18n("Not analysed yet")}
      </p>
    );
  }
  if (analysis.status !== "completed") {
    return (
      <p className="content-compact text-default-secondary" data-testid="health-analysis-reason">
        {analysisReasonLabel(analysis.reason, t_i18n)}
      </p>
    );
  }
  // Show more when the summary holds more than its headline OR the clamped headline is cut.
  const more = analysis.summary.trim() !== headline || cut;
  const asOf = analysis.analysed_at ? (
    <span className="inline-flex items-center gap-1">
      {t_i18n("As of")} <DateTimeCell value={analysis.analysed_at} />
    </span>
  ) : null;
  return (
    <div className="min-w-0 space-y-1">
      <p ref={ref} className="line-clamp-2 content-compact text-default-primary" data-testid="health-analysis-headline">
        {headline}
      </p>
      <div className="flex flex-wrap items-center gap-x-2 content-caption text-default-secondary">
        {asOf}
        {more ? (
          // The whole summary and its order of fixes open in the shared popover, never in place.
          <InfoPopover
            label={t_i18n("Show more")}
            icon={t_i18n("Show more")}
            heading={t_i18n("AI summary")}
            headingClassName="content-compact"
            headingAside={<span className="content-compact text-default-secondary">{t_i18n(HEALTH_MODULE_LABELS[module])}</span>}
            width={384}
            triggerClassName="rounded-sm text-accent underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-focus"
            popoverClassName="p-3"
            triggerTestId="health-analysis-expand"
            popoverTestId="health-analysis-summary"
          >
            <p className="whitespace-pre-line content-compact text-default-primary">{analysis.summary.trim()}</p>
            {asOf ? <div className="mt-2 content-caption text-default-secondary">{asOf}</div> : null}
          </InfoPopover>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The AI block of one module card: nothing for a module with no open finding; else its header (the
 * actions menu holds Analyse now, then the card's other AI actions) and the outcome of the latest
 * analysis. Nothing either while the analysis is off.
 */
export function ModuleAnalysisSummary({
  module,
  hasOpenFindings,
  overview,
  extraItems = [],
  extraBusy = false,
}: {
  module: HealthModule;
  hasOpenFindings: boolean;
  overview: HealthAnalysisOverview | undefined;
  /** The card's other AI actions (Run checks now on an analyst module), after Analyse now. */
  extraItems?: OverflowMenuItem[];
  extraBusy?: boolean;
}) {
  // Analysis off: there is no outcome to show, and the settings say why.
  const analysis = analysisRowFor(module, hasOpenFindings, overview);
  if (!analysis || !overview) return null;
  return <SummaryBlock module={module} analysis={analysis} overview={overview} extraItems={extraItems} extraBusy={extraBusy} />;
}

function SummaryBlock({
  module,
  analysis,
  overview,
  extraItems,
  extraBusy,
}: {
  module: HealthModule;
  analysis: HealthModuleAnalysis;
  overview: HealthAnalysisOverview;
  extraItems: OverflowMenuItem[];
  extraBusy: boolean;
}) {
  const { t_i18n } = useFormatter();
  const analyse = useAnalyseNowItem(module, overview);
  return (
    <section className="space-y-1.5" aria-label={t_i18n("AI summary")} data-testid={`health-analysis-${module}`}>
      <AiBlockHeader module={module} caption={t_i18n("AI summary")} items={[analyse.item, ...extraItems]} busy={analyse.busy || extraBusy} />
      <Outcome module={module} analysis={analysis} />
      {analyse.failed ? <AnalysisStartFailed /> : null}
    </section>
  );
}
