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
 * Full Exposure Briefing page - the dedicated, full-fledged view behind the home
 * "Latest exposure briefing" card. The home card shows a light summary and links
 * here; this page renders the complete AI briefing: headline + executive summary,
 * posture trend, coverage, the cross-product insight, every key finding with its
 * recommendation, the prioritized next steps, and the single recommended focus.
 *
 * Per-user + per-vertical (scoped to the viewer's OpenCTI/OpenAEV access and their
 * active CTEM vertical), EE-gated, with on-demand regeneration. Falls back to a
 * greyed sample (DEMO_BRIEFING) until the first real briefing is published.
 */
import {
  Sparkles,
  Target,
  TrendingUp,
  TrendingDown,
  Minus,
  RefreshCw,
  Lock,
  ListChecks,
  Lightbulb,
  ExternalLink,
  type LucideIcon,
} from "lucide-react";
import { Icon } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { SampleBadge } from "@/components/ui/sample-badge";
import { ToneBadge } from "@/components/ui/tone-badge";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import { useAiLockReason } from "@/hooks/useAiLockReason";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { PageSkeleton } from "@/components/ui/skeleton";
import { cn, safeExternalUrl } from "@/lib/utils";
import { CTEM_PRIORITY_CONFIG, CTEM_PRODUCT_INFO, CTEM_VERTICAL_CONFIG, ctemDeepLink, type CtemVerticalKey } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { useMe } from "@/hooks/api/useAuthApi";
import { useCtemBriefing, useCtemPosture, useRefreshMyBriefing } from "@/hooks/api/useCtemMetricsApi";
import type { CtemVertical } from "@/types";
import { demoBriefing } from "./ctem/ctem-demo";
import { FollowUpInChatButton, AssignAgentButton } from "./ctem/ImplementWithAgent";
import { CompanyContextNotice, ContextScoredLine } from "./ctem/CompanyContextNotice";
import { AssessmentFreshness, GeneratedByline } from "./ctem/assessment-freshness";
import { briefingShown } from "./ctem/landing-refresh";
import { CtemPageHeader } from "./ctem/CtemPageHeader";

const SEVERITY_DOT: Record<string, string> = {
  critical: "var(--color-feedback-error-primary)",
  high: "var(--color-feedback-warning-primary)",
  medium: "var(--color-feedback-alert-primary)",
  low: "var(--color-feedback-success-primary)",
  info: "#64748b",
};

const TREND: Record<string, { label: string; icon: LucideIcon; color: string }> = {
  improving: { label: "Improving", icon: TrendingUp, color: "text-feedback-success-primary" },
  declining: { label: "Declining", icon: TrendingDown, color: "text-feedback-error-primary" },
  stable: { label: "Stable", icon: Minus, color: "text-default-secondary" },
  unknown: { label: "No trend", icon: Minus, color: "text-default-secondary" },
};

function sourceLabel(source: string): string {
  if (source === "both") return "Correlated";
  return CTEM_PRODUCT_INFO[source]?.name ?? source;
}

