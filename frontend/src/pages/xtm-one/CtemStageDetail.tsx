// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM stage overview - the full, actionable per-phase page (one per card).
 *
 * Deep-dive for a single CTEM stage in the user's active vertical: the score +
 * band, the AI analyst's assessment (headline, narrative, the metrics that
 * matter, and a concrete recommended action), the full KPI set + visualizations,
 * the scoring methodology, the owning analyst, the products that feed the stage
 * (with connect / enrich CTAs), and the organization context notice of
 * Settings (the only company context, #11464): missing, partial (fields
 * named) or a quiet confirmation. Two scores, labelled: Quantitative (the
 * metrics) and Qualitative (read against the organization context).
 */
import { useEffect, useRef, useState } from "react";
import { useNow } from "@/hooks/useTick";
import { useParams, useNavigate } from "react-router-dom";
import {
  Sparkles, RefreshCw, Lightbulb, Bot, ExternalLink, Plus, ShieldCheck,
  Lock, Radar, TrendingUp,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { PageSkeleton } from "@/components/ui/skeleton";
import { SampleBadge } from "@/components/ui/sample-badge";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import { useAiLockReason } from "@/hooks/useAiLockReason";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { NotFoundState } from "@/components/ui/not-found-state";
import { Tip } from "@/components/ui/tooltip";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { cn, safeExternalUrl, exactMetricValue, formatMetricValue } from "@/lib/utils";
import {
  CTEM_BAND_CONFIG, CTEM_PRODUCT_INFO, CTEM_VERTICAL_CONFIG, CTEM_STAGE_KEYS,
  CTEM_EXPECTATION_BUCKETS, ctemExpectationLabel, ctemExpectationOrder, ctemBestDeepLink,
  type CtemVerticalKey,
} from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { useMe } from "@/hooks/api/useAuthApi";
import {
  useCtemPosture, useCtemPostureHistory, useCtemStageAssessment, useRefreshStageAssessment,
} from "@/hooks/api/useCtemMetricsApi";
import type { CtemStage, CtemVertical } from "@/types";
import { MetricInfo } from "@/components/ui/metric-info";
import { signedDelta, SeverityBars, ValidationDonut, MiniMeter, LegendDot, TrendChart, type TrendSeries } from "./ctem/ctem-viz";
import { CompanyContextNotice, ContextScoredLine } from "./ctem/CompanyContextNotice";
import { demoStageByKey } from "./ctem/ctem-demo";
import { TREND_PERIODS, formatTrendDay, historyWindow, seriesChange, type TrendPeriod } from "./ctem/trend-window";
import { FollowUpInChatButton, AssignAgentButton } from "./ctem/ImplementWithAgent";
import { AssessmentFreshness, shouldAutoGenerate } from "./ctem/assessment-freshness";
import { postureBusy } from "./ctem/landing-refresh";
import { Spinner } from "@filigran/design-system";
import { ScopingRing, DiscoveryArea, ValidationMatrix, type DiscoveryPoint } from "./ctem/stage-heroes";
import { DiscoveryMix } from "./ctem/DiscoveryMix";
import { TriggerPathsCard } from "./ctem/loop/TriggerPathsCard";
import { ScopingMix } from "./ctem/ScopingMix";
import { ValidationMix } from "./ctem/ValidationMix";
import { PrioritizationMix } from "./ctem/PrioritizationMix";
import { MobilizationMix } from "./ctem/MobilizationMix";
import { savedLayoutFramework } from "./ctem/dashboard-context";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import type { CtemFact } from "@/types";

/** A fact's numeric value by its label, or null. */
function factNumber(facts: CtemFact[] | undefined, label: string): number | null {
  const v = facts?.find((f) => f.label === label)?.value;
  return typeof v === "number" ? v : null;
}

/**
 * The hero viz of a stage page (dev-docs/ctem-stage-viz.md section 3, S1):
 * drawn only from the live posture, never the sample; a missing source draws
 * the not-configured state. Prioritization (item A) and Remediation (item C)
 * have their own heroes; Governance comes with S2.
 */
function StageHero({
  stage,
  live,
  history,
  labelOf,
  aevBaseUrl,
  connected,
  framework = null,
}: {
  stage: string;
  live: CtemStage | undefined;
  /**
   * Which products are connected, from posture.products.<p>.present (UX round 9, B1):
   * a stage's products_present only lists the products that fed THAT stage, so it
   * cannot say whether a product is connected.
   */
  connected: { opencti: boolean; openaev: boolean };
  /** The connected OpenAEV's base URL (validated), for the no-data actions. */
  aevBaseUrl?: string;
  history: { date: string; stage_metrics?: Record<string, Record<string, number | null>> }[];
  labelOf: (date: string) => string;
  /** The command center's context framework (8.2): the Validation matrix opens on it. */
  framework?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const hasAev = connected.openaev;
  const hasCti = connected.opencti;
  const connect = () => navigate("/");
  const aevScenarios = aevBaseUrl ? safeExternalUrl(`${aevBaseUrl.replace(/\/+$/, "")}/admin/scenarios`) : undefined;
  const delta = (key: string) => {
    const vals = history.map((h) => h.stage_metrics?.[stage]?.[key]).filter((v): v is number => typeof v === "number");
    return vals.length >= 2 ? Math.round(vals[vals.length - 1] - vals[vals.length - 2]) : null;
  };
  if (stage === "scoping") {
    const domains = live?.viz.attack_domains ?? [];
    return (
      <ScopingRing
        domains={domains}
        hasAev={hasAev}
        aevUrl={aevBaseUrl}
        breadthPct={factNumber(live?.facts, "Scope breadth")}
        breadthDelta={delta("scope_breadth_pct")}
        figures={[
          { label: t_i18n("Endpoints in scope"), value: factNumber(live?.facts, "Endpoints in scope") },
          { label: t_i18n("Asset groups"), value: factNumber(live?.facts, "Asset groups") },
          { label: t_i18n("Security platforms"), value: factNumber(live?.facts, "Security platforms") },
        ]}
        onConnect={connect}
      />
    );
  }
  if (stage === "discovery") {
    const points: DiscoveryPoint[] = history.map((h) => ({
      date: h.date,
      label: labelOf(h.date),
      threat: h.stage_metrics?.discovery?.threat_techniques ?? null,
      tested: h.stage_metrics?.discovery?.exercised_techniques ?? null,
    }));
    return (
      <DiscoveryArea
        points={points}
        headlinePct={factNumber(live?.facts, "Threat-informed coverage")}
        headlineDelta={delta("threat_informed_coverage_pct")}
        threatTracked={factNumber(live?.facts, "Threat techniques")}
        hasCti={hasCti}
        hasAev={hasAev}
        aevUrl={aevBaseUrl}
        onConnect={connect}
      />
    );
  }
  if (stage === "validation") {
    return (
      <ValidationMatrix
        key={framework ?? "default"}
        matrix={live?.viz.outcome_matrix}
        initialChain={framework}
        preventionPct={factNumber(live?.facts, "Prevention success")}
        detectionPct={factNumber(live?.facts, "Detection success")}
        hasAev={hasAev}
        scenariosUrl={aevScenarios}
        onConnect={connect}
      />
    );
  }
  return null;
}
import {
  PrioritizationFunnel,
  PrioritizationFunnelPlaceholder,
  PrioritizationFunnelWithCases,
} from "./ctem/PrioritizationFunnel";
import { CasesBoard } from "./ctem/CasesBoard";
import { CasesInPhaseCard } from "./ctem/CasesInPhaseCard";
import { SAMPLE_FUNNEL, funnelDisplay } from "./ctem/prioritization-funnel";

function StageViz({ stage }: { stage: CtemStage }) {
  const { t_i18n } = useFormatter();
  if (stage.key === "prioritization" && (stage.viz.severity_distribution?.length ?? 0) > 0) {
    return (
      <div>
        <p className="text-[11px] text-default-secondary mb-2">{t_i18n("CVSS severity distribution")}</p>
        <SeverityBars buckets={stage.viz.severity_distribution ?? []} />
      </div>
    );
  }
  if (stage.key === "validation" && stage.viz.expectation_buckets) {
    const buckets = stage.viz.expectation_buckets;
    const types = Object.keys(buckets)
      .filter((t) => Object.values(buckets[t] ?? {}).some((v) => (v ?? 0) > 0))
      .sort((a, b) => ctemExpectationOrder(a) - ctemExpectationOrder(b));
    if (types.length === 0) return null;
    return (
      <div className="space-y-3">
        <div className="flex flex-wrap gap-5">
          {types.map((t) => (
            <div key={t} className="flex flex-col items-center gap-1.5">
              <ValidationDonut buckets={buckets[t]} size={92} />
              <span className="text-[11px] text-default-secondary">{ctemExpectationLabel(t)}</span>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-0.5">
          {CTEM_EXPECTATION_BUCKETS.map((b) => (
            <LegendDot key={b.id} color={b.color} label={b.label} />
          ))}
        </div>
      </div>
    );
  }
  return null;
}

export default function CtemStageDetail() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { stage = "" } = useParams<{ stage: string }>();
  const { data: user } = useMe();
  const vertical: CtemVertical = user?.ctem_vertical ?? "cyber";
  const verticalCfg = CTEM_VERTICAL_CONFIG[vertical as CtemVerticalKey];

  const validStage = CTEM_STAGE_KEYS.includes(stage as (typeof CTEM_STAGE_KEYS)[number]);
  const { data: posture, isLoading } = useCtemPosture(vertical);
  // No assessment read for an unknown stage: its error would replace the
  // NotFoundState below with the error boundary.
  const { data: assessmentResp } = useCtemStageAssessment(validStage ? stage : "", vertical);
  const { data: histResp } = useCtemPostureHistory(vertical);
  const refresh = useRefreshStageAssessment();
  // One lock reason (no license, AI off for the platform or the group, no
  // model), the backend status filling in what the config cannot say (C1).
  const lockReason = useAiLockReason(assessmentResp?.status);
  // The Refresh and Generate controls start a run: disabled with AI off.
  const aiOff = useAiActionDisabled();
  // Generate on visit (#12002): none stored for this vertical, or one past the
  // weekly window, once per stage and vertical for this page; the refresh
  // endpoint answers "generating" without a second run while one is in flight,
  // so concurrent visitors start one run. A locked page never generates.
  const autoGenerated = useRef<Set<string>>(new Set());
  const autoKey = `${stage}:${vertical}`;
  const wantsGeneration = validStage && lockReason === null && shouldAutoGenerate(assessmentResp);
  const { mutate: generate } = refresh;
  useEffect(() => {
    if (!wantsGeneration || autoGenerated.current.has(autoKey)) return;
    autoGenerated.current.add(autoKey);
    generate({ stage, vertical, auto: true });
  }, [wantsGeneration, autoKey, stage, vertical, generate]);
  // The figures of the page are being rebuilt (#12183): a stale posture the
  // server refreshes in the background, polled faster until it lands.
  const postureRefreshing = postureBusy(posture, { loading: isLoading, forcing: false });
  // D3-2 (#9618): the period of the History card, 30 days by default.
  const [period, setPeriod] = useState<TrendPeriod>(30);
  const today = new Date(useNow());

  if (!validStage) {
    return (
      <NotFoundState
        icon={ShieldCheck}
        title="Unknown CTEM stage"
        description="This exposure stage does not exist."
        actionLabel="Back to Command Center"
        onAction={() => navigate("/")}
        backTo="/"
        backLabel="Back to Command Center"
      />
    );
  }
  if (isLoading && !posture) return <PageSkeleton cards={3} />;

  const live = posture?.stages.find((s) => s.key === stage);
  // Deep-link to the DEDICATED resource page of the platform that DRIVES this
  // stage (owner-first: OpenAEV for Scoping / Validation, OpenCTI for the rest),
  // validated + built from the integration base_url, not always OpenCTI.
  const deep = ctemBestDeepLink(posture?.products, vertical, stage, live?.products_present);
  const stageProductUrl = deep ? safeExternalUrl(deep.url) : undefined;
  const hasReal = live?.covered ?? false;
  const demoByKey = demoStageByKey(vertical);
  const data = hasReal ? live! : demoByKey[stage] ?? live!;
  const band = CTEM_BAND_CONFIG[data.band] ?? CTEM_BAND_CONFIG.unknown;
  const dim = !hasReal;
  // Covered with real facts but no score: say why, in the lifecycle band's words.
  const unscored = hasReal && live?.score == null;
  // Discovery's score is the tested share of the tracked techniques (OpenAEV), else
  // OpenCTI's detection coverage: a 0 without OpenAEV is the lack of a module, not
  // a Critical posture, and is said so (#11998). A detection coverage above 0 stays.
  const scoreNeedsOpenAev =
    hasReal &&
    stage === "discovery" &&
    vertical === "cyber" &&
    !posture?.products?.openaev?.present &&
    !(Number(live?.score) > 0);

  // Always render the full fact set + viz with greyed placeholders for the gaps
  // (identical whether one product or both are connected), like the dashboard
  // cards. Merge the real facts over the sample's complete set.
  const sampleStage = demoByKey[stage];
  const realByLabel = new Map((live?.facts ?? []).map((f) => [f.label, f]));
  const sampleLabels = new Set((sampleStage?.facts ?? []).map((f) => f.label));
  const mergedFacts = [
    ...(sampleStage?.facts ?? []).map((sf) => {
      const real = realByLabel.get(sf.label);
      return real ? { ...real, placeholder: false } : { ...sf, placeholder: true };
    }),
    ...(live?.facts ?? [])
      .filter((f) => !sampleLabels.has(f.label))
      .map((f) => ({ ...f, placeholder: false })),
  ];
  const realHasViz =
    !!live &&
    (stage === "prioritization"
      ? (live.viz.severity_distribution?.length ?? 0) > 0
      : stage === "validation"
        ? Object.values(live.viz.expectation_buckets ?? {}).some((b) =>
            Object.values(b ?? {}).some((v) => (v ?? 0) > 0),
          )
        : false);
  const vizStage = realHasViz ? live! : sampleStage ?? data;
  const vizPlaceholder = !realHasViz;
  const meterFact = mergedFacts.find(
    (f) => f.kind === "derived" && f.unit === "%" && typeof f.value === "number",
  );

  const assessment = assessmentResp?.assessment ?? null;
  const status = assessmentResp?.status;
  // Locked for any reason: the box says why with one way forward, and no
  // Refresh or Generate button calls an endpoint that refuses.
  const eeLocked = lockReason !== null;
  const generating = status === "generating" || !!assessmentResp?.generating;

  const owner = live?.agent;
  const contextMissing = posture?.context?.state === "missing";
  const qualitative = {
    score: hasReal ? live?.qualitative_score ?? null : null,
    band: hasReal ? live?.qualitative_band ?? "unknown" : "unknown",
  } as const;
  // A module a gap filler of the stage's mixed views already offers is never offered
  // again in the bottom card (UX round 1 of #12142, B2): the mixes draw a gap filler for
  // every module posture.products says is absent.
  const hasGapFillers = hasReal && vertical === "cyber" && ["discovery", "prioritization", "validation", "scoping", "remediation"].includes(stage);
  const missing = (live?.missing_products ?? []).filter(
    (m) => !hasGapFillers || Boolean(posture?.products?.[m as "opencti" | "openaev" | "opencrq"]?.present),
  );

  // Historical evolution of THIS stage's readiness metrics: the stage score plus
  // every percentage metric snapshotted over time (one point per recorded day).
  const history = historyWindow(histResp?.history ?? [], period, today);
  const histLabels = history.map((h) => formatTrendDay(intl, h.date));
  const histMetricKeys = Array.from(
    new Set(
      history.flatMap((h) =>
        Object.keys(h.stage_metrics?.[stage] ?? {}).filter((k) => k.endsWith("_pct")),
      ),
    ),
  );
  const humanizeMetric = (k: string) => {
    const s = k.replace(/_pct$/, "").replace(/_/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  };
  const histSeries: TrendSeries[] = [
    {
      key: "score",
      label: t_i18n("Stage score"),
      color: "var(--color-accent)",
      values: history.map((h) => h.stage_metrics?.[stage]?.score ?? null),
    },
    ...histMetricKeys.map((k) => ({
      key: k,
      label: humanizeMetric(k),
      values: history.map((h) => h.stage_metrics?.[stage]?.[k] ?? null),
    })),
  ];
  // The stage score change over the period: last recorded day minus the first.
  const scoreTrend = seriesChange(histSeries[0].values);
  const trendSince = scoreTrend.fromIndex === null ? null : history[scoreTrend.fromIndex]?.date ?? null;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* The one CTEM header band (#11660): breadcrumb over the title, the
          methodology behind the "i", one cell per score, the period and the
          deep link on the title line. */}
      <CtemPageHeader
        back={{ to: "/", label: "Back to Command Center" }}
        crumb={t_i18n("Stage {index} of {count}", { index: CTEM_STAGE_KEYS.indexOf(stage as (typeof CTEM_STAGE_KEYS)[number]) + 1, count: CTEM_STAGE_KEYS.length })}
        crumbExtras={
          <>
            {verticalCfg && (
              <Badge variant="outline" className="content-caption text-accent border-accent/30">
                {verticalCfg.label}
              </Badge>
            )}
            {dim && <SampleBadge />}
            {postureRefreshing && (
              <span className="inline-flex items-center gap-1.5 content-caption text-default-secondary" role="status" data-testid="ctem-stage-refreshing">
                <Spinner size="sm" tone="inherit" className="text-accent" />
                {t_i18n("Refreshing...")}
              </span>
            )}
            {unscored && (
              <Badge variant="outline" className="content-caption" data-testid="ctem-stage-unscored">
                {t_i18n("Not enough data to score")}
              </Badge>
            )}
            {owner && (
              <button
                type="button"
                onClick={() => navigate(`/agents/${owner.id}`)}
                className="inline-flex min-w-0 items-center gap-1.5 truncate content-caption text-default-secondary hover:text-accent transition-colors"
                title={owner.name}
              >
                <Bot className="h-3 w-3 shrink-0 text-accent/70" /> {owner.name}
              </button>
            )}
            {/* A complete organization context is confirmed on this line, never on a row of its own. */}
            {posture?.context?.state === "complete" && <ContextScoredLine inline />}
          </>
        }
        title={data.label}
        hint={data.methodology || undefined}
        hintLabel={t_i18n("About {name}", { name: data.label })}
        stats={
          unscored
            ? []
            : scoreNeedsOpenAev
            ? [
                {
                  id: "stage-score",
                  label: t_i18n("Stage score"),
                  value: <span className="text-default-secondary" data-testid="ctem-score-needs-module">--</span>,
                  sub: <span className="text-default-secondary">{t_i18n("Not scored: needs OpenAEV")}</span>,
                },
              ]
            : [
                {
                  id: "stage-score",
                  label: t_i18n("Stage score"),
                  value: <span className={band.color} data-testid="ctem-score-quantitative">{data.score ?? "--"}</span>,
                  sub: (
                    <span className="inline-flex items-center gap-1.5">
                      <span className={band.color}>{t_i18n(band.label)}</span>
                      {hasReal && scoreTrend.change != null && (
                        <span
                          className={scoreTrend.change > 0 ? "text-feedback-success-primary" : scoreTrend.change < 0 ? "text-feedback-error-primary" : "text-default-secondary"}
                          aria-label={t_i18n("{delta} since the start of the period", { delta: signedDelta(scoreTrend.change) })}
                          data-testid="ctem-score-quantitative-delta"
                        >
                          {signedDelta(scoreTrend.change)}
                        </span>
                      )}
                    </span>
                  ),
                },
                ...(hasReal
                  ? [
                      {
                        id: "organization-score",
                        label: t_i18n("Organization"),
                        value: (
                          <span
                            className={(CTEM_BAND_CONFIG[qualitative.band] ?? CTEM_BAND_CONFIG.unknown).color}
                            data-testid="ctem-score-qualitative"
                          >
                            {qualitative.score ?? "--"}
                          </span>
                        ),
                        sub:
                          qualitative.score != null ? (
                            <span className={(CTEM_BAND_CONFIG[qualitative.band] ?? CTEM_BAND_CONFIG.unknown).color}>
                              {t_i18n((CTEM_BAND_CONFIG[qualitative.band] ?? CTEM_BAND_CONFIG.unknown).label)}
                            </span>
                          ) : contextMissing ? (
                            <span className="text-default-secondary">{t_i18n("No organization context")}</span>
                          ) : undefined,
                      },
                    ]
                  : []),
              ]
        }
        actions={
          <>
            {hasReal && (
              <SegmentedFilter
                size="sm"
                selection="radio"
                ariaLabel={t_i18n("History period")}
                testId="ctem-stage-history-period"
                options={TREND_PERIODS.map((days) => ({
                  key: String(days),
                  label: t_i18n("{count, plural, one {# day} other {# days}}", { count: days }),
                }))}
                value={String(period)}
                onChange={(key) => setPeriod(Number(key) as TrendPeriod)}
              />
            )}
            {deep && stageProductUrl && (
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5 h-8 content-compact border-accent/30 text-accent hover:bg-accent/10 shrink-0"
                asChild
              >
                <a href={stageProductUrl} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="h-3.5 w-3.5" /> {deep.label}
                </a>
              </Button>
            )}
          </>
        }
      />

      {/* The organization context of Settings, the only company context (#11464):
          a missing or partial context only; a complete one sits on the breadcrumb line. */}
      <CompanyContextNotice context={posture?.context} placement="brief" />

      {/* The signature viz of Prioritization (#11546): the funnel from the raw
          volume to what needs action, above the AI assessment and the facts.
          No OpenCTI connected: the sample funnel, badged Sample. OpenCTI connected
          and no funnel yet: a placeholder, never the sample (#11662). */}
      {stage === "prioritization" && vertical === "cyber" && (
        <Card className="overflow-hidden">
          <CardContent className="p-5">
            {/* A live funnel ends on the open cases (#11545 S5b); the sample reads nothing. */}
            {(() => {
              const openctiUrl = posture?.products?.opencti?.url;
              const display = funnelDisplay({
                funnel: live?.viz.funnel,
                openctiPresent: Boolean(posture?.products?.opencti?.present),
                shape: posture?.shape,
              });
              if (display === "live" && live?.viz.funnel) {
                return <PrioritizationFunnelWithCases funnel={live.viz.funnel} openctiUrl={openctiUrl} readAt={posture?.generated_at} />;
              }
              if (display === "pending" || display === "unavailable") {
                return <PrioritizationFunnelPlaceholder display={display} variant="stage" openctiUrl={openctiUrl} />;
              }
              return <PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} sample openctiUrl={openctiUrl} />;
            })()}
          </CardContent>
        </Card>
      )}

      {/* The signature viz of Mobilization (dev-docs/ctem-cases.md 7.1, S5c of #11545):
          the Cases board, drawn from the cases the caller sees. */}
      {stage === "remediation" && vertical === "cyber" && <CasesBoard />}

      {/* Hero first (dev-docs/ctem-stage-viz.md rule 1): the stage's signature viz. */}
      {/* Without OpenAEV the Validation hero would be an empty matrix frame: ValidationMix's OpenAEV gap filler says it instead (#11998). */}
      {hasReal && !(stage === "validation" && vertical === "cyber" && !posture?.products?.openaev?.present) && (
        <StageHero
          stage={stage}
          live={live}
          history={historyWindow(histResp?.history ?? [], period, today)}
          labelOf={(d) => formatTrendDay(intl, d)}
          aevBaseUrl={safeExternalUrl(posture?.products?.openaev?.url ?? undefined)}
          connected={{
            opencti: Boolean(posture?.products?.opencti?.present),
            openaev: Boolean(posture?.products?.openaev?.present),
          }}
          framework={savedLayoutFramework(user?.ctem_dashboard_layouts?.[vertical], live?.viz.outcome_matrix)}
        />
      )}

      {/* Discovery's mixed views and gap fillers (#11998): no module combination is an empty page. */}
      {hasReal && stage === "discovery" && vertical === "cyber" && (
        <DiscoveryMix
          viz={live?.viz}
          connected={{
            opencti: Boolean(posture?.products?.opencti?.present),
            openaev: Boolean(posture?.products?.openaev?.present),
            opencrq: Boolean(posture?.products?.opencrq?.present),
          }}
          onConnect={() => navigate("/")}
        />
      )}
      {/* The loop's signals by trigger path and lens (CTEM-30, #937): each path's status and Connect are the Signal sources page's (#1263). */}
      {stage === "discovery" && vertical === "cyber" && <TriggerPathsCard />}
      {/* Prioritization's mixed views and gap fillers (#11998), under the funnel. */}
      {hasReal && stage === "prioritization" && vertical === "cyber" && (
        <PrioritizationMix
          viz={live?.viz}
          connected={{
            opencti: Boolean(posture?.products?.opencti?.present),
            openaev: Boolean(posture?.products?.openaev?.present),
            opencrq: Boolean(posture?.products?.opencrq?.present),
          }}
          onConnect={() => navigate("/")}
          openctiUrl={posture?.products?.opencti?.present ? posture.products.opencti.url : null}
        />
      )}
      {/* Validation's mixed views and gap fillers (#11998), under the outcome matrix. */}
      {hasReal && stage === "validation" && vertical === "cyber" && (
        <ValidationMix
          viz={live?.viz}
          connected={{
            opencti: Boolean(posture?.products?.opencti?.present),
            openaev: Boolean(posture?.products?.openaev?.present),
            opencrq: Boolean(posture?.products?.opencrq?.present),
          }}
          onConnect={() => navigate("/")}
        />
      )}
      {/* Mobilization's mixed views and gap fillers (#11998), under the Cases board. */}
      {hasReal && stage === "remediation" && vertical === "cyber" && (
        <MobilizationMix
          viz={live?.viz}
          connected={{
            opencti: Boolean(posture?.products?.opencti?.present),
            openaev: Boolean(posture?.products?.openaev?.present),
            opencrq: Boolean(posture?.products?.opencrq?.present),
          }}
          onConnect={() => navigate("/")}
        />
      )}
      {/* Scoping's mixed views and gap fillers (#11998). */}
      {hasReal && stage === "scoping" && vertical === "cyber" && (
        <ScopingMix
          viz={live?.viz}
          connected={{
            opencti: Boolean(posture?.products?.opencti?.present),
            openaev: Boolean(posture?.products?.openaev?.present),
            opencrq: Boolean(posture?.products?.opencrq?.present),
          }}
          onConnect={() => navigate("/")}
        />
      )}

      {/* The cases in this phase (#1621): its own full-width row, so the AI assessment and
          Metrics row below keeps its equal heights. */}
      {validStage && vertical === "cyber" && <CasesInPhaseCard stage={stage as (typeof CTEM_STAGE_KEYS)[number]} backTo={`/ctem/${stage}`} />}

      <div className="grid gap-5 lg:grid-cols-3">
        {/* AI assessment - the actionable centerpiece */}
        <Card className="lg:col-span-2 overflow-hidden border-accent/20">
          <CardContent className="p-5 space-y-4">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0 space-y-0.5">
                <h2 className="content-base font-semibold flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-accent" /> {t_i18n("AI assessment")}
                </h2>
                {!eeLocked && assessment && (
                  <AssessmentFreshness
                    generatedAt={assessmentResp?.generated_at}
                    agentName={assessmentResp?.agent_name}
                    stale={!!assessmentResp?.stale}
                    generating={generating || refresh.isPending}
                    freshDays={assessmentResp?.fresh_days}
                  />
                )}
              </div>
              {!eeLocked && (
                <Tip content={t_i18n("Regenerate this assessment (scoped to you and your active vertical)")}>
                  <Button
                    size="sm"
                    variant="outline"
                    className="gap-1.5 h-7 content-compact"
                    onClick={() => refresh.mutate({ stage, vertical })}
                    disabled={refresh.isPending || generating || aiOff}
                  >
                    <RefreshCw className={cn("h-3 w-3", (refresh.isPending || generating) && "animate-spin")} />
                    {t_i18n("Refresh")}
                  </Button>
                </Tip>
              )}
            </div>

            {eeLocked ? (
              <div className="flex items-start gap-3 rounded-sm bg-elevation-hover p-4">
                <Lock className="h-4 w-4 mt-0.5 text-default-secondary shrink-0" />
                {/* Stacked: the card is ~470 px wide at 1024, too narrow for the
                    sentence and both actions on one row (UX round 2 B1). */}
                <AiLockedFooter reason={lockReason} layout="stacked" className="flex-1" />
              </div>
            ) : assessment ? (
              <div className="space-y-4">
                <div>
                  <p className="text-base font-semibold leading-snug">{assessment.headline}</p>
                  <p className="content-base text-default-secondary leading-relaxed mt-1.5">
                    {assessment.assessment}
                  </p>
                </div>

                {(assessment.key_metrics?.length ?? 0) > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {assessment.key_metrics!.map((m, i) => (
                      <div key={i} className="rounded-sm bg-elevation-hover px-3 py-1.5">
                        <p className="content-caption text-default-secondary">{m.label}</p>
                        <p className="content-base font-semibold tabular-nums">{m.value}</p>
                      </div>
                    ))}
                  </div>
                )}

                {assessment.recommended_action && (
                  <div className="rounded-sm bg-accent/[0.06] ring-1 ring-accent/20 p-3.5 flex items-start gap-2.5">
                    <Lightbulb className="h-4 w-4 text-accent mt-0.5 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="content-caption uppercase tracking-wider text-accent/80 font-medium mb-0.5">
                        {t_i18n("Recommended action")}
                      </p>
                      <p className="content-base">{assessment.recommended_action}</p>
                    </div>
                  </div>
                )}

                {/* Every assessment carries both actions, with or without a
                    recommended action, as the exposure briefing card (#12002). */}
                <div className="flex flex-wrap items-center gap-2">
                  <FollowUpInChatButton
                    vertical={vertical}
                    contextLabel={`${data.label} stage`}
                    action={assessment.recommended_action || assessment.headline}
                    agentId={assessmentResp?.agent_id}
                    agentName={assessmentResp?.agent_name}
                    agentIsBuiltin={assessmentResp?.agent_is_builtin}
                  />
                  <AssignAgentButton />
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-3 rounded-sm bg-elevation-hover p-4">
                <Radar className={cn("h-4 w-4 text-accent shrink-0", (generating || refresh.isPending) && "animate-pulse")} />
                <div className="flex-1">
                  <p className="content-compact text-default-secondary">
                    {generating || refresh.isPending
                      ? t_i18n("Generating your assessment - this runs as you, scoped to your access.")
                      : t_i18n("No assessment yet for this vertical. Generate one to get an actionable read.")}
                  </p>
                </div>
                {!generating && !refresh.isPending && (
                  <Button
                    size="sm"
                    className="gap-1.5 h-7 content-compact bg-accent text-negative-primary hover:bg-accent/90"
                    onClick={() => refresh.mutate({ stage, vertical })}
                    disabled={refresh.isPending || aiOff}
                  >
                    <Sparkles className="h-3 w-3" /> {t_i18n("Generate")}
                  </Button>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* KPIs + viz */}
        <Card className="overflow-hidden">
          <CardContent className="p-5 space-y-4">
            <h2 className="content-base font-semibold flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-accent" /> {t_i18n("Metrics")}
              <span
                className={cn("ml-auto text-2xl font-bold tabular-nums", scoreNeedsOpenAev ? "text-default-secondary" : band.color)}
                data-testid="ctem-metrics-score"
                data-band={scoreNeedsOpenAev ? "none" : "score"}
              >
                {scoreNeedsOpenAev ? "--" : (data.score ?? "--")}
              </span>
            </h2>
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-x-4 gap-y-3">
                {mergedFacts.map((f) => (
                  <div key={f.label} className={cn("min-w-0", f.placeholder && "opacity-50")}>
                    <div className="flex items-center gap-1 min-w-0">
                      <p className="content-caption text-default-secondary truncate">{f.label}</p>
                      <MetricInfo label={f.label} />
                    </div>
                    <p className="content-base font-semibold tabular-nums" title={exactMetricValue(f.value)}>
                      {formatMetricValue(f.value)}
                      {f.unit && <span className="text-[11px] text-default-secondary ml-0.5">{f.unit}</span>}
                    </p>
                  </div>
                ))}
              </div>
              {meterFact && (
                <div className={cn(meterFact.placeholder && "opacity-50 grayscale")}>
                  <MiniMeter label={meterFact.label} value={meterFact.value as number} color={band.ring} />
                </div>
              )}
              <div className={cn(vizPlaceholder && "opacity-50 grayscale")}>
                <StageViz stage={vizStage} />
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Historical evolution of this stage's readiness metrics: deterministic,
          so it shows in Community Edition and with AI off as well. */}
      {hasReal && (
        <Card className="overflow-hidden">
          <CardContent className="p-5 space-y-3">
            {/* The period is chosen once, in the header band (#11660). */}
            <h2 className="content-base font-semibold flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-accent" /> {t_i18n("History")}
            </h2>
            <p className="content-compact text-default-secondary tabular-nums" data-testid="ctem-stage-history-change">
              {scoreTrend.change === null || trendSince === null
                ? t_i18n("No change to show yet: the stage needs a score on two recorded days in the period.")
                : scoreTrend.change === 0
                  ? t_i18n("Stage score unchanged since {date}", { date: formatTrendDay(intl, trendSince) })
                  : t_i18n("Stage score {count, plural, one {{signed} point} other {{signed} points}} since {date}", {
                      count: Math.abs(scoreTrend.change),
                      signed: scoreTrend.change > 0 ? `+${scoreTrend.change}` : String(scoreTrend.change),
                      date: formatTrendDay(intl, trendSince),
                    })}
            </p>
            <TrendChart labels={histLabels} series={histSeries} height={120} />
          </CardContent>
        </Card>
      )}

      {/* Products feeding this stage + blind-spot CTAs */}
      {missing.length > 0 && (
        <Card className="overflow-hidden border-dashed border-elevation-subtle-soft">
          <CardContent className="p-4 flex items-center gap-3 flex-wrap">
            <p className="content-compact text-default-secondary flex-1 min-w-0">
              {t_i18n(
                "This stage is strengthened by {products}. Connect {count, plural, one {it} other {them}} to unlock the full assessment.",
                { products: missing.map((m) => CTEM_PRODUCT_INFO[m]?.name ?? m).join(", "), count: missing.length },
              )}
            </p>
            {missing.map((m) => (
              <Button
                key={m}
                size="sm"
                variant="outline"
                className="gap-1.5 h-8 content-compact border-accent/30 text-accent hover:bg-accent/10"
                onClick={() => navigate("/")}
              >
                <Plus className="h-3 w-3" /> {t_i18n("Connect {name}", { name: CTEM_PRODUCT_INFO[m]?.name ?? m })}
              </Button>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
