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
 * time it was written. A module with no open finding shows nothing; a refused or failed analysis
 * shows one muted line saying why. Analyse now sits in the card's actions menu, disabled and
 * explained while AI or the analysis is off. Administrators only: the overview mounts it for the
 * "all" scope. One AI block per card: the Module Analyst's line joins this block, never a second one.
 */
import { useId, useState } from "react";
import { Sparkles } from "lucide-react";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useRunHealthAnalysis } from "@/hooks/api/useHealthApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { HEALTH_MODULE_LABELS } from "@/lib/health-modules";
import type { HealthAnalysisOverview, HealthModule, HealthModuleAnalysis } from "@/types";

/** What each refusal or failure reason means to an administrator. @i18n-keys */
export const ANALYSIS_REASON_LABELS = {
  disabled: "Analysis is off.",
  ai_off: "AI is off on this platform.",
  ceiling_not_set: "Ceiling not set: a cleared ceiling refuses every analysis.",
  ceiling_reached: "Ceiling reached: this analysis would cross a daily or monthly ceiling.",
  no_model: "No routing model is configured.",
  no_price: "The routing model has no price, so its cost cannot be held under the ceiling.",
  model_error: "The model call failed. It is tried again tomorrow, or now with Analyse now.",
} as const;

export function analysisReasonLabel(reason: string | null, t: (key: string) => string): string {
  return t((ANALYSIS_REASON_LABELS as Record<string, string>)[reason ?? ""] ?? ANALYSIS_REASON_LABELS.model_error);
}

/** The first non-empty line of a summary: its headline, the first fix. */
export function summaryHeadline(summary: string): string {
  return summary.split("\n").map((line) => line.trim()).find(Boolean) ?? "";
}

/** Why Analyse now is not offered, in plain words; null when it may run. */
function runBlockedReason(overview: HealthAnalysisOverview, aiOff: boolean, t: (key: string) => string): string | null {
  if (aiOff) return t(ANALYSIS_REASON_LABELS.ai_off);
  if (!overview.enabled) return t(ANALYSIS_REASON_LABELS.disabled);
  if (!overview.ceilings_set) return t(ANALYSIS_REASON_LABELS.ceiling_not_set);
  return null;
}

function AnalysisActions({
  module,
  overview,
  run,
}: {
  module: HealthModule;
  overview: HealthAnalysisOverview;
  run: ReturnType<typeof useRunHealthAnalysis>;
}) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const blocked = runBlockedReason(overview, aiOff, t_i18n);
  return (
    <OverflowMenu
      label={t_i18n("{module} actions", { module: t_i18n(HEALTH_MODULE_LABELS[module]) })}
      busy={run.isPending}
      side="auto"
      items={[
        {
          id: `health-analysis-run-${module}`,
          label: t_i18n("Analyse now"),
          icon: Sparkles,
          onSelect: () => run.mutate(module),
          disabled: blocked !== null,
          disabledReason: blocked ?? undefined,
        },
      ]}
    />
  );
}

function Outcome({ analysis }: { analysis: HealthModuleAnalysis | undefined }) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId();
  if (!analysis?.status) {
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
  const headline = summaryHeadline(analysis.summary);
  const more = analysis.summary.trim() !== headline;
  return (
    <div className="min-w-0 space-y-1">
      {expanded ? (
        <p id={bodyId} className="whitespace-pre-line content-compact text-default-primary" data-testid="health-analysis-summary">
          {analysis.summary.trim()}
        </p>
      ) : (
        <p className="line-clamp-2 content-compact text-default-primary" data-testid="health-analysis-headline">
          {headline}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-2 content-caption text-default-secondary">
        {analysis.analysed_at ? (
          <span className="inline-flex items-center gap-1">
            {t_i18n("As of")} <DateTimeCell value={analysis.analysed_at} />
          </span>
        ) : null}
        {more ? (
          <button
            type="button"
            className="rounded-sm text-accent underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-focus"
            aria-expanded={expanded}
            aria-controls={expanded ? bodyId : undefined}
            onClick={() => setExpanded((open) => !open)}
            data-testid="health-analysis-expand"
          >
            {expanded ? t_i18n("Show less") : t_i18n("Show more")}
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The AI block of one module card: nothing for a module with no open finding; else its title, the
 * actions menu and the outcome of the latest analysis. Nothing either while the analysis is off.
 */
export function ModuleAnalysisSummary({
  module,
  hasOpenFindings,
  overview,
}: {
  module: HealthModule;
  hasOpenFindings: boolean;
  overview: HealthAnalysisOverview | undefined;
}) {
  const { t_i18n } = useFormatter();
  const run = useRunHealthAnalysis();
  // Analysis off: there is no outcome to show, and the settings say why.
  if (!hasOpenFindings || !overview?.enabled) return null;
  const analysis = overview.modules.find((row) => row.module === module);
  if (!analysis) return null;
  return (
    <section className="space-y-1.5" aria-label={t_i18n("AI summary")} data-testid={`health-analysis-${module}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 content-caption font-medium uppercase tracking-wider text-default-secondary">
          <Sparkles className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {t_i18n("AI summary")}
        </span>
        <AnalysisActions module={module} overview={overview} run={run} />
      </div>
      <Outcome analysis={analysis} />
      {run.isError ? (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The analysis could not be started.")}
        </p>
      ) : null}
    </section>
  );
}
