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
 * Exposure briefing widget of the CTEM command center (restored by #10721):
 * what happened and what to do, right under the header. It renders what the
 * /briefing page renders, compact by default: the headline and summary, the
 * cross-product insight, the top 3 key findings with their action line, the
 * top 3 next steps numbered with their priority and "Follow up in chat", and
 * the recommended focus. "Show more" draws every finding and step in place;
 * "View full briefing" opens the page.
 *
 * Powered by the seeded "Exposure Briefing Analyst" agent (a monthly fan-out
 * assignment plus an on-demand per-user one). A greyed sample shows until the
 * first real briefing is published; locked (no license, AI off, no model) it
 * keeps the sample with one reason and one way forward. Nothing here runs a
 * model at load: Refresh is the only thing that generates.
 *
 * The analyst reads the organization profile (sector, regions, primary assets,
 * tech stack, PIRs) to rank what it reports; while no profile was ever saved
 * the briefing is generic, and the shared notice says so with the setup call
 * to action (#11324).
 */
import { useEffect, useRef, useState } from "react";
import {
  Newspaper,
  Sparkles,
  Target,
  TrendingUp,
  TrendingDown,
  Minus,
  RefreshCw,
  Lock,
  ChevronRight,
  Lightbulb,
  ListChecks,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import { SampleBadge } from "@/components/ui/sample-badge";
import { ToneBadge } from "@/components/ui/tone-badge";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { IconButton } from "@filigran/design-system";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { CTEM_PRIORITY_CONFIG, CTEM_PRODUCT_INFO } from "@/lib/constants";
import type { CtemBriefing, CtemVertical } from "@/types";
import { useNoOrganizationProfile } from "@/hooks/api/useOrganizationContextApi";
import { GenericContextNotice } from "./GenericContextNotice";
import { FollowUpInChatButton } from "./ImplementWithAgent";
import { AssessmentFreshness, GeneratedByline } from "./assessment-freshness";

const SEVERITY_DOT: Record<string, string> = {
  critical: "var(--color-feedback-error-primary)",
  high: "var(--color-feedback-warning-primary)",
  medium: "var(--color-feedback-alert-primary)",
  low: "var(--color-feedback-success-primary)",
  info: "var(--color-default-secondary)",
};

const TREND: Record<string, { label: string; icon: LucideIcon; color: string }> = {
  improving: { label: "Improving", icon: TrendingUp, color: "text-feedback-success-primary" },
  declining: { label: "Declining", icon: TrendingDown, color: "text-feedback-error-primary" },
  stable: { label: "Stable", icon: Minus, color: "text-default-secondary" },
  unknown: { label: "No trend", icon: Minus, color: "text-default-secondary" },
};

/** Findings and next steps drawn before "Show more". */
const BRIEFING_COLLAPSED = 3;

function sourceLabel(source: string): string {
  if (source === "both") return "Correlated";
  return CTEM_PRODUCT_INFO[source]?.name ?? source;
}

export function BriefingCard({
  briefing,
  isSample,
  agentName,
  generatedAt,
  generating = false,
  stale = false,
  lockReason = null,
  onRefresh,
  refreshing = false,
  onOpen,
  vertical = "cyber",
  agentId,
  agentIsBuiltin,
  onContentHeight,
}: {
  briefing: CtemBriefing;
  isSample: boolean;
  agentName: string;
  generatedAt?: string | null;
  generating?: boolean;
  /** Past the daily window: marked Out of date while the new one is generated (#12183). */
  stale?: boolean;
  /** Why the briefing cannot run (`useAiLockReason`), or null when it can. */
  lockReason?: AiLockReason | null;
  onRefresh?: () => void;
  refreshing?: boolean;
  onOpen?: () => void;
  vertical?: CtemVertical;
  /** The analyst agent "Follow up in chat" hands the step to. */
  agentId?: string | null;
  agentIsBuiltin?: boolean;
  /** The height its content needs, so the page sizes the widget to it. */
  onContentHeight?: (px: number) => void;
}) {
  const { t_i18n } = useFormatter();
  const numbers = useNumberFormat();
  const [expanded, setExpanded] = useState(false);
  const trend = TREND[briefing.posture_trend] ?? TREND.unknown;
  const TrendIcon = trend.icon;

  const locked = lockReason !== null;
  const ready = !isSample && !locked;
  const noProfile = useNoOrganizationProfile();
  const busy = refreshing || generating;
  // A briefing on screen keeps its byline, its age and an Out of date marker
  // while a new one replaces it (#12183); only a first run reads "Generating".
  const subtitle = locked
    ? null
    : !isSample && generatedAt
      ? <AssessmentFreshness agentName={agentName} generatedAt={generatedAt} stale={stale} generating={busy} freshDays={1} />
      : busy
        ? t_i18n("Generating your briefing - scoped to your access")
        : isSample
          ? t_i18n("Generate yours, or wait for the monthly run")
          : <GeneratedByline agentName={agentName} generatedAt={generatedAt} />;

  const findings = briefing.key_findings ?? [];
  const steps = briefing.next_steps ?? [];
  const shownFindings = expanded ? findings : findings.slice(0, BRIEFING_COLLAPSED);
  const shownSteps = expanded ? steps : steps.slice(0, BRIEFING_COLLAPSED);
  const more = findings.length > BRIEFING_COLLAPSED || steps.length > BRIEFING_COLLAPSED;

  const contentRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = contentRef.current;
    if (!node || !onContentHeight) return;
    const report = () => onContentHeight(Math.ceil(node.getBoundingClientRect().height) + 2);
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onContentHeight]);

  return (
    <Card
      className={cn(
        "@container flex flex-col",
        locked ? "border-elevation-subtle-soft" : "border-accent/20",
      )}
      data-testid="ctem-briefing"
    >
      <CardContent ref={contentRef} className="flex flex-col gap-4 p-5">
        {/* Header: title, author and age, trend, refresh, the way to the page */}
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <div
              className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center rounded-sm ring-1",
                locked ? "bg-elevation-hover ring-[var(--border-elevation-subtle)]" : "bg-accent/10 ring-accent/20",
              )}
            >
              {locked ? <Lock className="h-4 w-4 text-default-secondary" aria-hidden /> : <Newspaper className="h-4 w-4 text-accent" aria-hidden />}
            </div>
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 content-base font-semibold">
                {t_i18n("Exposure briefing")}
                <span className={cn("inline-flex items-center gap-1 content-caption font-medium", locked ? "text-default-secondary" : "text-accent")}>
                  <Sparkles className="h-3 w-3" aria-hidden /> {t_i18n("AI")}
                </span>
                {(isSample || locked) && <SampleBadge />}
              </h2>
              {subtitle && <div className="truncate content-caption text-default-secondary">{subtitle}</div>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {!locked && (
              <span className={cn("flex items-center gap-1 content-caption font-medium", trend.color)}>
                <TrendIcon className="h-3.5 w-3.5" aria-hidden />
                {t_i18n(trend.label)}
              </span>
            )}
            {onRefresh && !locked && (
              <Tip content={generating ? t_i18n("Generating...") : t_i18n("Generate my briefing")}>
                <IconButton
                  variant="default"
                  priority="tertiary"
                  aria-label={generating ? t_i18n("Generating...") : t_i18n("Generate my briefing")}
                  icon={<RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />}
                  className="h-9 w-9 text-default-secondary hover:text-accent"
                  disabled={busy}
                  onClick={onRefresh}
                />
              </Tip>
            )}
            {onOpen && !locked && (
              <Button type="button" variant="ghost" size="sm" className="h-9 gap-1 text-accent" onClick={onOpen}>
                {t_i18n("View full briefing")} <ChevronRight className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )}
          </div>
        </div>

        {ready && noProfile && (
          <GenericContextNotice
            title={t_i18n("This briefing is generic")}
            adminText={t_i18n("No organization context is set, so the analyst cannot rank findings by your sector, regions, primary assets or software. Set it up, then generate the briefing again.")}
            readerText={t_i18n("No organization context is set, so the analyst cannot rank findings by your sector, regions, primary assets or software. An administrator can set it up.")}
            testId="ctem-briefing-generic"
          />
        )}

        {/* Body (dimmed while it is a sample) */}
        <div className={cn("flex flex-col gap-4", (isSample || locked) && "pointer-events-none select-none opacity-60 grayscale")}>
          <div className="space-y-1.5">
            <p className="text-base font-semibold leading-snug" data-testid="ctem-briefing-headline">{briefing.headline}</p>
            {briefing.summary && (
              <p className={cn("content-base leading-relaxed text-default-secondary", !expanded && "line-clamp-2")}>{briefing.summary}</p>
            )}
          </div>

          {briefing.cross_product_insight && (
            <div className="flex items-start gap-2.5 rounded-sm bg-accent/[0.05] p-3">
              <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden />
              <p className={cn("content-compact leading-relaxed", !expanded && "line-clamp-2")}>
                <span className="font-semibold text-accent">{t_i18n("Cross-product insight")}: </span>
                <span className="text-default-secondary">{briefing.cross_product_insight}</span>
              </p>
            </div>
          )}

          <div className="grid gap-5 @3xl:grid-cols-2">
            {shownFindings.length > 0 && (
              <section className="space-y-3" data-testid="ctem-briefing-findings">
                <h3 className="flex items-center gap-2 content-compact font-semibold">
                  <Lightbulb className="h-4 w-4 text-accent" aria-hidden /> {t_i18n("Key findings")}
                </h3>
                <ul className="space-y-3">
                  {shownFindings.map((f, i) => (
                    <li key={i} className="flex items-start gap-2.5">
                      <span
                        className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                        style={{ backgroundColor: SEVERITY_DOT[f.severity] ?? SEVERITY_DOT.info }}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <p className="content-compact font-medium leading-tight">
                          {f.title}{" "}
                          <span className="content-caption font-normal text-default-secondary">· {t_i18n(sourceLabel(f.source))}</span>
                        </p>
                        {f.recommendation && (
                          <p className="mt-1 content-caption leading-snug text-accent">
                            <span className="font-semibold">{t_i18n("Action:")} </span>
                            {f.recommendation}
                          </p>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {shownSteps.length > 0 && (
              <section className="space-y-3" data-testid="ctem-briefing-steps">
                <h3 className="flex items-center gap-2 content-compact font-semibold">
                  <ListChecks className="h-4 w-4 text-accent" aria-hidden /> {t_i18n("Next steps")}
                </h3>
                <ol className="space-y-3">
                  {shownSteps.map((s, i) => {
                    const prio = CTEM_PRIORITY_CONFIG[s.priority ?? "medium"] ?? CTEM_PRIORITY_CONFIG.medium;
                    return (
                      <li key={i} className="flex items-start gap-3">
                        <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/10 content-caption font-bold text-accent">
                          {numbers.count(i + 1)}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="content-compact font-medium leading-tight">
                            {s.action}{" "}
                            <ToneBadge tone={prio.tone} className="ml-1 inline-flex align-middle">
                              {t_i18n(prio.label)}
                            </ToneBadge>
                          </div>
                          {ready && (
                            <div className="mt-1">
                              <FollowUpInChatButton
                                vertical={vertical}
                                contextLabel="exposure briefing"
                                action={s.action}
                                rationale={s.rationale}
                                agentId={agentId}
                                agentName={agentName}
                                agentIsBuiltin={agentIsBuiltin}
                              />
                            </div>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </section>
            )}
          </div>

          {briefing.recommended_focus && (
            <div className="flex items-start gap-2.5 border-t border-elevation-subtle-soft pt-3">
              <Target className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden />
              <p className={cn("content-compact leading-relaxed", !expanded && "line-clamp-2")}>
                <span className="font-semibold text-accent">{t_i18n("Recommended focus")}: </span>
                <span className="text-default-secondary">{briefing.recommended_focus}</span>
              </p>
            </div>
          )}
        </div>

        {more && !locked && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-9 self-start text-accent"
            onClick={() => setExpanded((e) => !e)}
            aria-expanded={expanded}
          >
            {expanded ? t_i18n("Show less") : t_i18n("Show more")}
          </Button>
        )}

        <AiLockedFooter reason={lockReason} className="border-t border-elevation-subtle-soft pt-3" />
      </CardContent>
    </Card>
  );
}
