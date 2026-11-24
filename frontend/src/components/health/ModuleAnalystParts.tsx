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
 * the last investigation of an OpenCTI, OpenAEV or OpenCRQ card and its "Run checks now" action
 * (administrators; #101 S15e-2, section 13.8: it replaced "Investigate now" and starts the module's
 * checker round on every active registration), and the "Module Analyst" block of a finding the analyst
 * recommended on. One AI block per card (#965): the investigation line renders the shared AI block
 * header of the analysis summary, and "Run checks now" sits in the card's AI actions menu whatever the
 * block's body shows (UX round 6 B1 and B2 on #953). Reading calls no model; "Run checks now" is an AI
 * action, disabled and explained while AI is off.
 */
import { Icon } from "@filigran/design-system";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { Badge } from "@/components/ui/badge";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { AiBlockHeader, AnalysisStartFailed, ModuleAnalysisSummary, useAnalyseNowItem } from "@/components/health/ModuleAnalysisSummary";
import { useHealthName, useInvestigateHealthModule } from "@/hooks/api/useHealthApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { healthRefusalCodeKey } from "@/lib/health";
import type { HealthAnalysisOverview, HealthAnalystRecommendation, HealthLastInvestigation, HealthModule } from "@/types";

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

/** Why "Run checks now" started nothing, for the codes of the module route. @i18n-keys */
const INVESTIGATE_REFUSAL_LABELS = {
  module_analyst_off: "The Module Analyst is off. An administrator can turn it on in Platform Health settings.",
  no_registration: "No active registration of this module to check.",
} as const;

/** The glyph of Run checks now, so its label lines up with Analyse now in the menu (UX round 7 B1 on #953). */
function InvestigateGlyph({ className }: { className?: string }) {
  return <Icon name="file-search" size={16} className={className} aria-hidden />;
}

function labelOf<T extends Record<string, string>>(labels: T, key: string | null | undefined, fallback: string): string {
  return key && Object.prototype.hasOwnProperty.call(labels, key) ? labels[key as keyof T] : fallback;
}

/** The i18n key of a refusal code of "Run checks now" (`{name}` placeholder for the shared cycle codes). */
export function investigateRefusalKey(code: unknown): string {
  if (typeof code === "string" && Object.prototype.hasOwnProperty.call(INVESTIGATE_REFUSAL_LABELS, code)) {
    return INVESTIGATE_REFUSAL_LABELS[code as keyof typeof INVESTIGATE_REFUSAL_LABELS];
  }
  return healthRefusalCodeKey(code) ?? "{name} could not start this work.";
}

/** The "Run checks now" item of an analyst module card's AI actions menu, with its toasts. */
function useInvestigateNowItem(module: HealthModule): { item: OverflowMenuItem; busy: boolean } {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const healthName = useHealthName();
  const aiOff = useAiActionDisabled();
  const investigate = useInvestigateHealthModule();

  const run = () =>
    investigate.mutate(module, {
      onSuccess: (answer) => {
        const started = answer.items.filter((item) => !item.refusal).length;
        if (started) {
          toast({
            title: t_i18n("Checks started"),
            description: t_i18n("{count, plural, one {# check round started. Its report shows under Agentic checks.} other {# check rounds started. Their reports show under Agentic checks.}}", { count: started }),
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

  const item: OverflowMenuItem = {
    id: `health-investigate-${module}`,
    label: t_i18n("Run checks now"),
    icon: InvestigateGlyph,
    onSelect: run,
    disabled: aiOff || investigate.isPending,
    disabledReason: aiOff ? t_i18n("AI is off on this platform.") : undefined,
  };
  return { item, busy: investigate.isPending };
}

/**
 * The check round body of a module card's AI block: the outcome of the module checker's newest round,
 * when and what started it (named after the Run checks now action, UX round 1 B1 on #1269).
 */
function InvestigationBody({ investigation }: { investigation: HealthLastInvestigation | null | undefined }) {
  const { t_i18n } = useFormatter();
  if (!investigation) {
    // The badge line height (24 px), so a card with no round matches its row (UX round 2 B1 on #953).
    return <p className="flex min-h-6 items-center content-compact text-default-secondary">{t_i18n("No check round yet")}</p>;
  }
  return (
    <div className="flex min-h-6 flex-wrap items-center gap-x-2 gap-y-1 content-compact text-default-secondary">
      <Badge variant={INVESTIGATION_STATUS_BADGE[investigation.status] ?? "outline"} className="whitespace-nowrap">
        {t_i18n(labelOf(INVESTIGATION_STATUS_LABELS, investigation.status, INVESTIGATION_STATUS_LABELS.failed))}
      </Badge>
      <DateTimeCell value={investigation.closed_at ?? investigation.opened_at} />
      <span>{t_i18n(labelOf(INVESTIGATION_TRIGGER_LABELS, investigation.trigger, INVESTIGATION_TRIGGER_LABELS.schedule))}</span>
    </div>
  );
}

/**
 * The one AI block of an OpenCTI, OpenAEV or OpenCRQ card (administrators): the analysis summary when
 * it is the newer, else the last investigation, under one shared header whose menu always holds
 * Analyse now and Run checks now (UX round 6 B1 and B2 on #953). In normal flow in the card footer:
 * a status wrapping to two lines grows the block below the divider, never moves the divider.
 */
export function ModuleAnalystBlock({
  module,
  investigation,
  showSummary,
  hasOpenFindings,
  overview,
}: {
  module: HealthModule;
  investigation: HealthLastInvestigation | null | undefined;
  /** The analysis summary is newer than the last investigation: the block's body is the summary. */
  showSummary: boolean;
  hasOpenFindings: boolean;
  overview: HealthAnalysisOverview | undefined;
}) {
  const { t_i18n } = useFormatter();
  const investigate = useInvestigateNowItem(module);
  const analyse = useAnalyseNowItem(module, overview);
  if (showSummary) {
    return <ModuleAnalysisSummary module={module} hasOpenFindings={hasOpenFindings} overview={overview} extraItems={[investigate.item]} extraBusy={investigate.busy} />;
  }
  return (
    <section className="space-y-1.5" aria-label={t_i18n("Last check round")} data-testid={`health-investigation-${module}`}>
      <AiBlockHeader module={module} caption={t_i18n("Last check round")} items={[analyse.item, investigate.item]} busy={analyse.busy || investigate.busy} />
      <InvestigationBody investigation={investigation} />
      {analyse.failed ? <AnalysisStartFailed /> : null}
    </section>
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
