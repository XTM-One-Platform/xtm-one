// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNow } from "@/hooks/useTick";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import {
  Bot, Zap, MessageSquare, ShieldAlert, Radar, Target, BarChart3,
  ArrowUpRight, Plus, ChevronRight, ShieldCheck, Link2,
  Scale, ExternalLink, Sparkles, RefreshCw, type LucideIcon,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { DiffBadge } from "@/components/ui/diff-badge";
import { RunCard } from "@/components/ui/run-card";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { PageSkeleton } from "@/components/ui/skeleton";
import { AiLockedFooter, EditionBadge } from "@/components/ui/enterprise-upsell";
import { EmptyState } from "@/components/ui/empty-state";
import { SampleBadge } from "@/components/ui/sample-badge";
import { ToneBadge } from "@/components/ui/tone-badge";
import { cn, formatCompactNumber, safeExternalUrl, saveBlobAs } from "@/lib/utils";
import { csvBlob } from "@/lib/csv";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { useFormatter } from "@/i18n";
import {
  CTEM_STAGE_KEYS, CTEM_STAGE_CONFIG, CTEM_PRODUCT_INFO, CTEM_PRIORITY_CONFIG, CTEM_BAND_CONFIG, ICON_MAP, PROVIDER_LOGOS, ctemBestDeepLink, ctemDeepLink, ctemExpectationLabel, type CtemStageKey,
} from "@/lib/constants";
import { XTM_HUB_PROVIDER, xtmHubPill } from "@/lib/xtm-hub";
import { productHealth, productPillTone, type ProductHealth } from "@/lib/health-modules";
import { useHealthOverview } from "@/hooks/api/useHealthApi";
import type { PlatformRegistration } from "@/hooks/api/usePlatformApi";
import type { CtemStage, CtemVertical, CtemWidgetId, DigestItem } from "@/types";
import { MetricInfo } from "@/components/ui/metric-info";
import {
  useCtemPosture, useCtemPostureHistory, useCtemBriefing, useRefreshMyBriefing, useRefreshCtemPosture,
  useCtemStageAssessments, useRefreshStageAssessment,
} from "@/hooks/api/useCtemMetricsApi";
import { assessmentBusy, briefingBusy, briefingShown, postureBusy, shouldAutoGenerateBriefing } from "./ctem/landing-refresh";
import { shouldAutoGenerate } from "./ctem/assessment-freshness";
import { useDashboardStats } from "@/hooks/api/useMiscApi";
import { useAllAssignmentRuns } from "@/hooks/api/useAssignmentApi";
import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useXtmHubRegistrationStatus } from "@/hooks/api/useXtmHubApi";
import { useAuth } from "@/hooks/useAuth";
import {
  usePlatformName, useAgenticSource, useXTMLicense, useAgenticAIEnabled, usePlatformConfig,
} from "@/hooks/usePlatformConfig";
import { useAiLockReason } from "@/hooks/useAiLockReason";
import { MiniTrend, PostureOrbit, signedDelta } from "./ctem/ctem-viz";
import { CtemStatRow, type CtemHeaderStat } from "./ctem/CtemPageHeader";
import { CtemWidgetHeader } from "./ctem/CtemWidgetHeader";
import { ChooseFleetsDialog } from "@/components/fleets/ChooseFleetsDialog";
import { CtemLoopSetupEntry, useDashboardSetupEntry } from "@/components/fleets/CtemLoopSetupEntry";
import { CompanyContextNotice, ContextScoredLine } from "./ctem/CompanyContextNotice";
import { useFleets } from "@/hooks/api/useFleetApi";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { CtemStageCard } from "./ctem/CtemStageCard";
import { BriefingCard } from "./ctem/BriefingCard";
import { VerticalSwitcher } from "./ctem/VerticalSwitcher";
import { ConnectPlatformDialog } from "./ctem/ConnectPlatformDialog";
import { PostureExplainDialog } from "./ctem/PostureExplainDialog";
import { CustomizeButton, DashboardGrid } from "@/components/dashboard-grid/DashboardGrid";
import { kpiDensity, kpiLayout, kpiShowsIcon, listCapacity } from "@/components/dashboard-grid/widget-density";
import { WidgetSized } from "@/components/dashboard-grid/widget-size";
import { useWidgetSize } from "@/components/dashboard-grid/widget-size-context";
import { useDashboardLayoutState } from "@/hooks/useDashboardLayoutState";
import { CTEM_WIDGETS, valueBandCarriesTopLine, withActNowHeight, withClosedFleetActivity, withContentHeight } from "./ctem/dashboard-layout";
import { heroDensity } from "./ctem/widget-density";
import { DEMO_ACTIVITY, demoPosture, demoStageByKey, demoBriefing } from "./ctem/ctem-demo";
import { DashboardNotices } from "./DashboardNotices";
import { FleetActivityCard } from "./ctem/FleetActivityCard";
import { FleetTimelineCard } from "./ctem/FleetTimelineCard";
import { RecommendedActionsEmpty } from "./ctem/RecommendedActionsEmpty";
import { ActNowCard } from "./ctem/ActNowCard";
import { DoneForYouCard } from "./ctem/DoneForYouCard";
import { LifecycleBandCard } from "./ctem/LifecycleBandCard";
import { StatsCoverageCard } from "./ctem/StatsCoverageCard";
import { metricSeries } from "./ctem/stats-coverage";
import { FleetsRowCard } from "./ctem/FleetsRowCard";
import { PrioritizationFunnelCard } from "./ctem/PrioritizationFunnel";
import { CasesCountsCard } from "./ctem/CasesCountsCard";
import { SAMPLE_FUNNEL, funnelDisplay } from "./ctem/prioritization-funnel";
import { ctemKpiOpenCtiHref } from "./ctem/opencti-link";
import { ACT_NOW_PRIORITY, ASK_AGENT_ROW_ID, actNowFleetWork, type ActNowRow, type ActNowSource } from "./ctem/act-now";
import { lockedKpiFor, type KpiDef } from "./ctem/locked-kpis";
import { deterministicNextActions, EXPECTATION_REASONS, EXPECTATION_REASON_FALLBACK, rankNextActions } from "./ctem/next-actions";
import { postureCsv, postureCsvFilename } from "./ctem/posture-export";
import { LifecycleHeader } from "./ctem/LifecycleHeader";
import { ActivityFeed } from "./ctem/ActivityFeed";
import { ActivityRow } from "./ctem/ActivityRow";
import { deterministicActivity, feedCapacity, mergeActivity, signalActivity } from "./ctem/activity-feed";
import { useRecentCtemSignals, type CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { signalsHref } from "./ctem/loop/signals-route";
import { CoverageMatrixCard } from "./ctem/CoverageMatrixCard";
import { ExposureStatsCard } from "./ctem/ExposureStatsCard";
import { ValueBandCard } from "./ctem/ValueBandCard";
import { historyWindow, periodChange, seriesChange } from "./ctem/trend-window";
import { DashboardContextBar } from "./ctem/DashboardContextBar";
import { WidgetSettingsDialog } from "./ctem/WidgetSettingsDialog";
import {
  PERIOD_WIDGETS, contextFrameworks, effectiveFramework, overriddenWidgets, readContext, widgetPeriod,
  withContext, withDefaultContext, withWidgetSettings,
} from "./ctem/dashboard-context";
import { coverageMatrix, exposureStats } from "@/lib/ctem-coverage-stats";
import { useCtemFleetActivity } from "@/hooks/api/useCtemFleetActivity";
import { useCtemCases, useCtemCasesOverview } from "@/hooks/api/useCtemCasesApi";
import { inviteVisible } from "@/lib/fleet-activity";
import { formatTimeAgo } from "@/lib/relative-time";

type Product = "opencti" | "openaev" | "opencrq";
const PRODUCT_ORDER = ["opencti", "openaev", "opencrq"] as const;

/** Per-registration edition: backend-resolved contract, XTM sub-license
 *  coverage, OR self-reported EE, mirroring the backend `_ee_platform_status`
 *  rule. */
function editionFor(
  reg: PlatformRegistration,
  subLicenses?: { opencti_ids: string[]; openaev_ids: string[]; opencrq_ids: string[] } | null,
): "EE" | "CE" {
  // `reg.contract` is the backend's single source of truth (`_resolve_edition`),
  // which already folds in both a server-validated enterprise PEM and a
  // self-reported EE `license_type` (e.g. NFR/enterprise/trial that ships no
  // PEM). Trust it first so an EE platform without a PEM-validated license isn't
  // mislabeled "CE" here.
  if (reg.contract === "EE") return "EE";
  if (reg.license_validated) return "EE";
  if (subLicenses) {
    const ids =
      reg.platform_identifier === "opencti" ? subLicenses.opencti_ids
      : reg.platform_identifier === "openaev" || reg.platform_identifier === "openbas" ? subLicenses.openaev_ids
      : reg.platform_identifier === "opencrq" ? subLicenses.opencrq_ids
      : [];
    if (ids.includes("global") || (reg.platform_id && ids.includes(reg.platform_id))) return "EE";
  }
  return "CE";
}

/** Empty real stage so a card renders the greyed sample preview when `/posture`
 *  has no row for this stage yet (or failed to load). Takes the already-resolved
 *  (memoized) demo-stage map so it never rebuilds the sample data per call. */
function emptyStage(key: string, demoStages: Record<string, CtemStage>): CtemStage {
  const demo = demoStages[key];
  return {
    ...demo,
    score: null,
    prev_score: null,
    covered: false,
    confidence: "none",
    missing_products: [],
    products_present: [],
    facts: [],
    viz: {},
  };
}

/** The three vertical-specific KPIs shown between "Exposure posture" and the
 *  agentic KPIs. Each reads a key from the (vertical-complete) posture headline,
 *  so FIMI/Fraud never surface cyber CVE/ATT&CK metrics. `product` is the source
 *  whose presence gates a real (vs greyed sample) value. */
const VERTICAL_KPIS: Record<CtemVertical, KpiDef[]> = {
  cyber: [
    { label: "Known-exploited", headlineKey: "kev_count", sub: "actively exploited (KEV)", icon: ShieldAlert, accent: "text-feedback-error-primary/40", product: "opencti", stage: "prioritization" },
    { label: "Detection coverage", headlineKey: "detection_coverage_pct", unit: "%", sub: "ATT&CK with detection", icon: Radar, accent: "text-accent/40", product: "opencti", stage: "discovery" },
    { label: "Validation success", headlineKey: "validation_success_pct", unit: "%", sub: "prevention + detection", icon: Target, accent: "text-filigran-ia-primary/40", product: "openaev", stage: "validation" },
  ],
  fimi: [
    { label: "Narratives tracked", headlineKey: "narratives_total", sub: "across monitored channels", icon: Radar, accent: "text-accent/40", product: "opencti", stage: "scoping" },
    { label: "Attributed channels", headlineKey: "attribution_pct", unit: "%", sub: "linked to a threat actor", icon: Target, accent: "text-filigran-ia-primary/40", product: "opencti", stage: "prioritization" },
    { label: "Corroborated", headlineKey: "corroboration_pct", unit: "%", sub: "with mapped infrastructure", icon: ShieldCheck, accent: "text-accent/40", product: "opencti", stage: "validation" },
  ],
  fraud: [
    { label: "Phishing assets", headlineKey: "phishing_assets", sub: "domains / URLs in the wild", icon: ShieldAlert, accent: "text-feedback-error-primary/40", product: "opencti", stage: "discovery" },
    { label: "Confirmed malicious", headlineKey: "confirmed_pct", unit: "%", sub: "validated fraud indicators", icon: Target, accent: "text-filigran-ia-primary/40", product: "opencti", stage: "validation" },
    { label: "Takedown closure", headlineKey: "case_closure_pct", unit: "%", sub: "phishing / brand-abuse cases", icon: ShieldCheck, accent: "text-accent/40", product: "opencti", stage: "remediation" },
  ],
};

// Open an external target in a new tab, but only after validating it down to an
// absolute http(s) URL (product URLs are stored unvalidated at registration).
function openExternal(url?: string | null) {
  const safe = safeExternalUrl(url);
  if (safe) window.open(safe, "_blank", "noopener,noreferrer");
}

/** Human-friendly "time until next sync" label. Posture is collected daily, so
 *  the remaining window is usually hours - render m / h / d instead of e.g.
 *  "1440m". */
function formatNextSync(ms: number): string {
  // Always round UP so the label never promises a refresh sooner than it lands
  // (e.g. 89 min reads "2h", not "1h").
  const mins = Math.ceil(ms / 60000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.ceil(mins / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.ceil(hours / 24)}d`;
}

// ── KPI card ──────────────────────────────────────────────────────────────────
function KpiCard({
  label, value, unit, sub, icon: Icon, accent, onClick, current, previous, sample,
}: {
  label: string;
  value: number | null;
  unit?: string;
  sub?: string;
  icon: LucideIcon;
  accent?: string;
  onClick?: () => void;
  current?: number;
  previous?: number;
  sample?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const size = useWidgetSize();
  const density = kpiDensity(size);
  const layout = kpiLayout(size);
  const badge = sample ? (
    <SampleBadge className="shrink-0" />
  ) : current != null && previous != null ? (
    <DiffBadge current={current} previous={previous} label={t_i18n("vs prev")} />
  ) : null;
  return (
    <Card
      className={cn("h-full flex flex-col transition-all duration-300", layout.card, onClick && ["cursor-pointer hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING])}
      {...buttonRoleProps(onClick)}
    >
      <CardContent className={cn("flex justify-between", layout.content)}>
        <div className="min-w-0">
          {/* In the grid every card reserves a two-line label block and
              top-aligns its content, so the values of a row share one baseline
              (kpiLayout). A narrow card wraps its label in that block and drops
              the wide tracking so its longest word ("Corroborated") fits whole;
              the title reads the label in full if a translation is still
              clamped. */}
          <div className={cn("flex gap-2", layout.labelRow)} data-kpi-label-row>
            <p
              className={cn("content-caption font-medium text-default-secondary uppercase", layout.label)}
              title={t_i18n(label)}
            >
              {t_i18n(label)}
            </p>
            <MetricInfo label={label} />
            {!density.badgeByValue && badge}
          </div>
          <div className="flex items-center gap-1.5 mt-1">
            <p className={cn("text-3xl font-bold tabular-nums leading-none", sample && "text-default-secondary")}>
              {value == null ? <span className="text-default-secondary">--</span> : <AnimatedNumber value={value} />}
              {value != null && unit && <span className="text-base text-default-secondary ml-0.5">{unit}</span>}
            </p>
            {density.badgeByValue && badge}
          </div>
          {sub && !density.hideSub && (
            <p className="text-[11px] text-default-secondary mt-2 line-clamp-2" title={t_i18n(sub)}>
              {t_i18n(sub)}
            </p>
          )}
        </div>
        {kpiShowsIcon(density, !!sample) && (
          <Icon className={cn("h-8 w-8 shrink-0", sample ? "text-default-disabled" : accent ?? "text-default-disabled")} />
        )}
      </CardContent>
    </Card>
  );
}

// ── Product status pill ─────────────────────────────────────────────────────
function ProductPill({
  product, present, edition, healthy, health, version, url, comingSoon, onConnect, label, logo, compact = false,
}: {
  product: string;
  present: boolean;
  edition?: "EE" | "CE";
  healthy?: boolean;
  /** The product's open findings in Platform Health (#101 S8b): the dot turns amber on a warning,
   *  red on a critical, and the tooltip says the counts. Undefined when the reader has no module card. */
  health?: ProductHealth;
  version?: string | null;
  url?: string | null;
  comingSoon?: boolean;
  onConnect?: () => void;
  /** Name for a platform outside the suite catalogue, which has no
   *  CTEM_PRODUCT_INFO entry — its registered title rather than its slug. */
  label?: string;
  /** Logo for a product outside the suite catalogue (the XTM Hub). */
  logo?: string;
  /** Logo, edition and health only, for a narrow overview: the name, version
   *  and health are in the tooltip and the accessible name. */
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const info = CTEM_PRODUCT_INFO[product];
  const logoSrc = logo ?? info?.logo;
  const name = label ?? info?.name ?? product;
  if (comingSoon && compact) {
    const soon = `${info?.name ?? product} - ${t_i18n("Soon")}`;
    return (
      <Tip content={soon}>
        <span
          aria-label={soon}
          className="inline-flex items-center rounded-sm border border-dashed border-elevation-subtle-soft px-2 py-1 text-default-secondary"
        >
          {info?.logo ? <img src={info.logo} alt="" className="h-3.5 w-3.5 opacity-60" /> : <Scale className="h-3 w-3" />}
        </span>
      </Tip>
    );
  }
  if (comingSoon) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-sm border border-dashed border-elevation-subtle-soft px-2 py-1 text-[11px] text-default-secondary">
        {info?.logo ? <img src={info.logo} alt="" className="h-3.5 w-3.5 opacity-60" /> : <Scale className="h-3 w-3" />}
        {info?.name ?? product}
        <Badge variant="outline" className="text-[9px] ml-0.5">{t_i18n("Soon")}</Badge>
      </span>
    );
  }
  // OpenCTI / OpenAEV register themselves inbound - so "connect" opens the
  // registration instructions dialog, never an outbound install/integration.
  // The XTM Hub is the exception: an outbound integration, created here.
  if (!present && compact) {
    const connect = t_i18n("Connect {name}", { name });
    return (
      <Tip content={connect}>
        <button
          onClick={onConnect}
          aria-label={connect}
          className="inline-flex items-center gap-1 rounded-sm border border-dashed border-accent/30 px-2 py-1 text-accent/80 hover:bg-accent/10 transition-colors"
        >
          <Plus className="h-3 w-3" />
          {logoSrc ? <img src={logoSrc} alt="" className="h-3.5 w-3.5 opacity-70" /> : <Link2 className="h-3 w-3" />}
        </button>
      </Tip>
    );
  }
  if (!present) {
    return (
      <button
        onClick={onConnect}
        className="inline-flex items-center gap-1.5 rounded-sm border border-dashed border-accent/30 px-2 py-1 text-[11px] text-accent/80 hover:bg-accent/10 transition-colors"
      >
        <Plus className="h-3 w-3" /> {t_i18n("Connect {name}", { name })}
      </button>
    );
  }
  // Only render an external link when the registered platform_url is a safe
  // http(s) URL; otherwise show a non-interactive pill so it doesn't behave
  // like a broken link that opens a blank tab.
  const href = safeExternalUrl(url);
  const base = cn(
    "inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft px-2 py-1 text-[11px] text-default-primary no-underline",
  );
  // What the compact pill no longer writes out.
  const tone = productPillTone(healthy, health);
  const findings = health && (health.critical > 0 || health.warning > 0)
    ? health.critical > 0
      ? t_i18n("{count, plural, one {# critical finding in Platform Health} other {# critical findings in Platform Health}}", { count: health.critical })
      : t_i18n("{count, plural, one {# warning in Platform Health} other {# warnings in Platform Health}}", { count: health.warning })
    : null;
  const state = healthy ? (findings ?? t_i18n("Healthy")) : t_i18n("Unreachable");
  const summary = `${name}${version ? ` v${version}` : ""} - ${state}`;
  const inner = (
    <>
      {logoSrc
        ? <img src={logoSrc} alt="" className="h-3.5 w-3.5" />
        : <Link2 className="h-3 w-3 text-default-secondary" />}
      {!compact && <span className="font-medium">{name}</span>}
      {edition && (
        <Badge
          variant={edition === "EE" ? "cyan" : "outline"}
          className={cn("text-[8px] py-0 px-1 h-3.5", edition === "CE" && "text-default-secondary border-elevation-subtle")}
        >
          {edition}
        </Badge>
      )}
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          tone === "success" && "bg-feedback-success-primary",
          tone === "warning" && "bg-feedback-warning-primary",
          tone === "error" && "bg-feedback-error-primary",
        )}
      />
      {!compact && version && <span className="text-default-secondary">v{version}</span>}
      {!compact && href && <ExternalLink className="h-2.5 w-2.5 text-default-disabled" />}
    </>
  );
  // A finding is worth a tooltip on the full pill too: its line only writes the name and version.
  const tipped = compact || findings !== null;
  const pill = !href ? (
    <span className={base} aria-label={tipped ? summary : undefined}>{inner}</span>
  ) : (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={tipped ? summary : undefined}
      className={cn(base, "hover:border-accent/30 transition-colors")}
    >
      {inner}
    </a>
  );
  return tipped ? <Tip content={summary}>{pill}</Tip> : pill;
}

/** A greyed sample row of the Live activity card: the card's one row layout
 *  (`ActivityRow`), shared with the deterministic feed (#9568 UX round 1 B2). */
function DemoActivityRow({ row }: { row: (typeof DEMO_ACTIVITY)[number] }) {
  return <ActivityRow icon={Bot} title={row.agent} detail={row.action} status={row.status} time={row.ago} />;
}

export default function CtemCommandCenter({
  registrations,
}: {
  registrations?: PlatformRegistration[];
}) {
  const { t_i18n, intl } = useFormatter();
  const numbers = useNumberFormat();
  const navigate = useNavigate();
  const platformName = usePlatformName();
  const { isAdmin } = useAuth();
  // Where the agentic ceilings come from - the one thing the badge tooltip
  // adds to the edition label: the license's own figures, the included
  // allowance of an inherited Enterprise Edition, or nothing (Community).
  const agenticSource = useAgenticSource();
  const xtm = useXTMLicense();
  const { data: user } = useMe();
  const updateProfile = useUpdateProfile();
  // Active CTEM vertical: optimistic local override (instant switch) over the
  // saved per-user preference. Reshapes the whole command center.
  const [verticalOverride, setVerticalOverride] = useState<CtemVertical | null>(null);
  const vertical: CtemVertical = verticalOverride ?? user?.ctem_vertical ?? "cyber";
  const onVerticalChange = (v: CtemVertical) => {
    if (v === vertical) return;
    setVerticalOverride(v);
    updateProfile.mutate({ ctem_vertical: v });
  };
  const { data: posture, isLoading: postureLoading } = useCtemPosture(vertical);
  const { data: histResp } = useCtemPostureHistory(vertical);
  const { data: briefingResp } = useCtemBriefing(vertical);
  const refreshBriefing = useRefreshMyBriefing();
  const refreshPosture = useRefreshCtemPosture();
  // One lock reason for every AI surface of the page (no license, AI off for
  // the platform or the group, no model): the backend status of the briefing
  // fills in what the config cannot say (no model).
  const aiLockReason = useAiLockReason(briefingResp?.status);

  // Refresh on landing (#12183): the briefing is generated when this vertical
  // has none (pending) or when it is past the daily window (stale), once per
  // vertical for this page visit; the backend dedupes in-flight runs, so a
  // reload during the run starts no second one. `auto` lets the server exempt
  // it from the quota when it sees the stored briefing stale itself.
  const autoBriefedVerticals = useRef<Set<string>>(new Set());
  const wantsBriefing = aiLockReason === null && shouldAutoGenerateBriefing(briefingResp);
  const { mutate: generateBriefing } = refreshBriefing;
  useEffect(() => {
    if (!wantsBriefing) return;
    if (!(posture && posture.posture_score != null)) return;
    if (autoBriefedVerticals.current.has(vertical)) return;
    autoBriefedVerticals.current.add(vertical);
    generateBriefing({ vertical, auto: true });
  }, [vertical, wantsBriefing, posture, generateBriefing]);
  // The stage assessments behind Act now and the lifecycle band: a missing or
  // stale one is regenerated on landing too, once per stage and vertical.
  const coveredStageKeys = (posture?.stages ?? []).filter((s) => s.covered).map((s) => s.key);
  const stageAssessments = useCtemStageAssessments(coveredStageKeys, vertical, aiLockReason === null);
  const { mutate: generateAssessment } = useRefreshStageAssessment();
  const autoAssessed = useRef<Set<string>>(new Set());
  const staleStageKeys = aiLockReason === null
    ? coveredStageKeys.filter((_, i) => shouldAutoGenerate(stageAssessments[i]?.data)).join(",")
    : "";
  useEffect(() => {
    for (const stage of staleStageKeys ? staleStageKeys.split(",") : []) {
      const key = `${stage}:${vertical}`;
      if (autoAssessed.current.has(key)) continue;
      autoAssessed.current.add(key);
      generateAssessment({ stage, vertical, auto: true });
    }
  }, [staleStageKeys, vertical, generateAssessment]);
  // What the reader sees moving: the posture rebuild (first build, background
  // rebuild, Force sync) and the assessment runs behind the stage rows.
  const postureRefreshing = postureBusy(posture, { loading: postureLoading, forcing: refreshPosture.isPending });
  const assessmentsRefreshing = stageAssessments.some((q) => assessmentBusy(q.data));

  const { data: stats, isLoading: statsLoading } = useDashboardStats();
  // Live activity shows the newest six; Done for you places up to 50 on its axis.
  const { data: recentRuns } = useAllAssignmentRuns(50);
  const { data: integrations } = useIntegrations();
  // Community Edition asks nothing of the fleet API (it would refuse on every
  // load); while the config is in flight the card waits rather than flash a state.
  const configLoaded = !!usePlatformConfig();
  const agenticLicensed = useAgenticAIEnabled();
  // The fleets this person chose for the page (#11464); null = the built-in
  // CTEM fleets. Optimistic: the row follows a hide or a save at once.
  const [fleetChoiceOverride, setFleetChoiceOverride] = useState<string[] | null | undefined>(undefined);
  const chosenFleetIds = fleetChoiceOverride !== undefined ? fleetChoiceOverride : user?.ctem_dashboard_fleet_ids ?? null;
  const fleetActivity = useCtemFleetActivity({ isAdmin, licensed: configLoaded && agenticLicensed, chosenIds: chosenFleetIds });
  // The cases the reader sees, by id: a timeline row of a case run names its case (#915).
  // Same query as the Cases card and board (shared cache), never a request per row.
  const { data: casesRead } = useCtemCases({ throwOnError: false });
  const casesById = useMemo(
    () => Object.fromEntries((casesRead?.cases ?? []).map((c) => [c.id, c])),
    [casesRead],
  );
  const { data: allFleets } = useFleets({ optional: true, enabled: configLoaded && agenticLicensed });
  const [chooseFleetsOpen, setChooseFleetsOpen] = useState(false);
  const saveFleetChoice = useCallback(
    (ids: string[] | null) => {
      setFleetChoiceOverride(ids);
      updateProfile.mutate({ ctem_dashboard_fleet_ids: ids });
    },
    [updateProfile],
  );
  const hideFleet = useCallback(
    (tile: { id: string }) => saveFleetChoice(fleetActivity.tiles.map((t) => t.id).filter((id) => id !== tile.id)),
    [saveFleetChoice, fleetActivity.tiles],
  );
  const contextMissing = posture?.context?.state === "missing";
  // The Governance & Risk column reads the target saved on the profile (S2e of #11547).
  const { data: orgContext } = useOrganizationContext();
  const postureTarget = orgContext?.profile?.posture_target ?? null;
  const qualitativePosture = { score: posture?.qualitative_score ?? null, band: posture?.qualitative_band ?? "unknown" };

  const [connectProduct, setConnectProduct] = useState<Product | null>(null);
  const [postureExplainOpen, setPostureExplainOpen] = useState(false);
  // The command center this person built for the vertical on screen (their
  // saved layout, every widget exactly once), and whether they are editing it.
  const { layout: dashboardLayout, onChange: onLayoutChange } = useDashboardLayoutState({
    key: vertical,
    saved: user?.ctem_dashboard_layouts?.[vertical],
    registry: CTEM_WIDGETS,
    write: (layout, settled) =>
      updateProfile.mutate({ ctem_dashboard_layout: { vertical, layout } }, { onSettled: settled }),
  });
  const [customizing, setCustomizing] = useState(false);
  // The widget whose settings are open (dev-docs/ctem-stage-viz.md 8.3).
  const [configuring, setConfiguring] = useState<CtemWidgetId | null>(null);

  // Active registration per product (authoritative for url / version / edition).
  const regByProduct = useMemo(() => {
    const map: Record<string, PlatformRegistration> = {};
    for (const r of registrations ?? []) {
      if (r.status !== "active") continue;
      const canon = r.platform_identifier === "openbas" ? "openaev" : r.platform_identifier;
      if (!map[canon]) map[canon] = r;
    }
    return map;
  }, [registrations]);

  // Platforms outside the suite catalogue — today the chat playground, tomorrow
  // anything else that registers. Shown only while actually connected, unlike
  // the three products above: those are listed whether or not you have them,
  // because a "Connect OpenCTI" pill is also an invitation to want one. There
  // is nothing to promote about a platform that is not a product, so an absent
  // one should leave no trace at all.
  // No dedup pass, unlike `regByProduct` above: ``platform_identifier`` is
  // ``unique=True`` on the registrations table and ``POST /platform/register``
  // upserts on it, so one platform cannot hold two active rows. Two rows mean
  // two genuinely different platforms, and two pills is then the right answer.
  const extraRegistrations = useMemo(
    () =>
      (registrations ?? []).filter(
        (r) =>
          r.status === "active"
          && !PRODUCT_ORDER.includes(
            (r.platform_identifier === "openbas" ? "openaev" : r.platform_identifier) as (typeof PRODUCT_ORDER)[number],
          ),
      ),
    [registrations],
  );

  // The clock of the greeting and the sync countdown below, read again every
  // 30 s outside render: the countdown re-renders the page that often.
  const now = new Date(useNow());
  const firstName = user?.full_name?.split(" ")[0] ?? "";
  const hour = now.getHours();
  const timeGreeting = hour < 12 ? t_i18n("Good morning") : hour < 17 ? t_i18n("Good afternoon") : t_i18n("Good evening");
  const greeting = firstName ? t_i18n("{greeting}, {name}", { greeting: timeGreeting, name: firstName }) : timeGreeting;

  const latestRuns = useMemo(() => (recentRuns ?? []).slice(0, 6), [recentRuns]);
  // D3-4 (#9567): with no agent run, Live activity lists the deterministic
  // events the page already holds (syncs, registrations, health, validation).
  const deterministicEvents = useMemo(() => deterministicActivity(posture, registrations), [posture, registrations]);
  // The CTEM loop is never hidden (#838, dev-docs/ctem-loop-visibility.md):
  // the newest signals join the feed, refreshed by the activity stream frame
  // ctem_signal_update, and open their loop trace.
  const { data: recentSignals } = useRecentCtemSignals();
  const signalEvents = useMemo(() => signalActivity(recentSignals?.signals), [recentSignals]);
  const activityEvents = useMemo(() => mergeActivity(signalEvents, deterministicEvents), [signalEvents, deterministicEvents]);
  // The signals of the loop are a page (#1263): a feed row opens its loop trace there.
  const openSignalTrace = useCallback((signalId: string) => navigate(signalsHref({ signal: signalId })), [navigate]);
  // CTEM-01-UI: the signals counter of the lifecycle card opens the Signals
  // page filtered on the state clicked.
  const openSignalList = useCallback((state: CtemSignalState | null) => navigate(signalsHref({ state })), [navigate]);
  const signalCounter = useMemo(
    () => (recentSignals ? { counts: recentSignals.counts, onOpen: openSignalList } : null),
    [recentSignals, openSignalList],
  );
  // The open cases of each stage (#1621): one footer line per lifecycle column, no read per column.
  const { data: casesOverview } = useCtemCasesOverview();
  const lifecycleCases = useMemo(
    () => (casesOverview ? { phases: casesOverview.phases, acceptedRisk: casesOverview.counts.accepted_risk ?? 0 } : null),
    [casesOverview],
  );
  // Done for you (C2 V3): every deterministic event, its axis keeps the period's.
  const timelineEvents = useMemo(() => deterministicActivity(posture, registrations, 50), [posture, registrations]);

  // The XTM Hub sits with the suite products: this platform's registration
  // with it decides first, then the XTM Hub integrations the user can see.
  const { data: hubRegistration } = useXtmHubRegistrationStatus();
  const hubPill = useMemo(() => xtmHubPill(integrations, hubRegistration), [integrations, hubRegistration]);
  // The product pills read the Platform Health engine (#101 S8b): an administrator's module cards.
  const { data: healthOverview } = useHealthOverview({ enabled: isAdmin });

  const connectedLogos = useMemo(() => {
    if (!integrations) return [];
    return integrations
      .filter((i) => i.status === "connected")
      .map((i) => i.provider)
      .filter((v, idx, arr) => arr.indexOf(v) === idx);
  }, [integrations]);

  // The fleets invitation (#9566): closed per person, back when a CTEM fleet
  // or a connected module appears that was not there when it was closed.
  const ctemFleetSlugs = useMemo(() => fleetActivity.tiles.map((t) => t.slug).filter(Boolean), [fleetActivity.tiles]);
  const fleetInviteShown = inviteVisible(user?.ctem_fleet_invite_dismissed, ctemFleetSlugs, connectedLogos);
  const dismissFleetInvite = useCallback(
    () => updateProfile.mutate({ ctem_fleet_invite_dismissed: { fleets: ctemFleetSlugs, modules: connectedLogos } }),
    [updateProfile, ctemFleetSlugs, connectedLogos],
  );
  // "Set up the CTEM loop" is a nudge (Sam, 23 Nov 2026): gone once the CTEM
  // loop fleet or any fleet of the page has an automation on, or once the
  // person closed it; not drawn while that is loading.
  const setupEntry = useDashboardSetupEntry(
    fleetActivity.isLoading || !configLoaded ? null : fleetActivity.tiles.map((t) => t.automationsOn),
  );
  // Every CTEM fleet off and the invitation closed: the Fleet activity card is
  // one line, so it is drawn at that height and the widgets below move up (UX
  // round 3 on #9570, B1). The saved layout is untouched, and the editor shows it.
  const fleetActivityClosed =
    configLoaded && agenticLicensed && !fleetActivity.isLoading && fleetActivity.allOff && !fleetInviteShown;
  // The Act now card is drawn at the height of its rows (UX round 1 on
  // #10357, B1 and B2): no empty band, Show all grows it in place. Not while
  // customizing: the editor shows the saved layout.
  const [actNowPx, setActNowPx] = useState<number | null>(null);
  // Done for you is drawn at the height of its lanes the same way (C2 V3).
  const [doneForYouPx, setDoneForYouPx] = useState<number | null>(null);
  // And the lifecycle band at the height of its chips (C2 V4).
  const [lifecyclePx, setLifecyclePx] = useState<number | null>(null);
  // And stats and coverage at the height of its tiles (C2 V5).
  const [statsCoveragePx, setStatsCoveragePx] = useState<number | null>(null);
  // And the fleets row at the height of its lines (C2 V6).
  const [fleetsRowPx, setFleetsRowPx] = useState<number | null>(null);
  // And the Exposure briefing at the height of its findings and steps (#10721).
  const [briefingPx, setBriefingPx] = useState<number | null>(null);
  const drawnLayout = useMemo(() => {
    if (customizing) return dashboardLayout;
    const closed = fleetActivityClosed ? withClosedFleetActivity(dashboardLayout) : dashboardLayout;
    const base = withContentHeight(closed, "briefing", briefingPx);
    const sized = withContentHeight(withActNowHeight(base, actNowPx), "done_for_you", doneForYouPx);
    const stats = withContentHeight(withContentHeight(sized, "lifecycle_band", lifecyclePx), "stats_coverage", statsCoveragePx);
    return withContentHeight(stats, "fleets_row", fleetsRowPx);
  }, [fleetActivityClosed, customizing, dashboardLayout, briefingPx, actNowPx, doneForYouPx, lifecyclePx, statsCoveragePx, fleetsRowPx]);
  // A saved layout that shows the v1 hero keeps the greeting and the pills in
  // it: one h1 and one pill set on the page (UX round 1 on #10534, B1).
  const bandTopLine = valueBandCarriesTopLine(drawnLayout);

  const stageByKey = useMemo(() => {
    const map: Record<string, CtemStage> = {};
    for (const s of posture?.stages ?? []) map[s.key] = s;
    return map;
  }, [posture]);

  // Vertical-keyed sample/demo data is otherwise rebuilt on every render - and
  // this component re-renders on every posture / briefing / stats / runs poll.
  // Memoize so only a vertical switch recomputes it.
  const demoStages = useMemo(() => demoStageByKey(vertical), [vertical]);
  const demoP = useMemo(() => demoPosture(vertical), [vertical]);

  // Stable per-stage props so the memoized stage cards skip re-rendering on every
  // poll tick: the resolved stage object, the score-history sparkline series, and
  // a per-stage "open" handler. Connect / open-agent handlers are shared.
  const resolvedStages = useMemo(() => {
    const map: Record<string, CtemStage> = {};
    for (const key of CTEM_STAGE_KEYS) map[key] = stageByKey[key] ?? emptyStage(key, demoStages);
    return map;
  }, [stageByKey, demoStages]);
  const stageScoreHistories = useMemo(() => {
    const pts = histResp?.history ?? [];
    const map: Record<string, (number | null)[]> = {};
    for (const key of CTEM_STAGE_KEYS) {
      map[key] = pts.map((point) => point.stage_metrics?.[key]?.score ?? null);
    }
    return map;
  }, [histResp]);
  const stageOpenHandlers = useMemo(() => {
    const map: Record<string, () => void> = {};
    for (const key of CTEM_STAGE_KEYS) map[key] = () => navigate(`/ctem/${key}`);
    return map;
  }, [navigate]);
  const handleConnectProduct = useCallback(
    (product: string) =>
      setConnectProduct((product === "openbas" ? "openaev" : product) as Product),
    [],
  );
  const handleOpenAgent = useCallback((id: string) => navigate(`/agents/${id}`), [navigate]);
  // Coverage and stats (#9050 slice 4): deterministic, from the posture and its history.
  const coverage = useMemo(() => coverageMatrix(posture?.stages ? posture : null), [posture]);
  // The period each history widget reads: its own, else the context bar's (8.2, 8.3).
  const exposureStatsDays = widgetPeriod(dashboardLayout, "exposure_stats");
  const statsCoverageDays = widgetPeriod(dashboardLayout, "stats_coverage");
  const periodStats = useMemo(
    () => exposureStats(posture?.stages ? posture : null, historyWindow(histResp?.history ?? [], exposureStatsDays, now)),
    // `now` moves every 30 s; the window only moves with the day, read again then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [posture, histResp, exposureStatsDays],
  );
  const coverageStats = useMemo(
    () => exposureStats(posture?.stages ? posture : null, historyWindow(histResp?.history ?? [], statsCoverageDays, now)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [posture, histResp, statsCoverageDays],
  );

  // Posture is deliberately NOT in this gate: it can take ~20-30s to build on a
  // cold cache, and the page is designed to render its greyed "sample" shell
  // when posture is absent/computing (see `live`/`isDemo` below). Gating only on
  // the fast dashboard stats + registrations lets the shell paint immediately
  // and the live posture swap in automatically when it arrives (the backend
  // returns a `status: "computing"` placeholder and the query polls faster
  // until the real payload lands).
  if (statsLoading || !stats || !registrations) {
    return <PageSkeleton cards={6} />;
  }

  const live = Boolean(posture && posture.posture_score != null);

  // D3-5 (#9477): the live posture as a CSV file, headers in the UI language.
  const exportPosture = () => {
    if (!posture || !live) return;
    const csv = postureCsv(
      posture,
      {
        section: t_i18n("Section"),
        stage: t_i18n("Stage"),
        metric: t_i18n("Metric"),
        value: t_i18n("Value"),
        unit: t_i18n("Unit"),
        key: t_i18n("Key"),
      },
      (key) => t_i18n(key),
    );
    saveBlobAs(csvBlob(csv), postureCsvFilename(vertical));
  };
  const isDemo = !live;

  // Sync status: live posture refreshes on a simple ~24h cycle
  // (cache_ttl_seconds). Show "next sync in Xh" as a plain countdown to that
  // daily refresh; once the window has elapsed we just drop the clause (the
  // value is in the past) rather than surface any reload/force wording.
  const syncedAt = posture?.generated_at;
  const syncTtl = posture?.cache_ttl_seconds ?? 86400;
  const syncRemainingMs = syncedAt
    ? new Date(syncedAt).getTime() + syncTtl * 1000 - now.getTime()
    : 0;
  const nextSyncLabel = syncRemainingMs > 0 ? formatNextSync(syncRemainingMs) : null;

  // Discrete in-card history (one snapshot per day): the composite for the hero
  // and per-stage score series for each card's sparkline.
  const histPoints = histResp?.history ?? [];
  const compositeHistory = historyWindow(histPoints, widgetPeriod(dashboardLayout, "hero"), now).map((h) => h.posture_score);
  const valueBandHistory = historyWindow(histPoints, widgetPeriod(dashboardLayout, "value_band"), now).map((h) => h.posture_score);
  // The context bar (8.2): the frameworks the Validation data carries, the period.
  const validationMatrix = stageByKey.validation?.viz.outcome_matrix;
  const context = readContext(dashboardLayout);
  const framework = effectiveFramework(dashboardLayout, validationMatrix);
  const ownPeriods = overriddenWidgets(dashboardLayout);
  // The one period delta of the page: the hero badge and the value band read it (C2 V1, UX r1 B4).
  const postureChange = periodChange(posture?.posture_score ?? null, compositeHistory);
  // The hero's top risk tile (S2b of #11547): the known-exploited count per
  // recorded day, read from the same key as the figure shown (tailored to the
  // software inventory, or every KEV), so the line never mixes the two populations.
  const kevHistoryKey = posture?.headline?.kev_tailored_count != null ? "kev_tailored_count" : "kev_count";
  const kevHistory = histPoints.map((p) => p.stage_metrics?.prioritization?.[kevHistoryKey] ?? null);
  const h = posture?.headline;
  const demoH = demoP.headline;

  // Per-user AI briefing: real artifact when this user's analyst run exists,
  // otherwise a greyed sample so the widget previews its value immediately.
  const briefingStatus = briefingResp?.status;
  const briefingReady = briefingShown(briefingResp);
  const briefingGenerating = briefingBusy(briefingResp, false);
  const briefing = briefingReady && briefingResp?.briefing ? briefingResp.briefing : demoBriefing(vertical);
  const hasRunning = (stats.runs.running ?? 0) > 0;

  // Exposure KPIs: gate on the source product being connected (a stale stored
  // /posture snapshot must never show real numbers next to a Connect CTA), then
  // fall back to greyed sample data when there is no live value.
  const octiPresent = Boolean(posture?.products.opencti?.present);
  const aevPresent = Boolean(posture?.products.openaev?.present);
  const kev = octiPresent ? h?.kev_count ?? null : null;
  const cov = octiPresent ? h?.detection_coverage_pct ?? null : null;
  const lockedAgentsKpi = lockedKpiFor(vertical, "kpi_agents", aiLockReason !== null);
  const lockedExecutionsKpi = lockedKpiFor(vertical, "kpi_executions", aiLockReason !== null);

  // ── Mobilize: context-aware recommended actions (the CTEM "fix what matters"
  //    plan). Each item is executable and routes to the right owner: connect a
  //    platform, open the source product, create an agent, add an integration,
  //    activate a license, or chat. Highly dependent on tier / role / context. ─
  type Rec = {
    id: string; icon: LucideIcon; tone: string; title: string; reason: string;
    cta: string; onAction: () => void; priority: number;
    // Original briefing band ("high"/"medium"/"low") for the badge, when this
    // action came from the Exposure Briefing Analyst's prioritized next steps.
    priorityLabel?: string;
    source: ActNowSource;
    stage?: string;
    tailored?: boolean;
    subject?: string;
    figure?: string;
    mergeTitle?: (subjects: string[]) => string;
  };
  const recs: Rec[] = [];
  // The rule-based rows (KEV, expectations, coverage, stack, first simulation):
  // the v1 card shows them only without a briefing (D3); the v2 Act now queue
  // keeps both and dedupes (C2 V2).
  const ruleRecs: Rec[] = [];
  for (const bs of posture?.blind_spots ?? []) {
    const m = bs.missing_products[0];
    const canon = (m === "openbas" ? "openaev" : m) as Product;
    const pinfo = m ? CTEM_PRODUCT_INFO[m] : undefined;
    recs.push({
      id: `connect-${bs.stage}`, icon: Plus, tone: "text-accent",
      title: t_i18n("Connect {name}", { name: pinfo?.name ?? m }),
      reason: t_i18n("Unlocks the {stage} stage of your CTEM loop", {
        stage: t_i18n(CTEM_STAGE_CONFIG[bs.stage as CtemStageKey]?.label ?? bs.label),
      }),
      cta: "Connect", onAction: () => setConnectProduct(canon), priority: 100, source: "setup",
    });
  }

  // Recommended actions are authored by the vertical-aware Exposure Briefing
  // Analyst (its prioritized next steps) when a briefing exists; the deterministic
  // heuristics below are the fallback (CE / before the first briefing is generated).
  const briefingSteps = briefingReady ? briefingResp?.briefing?.next_steps ?? [] : [];
  briefingSteps.forEach((s, i) => {
    const base = s.priority === "high" ? 92 : s.priority === "low" ? 60 : 76;
    recs.push({
      id: `ai-step-${i}`,
      icon: Sparkles,
      tone: "text-accent",
      title: s.action,
      reason: s.rationale ?? t_i18n("Recommended by the Exposure Briefing Analyst"),
      cta: "Open briefing",
      onAction: () => navigate("/briefing"),
      priority: base - i,
      priorityLabel: s.priority ?? "medium",
      source: "briefing",
    });
  });

  // Known-exploited restricted to the organization profile's software inventory (slice 2
  // of #11213): when the backend could match the stack, the row counts only
  // those, says which products it found, and is tailored (no generic notice).
  const kevTailored = octiPresent ? h?.kev_tailored_count ?? null : null;
  const kevProducts = h?.kev_tailored_products ?? [];
  const kevShown = kevTailored ?? kev;
  if (octiPresent && (kevShown ?? 0) > 0) {
    // The in-app Prioritization page, where the KEV figure sits beside the top
    // exposures and their links: never the unfiltered OpenCTI list, whose
    // population is the whole knowledge base (#11213).
    ruleRecs.push({
      id: "triage-kev", source: "module", stage: "prioritization", icon: ShieldAlert, tone: "text-feedback-error-primary",
      title: t_i18n("Triage {count, plural, one {{shown} known-exploited exposure} other {{shown} known-exploited exposures}}", { count: kevShown as number, shown: formatCompactNumber(kevShown as number) }),
      reason: kevTailored !== null && kevProducts.length > 0
        ? t_i18n("Known-exploited and affects {products} in your software", { products: intl.formatList(kevProducts, { type: "conjunction" }) })
        : t_i18n("Actively exploited in the wild - remediate first (KEV SLA)"),
      tailored: kevTailored !== null,
      cta: "Triage",
      onAction: () => navigate("/ctem/prioritization"), priority: 95,
    });
  }
  // Rule-based next actions (D3-3, #9459): one row per failed OpenAEV
  // expectation type and per stage with a connected source but no data. They
  // are the briefing's next steps without a model, so the v1 card never shows
  // them beside them.
  {
    // The in-app Validation page, where the failed expectations sit per type
    // with their simulations: a bare OpenAEV list left the reader with no next
    // step (#11213). Equal counts of one stage read as one phrase.
    const failedTitle = (subjects: string[], count: number) =>
      t_i18n("{type}: {count, plural, one {# failed expectation} other {# failed expectations}}", {
        type: intl.formatList(subjects, { type: "conjunction" }), count,
      });
    rankNextActions(deterministicNextActions(posture)).forEach((a) => {
      if (a.kind === "expectation") {
        const subject = t_i18n(ctemExpectationLabel(a.type));
        ruleRecs.push({
          id: `close-validation-${a.type}`, source: "module", stage: "validation", icon: Target, tone: "text-filigran-ia-primary",
          title: failedTitle([subject], a.failed),
          subject, figure: String(a.failed), mergeTitle: (subjects) => failedTitle(subjects, a.failed),
          reason: t_i18n(EXPECTATION_REASONS[a.type] ?? EXPECTATION_REASON_FALLBACK),
          cta: "Review",
          onAction: () => navigate("/ctem/validation"), priority: a.priority,
        });
      } else {
        ruleRecs.push({
          id: `stage-no-data-${a.stage}`, source: "module", stage: a.stage, icon: Radar, tone: "text-feedback-warning-primary",
          title: t_i18n("No data for the {stage} stage", {
            stage: t_i18n(CTEM_STAGE_CONFIG[a.stage as CtemStageKey]?.label ?? a.label),
          }),
          reason: t_i18n("{count, plural, one {{products} connected} other {{products} connected}}, nothing reaches this stage yet - check its data and sync", {
            count: a.products.length,
            products: a.products.map((p) => CTEM_PRODUCT_INFO[p]?.name ?? p).join(", "),
          }),
          cta: "Review", onAction: () => navigate(`/ctem/${a.stage}`), priority: a.priority,
        });
      }
    });
  }
  if (octiPresent && cov != null && cov < 60) {
    // With AI locked, "Create agent" leads to a locked page (D-2): the row
    // opens the Discovery stage, where the coverage figures are.
    ruleRecs.push({
      id: "improve-detection", source: "module", stage: "discovery", icon: Radar, tone: "text-accent",
      title: t_i18n("Close your detection coverage gap"),
      reason: t_i18n("Only {pct}% of ATT&CK techniques have detection content", { pct: cov }),
      ...(aiLockReason
        ? { cta: "Review", onAction: () => navigate("/ctem/discovery") }
        : { cta: "Create agent", onAction: () => navigate("/agents/new") }),
      priority: 80,
    });
  }
  if (isAdmin && stats.integrations_connected < 4) {
    ruleRecs.push({
      id: "connect-stack", source: "setup", icon: Link2, tone: "text-default-secondary",
      title: t_i18n("Wire in your security stack"),
      reason: t_i18n("Connect EDR / SIEM / ticketing so agents can act end to end"),
      cta: "Add integration", onAction: () => navigate("/integrations"), priority: 50,
    });
  }
  // The chat rows only when AI can run: in Community Edition or with AI off
  // they lead to a refused chat (D-2); the deterministic rows above stay.
  if (isDemo && !aiLockReason) {
    ruleRecs.push({
      id: "first-sim", source: "agent", icon: Target, tone: "text-filigran-ia-primary",
      title: t_i18n("Design your first ATT&CK simulation"),
      reason: t_i18n("Ask the agent to build an emulation mapped to your top threats"),
      cta: "Start", onAction: () => navigate("/chat"), priority: 40,
    });
  }
  const askRecs: Rec[] = [];
  if (!aiLockReason) {
    askRecs.push({
      id: ASK_AGENT_ROW_ID, source: "agent", icon: Sparkles, tone: "text-accent",
      title: t_i18n("Ask your CTEM agent"),
      reason: t_i18n("Build dashboards, simulations and detections in natural language"),
      cta: "Chat", onAction: () => navigate("/chat"), priority: 10,
    });
  }
  // Act now: the posture rows and the setup; the queue leaves the briefing's
  // steps to the briefing widget and the chat to the hero (#10721).
  // A fleet working on a case (#915, CTEM-04-UI slice 3): one row per case
  // with a run in flight, naming the member at work; it opens the case, where
  // the run's steps are listed with the case events.
  const fleetWorkRows: ActNowRow[] = actNowFleetWork(
    fleetActivity.timeline,
    Object.fromEntries(Object.values(casesById).map((c) => [c.id, c.title])),
  ).map((work) => ({
    id: `fleet-working-${work.caseId}`, source: "fleet", icon: Bot, tone: "text-feedback-info-primary",
    title: t_i18n("{fleet} is working on {case}", { fleet: work.fleetName, case: work.caseTitle }),
    reason: work.pending
      ? t_i18n("Run queued, it starts shortly")
      : work.agentName
        ? t_i18n("Step in progress: {agent}", { agent: work.agentName })
        : t_i18n("Step in progress"),
    cta: "Open",
    onAction: () => navigate(`/ctem/cases/${work.caseId}`, { state: { backTo: "/", backLabel: "Back to Command Center" } }),
    priority: ACT_NOW_PRIORITY.fleet,
  }));
  const actNowBase: ActNowRow[] = [...recs, ...ruleRecs, ...fleetWorkRows];
  const briefingActions = briefingSteps.map((s) => s.action);
  const recommendations = [...recs, ...(briefingSteps.length === 0 ? ruleRecs : []), ...askRecs]
    .sort((a, b) => b.priority - a.priority)
    .slice(0, 6);

  // A KPI card read from the posture headline: a vertical-specific one
  // (`VERTICAL_KPIS`) or a deterministic one of an agentic slot when AI is
  // locked (`LOCKED_KPIS`, D3-1).
  const headlineKpi = (kpi: KpiDef) => {
    // "sample" means the SOURCE PRODUCT is not connected - NOT merely that
    // the metric is null. With the product connected, a not-yet-computable
    // value (e.g. FIMI attribution / corroboration with zero channels) shows
    // its real "--", never a greyed demo number that wrongly implies the
    // product is missing.
    const present = kpi.product === "openaev" ? aevPresent : octiPresent;
    const liveVal = present ? h?.[kpi.headlineKey] ?? null : null;
    const sampleVal = demoH[kpi.headlineKey] ?? 0;
    // Deep-link the card to the SPECIFIC resource page for this metric's
    // stage in its source product - never the product home. No deep link
    // (or product absent) => not clickable.
    // A vulnerability KPI opens the OpenCTI list it counts, with its filter
    // (#11954); any other KPI keeps the stage's resource page.
    const productUrl = posture?.products?.[kpi.product]?.url;
    const kpiHref = present
      ? safeExternalUrl(
          (kpi.product === "opencti" ? ctemKpiOpenCtiHref(kpi.headlineKey, productUrl) : null)
            ?? ctemDeepLink(productUrl, vertical, kpi.stage, kpi.product)?.url,
        )
      : undefined;
    return (
      <KpiCard
        label={kpi.label}
        value={present ? liveVal : sampleVal}
        unit={kpi.unit}
        sub={kpi.sub}
        icon={kpi.icon}
        accent={kpi.accent}
        sample={!present}
        onClick={kpiHref ? () => openExternal(kpiHref) : undefined}
      />
    );
  };

  // A CTEM stage card: real score when the stage is covered, greyed sample otherwise.
  const stageCard = (key: CtemStageKey) => {
    const stageObj = resolvedStages[key] ?? emptyStage(key, demoStages);
    // Deep-link to the DEDICATED resource page of the platform that
    // actually DRIVES this stage (owner-first: OpenAEV for Scoping /
    // Validation, OpenCTI for the rest), not always OpenCTI - e.g.
    // Validation -> OpenAEV Simulations, Prioritization -> OpenCTI
    // Vulnerabilities.
    const deep = ctemBestDeepLink(
      posture?.products,
      vertical,
      key,
      stageObj.products_present,
    );
    return (
      <CtemStageCard
        stage={stageObj}
        sample={demoStages[key]}
        vertical={vertical}
        aiEnabled={aiLockReason === null && briefingStatus !== "unavailable"}
        openUrl={deep?.url}
        openLabel={deep?.label}
        scoreHistory={stageScoreHistories[key]}
        onConnect={handleConnectProduct}
        onOpenAgent={handleOpenAgent}
        onOpen={stageOpenHandlers[key]}
      />
    );
  };

  // The product status pills (posture is fed by these): the hero's line and
  // the value band's top line (C2 V7, parity W7) draw the same pills.
  const productPills = (compact: boolean, className?: string) => (
    <div className={cn("flex items-center flex-wrap", compact ? "gap-1.5" : "gap-2", className)}>
      {PRODUCT_ORDER.map((p) => {
        const reg = regByProduct[p];
        return (
          <ProductPill
            key={p}
            compact={compact}
            product={p}
            present={Boolean(reg)}
            // OpenCRQ keeps its "Soon" placeholder (instead of a Connect CTA)
            // until an instance registers; then the pill goes fully live.
            comingSoon={p === "opencrq" && !reg}
            edition={reg ? editionFor(reg, xtm?.sub_licenses) : undefined}
            // Live connectivity from the registration (refetched every 30s),
            // not the cached posture snapshot.
            healthy={reg ? reg.healthy : posture?.products[p]?.healthy}
            // The open findings of the product's module in Platform Health (#101 S8b).
            health={reg ? productHealth(p, healthOverview) : undefined}
            version={reg?.platform_version ?? posture?.products[p]?.version}
            // The quick "open the platform" jump; ProductPill validates the URL.
            url={reg?.platform_url ?? posture?.products[p]?.url}
            onConnect={() => setConnectProduct(p)}
          />
        );
      })}
      <ProductPill
        product={XTM_HUB_PROVIDER}
        compact={compact}
        label="XTM Hub"
        logo={PROVIDER_LOGOS[XTM_HUB_PROVIDER]}
        present={hubPill.present}
        healthy={hubPill.present ? hubPill.healthy : undefined}
        url={hubPill.present ? hubPill.url : undefined}
        // An admin registers the platform; anyone else can still connect an
        // XTM Hub integration of their own.
        onConnect={() => navigate(isAdmin ? "/settings/platform#xtm-hub" : `/integrations/new?provider=${XTM_HUB_PROVIDER}`)}
      />
      {extraRegistrations.map((reg) => (
        <ProductPill
          key={reg.id}
          compact={compact}
          product={reg.platform_identifier}
          // `|| undefined`: the pill falls back through `??`, which an empty
          // string satisfies (a nameless pill).
          label={reg.platform_title || undefined}
          // Always present: absent ones are filtered out above.
          present
          healthy={reg.healthy}
          version={reg.platform_version}
          url={reg.platform_url}
        />
      ))}
    </div>
  );

  // Every widget of the command center grid; each fills the cell it is given.
  const widgets: Record<CtemWidgetId, ReactNode> = {
    // ── Posture-core hero ─ drawn at the density its size allows
    //    (`heroDensity`): the orbit beside or below the text, or a score chip
    //    in its header; full or compact product pills. The greeting and the
    //    orbit are the command center's own (Sam, 13 Nov 2026); the two scores
    //    and the top risk are one row of stat cells instead of the score key
    //    and the top risk card stacked under the actions.
    hero: (
      <WidgetSized>
        {(size) => {
          const d = heroDensity(size);
          const postureBand = CTEM_BAND_CONFIG[posture?.posture_band ?? "unknown"] ?? CTEM_BAND_CONFIG.unknown;
          const orgBand = CTEM_BAND_CONFIG[qualitativePosture.band] ?? CTEM_BAND_CONFIG.unknown;
          const kevTrend = seriesChange(kevHistory);
          const kevLabel = kevTailored !== null ? t_i18n("Known-exploited in your software") : t_i18n("Known-exploited exposures");
          const scoreCells: CtemHeaderStat[] = live
            ? [
                {
                  id: "posture",
                  label: t_i18n("Exposure posture"),
                  title: t_i18n("How the Exposure Posture is computed"),
                  onOpen: () => setPostureExplainOpen(true),
                  value: <span className={postureBand.color}>{posture?.posture_score ?? "--"}</span>,
                  sub: (
                    <span className="inline-flex items-center gap-1.5">
                      <span className={postureBand.color}>{t_i18n(postureBand.label)}</span>
                      {postureChange?.change != null && (
                        <span
                          className={postureChange.change > 0 ? "text-feedback-success-primary" : postureChange.change < 0 ? "text-feedback-error-primary" : "text-default-secondary"}
                          aria-label={t_i18n("{delta} since the start of the period", { delta: signedDelta(postureChange.change) })}
                          data-testid="ctem-header-posture-delta"
                        >
                          {signedDelta(postureChange.change)}
                        </span>
                      )}
                    </span>
                  ),
                },
                {
                  id: "organization",
                  label: t_i18n("Organization"),
                  value: <span className={orgBand.color}>{qualitativePosture.score ?? "--"}</span>,
                  sub:
                    qualitativePosture.score != null ? (
                      <span className={orgBand.color}>{t_i18n(orgBand.label)}</span>
                    ) : contextMissing ? (
                      <span className="text-default-secondary">{t_i18n("No organization context")}</span>
                    ) : undefined,
                },
                ...(vertical === "cyber"
                  ? [
                      octiPresent && kevShown != null
                        ? {
                            id: "top-risk",
                            label: t_i18n("Top risk"),
                            title: `${t_i18n("Top risk")}: ${kevLabel}`,
                            onOpen: () => navigate("/ctem/prioritization"),
                            value: (
                              <span className={kevShown > 0 ? "text-feedback-error-primary" : undefined}>{formatCompactNumber(kevShown)}</span>
                            ),
                            sub:
                              kevTrend.change != null ? (
                                <span
                                  // Fewer known-exploited exposures is better.
                                  className={kevTrend.change < 0 ? "text-feedback-success-primary" : kevTrend.change > 0 ? "text-feedback-error-primary" : "text-default-secondary"}
                                  aria-label={t_i18n("{delta} since the start of the period", { delta: signedDelta(kevTrend.change) })}
                                >
                                  {signedDelta(kevTrend.change)}
                                </span>
                              ) : undefined,
                          }
                        : {
                            id: "top-risk",
                            label: t_i18n("Top risk"),
                            title: t_i18n("Connect OpenCTI"),
                            onOpen: () => setConnectProduct("opencti"),
                            value: <span className="text-default-secondary">--</span>,
                            sub: <span className="text-accent">{t_i18n("Connect OpenCTI")}</span>,
                          },
                    ]
                  : []),
              ]
            : [];
          const orbit = (
            <PostureOrbit
              score={posture?.posture_score ?? null}
              band={posture?.posture_band ?? "unknown"}
              logos={connectedLogos}
              integrationCount={stats.integrations_connected}
              onExplain={() => setPostureExplainOpen(true)}
              qualitative={live ? qualitativePosture : undefined}
            />
          );
          return (
            <div className="@container relative h-full flex flex-col overflow-hidden rounded-sm border border-elevation-subtle-soft bg-gradient-to-br from-[var(--bg-elevation-default)] via-[var(--bg-elevation-default)] to-accent/[0.04]">
              <div className="absolute inset-0 pointer-events-none overflow-hidden">
                <div className="absolute -top-32 -right-32 w-96 h-96 bg-accent/[0.04] rounded-full blur-3xl animate-pulse-soft motion-reduce:animate-none" />
                <div className="absolute -bottom-24 -left-24 w-72 h-72 bg-filigran-ia-primary/[0.03] rounded-full blur-3xl" />
              </div>

              <div
                className={cn(
                  "relative flex-1 min-h-0 flex justify-between",
                  d.orbit === "side"
                    ? "p-6 @3xl:p-8 flex-col @xl:flex-row items-start @xl:items-center gap-8"
                    : "p-5 flex-col items-stretch gap-4",
                )}
              >
                <div className={cn("min-w-0", d.orbit === "side" ? "flex-1 space-y-3" : "space-y-2.5")}>
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="relative flex h-2 w-2 shrink-0">
                      <span className="animate-ping motion-reduce:animate-none absolute inline-flex h-full w-full rounded-full bg-accent opacity-75" />
                      <span className="relative inline-flex rounded-full h-2 w-2 bg-accent" />
                    </span>
                    <p className="content-compact text-accent font-medium uppercase tracking-wider truncate">
                      {t_i18n("{platformName} CTEM Command Center", { platformName })}
                    </p>
                    {/* Discreet, non-naggy: a quiet badge that explains itself. On
                        Community Edition clicking it opens the upgrade path for
                        everyone (admins additionally get the activation form).
                        An inline-flex wrapper: a plain span lays the badge on a
                        line box whose strut drops it below the row centre. */}
                    <span className="inline-flex shrink-0 items-center">
                    <EditionBadge
                      className="content-caption"
                      tooltip={
                        agenticSource === "xtm_license"
                          ? `${t_i18n("XTM One Enterprise")}${xtm?.customer ? ` - ${xtm.customer}` : ""}${xtm?.license_type ? ` (${xtm.license_type})` : ""}`
                          : agenticSource === "ee_platform"
                            ? t_i18n("Enterprise Edition through your connected Enterprise Edition platform, with the included agentic allowance. Install a Filigran license to carry your own capacity.")
                            : t_i18n("Community Edition: no Filigran license and no Enterprise Edition platform connected. AI is not activated.")
                      }
                    />
                    </span>
                    {/* No room for the orbit: its score, as a chip that opens
                        the same explanation. */}
                    {d.orbit === "chip" && (
                      <button
                        type="button"
                        onClick={() => setPostureExplainOpen(true)}
                        className="ml-auto shrink-0 inline-flex items-baseline gap-1 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-2 py-1 hover:border-accent/40 transition-colors"
                        aria-label={t_i18n("How the Exposure Posture is computed")}
                      >
                        <span
                          className={cn(
                            "text-lg font-bold tabular-nums leading-none",
                            posture?.posture_score != null && CTEM_BAND_CONFIG[posture.posture_band]?.color,
                          )}
                        >
                          {posture?.posture_score ?? "--"}
                        </span>
                        <span className="content-caption text-default-secondary">/100</span>
                      </button>
                    )}
                  </div>
                  <h1 className={cn("font-bold font-sans-geologica tracking-tight", d.orbit === "side" ? "text-3xl @3xl:text-4xl" : "text-2xl")}>{greeting}</h1>
                  <p className={cn("content-base text-default-secondary", d.subtitle === "full" ? "@5xl:whitespace-nowrap" : "line-clamp-2")}>
                    {live
                      ? t_i18n("Exposure posture {score}/100 across {covered} of {total} CTEM stages.", { score: posture?.posture_score ?? 0, covered: posture?.covered_stages ?? 0, total: posture?.total_stages ?? 0 }) + " "
                      : t_i18n("Your cross-product exposure posture, fully automated.") + " "}
                    {/* Agents cannot run while AI is locked (M2): no "agents
                        watching" clause, the exposure count stands alone. */}
                    {aiLockReason ? (
                      kev != null ? `${t_i18n("{count, plural, one {{shown} known-exploited exposure} other {{shown} known-exploited exposures}}", { count: kev, shown: formatCompactNumber(kev) })}.` : ""
                    ) : (
                      <>
                        {t_i18n("{count, plural, one {# agent watching} other {# agents watching}}", { count: stats.agents.active })}
                        {hasRunning ? `, ${t_i18n("{count} running", { count: formatCompactNumber(stats.runs.running) })}` : ""}
                        {kev != null ? `, ${t_i18n("{count, plural, one {{shown} known-exploited exposure} other {{shown} known-exploited exposures}}", { count: kev, shown: formatCompactNumber(kev) })}.` : "."}
                      </>
                    )}
                  </p>

                  <div className="flex items-center gap-x-3 gap-y-2 flex-wrap">
                    {/* The primary slot: "Start chatting" only when AI can run;
                        otherwise the one way forward of the lock reason (D-1). */}
                    {aiLockReason ? (
                      // Stacked: the sentence on its line, the actions on one row (B2).
                      <AiLockedFooter reason={aiLockReason} layout="stacked" />
                    ) : (
                      <Button
                        size="sm"
                        className="gap-1.5 bg-accent text-negative-primary font-medium hover:bg-accent/90 shadow-[0_0_15px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)] hover:shadow-[0_0_20px_color-mix(in_srgb,_var(--color-accent)_calc(0.4_*_100%),_transparent)] transition-all duration-300"
                        onClick={() => navigate("/chat")}
                      >
                        <MessageSquare className="h-3.5 w-3.5" /> {t_i18n("Start chatting")}
                      </Button>
                    )}
                    {/* The guided CTEM loop setup (#1087): administrators only, while no CTEM work runs;
                        the CTEM loop fleet page keeps it reachable. */}
                    {setupEntry.shown && <CtemLoopSetupEntry onDismiss={setupEntry.dismiss} />}
                    <span className="inline-flex items-center gap-1.5 text-[11px] text-default-secondary min-w-0">
                      <Sparkles className="h-3.5 w-3.5 shrink-0 text-accent/70" />
                      <span className="truncate">
                        {postureRefreshing
                          ? t_i18n("Syncing...")
                          : syncedAt
                          ? nextSyncLabel && d.syncDetails
                            ? t_i18n("Last synced {timeAgo} - next sync in {next}", { timeAgo: formatTimeAgo(intl, syncedAt), next: nextSyncLabel })
                            : t_i18n("Last synced {timeAgo}", { timeAgo: formatTimeAgo(intl, syncedAt) })
                          : t_i18n("Not synced yet")}
                      </span>
                      <Tip content={t_i18n("Force sync now: refetches your exposure data live (just for you)")}>
                        <button
                          onClick={() => refreshPosture.mutate(vertical)}
                          disabled={postureRefreshing}
                          className="shrink-0 text-default-secondary hover:text-accent transition-colors disabled:opacity-50"
                          aria-label={t_i18n("Force sync")}
                          data-testid="ctem-hero-sync"
                        >
                          <RefreshCw className={cn("h-3 w-3", postureRefreshing && "animate-spin text-accent")} />
                        </button>
                      </Tip>
                    </span>
                    {/* Composite posture trend - placed by the sync status (between the
                        subtitle and the product pills) so the orbit column stays compact
                        with no blank space. */}
                    {d.syncDetails && (
                      <div className="flex items-center gap-1.5">
                        <div className="w-[72px] h-5">
                          <MiniTrend values={compositeHistory} color="var(--color-accent)" />
                        </div>
                      </div>
                    )}
                  </div>

                  {/* The two scores of the posture and the top risk: one row of
                      stat cells, each figure once, wrapping whole when the
                      column is narrow (item 9d53ff4b). */}
                  {scoreCells.length > 0 && (
                    <CtemStatRow
                      stats={scoreCells}
                      stacked
                      className="w-fit rounded-sm border border-elevation-subtle-soft bg-elevation-default py-1.5"
                    />
                  )}
                  {/* Product status pills - posture is fed by these */}
                  {productPills(d.pills === "compact", "pt-1")}
                </div>

                {/* Below the text, the orbit takes the room left, centred in it. */}
                {d.orbit !== "chip" && (
                  <div className={cn("flex items-center justify-center", d.orbit === "below" ? "flex-1 min-h-0" : "shrink-0")}>
                    {orbit}
                  </div>
                )}
              </div>

            </div>
          );
        }}
      </WidgetSized>
    ),
    // ── KPI cards (exposure + agentic) ────────────────────────────────
    kpi_posture: (
      <KpiCard
        label="Exposure posture"
        value={posture?.posture_score ?? demoP.posture_score}
        unit="/100"
        sub={live ? t_i18n("{covered}/{total} stages covered", { covered: posture?.covered_stages ?? 0, total: posture?.total_stages ?? 0 }) : "fully connected preview"}
        icon={ShieldCheck}
        accent="text-accent/40"
        sample={isDemo}
        // Same affordance as the posture orbit: open the "how it's computed"
        // explainer so this hero KPI isn't the one dead card in the strip.
        onClick={() => setPostureExplainOpen(true)}
      />
    ),
    // Vertical-specific KPIs: cyber CVE/ATT&CK, FIMI narratives/attribution,
    // fraud phishing/takedowns - never cross-contaminated.
    kpi_exposure_1: headlineKpi(VERTICAL_KPIS[vertical][0]),
    kpi_exposure_2: headlineKpi(VERTICAL_KPIS[vertical][1]),
    kpi_exposure_3: headlineKpi(VERTICAL_KPIS[vertical][2]),
    // With AI locked no agent can run: the two agentic slots show
    // deterministic exposure metrics instead (D3-1, #9425).
    kpi_agents: lockedAgentsKpi ? headlineKpi(lockedAgentsKpi) : (
      <KpiCard
        label="Agents"
        value={stats.agents.active}
        sub={t_i18n("{count} active assignments", { count: formatCompactNumber(stats.assignments.active) })}
        icon={Bot}
        current={stats.agents.active}
        previous={stats.diffs.agents_prev_month}
        onClick={() => navigate("/agents")}
      />
    ),
    kpi_executions: lockedExecutionsKpi ? headlineKpi(lockedExecutionsKpi) : (
      <KpiCard
        label="Agentic executions"
        value={stats.runs.this_month}
        sub="last 30 days"
        icon={BarChart3}
        current={stats.runs.this_month}
        previous={stats.diffs.runs_prev_month_count}
        onClick={() => navigate("/operations/executions")}
      />

    ),
    // ── Weekly AI exposure briefing (proactive seeded analyst agent) ────
    briefing: (
      <BriefingCard
        briefing={briefing}
        isSample={!briefingReady}
        agentName={briefingResp?.agent_name ?? "Exposure Briefing Analyst"}
        generatedAt={briefingResp?.generated_at}
        generating={briefingGenerating}
        stale={!!briefingResp?.stale}
        lockReason={aiLockReason}
        onRefresh={() => refreshBriefing.mutate(vertical)}
        refreshing={refreshBriefing.isPending}
        onOpen={() => navigate("/briefing")}
        vertical={vertical}
        agentId={briefingResp?.agent_id}
        agentIsBuiltin={briefingResp?.agent_is_builtin}
        onContentHeight={setBriefingPx}
      />
    ),
    // ── CTEM lifecycle header: the title line of the stage cards ────────
    lifecycle_header: (
      <LifecycleHeader
        live={live}
        covered={posture?.covered_stages ?? 0}
        total={posture?.total_stages ?? 0}
        onExport={exportPosture}
      />
    ),
    // ── CTEM lifecycle: one card per stage ────────────────────────────
    stage_scoping: stageCard("scoping"),
    stage_discovery: stageCard("discovery"),
    stage_prioritization: stageCard("prioritization"),
    stage_validation: stageCard("validation"),
    stage_remediation: stageCard("remediation"),
    // 6th slot - Governance & Risk (OpenCRQ): what OpenCRQ's registration
    // says, never a sample figure (C2 V4, design D2).
    stage_governance: (
      <Card className="overflow-hidden flex h-full flex-col" data-testid="ctem-stage-governance">
        <CardContent className="p-4 flex flex-1 flex-col gap-2.5">
          <CtemWidgetHeader
            level={3}
            icon={<img src={CTEM_PRODUCT_INFO.opencrq.logo} alt="" className="h-4 w-4" />}
            title={t_i18n("Governance & Risk")}
            hint={t_i18n("Real-time CRQ, controls & risk register")}
          />
          <Button type="button" variant="outline" size="sm" className="h-8 w-fit" onClick={() => navigate("/ctem/governance")} data-testid="ctem-stage-governance-open">
            {t_i18n("Open Governance & Risk")}
          </Button>
          {safeExternalUrl(regByProduct.opencrq?.platform_url ?? undefined) ? (
            <Button type="button" variant="ghost" size="sm" className="h-8 w-fit gap-1.5" asChild>
              <a href={safeExternalUrl(regByProduct.opencrq?.platform_url ?? undefined)} target="_blank" rel="noopener noreferrer">
                <ExternalLink className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Open in OpenCRQ")}
              </a>
            </Button>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <span className="content-caption text-default-secondary">{t_i18n("OpenCRQ not connected")}</span>
              <Badge variant="outline" className="content-caption">{t_i18n("Soon")}</Badge>
            </div>
          )}
        </CardContent>
      </Card>
    ),
    // ── Mobilize: recommended actions ─────────────────────────────────
    recommended_actions: (
      <WidgetSized>
        {(size) => (
          <Card className="@container h-full overflow-hidden flex flex-col">
            <CardContent className="px-4 pt-4 pb-0 space-y-3 flex flex-col flex-1">
              <CtemWidgetHeader
                icon={<Target className="h-4 w-4" />}
                title={t_i18n("Recommended actions")}
                hint={t_i18n("Mobilize - fix what matters first")}
                tags={
                  briefingSteps.length > 0 ? (
                    <span className="inline-flex items-center gap-1 content-caption font-medium text-accent/80">
                      <Sparkles className="h-3 w-3" /> {t_i18n("AI")}
                    </span>
                  ) : undefined
                }
                count={recommendations.length > 0 ? numbers.count(recommendations.length) : undefined}
              />
              {recommendations.length === 0 ? (
                // Locked with no deterministic rule firing (B1): the card says
                // why it is empty and gives the one way forward.
                <RecommendedActionsEmpty lockReason={aiLockReason} />
              ) : (
              <div className="flex flex-col flex-1 divide-y divide-[var(--border-elevation-subtle)]">
                {/* As many as the widget holds, most important first: a row is
                    ~52px once the header, the footer and the padding are taken. */}
                {recommendations.slice(0, listCapacity(size, { chrome: 92, rowHeight: 52, max: recommendations.length })).map((r) => (
                  <div key={r.id} className="flex items-center gap-3 flex-1 min-h-[3.25rem]">
                    <div className={cn("h-7 w-7 rounded-sm bg-elevation-hover ring-1 ring-[var(--border-elevation-subtle)] flex items-center justify-center shrink-0", r.tone)}>
                      <r.icon className="h-3.5 w-3.5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <p className="content-base font-medium truncate">{r.title}</p>
                        {r.priorityLabel && CTEM_PRIORITY_CONFIG[r.priorityLabel] && (
                          <ToneBadge tone={CTEM_PRIORITY_CONFIG[r.priorityLabel].tone}>
                            {t_i18n(CTEM_PRIORITY_CONFIG[r.priorityLabel].label)}
                          </ToneBadge>
                        )}
                      </div>
                      {/* Two lines, full text on hover: a truncated reason loses its
                          action part in fr and ja at 1440 (UX #9460 round 2 B1); 12 px floor. */}
                      <p className="text-xs leading-snug text-default-secondary line-clamp-2" title={r.reason}>
                        {r.reason}
                      </p>
                    </div>
                    {/* Briefing-derived steps share a single "Open briefing" link in
                        the footer (no repetitive per-row link); other actions
                        (connect, triage, ...) keep their distinct per-row CTA. */}
                    {r.cta !== "Open briefing" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 content-compact gap-1 shrink-0 text-accent hover:bg-accent/10"
                        onClick={r.onAction}
                        aria-label={t_i18n(r.cta)}
                      >
                        {/* The label once the widget has room for it. */}
                        <span className="hidden @md:inline">{t_i18n(r.cta)}</span> <ChevronRight className="h-3 w-3" />
                      </Button>
                    )}
                  </div>
                ))}
              </div>
              )}
              {/* Footer: owner (left) + a single "Open briefing" link (right) -
                  matches the stage cards' footer (centered, flush via pb-0). */}
              <div className="border-t border-elevation-subtle-soft flex flex-col justify-center min-h-[2.25rem]">
                <div className="flex items-center gap-2">
                  {/* No owner line while AI is locked: the agent cannot run (B1). */}
                  <span className="flex items-center gap-1.5 text-[11px] text-default-secondary min-w-0 flex-1">
                    {!aiLockReason && (
                      <>
                        <Bot className="h-3 w-3 text-accent/70 shrink-0" />
                        <span className="truncate">{t_i18n("Owned by {name}", { name: briefingResp?.agent_name ?? "Exposure Briefing Analyst" })}</span>
                      </>
                    )}
                  </span>
                  {briefingReady && (
                    <button
                      onClick={() => navigate("/briefing")}
                      className="text-[11px] text-accent/80 hover:text-accent inline-flex items-center gap-1 shrink-0"
                    >
                      {t_i18n("Open briefing")} <ChevronRight className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        )}
      </WidgetSized>
    ),
    // ── Live agentic activity ─────────────────────────────────────────
    live_activity: (
      <WidgetSized>
        {(size) => (
      <Card className="h-full overflow-hidden flex flex-col">
        {/* min-h-0: the run list scrolls inside the widget instead of being cut. */}
        <CardContent className="p-4 space-y-3 flex flex-col flex-1 min-h-0">
          <CtemWidgetHeader
            icon={<Zap className="h-4 w-4" />}
            title={t_i18n("Live Activity")}
            actions={
              <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/operations/executions")}>
                <ArrowUpRight className="h-3 w-3" /> {t_i18n("All")}
              </Button>
            }
          />
          <div className="flex-1 overflow-auto min-h-0">
            {latestRuns.length > 0 ? (
              <div className="space-y-2">
                {/* Signals stay visible beside the runs (#838): the two newest, above. */}
                {signalEvents.length > 0 && <ActivityFeed events={signalEvents.slice(0, 2)} onOpenSignal={openSignalTrace} />}
                {latestRuns.map((run) => <RunCard key={run.id} run={run} compact card={false} />)}
              </div>
            ) : activityEvents.length > 0 ? (
              // Only the rows the card holds whole: the oldest never hides under
              // an inner scroll (#9568 UX round 1 B1).
              <ActivityFeed events={activityEvents.slice(0, feedCapacity(size, activityEvents.length))} onOpenSignal={openSignalTrace} />
            ) : (
              <div className="grayscale opacity-50 pointer-events-none select-none divide-y divide-[var(--border-elevation-subtle)]">
                {DEMO_ACTIVITY.slice(0, feedCapacity(size, DEMO_ACTIVITY.length)).map((row) => <DemoActivityRow key={row.id} row={row} />)}
              </div>
            )}
          </div>
          {latestRuns.length === 0 && (
            // Locked (M3): the sample keeps its badge, the caption that promises
            // runs becomes the footer's one way forward.
            <div className="content-caption text-default-secondary flex flex-wrap items-center gap-1.5 pt-1">
              {activityEvents.length === 0 && <SampleBadge />}
              {aiLockReason ? (
                <AiLockedFooter reason={aiLockReason} />
              ) : (
                <span>{t_i18n("Real agent runs stream here as assignments fire")}</span>
              )}
            </div>
          )}
        </CardContent>
      </Card>
        )}
      </WidgetSized>
    ),
    // ── Latest Results & Digests (agent reports / digests) ──────────────
    digests: (
      <Card className="@container h-full overflow-hidden flex flex-col">
        <CardContent className="p-4 flex flex-col flex-1 min-h-0">
          <CtemWidgetHeader
            className="mb-3"
            icon={<BarChart3 className="h-4 w-4" />}
            title={t_i18n("Latest Results & Digests")}
            actions={
              stats.recent_digests.length > 0 ? (
                <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/operations/executions?filter=digests")}>
                  <ArrowUpRight className="h-3 w-3" /> {t_i18n("View all")}
                </Button>
              ) : undefined
            }
          />

          {stats.recent_digests.length > 0 ? (
            <div className="grid gap-4 @xl:grid-cols-2 @4xl:grid-cols-3 content-start min-h-0 flex-1 overflow-y-auto">
              {stats.recent_digests.slice(0, 9).map((digest: DigestItem) => {
                const Icon = ICON_MAP[digest.agent_icon] ?? Bot;
                return (
                  <Card
                    key={digest.id}
                    className="cursor-pointer hover:bg-elevation-hover transition-all duration-300 group"
                    onClick={() => navigate(`/operations/executions/${digest.id}`)}
                  >
                    <CardContent className="pt-5 pb-4 space-y-3">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <div className="flex h-7 w-7 items-center justify-center rounded-sm bg-accent/10 group-hover:bg-accent/15 transition-colors">
                            <Icon className="h-3.5 w-3.5 text-accent" />
                          </div>
                          <div>
                            <p className="content-compact font-semibold">{digest.assignment_name}</p>
                            <p className="content-caption text-default-secondary">{digest.agent_name}</p>
                          </div>
                        </div>
                        <span className="text-[9px] text-default-secondary shrink-0">{formatTimeAgo(intl, digest.created_at)}</span>
                      </div>
                      <div className="rounded-sm bg-elevation-hover p-3 group-hover:bg-elevation-hover transition-colors line-clamp-4 overflow-hidden text-[11px] text-default-secondary leading-relaxed [&_.chat-markdown]:contents [&_p]:mb-1 [&_p:last-child]:mb-0 [&_h1]:content-compact [&_h1]:font-semibold [&_h1]:mb-1 [&_h2]:content-compact [&_h2]:font-semibold [&_h2]:mb-1 [&_h3]:text-[11px] [&_h3]:font-semibold [&_h3]:mb-1 [&_ul]:mb-1 [&_ol]:mb-1 [&_li]:text-[11px] [&_code]:content-caption [&_pre]:hidden [&_table]:hidden [&_img]:hidden [&_hr]:hidden [&_blockquote]:border-l [&_blockquote]:border-accent/20 [&_blockquote]:pl-2 [&_blockquote]:my-1">
                        <ChatMarkdown content={digest.summary} />
                      </div>
                      <div className="flex items-center justify-between">
                        {digest.actions_count > 0 && (
                          <span className="text-[9px] text-accent">{t_i18n("{count, plural, one {# action taken} other {# actions taken}}", { count: digest.actions_count })}</span>
                        )}
                        <span className="text-[9px] text-accent ml-auto group-hover:underline">{t_i18n("Read more")} →</span>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          ) : aiLockReason ? (
            // Locked (E1): the same empty state, with the one way forward. No
            // `requiresAi`: EmptyState would hide the footer with AI off.
            <EmptyState
              variant="inset"
              icon={BarChart3}
              title={t_i18n("No results yet")}
              description={t_i18n("Digests and reports from your agents will appear here.")}
            >
              <AiLockedFooter reason={aiLockReason} className="justify-center" />
            </EmptyState>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-center py-8">
              <BarChart3 className="h-8 w-8 text-default-disabled mb-2" />
              <p className="content-compact text-default-secondary">{t_i18n("No results yet")}</p>
              <p className="content-compact text-default-secondary mt-1">{t_i18n("Digests and reports from your agents will appear here.")}</p>
            </div>
          )}
        </CardContent>
      </Card>
    ),
    // ── Fleet activity: one tile per fleet, from its health and the ROI report
    fleet_activity: (
      <FleetActivityCard
        activity={fleetActivity}
        isLoading={fleetActivity.isLoading || !configLoaded}
        licensed={!configLoaded || agenticLicensed}
        lockReason={aiLockReason}
        canSetUp={isAdmin}
        inviteShown={fleetInviteShown}
        onDismissInvite={dismissFleetInvite}
      />
    ),
    // ── Fleet timeline: the fleets' latest runs and what started each
    fleet_timeline: (
      <FleetTimelineCard
        timeline={fleetActivity.timeline}
        casesById={casesById}
        isLoading={fleetActivity.timelineLoading || !configLoaded}
        licensed={!configLoaded || agenticLicensed}
        lockReason={aiLockReason}
      />
    ),
    // ── Coverage: the five stages against the three modules, with the connect path
    coverage_matrix: <CoverageMatrixCard matrix={coverage} onConnect={(m) => setConnectProduct(m)} />,
    // ── Exposure stats: posture and stage scores over the recorded period
    exposure_stats: <ExposureStatsCard stats={periodStats} />,
    // ── Value band (C2 V1): posture, risk, value and actions, fleets off first
    value_band: (
      <ValueBandCard
        score={live ? posture?.posture_score ?? null : null}
        band={posture?.posture_band ?? "unknown"}
        coveredStages={posture?.covered_stages ?? 0}
        totalStages={posture?.total_stages ?? CTEM_STAGE_KEYS.length}
        history={valueBandHistory}
        kev={kev}
        briefing={
          briefingReady && briefingResp?.briefing
            ? { headline: briefingResp.briefing.headline, agentName: briefingResp.agent_name, generatedAt: briefingResp.generated_at }
            : null
        }
        syncedAt={syncedAt}
        syncing={postureRefreshing}
        onForceSync={() => refreshPosture.mutate(vertical)}
        onExplain={() => setPostureExplainOpen(true)}
        onConnectOpenCti={() => setConnectProduct("opencti")}
        runsThisMonth={stats.runs.this_month}
        aiLockReason={aiLockReason}
        isAdmin={isAdmin}
        greeting={bandTopLine ? greeting : undefined}
        products={bandTopLine ? productPills(true) : undefined}
      />
    ),
    // ── Act now (C2 V2): one ranked queue, fleets off first
    act_now: (
      <ActNowCard
        rows={actNowBase}
        briefingActions={briefingActions}
        aiLockReason={aiLockReason}
        onContentHeight={setActNowPx}
        refreshing={postureRefreshing || assessmentsRefreshing}
      />
    ),
    // ── Done for you (C2 V3): module events, automations, fleets, outcomes
    done_for_you: (
      <DoneForYouCard
        events={timelineEvents}
        runs={recentRuns ?? []}
        digests={stats.recent_digests}
        fleetRuns={configLoaded && agenticLicensed ? fleetActivity.timeline : []}
        aiLockReason={aiLockReason}
        onContentHeight={setDoneForYouPx}
      />
    ),
    // ── Lifecycle band (C2 V4): six stage chips, each opening its stage page
    lifecycle_band: (
      <LifecycleBandCard
        stages={posture?.stages ?? []}
        live={live}
        covered={posture?.covered_stages ?? 0}
        total={posture?.total_stages ?? CTEM_STAGE_KEYS.length}
        history={histPoints}
        vertical={vertical}
        aiLockReason={aiLockReason}
        opencrq={regByProduct.opencrq ? { url: regByProduct.opencrq.platform_url } : null}
        onExport={exportPosture}
        onConnect={(p) => setConnectProduct(p)}
        onContentHeight={setLifecyclePx}
        contextMissing={contextMissing}
        posture={posture ? {
          score: posture.posture_score,
          band: posture.posture_band,
          qualitativeScore: posture.qualitative_score ?? null,
          qualitativeBand: posture.qualitative_band,
          target: postureTarget,
        } : null}
        refreshing={postureRefreshing}
        signals={signalCounter}
        cases={lifecycleCases}
      />
    ),
    // ── Stats and coverage (C2 V5, #11547 S2d): weekly heat map, delta bars, findings with their trend
    stats_coverage: (
      <StatsCoverageCard
        history={histPoints}
        stats={coverageStats}
        findings={VERTICAL_KPIS[vertical].map((kpi) => {
          const present = kpi.product === "openaev" ? aevPresent : octiPresent;
          // Known-exploited counts the software inventory's when the profile names one
          // (#11251); the figure opens its in-app stage page, never the
          // unfiltered module list (#11324).
          const value = kpi.headlineKey === "kev_count" ? kevShown : h?.[kpi.headlineKey] ?? null;
          return {
            label: kpi.label,
            value: present ? value ?? null : null,
            unit: kpi.unit,
            stage: present ? kpi.stage : undefined,
            connect: present ? null : kpi.product,
            series: present ? metricSeries(histPoints, kpi.stage, kpi.headlineKey) : [],
          };
        })}
        generic={vertical === "cyber" && octiPresent && kevTailored === null && kev !== null}
        onConnect={(m) => setConnectProduct(m)}
        onContentHeight={setStatsCoveragePx}
      />
    ),
    // ── Fleets row (C2 V6): one line per CTEM fleet, or the setup invitation
    fleets_row: (
      <FleetsRowCard
        activity={fleetActivity}
        isLoading={fleetActivity.isLoading || !configLoaded}
        licensed={!configLoaded || agenticLicensed}
        lockReason={aiLockReason}
        canSetUp={isAdmin}
        inviteShown={fleetInviteShown}
        onDismissInvite={dismissFleetInvite}
        onContentHeight={setFleetsRowPx}
        onChooseFleets={() => setChooseFleetsOpen(true)}
        onHideFleet={hideFleet}
        noFleet={!!allFleets && allFleets.every((f) => f.is_archived)}
      />
    ),
    // The compact prioritization funnel (#11546), summarising the stage page's.
    prioritization_funnel: (() => {
      const funnel = posture?.stages.find((s) => s.key === "prioritization")?.viz.funnel ?? null;
      // OpenCTI connected and no funnel yet: a placeholder, never the sample (#11662).
      const display = funnelDisplay({
        funnel,
        openctiPresent: Boolean(posture?.products?.opencti?.present),
        shape: posture?.shape,
      });
      return (
        <PrioritizationFunnelCard
          funnel={funnel ?? SAMPLE_FUNNEL}
          sample={display === "sample"}
          placeholder={display === "pending" || display === "unavailable" ? display : undefined}
          openctiUrl={posture?.products?.opencti?.url}
          readAt={posture?.generated_at}
        />
      );
    })(),
    // The CTEM Cases counts by state (#11545 S5a), each opening the Cases list on it.
    cases: <CasesCountsCard configured={!contextMissing || aevPresent} onConnectOpenAev={() => setConnectProduct("openaev")} />,
  };

  // Each widget's name, for its edit overlay and the catalog of removed ones.
  const widgetLabels: Record<CtemWidgetId, string> = {
    hero: t_i18n("Posture overview"),
    kpi_posture: t_i18n("Exposure posture"),
    kpi_exposure_1: t_i18n(VERTICAL_KPIS[vertical][0].label),
    kpi_exposure_2: t_i18n(VERTICAL_KPIS[vertical][1].label),
    kpi_exposure_3: t_i18n(VERTICAL_KPIS[vertical][2].label),
    kpi_agents: t_i18n(lockedAgentsKpi?.label ?? "Agents"),
    kpi_executions: t_i18n(lockedExecutionsKpi?.label ?? "Agentic executions"),
    briefing: t_i18n("Exposure briefing"),
    lifecycle_header: t_i18n("Exposure lifecycle"),
    stage_scoping: resolvedStages.scoping.label,
    stage_discovery: resolvedStages.discovery.label,
    stage_prioritization: resolvedStages.prioritization.label,
    stage_validation: resolvedStages.validation.label,
    stage_remediation: resolvedStages.remediation.label,
    stage_governance: t_i18n("Governance & Risk"),
    recommended_actions: t_i18n("Recommended actions"),
    live_activity: t_i18n("Live Activity"),
    digests: t_i18n("Latest Results & Digests"),
    fleet_activity: t_i18n("Fleet activity"),
    fleet_timeline: t_i18n("Fleet timeline"),
    coverage_matrix: t_i18n("Coverage"),
    exposure_stats: t_i18n("Exposure stats"),
    value_band: t_i18n("Value band"),
    act_now: t_i18n("Act now"),
    done_for_you: t_i18n("Done for you"),
    lifecycle_band: t_i18n("Lifecycle band"),
    stats_coverage: t_i18n("Stats and coverage"),
    fleets_row: t_i18n("Fleets row"),
    prioritization_funnel: t_i18n("Prioritization funnel"),
    cases: t_i18n("Cases"),
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* ── Unified notices region: every health / config / work alert, one
            layout + size + semantic color, always at the top. ─── */}
      <DashboardNotices />
      {/* The organization context of Settings, the only company context (#11464):
          administrators only, dismissible per person (#11586). A complete
          context is one quiet line in the toolbar row below (#12258). */}
      <CompanyContextNotice context={posture?.context} placement="dashboard" />

      {/* Per-user vertical switcher - small + pinned top-right OUTSIDE the hero.
          The "Customize" button sits with it: it turns the grid below into a
          live editor of this person's command center for the vertical shown.
          `space-y-6` spaces this page with a BOTTOM margin on each child, so
          the row's own `mb-2` replaces it (a negative one would pull the grid
          over the row, and a grid widget, positioned, paints above it). No
          negative top margin: with no notice the row sits at the page's top
          padding (#11199). */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2" data-testid="ctem-toolbar">
        {/* The context bar (dev-docs/ctem-stage-viz.md 8.2): saved with this
            person's layout of the vertical shown, read by every widget. The
            confirmation of a complete organization context follows the
            period chip on the same row, no row of its own (#12258). */}
        <div className="flex flex-wrap items-center gap-3">
          <DashboardContextBar
            frameworks={contextFrameworks(validationMatrix)}
            framework={context.framework}
            defaultFramework={framework.id}
            frameworkFellBack={framework.fellBack}
            period={context.period}
            overridden={ownPeriods.map((o) => ({ label: widgetLabels[o.id], period: o.period }))}
            onFramework={(id) => onLayoutChange(withContext(dashboardLayout, { framework: id }))}
            onPeriod={(p) => onLayoutChange(withContext(dashboardLayout, { period: p }))}
            onReset={() => onLayoutChange(withDefaultContext(dashboardLayout))}
          />
          {posture?.context?.state === "complete" && <ContextScoredLine />}
        </div>
        {/* One row height (32 px) for every control of the row (UX round 1, B4). */}
        <div className="flex items-center gap-2 ml-auto">
          {!customizing && (
            <CustomizeButton
              className="h-8"
              onClick={() => setCustomizing(true)}
              tooltip={t_i18n("Build your command center: move, resize, remove and add widgets")}
            />
          )}
          <VerticalSwitcher value={vertical} onChange={onVerticalChange} size="sm" className="h-8" />
        </div>
      </div>
      {/* Under the row, never inside it: the notice does not push the right group to a new line (B4). */}
      {framework.fellBack && (
        <p className="-mt-1 mb-2 content-caption text-default-secondary" role="status" data-testid="ctem-context-fallback">
          {t_i18n("The framework saved in this context has no data any more: the one with the most data is shown.")}
        </p>
      )}

      <DashboardGrid
        registry={CTEM_WIDGETS}
        layout={drawnLayout}
        widgets={widgets}
        labels={widgetLabels}
        title={t_i18n("Customize your command center")}
        editing={customizing}
        onChange={onLayoutChange}
        onDone={() => setCustomizing(false)}
        configure={{
          ids: PERIOD_WIDGETS,
          overridden: new Set(ownPeriods.map((o) => o.id)),
          onOpen: setConfiguring,
        }}
      />

      <WidgetSettingsDialog
        key={configuring ?? "closed"}
        name={configuring ? widgetLabels[configuring] : null}
        period={configuring ? dashboardLayout.items.find((item) => item.i === configuring)?.settings?.period ?? null : null}
        dashboardPeriod={context.period}
        onSave={(p) => {
          if (configuring) onLayoutChange(withWidgetSettings(dashboardLayout, configuring, { period: p }));
        }}
        onClose={() => setConfiguring(null)}
      />

      <ChooseFleetsDialog
        open={chooseFleetsOpen}
        onOpenChange={setChooseFleetsOpen}
        fleets={allFleets ?? []}
        chosenIds={chosenFleetIds}
        onSave={saveFleetChoice}
      />

      <ConnectPlatformDialog
        product={connectProduct}
        onOpenChange={(open) => { if (!open) setConnectProduct(null); }}
      />

      <PostureExplainDialog open={postureExplainOpen} onOpenChange={setPostureExplainOpen} posture={posture} />
    </div>
  );
}
