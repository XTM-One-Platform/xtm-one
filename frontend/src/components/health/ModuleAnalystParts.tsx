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
 * The Module Analyst on the Health page (#101 S14c-2, dev-docs/platform-health.md section 12.7):
 * the last investigation line and "Investigate now" of an OpenCTI, OpenAEV or OpenCRQ card
 * (administrators), and the "Module Analyst" block of a finding the analyst recommended on.
 * Reading calls no model; "Investigate now" is an AI action, disabled and explained while AI is off.
 */
import { Icon, Spinner } from "@filigran/design-system";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { useToast } from "@/components/ui/toast";
import { useHealthName, useInvestigateHealthModule } from "@/hooks/api/useHealthApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { healthRefusalCodeKey } from "@/lib/health";
import { HEALTH_MODULE_LABELS } from "@/lib/health-modules";
import type { HealthAnalystRecommendation, HealthLastInvestigation, HealthModule } from "@/types";

/** The modules the Module Analyst investigates (`ANALYST_MODULES` in platform_health_findings.py). */
export const ANALYST_MODULES: readonly HealthModule[] = ["opencti", "openaev", "opencrq"] as const;

/** The outcome of the last investigation, in plain words. @i18n-keys */
const INVESTIGATION_STATUS_LABELS = {
  open: "Running",
  completed: "Completed",
  budget_reached: "Ceiling reached",
  failed: "Failed",
  cancelled: "Cancelled",
} as const;

const INVESTIGATION_STATUS_BADGE = {
  open: "secondary",
  completed: "success",
  budget_reached: "warning",
  failed: "destructive",
  cancelled: "outline",
} as const;

/** What started the last investigation. @i18n-keys */
const INVESTIGATION_TRIGGER_LABELS = {
  schedule: "Daily",
  signal: "Findings changed",
  on_demand: "On demand",
} as const;

/** The analyst's confidence in a recommendation. @i18n-keys */
const CONFIDENCE_LABELS = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
} as const;

/** Why "Investigate now" started nothing, for the codes of the module route. @i18n-keys */
const INVESTIGATE_REFUSAL_LABELS = {
  module_analyst_off: "The Module Analyst is off. An administrator can turn it on in Platform Health settings.",
  no_registration: "No active registration of this module to investigate.",
} as const;

function labelOf<T extends Record<string, string>>(labels: T, key: string | null | undefined, fallback: string): string {
  return key && Object.prototype.hasOwnProperty.call(labels, key) ? labels[key as keyof T] : fallback;
}

/** The i18n key of a refusal code of "Investigate now" (`{name}` placeholder for the shared cycle codes). */
export function investigateRefusalKey(code: unknown): string {
  if (typeof code === "string" && Object.prototype.hasOwnProperty.call(INVESTIGATE_REFUSAL_LABELS, code)) {
    return INVESTIGATE_REFUSAL_LABELS[code as keyof typeof INVESTIGATE_REFUSAL_LABELS];
  }
  return healthRefusalCodeKey(code) ?? "{name} could not start this work.";
}