export default function ExposureBriefing() {
  const { t_i18n } = useFormatter();
  const { data: user } = useMe();
  const vertical: CtemVertical = user?.ctem_vertical ?? "cyber";
  const verticalCfg = CTEM_VERTICAL_CONFIG[vertical as CtemVerticalKey];

  const { data: resp, isLoading } = useCtemBriefing(vertical);
  const { data: posture } = useCtemPosture(vertical);
  const refresh = useRefreshMyBriefing();
  // One lock reason (no license, AI off for the platform or the group, no
  // model), the backend status filling in what the config cannot say (C1).
  const lockReason = useAiLockReason(resp?.status);
  // Refresh starts a run: disabled with AI off.
  const aiOff = useAiActionDisabled();

  if (isLoading && !resp) return <PageSkeleton cards={3} />;

  const status = resp?.status;
  // Locked for any reason: one box with the reason and one way forward, and
  // no Refresh button that calls an endpoint that refuses.
  const eeLocked = lockReason !== null;
  const generating = status === "generating" || !!resp?.generating;
  const ready = briefingShown(resp);
  const briefing = ready && resp?.briefing ? resp.briefing : demoBriefing(vertical);

  // Deep-link a finding's source ("opencti"/"openaev"/"both") to the relevant
  // section of the connected product (OpenCTI exposures / OpenAEV simulations).
  // Only for a real briefing whose source product is actually connected; returns
  // null otherwise (the source then renders as a plain, non-clickable tag).
  const sourceLink = (source: string): { url: string; label: string } | null => {
    if (!ready) return null;
    const product = source === "both" ? "opencti" : source;
    if (product !== "opencti" && product !== "openaev") return null;
    const baseUrl = posture?.products?.[product]?.url;
    if (!baseUrl) return null;
    // Representative landing per product: OpenCTI -> the exposures view
    // (Vulnerabilities / Channels / Observables), OpenAEV -> Simulations.
    const stage = product === "openaev" ? "validation" : "prioritization";
    const deep = ctemDeepLink(baseUrl, vertical, stage, product);
    // Only a SPECIFIC resource deep link - never fall back to the product home.
    const url = safeExternalUrl(deep?.url);
    return url && deep ? { url, label: deep.label } : null;
  };
  const isSample = !ready;
  const trend = TREND[briefing.posture_trend] ?? TREND.unknown;
  const TrendIcon = trend.icon;
  const busy = refresh.isPending || generating;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* The one CTEM header band (#11660, #12273): the back link as the
          breadcrumb, the vertical, Sample, the AI mark and the complete-context
          line on the breadcrumb line, the freshness behind the "i", the trend
          and Refresh on the title line. */}
      <CtemPageHeader
        back={{ to: "/", label: "Back to Command Center" }}
        crumb={t_i18n("Exposure briefing")}
        crumbExtras={
          <>
            {verticalCfg && (
              <Badge variant="outline" className="content-caption text-accent border-accent/30">
                {verticalCfg.label}
              </Badge>
            )}
            {isSample && <SampleBadge />}
            <span className="inline-flex shrink-0 items-center gap-1 content-caption font-medium text-accent" data-testid="ctem-briefing-ai-mark">
              <Icon name="sparkles" size={16} aria-hidden /> {t_i18n("AI")}
            </span>
            {/* A complete organization context is confirmed on the breadcrumb line, never on a row of its own. */}
            {posture?.context?.state === "complete" && <ContextScoredLine inline />}
          </>
        }
        title={t_i18n("Exposure briefing")}
        hint={
          !isSample && resp?.generated_at
            ? <AssessmentFreshness agentName={resp.agent_name ?? "Exposure Briefing Analyst"} generatedAt={resp.generated_at} stale={!!resp.stale} generating={busy} freshDays={1} />
            : generating
            ? t_i18n("Generating your briefing - scoped to your access")
            : isSample
              ? t_i18n("Generate yours, or wait for the monthly run")
              : <GeneratedByline agentName={resp?.agent_name ?? "Exposure Briefing Analyst"} generatedAt={resp?.generated_at} />
        }
        hintLabel={t_i18n("About {name}", { name: t_i18n("Exposure briefing") })}
        actions={
          eeLocked ? undefined : (
            <>
              <span className={cn("inline-flex items-center gap-1.5 content-compact font-medium", trend.color)} data-testid="ctem-briefing-trend">
                <TrendIcon className="h-4 w-4" /> {t_i18n(trend.label)}
              </span>
              <Tip content={generating ? t_i18n("Generating...") : t_i18n("Regenerate my briefing (scoped to you and your active vertical)")}>
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1.5 h-8 content-compact"
                  disabled={busy || aiOff}
                  onClick={() => refresh.mutate(vertical)}
                >
                  <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} /> {t_i18n("Refresh")}
                </Button>
              </Tip>
            </>
          )
        }
      />

      {/* The organization context of Settings, the only company context (#11464):
          a missing or partial context only; a complete one sits on the breadcrumb line. */}
      <CompanyContextNotice context={posture?.context} placement="brief" />

      {eeLocked ? (
        <Card className="overflow-hidden border-elevation-subtle-soft layer-1 bg-elevation-default">
          <CardContent className="p-6 flex items-center gap-3">
            <div className="h-10 w-10 rounded-sm bg-elevation-hover ring-1 ring-[var(--border-elevation-subtle)] flex items-center justify-center shrink-0">
              <Lock className="h-4 w-4 text-default-secondary" />
            </div>
            <AiLockedFooter reason={lockReason} className="flex-1" />
          </CardContent>
        </Card>
      ) : (
        <div className={cn("space-y-6", isSample && "grayscale opacity-60")}>
          {/* Headline + summary */}
          <Card className="overflow-hidden border-accent/20">
            <CardContent className="p-6 space-y-3">
              <p className="text-lg font-semibold leading-snug">{briefing.headline}</p>
              <p className="content-base text-default-secondary leading-relaxed">{briefing.summary}</p>
              <div className="flex flex-wrap items-center gap-2 pt-1">
                {(["opencti", "openaev"] as const).map((p) => {
                  const on = briefing.coverage?.[p];
                  return (
                    <span
                      key={p}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-sm px-2 py-0.5 content-caption font-medium ring-1",
                        on ? "bg-accent/10 text-accent ring-accent/20" : "bg-elevation-hover text-default-secondary ring-[var(--border-elevation-subtle)]",
                      )}
                    >
                      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: on ? "var(--color-accent)" : "#475569" }} />
                      {CTEM_PRODUCT_INFO[p]?.name ?? p}
                      {!on && ` - ${t_i18n("gap")}`}
                    </span>
                  );
                })}
              </div>
            </CardContent>
          </Card>

          {/* Cross-product insight */}
          {briefing.cross_product_insight && (
            <Card className="overflow-hidden bg-accent/[0.05] border-accent/20">
              <CardContent className="p-5 flex items-start gap-3">
                <Sparkles className="h-5 w-5 text-accent shrink-0 mt-0.5" />
                <div>
                  <p className="content-compact font-semibold text-accent mb-1">{t_i18n("Cross-product insight")}</p>
                  <p className="content-base text-default-secondary leading-relaxed">{briefing.cross_product_insight}</p>
                </div>
              </CardContent>
            </Card>
          )}

          <div className="grid gap-6 lg:grid-cols-2">
            {/* Key findings */}
            {briefing.key_findings?.length > 0 && (
              <Card className="overflow-hidden">
                <CardContent className="p-5 space-y-4">
                  <h2 className="content-base font-semibold flex items-center gap-2">
                    <Lightbulb className="h-4 w-4 text-accent" /> {t_i18n("Key findings")}
                  </h2>
                  <div className="space-y-3.5">
                    {briefing.key_findings.map((f, i) => (
                      <div key={i} className="flex items-start gap-2.5">
                        <span
                          className="mt-1.5 h-2 w-2 rounded-full shrink-0"
                          style={{ backgroundColor: SEVERITY_DOT[f.severity] ?? SEVERITY_DOT.info }}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <p className="text-[13px] font-medium leading-tight">{f.title}</p>
                            {(() => {
                              // Prefer the agent's concrete source (a deep link to the
                              // exact cited entity); fall back to the deterministic
                              // product-section link; else a plain, non-clickable tag.
                              const agentUrl = ready ? safeExternalUrl(f.source_ref?.url) : null;
                              const det = sourceLink(f.source);
                              const url = agentUrl ?? det?.url ?? null;
                              const label = f.source_ref?.label || det?.label || sourceLabel(f.source);
                              return url ? (
                                <Tip content={label}>
                                  <a
                                    href={url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-[9px] uppercase tracking-wider text-accent/70 hover:text-accent shrink-0 inline-flex items-center gap-0.5 no-underline"
                                  >
                                    {t_i18n(sourceLabel(f.source))} <ExternalLink className="h-2.5 w-2.5" />
                                  </a>
                                </Tip>
                              ) : (
                                <span className="text-[9px] uppercase tracking-wider text-default-secondary shrink-0">
                                  {t_i18n(sourceLabel(f.source))}
                                </span>
                              );
                            })()}
                          </div>
                          {f.detail && (
                            <p className="text-[11px] text-default-secondary leading-snug mt-0.5">{f.detail}</p>
                          )}
                          {f.recommendation && (
                            <p className="text-[11px] leading-snug mt-1 text-accent/90">
                              <span className="font-semibold">{t_i18n("Action:")} </span>
                              {f.recommendation}
                            </p>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Next steps */}
            {(briefing.next_steps?.length ?? 0) > 0 && (
              <Card className="overflow-hidden">
                <CardContent className="p-5 space-y-4">
                  <h2 className="content-base font-semibold flex items-center gap-2">
                    <ListChecks className="h-4 w-4 text-accent" /> {t_i18n("Next steps")}
                  </h2>
                  <ol className="space-y-3">
                    {briefing.next_steps!.map((s, i) => {
                      const prio = CTEM_PRIORITY_CONFIG[s.priority ?? "medium"] ?? CTEM_PRIORITY_CONFIG.medium;
                      return (
                        <li key={i} className="flex items-start gap-3">
                          <span className="mt-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-accent/10 content-caption font-bold text-accent shrink-0">
                            {i + 1}
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 flex-wrap">
                              <p className="text-[13px] font-medium leading-tight">{s.action}</p>
                              <ToneBadge tone={prio.tone}>{t_i18n(prio.label)}</ToneBadge>
                            </div>
                            {s.rationale && (
                              <p className="text-[11px] text-default-secondary leading-snug mt-0.5">{s.rationale}</p>
                            )}
                            {ready && (
                              <div className="mt-2">
                                <FollowUpInChatButton
                                  vertical={vertical}
                                  contextLabel="exposure briefing"
                                  action={s.action}
                                  rationale={s.rationale}
                                  agentId={resp?.agent_id}
                                  agentName={resp?.agent_name}
                                  agentIsBuiltin={resp?.agent_is_builtin}
                                />
                              </div>
                            )}
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                  {/* One "assign your own agent" CTA for the whole list (not per step). */}
                  {ready && (
                    <div className="flex justify-end border-t border-elevation-subtle-soft pt-3">
                      <AssignAgentButton />
                    </div>
                  )}
                </CardContent>
              </Card>
            )}
          </div>

          {/* Recommended focus */}
          {briefing.recommended_focus && (
            <Card className="overflow-hidden border-accent/20">
              <CardContent className="p-5 flex items-start gap-3">
                <Target className="h-5 w-5 text-accent shrink-0 mt-0.5" />
                <div className="min-w-0 flex-1">
                  <p className="content-compact font-semibold text-accent mb-1">{t_i18n("Recommended focus")}</p>
                  <p className="content-base text-default-secondary leading-relaxed">{briefing.recommended_focus}</p>
                  {ready && (
                    <div className="mt-2.5 flex flex-wrap items-center gap-2">
                      <FollowUpInChatButton
                        vertical={vertical}
                        contextLabel="exposure briefing"
                        action={briefing.recommended_focus}
                        agentId={resp?.agent_id}
                        agentName={resp?.agent_name}
                        agentIsBuiltin={resp?.agent_is_builtin}
                      />
                      {/* Only surface the assign CTA here when there is no Next
                          steps card to carry it (keeps a single CTA on the page). */}
                      {(briefing.next_steps?.length ?? 0) === 0 && <AssignAgentButton />}
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          )}

          {isSample && (
            <button
              type="button"
              onClick={() => refresh.mutate(vertical)}
              disabled={busy}
              className="text-[11px] text-accent/80 hover:text-accent inline-flex items-center gap-1.5 disabled:opacity-50"
            >
              <Sparkles className={cn("h-3 w-3", generating && "animate-pulse")} />
              {generating ? t_i18n("Generating now - scoped to your access") : t_i18n("Generate my briefing now")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
