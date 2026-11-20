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
 * The analysis panel of the Platform Health overview (#101 S9c, dev-docs/platform-health.md
 * section 6): today's and this month's analysis spend against the ceilings, then one row per
 * module with its latest analysis (the summary, or why it did not run) and an Analyse now action.
 * Reading the panel calls no model; Analyse now is an AI action, disabled and explained while AI
 * is off. Administrators only: the overview mounts it for the "all" scope.
 */
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { useHealthAnalysis, useRunHealthAnalysis } from "@/hooks/api/useHealthApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { HEALTH_MODULE_LABELS } from "@/lib/health-modules";
import { HEALTH_SETTINGS_PATH } from "@/pages/settings/settings-routes";
import type { HealthAnalysisSpend, HealthModuleAnalysis } from "@/types";

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

/** The badge of a module's latest analysis. @i18n-keys */
const STATUS_LABELS = {
  completed: "Analysed",
  refused: "Not run",
  failed: "Failed",
  never: "Not analysed yet",
} as const;

const STATUS_BADGE = {
  completed: "success",
  refused: "warning",
  failed: "destructive",
  never: "secondary",
} as const;

function analysisReasonLabel(reason: string | null, t: (key: string) => string): string | null {
  if (!reason) return null;
  return t((ANALYSIS_REASON_LABELS as Record<string, string>)[reason] ?? ANALYSIS_REASON_LABELS.model_error);
}

function SpendLine({
  label,
  spend,
  tokens,
  cost,
}: {
  label: string;
  spend: HealthAnalysisSpend;
  tokens: number | null;
  cost: string | null;
}) {
  const { t_i18n } = useFormatter();
  const { count: n, money: usd } = useNumberFormat("USD");
  return (
    <div className="min-w-0 space-y-0.5">
      <p className="content-compact text-default-secondary">{label}</p>
      <p className="content-base font-medium tabular-nums text-default-primary">
        {tokens === null
          ? t_i18n("Tokens: {tokens}, no ceiling", { tokens: n(spend.tokens) })
          : t_i18n("Tokens: {tokens} of {ceiling}", { tokens: n(spend.tokens), ceiling: n(tokens) })}
      </p>
      <p className="content-compact tabular-nums text-default-secondary">
        {cost === null
          ? t_i18n("Cost: {cost}, no ceiling", { cost: usd(Number(spend.cost_usd)) })
          : t_i18n("Cost: {cost} of {ceiling}", { cost: usd(Number(spend.cost_usd)), ceiling: usd(Number(cost)) })}
      </p>
    </div>
  );
}

function ModuleRow({ analysis, canRun }: { analysis: HealthModuleAnalysis; canRun: boolean }) {
  const { t_i18n } = useFormatter();
  const run = useRunHealthAnalysis();
  const aiOff = useAiActionDisabled();
  const status = analysis.status ?? "never";
  const reason = analysis.status === "completed" ? null : analysisReasonLabel(analysis.reason, t_i18n);
  const label = t_i18n(HEALTH_MODULE_LABELS[analysis.module]);
  return (
    <li className="space-y-2 py-3 first:pt-0 last:pb-0" data-testid={`health-analysis-${analysis.module}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="content-base font-medium text-default-primary">{label}</span>
        <Badge variant={STATUS_BADGE[status]} className="whitespace-nowrap">
          {t_i18n(STATUS_LABELS[status])}
        </Badge>
        {analysis.analysed_at ? (
          <span className="content-compact text-default-secondary">
            <DateTimeCell value={analysis.analysed_at} />
          </span>
        ) : null}
        <span className="ml-auto">
          <AiDisabledTip>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              disabled={aiOff || !canRun || run.isPending}
              aria-busy={run.isPending || undefined}
              aria-label={t_i18n("Analyse {module} now", { module: label })}
              onClick={() => run.mutate(analysis.module)}
              data-testid={`health-analysis-run-${analysis.module}`}
            >
              <Icon name="sparkles" size={16} aria-hidden />
              {t_i18n("Analyse now")}
            </Button>
          </AiDisabledTip>
        </span>
      </div>
      {analysis.summary ? (
        <p className="whitespace-pre-line content-compact text-default-primary">{analysis.summary}</p>
      ) : null}
      {reason ? <p className="content-compact text-default-secondary">{reason}</p> : null}
      {run.isError ? (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The analysis could not be started.")}
        </p>
      ) : null}
    </li>
  );
}

export function HealthAnalysisPanel() {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = useHealthAnalysis();
  if (isLoading || isError || !data) return null;
  return (
    <section
      className="space-y-4 rounded-md border border-elevation-subtle bg-elevation-default p-4"
      aria-labelledby="health-analysis-title"
      data-testid="health-analysis"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 id="health-analysis-title" className="content-base font-medium text-default-primary">
            {t_i18n("Analysis")}
          </h3>
          <p className="content-compact text-default-secondary">
            {t_i18n("A short explanation of each module's open findings by the routing model, within hard daily and monthly ceilings. Never counted in agentic quotas.")}
          </p>
        </div>
        <Button variant="ghost" size="sm" asChild>
          <Link to={HEALTH_SETTINGS_PATH}>{t_i18n("Analysis settings")}</Link>
        </Button>
      </div>
      {!data.enabled ? (
        <p className="content-compact text-default-secondary" data-testid="health-analysis-off">
          {t_i18n("Analysis is off. An administrator can turn it on in Platform Health settings.")}
        </p>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2" data-testid="health-analysis-spend">
            <SpendLine label={t_i18n("Today")} spend={data.day} tokens={data.ceilings.day_tokens} cost={data.ceilings.day_cost_usd} />
            <SpendLine label={t_i18n("This month")} spend={data.month} tokens={data.ceilings.month_tokens} cost={data.ceilings.month_cost_usd} />
          </div>
          {!data.ceilings_set ? (
            <p className="content-compact text-feedback-warning-primary" data-testid="health-analysis-no-ceiling">
              {t_i18n(ANALYSIS_REASON_LABELS.ceiling_not_set)}
            </p>
          ) : null}
          <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]">
            {data.modules.map((analysis) => (
              <ModuleRow key={analysis.module} analysis={analysis} canRun={data.ceilings_set} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