/** The last investigation of a module card and its "Investigate now" action (administrators). */
export function ModuleInvestigationLine({ module, investigation }: { module: HealthModule; investigation: HealthLastInvestigation | null | undefined }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const healthName = useHealthName();
  const aiOff = useAiActionDisabled();
  const investigate = useInvestigateHealthModule();
  const label = t_i18n(HEALTH_MODULE_LABELS[module]);

  const run = () =>
    investigate.mutate(module, {
      onSuccess: (answer) => {
        const started = answer.items.filter((item) => !item.refusal).length;
        if (started) {
          toast({
            title: t_i18n("Investigation started"),
            description: t_i18n("{count, plural, one {# Module Analyst cycle started. Its recommendations show on the findings.} other {# Module Analyst cycles started. Their recommendations show on the findings.}}", { count: started }),
            variant: "success",
          });
          return;
        }
        const refusal = answer.items.find((item) => item.refusal)?.refusal;
        toast({ title: t_i18n("Not started"), description: t_i18n(investigateRefusalKey(refusal), { name: healthName }), variant: "error" });
      },
      onError: (error) => {
        const detail = (error as { detail?: unknown } | null)?.detail;
        const code = detail && typeof detail === "object" ? (detail as { code?: unknown }).code : undefined;
        toast({ title: t_i18n("Not started"), description: t_i18n(investigateRefusalKey(code), { name: healthName }), variant: "error" });
      },
    });

  return (
    // In normal flow in the card footer, the one AI block of the card (#965): a status wrapping to two
    // lines grows this block below the divider, never moves the divider.
    <div className="flex flex-col gap-2" data-testid={`health-investigation-${module}`}>
      <p className="content-compact font-medium text-default-secondary">{t_i18n("Last investigation")}</p>
      {investigation ? (
        <div className="flex min-h-6 flex-wrap items-center gap-x-2 gap-y-1 content-compact text-default-secondary">
          <Badge variant={INVESTIGATION_STATUS_BADGE[investigation.status] ?? "outline"} className="whitespace-nowrap">
            {t_i18n(labelOf(INVESTIGATION_STATUS_LABELS, investigation.status, INVESTIGATION_STATUS_LABELS.failed))}
          </Badge>
          <DateTimeCell value={investigation.closed_at ?? investigation.opened_at} />
          <span>{t_i18n(labelOf(INVESTIGATION_TRIGGER_LABELS, investigation.trigger, INVESTIGATION_TRIGGER_LABELS.schedule))}</span>
        </div>
      ) : (
        // The badge line height (24 px), so a never-investigated card matches its row (UX round 2 B1 on #953).
        <p className="flex min-h-6 items-center content-compact text-default-secondary">{t_i18n("Not investigated yet")}</p>
      )}
      <div>
        <AiDisabledTip>
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            disabled={aiOff || investigate.isPending}
            aria-busy={investigate.isPending || undefined}
            aria-label={t_i18n("Investigate {module} now", { module: label })}
            onClick={run}
            data-testid={`health-investigate-${module}`}
          >
            {investigate.isPending && <Spinner size="sm" tone="inherit" />}
            {!investigate.isPending && <Icon name="sparkles" size={16} aria-hidden />}
            {t_i18n("Investigate now")}
          </Button>
        </AiDisabledTip>
      </div>
    </div>
  );
}

/** The "Module Analyst" block of a finding: its newest recommendation, confidence, what it read and when. */
export function AnalystRecommendationBlock({ recommendation }: { recommendation: HealthAnalystRecommendation }) {
  const { t_i18n } = useFormatter();
  const tools = [...new Set(recommendation.evidence.map((read) => read.tool).filter((tool): tool is string => Boolean(tool)))];
  return (
    // The sibling of the check's "Recommendation" section: same heading, same left edge, no frame (UX round 1 B4 on #953).
    <div className="space-y-1" data-testid="health-analyst-recommendation">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="content-compact font-medium text-default-secondary">{t_i18n("Module Analyst")}</h4>
        <Badge variant="outline" className="whitespace-nowrap">
          {t_i18n(labelOf(CONFIDENCE_LABELS, recommendation.confidence, CONFIDENCE_LABELS.low))}
        </Badge>
        <span className="content-compact text-default-secondary">
          <DateTimeCell value={recommendation.created_at} />
        </span>
      </div>
      <p className="whitespace-pre-line content-base text-default-primary">{recommendation.text}</p>
      {recommendation.evidence.length ? (
        <p className="content-compact text-default-secondary" data-testid="health-analyst-evidence">
          {tools.length
            ? t_i18n("{count, plural, one {Based on # read of the module: {tools}} other {Based on # reads of the module: {tools}}}", {
                count: recommendation.evidence.length,
                tools: tools.join(", "),
              })
            : t_i18n("{count, plural, one {Based on # read of the module} other {Based on # reads of the module}}", { count: recommendation.evidence.length })}
        </p>
      ) : null}
    </div>
  );
}
