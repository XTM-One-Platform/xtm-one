// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Bot, Zap, CheckCircle2, XCircle, ArrowUpRight, BrainCircuit,
  Clock, BarChart3, Link2, Plus, Activity, FileText,
  MessageSquare, Wrench,
} from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { PageSkeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { RunCard } from "@/components/ui/run-card";
import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useConversations } from "@/hooks/api/useChatApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useDashboardStats } from "@/hooks/api/useMiscApi";
import { useAllAssignmentRuns } from "@/hooks/api/useAssignmentApi";
import type { AgentSummary, ConversationListItem, CopilotWidgetId, DigestItem } from "@/types";
import { cn, exactMetricValue, formatCompactNumber, pluralize, isEntityShared } from "@/lib/utils";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";
import { PROVIDER_LOGOS, REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import { Tip } from "@/components/ui/tooltip";
import { SharedBadge } from "@/components/ui/shared-badge";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { DiffBadge } from "@/components/ui/diff-badge";
import { useNow, useTick } from "@/hooks/useTick";
import { DashboardNotices } from "@/pages/xtm-one/DashboardNotices";
import { useFormatter } from "@/i18n";
import { CustomizeButton, DashboardGrid } from "@/components/dashboard-grid/DashboardGrid";
import { kpiDensity, kpiLayout } from "@/components/dashboard-grid/widget-density";
import { WidgetSized } from "@/components/dashboard-grid/widget-size";
import { useWidgetSize } from "@/components/dashboard-grid/widget-size-context";
import { useDashboardLayoutState } from "@/hooks/useDashboardLayoutState";
import { COPILOT_WIDGETS } from "@/pages/dashboard/copilot-dashboard-layout";
import { RoiValueCard } from "@/components/roi/RoiValueCard";
import { RoiValueDriversCard } from "@/components/roi/RoiValueDriversCard";
import { CopilotFleetsWidget } from "@/components/fleets/CopilotFleetsWidget";
import { withContentHeight } from "@/pages/xtm-one/ctem/dashboard-layout";
import { AgentIconTile } from "@/components/ui/agent-icon-tile";
import { copilotHeroDensity } from "@/pages/dashboard/copilot-density";
import { formatTimeAgo } from "@/lib/relative-time";

// ---------------------------------------------------------------------------
// Live Activity Ticker – scrolls recent runs as a marquee-style feed
// ---------------------------------------------------------------------------
function LiveActivityTicker({ runs }: { runs: { id: string; assignment_name?: string | null; agent_name?: string | null; assignment_id?: string | null; agent_id: string; status: string; created_at: string; parent_run_id?: string | null }[] }) {
  const { t_i18n, intl } = useFormatter();
  const [tickerIdx, setTickerIdx] = useState(0);
  const activeRuns = runs.slice(0, 5);

  useEffect(() => {
    if (activeRuns.length <= 1) return;
    const interval = setInterval(() => setTickerIdx((i) => (i + 1) % activeRuns.length), 4000);
    return () => clearInterval(interval);
  }, [activeRuns.length]);

  if (activeRuns.length === 0) return null;
  const run = activeRuns[tickerIdx];
  const isRunning = run.status === "running" || run.status === "pending";

  return (
    <div className="flex items-center gap-2 content-compact h-5">
      <div className="relative flex items-center justify-center h-2 w-2 shrink-0">
        <span className="absolute inline-flex h-full w-full rounded-full bg-accent opacity-75 animate-ping" />
        <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-accent" />
      </div>
      {/* The mask wraps ONLY the rotating text: ``animate-fade-in`` slides
          up 8px on entry and must stay clipped to the row, but putting
          ``overflow-hidden`` on the whole row also truncated the left half
          of the status dot's ping halo (it expands beyond the 8px box). */}
      <div className="overflow-hidden h-5 flex items-center min-w-0">
        <div
          key={run.id + tickerIdx}
          className="animate-fade-in flex items-center gap-1.5 text-default-secondary min-w-0"
        >
          {isRunning ? (
            <Spinner size="sm" tone="inherit" className="text-accent shrink-0" label={t_i18n("Running")} />
          ) : run.status === "completed" ? (
            <CheckCircle2 className="h-3 w-3 text-feedback-success-primary shrink-0" />
          ) : (
            <XCircle className="h-3 w-3 text-feedback-error-primary shrink-0" />
          )}
          <span className="truncate">
            <span className="text-default-primary font-medium">{run.assignment_name ?? (run.parent_run_id ? t_i18n("Delegation") : run.assignment_id?.slice(0, 8) ?? t_i18n("Run"))}</span>
            {" · "}
            <span>{run.agent_name ?? run.agent_id.slice(0, 8)}</span>
            {" · "}
            <span className="text-accent">{formatTimeAgo(intl, run.created_at)}</span>
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Ecosystem Orbit — animated ring showing connected integrations
// ---------------------------------------------------------------------------
function EcosystemOrbit({ integrationCount, logos }: { integrationCount: number; logos: string[] }) {
  const { t_i18n } = useFormatter();
  // Resolve each provider key through PROVIDER_LOGOS so providers whose key
  // doesn't match the SVG filename (e.g. `filigran_ai` → `filigran.svg`) render
  // their real logo, and providers without a logo entry are skipped instead of
  // showing a broken-image placeholder in the orbit.
  const visibleLogos = logos
    .map((provider) => ({ provider, src: PROVIDER_LOGOS[provider] }))
    .filter((entry): entry is { provider: string; src: string } => Boolean(entry.src))
    .slice(0, 8);
  const angleStep = 360 / Math.max(visibleLogos.length, 1);

  return (
    <div className="relative w-44 h-44 mx-auto shrink-0">
      {/* Outer orbit ring */}
      <div className="absolute inset-0 rounded-full border border-accent/10 animate-[orbit_60s_linear_infinite] motion-reduce:animate-none" />
      {/* Middle ring */}
      <div className="absolute inset-4 rounded-full border border-accent/5" />
      {/* Inner glow core */}
      <div className="absolute inset-[52px] rounded-full bg-accent/5 backdrop-blur-xs flex items-center justify-center">
        <div className="relative">
          <BrainCircuit className="h-8 w-8 text-accent animate-pulse-soft" />
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="absolute -inset-3 rounded-full bg-accent/10 animate-pulse-ring" />
          </div>
        </div>
      </div>
      {/* Orbiting integration logos */}
      <div className="absolute inset-0 animate-[orbit_40s_linear_infinite] motion-reduce:animate-none">
        {visibleLogos.map(({ provider, src }, i) => {
          const angle = (angleStep * i - 90) * (Math.PI / 180);
          const radius = 80;
          const x = 88 + radius * Math.cos(angle) - 14;
          const y = 88 + radius * Math.sin(angle) - 14;
          return (
            <div
              key={provider}
              className="absolute w-7 h-7 rounded-full layer-1 bg-elevation-default border border-elevation-subtle-soft flex items-center justify-center animate-counter-orbit motion-reduce:animate-none"
              style={{ left: x, top: y }}
            >
              <img
                src={src}
                alt={provider}
                className="h-4 w-4 object-contain"
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
              />
            </div>
          );
        })}
      </div>
      {/* Count badge */}
      {integrationCount > 0 && (
        <div className="absolute -bottom-1 left-1/2 -translate-x-1/2 bg-accent/20 text-accent content-caption font-bold rounded-sm px-2 py-0.5 border border-accent/30 backdrop-blur-xs">
          {t_i18n("{count} connected", { count: formatCompactNumber(integrationCount) })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agent Status Card — with subtle glow when running
// ---------------------------------------------------------------------------
function AgentCard({ agent, onClick, userId }: { agent: AgentSummary; onClick: () => void; userId?: string }) {
  const { t_i18n } = useFormatter();
  const isRunning = agent.last_run?.status === "running";
  const isFailed = agent.last_run?.status === "failed";
  const isShared = isEntityShared(agent, userId);

  return (
    <div
      className={cn(
        "group relative cursor-pointer transition-all duration-300 overflow-hidden h-14",
        agent.enabled
          ? "hover:bg-elevation-hover"
          : isFailed
          ? "hover:bg-elevation-hover"
          : "opacity-60",
      )}
      onClick={onClick}
    >
      {/* Running shimmer */}
      {isRunning && (
        <div className="absolute inset-0 bg-gradient-to-r from-accent/[0.03] via-transparent to-accent/[0.03] animate-shimmer" style={{ backgroundSize: "200% 100%" }} />
      )}

      {/* The row is 56 px: its content is centred in it rather than padded from the top, so the
          name line and the stats line always fit and nothing is clipped (design standards s.4). */}
      <div className="relative flex h-full items-center px-3">
        <div className="flex w-full items-center gap-2 min-w-0">
          {/* Icon with active pulse ring */}
          <div className="relative shrink-0">
            <AgentIconTile icon={agent.icon} muted={!agent.enabled && !isRunning} className={isRunning ? "bg-accent/20" : undefined}>
              {isRunning ? <Spinner size="sm" tone="inherit" className="text-accent" label={t_i18n("Running")} /> : undefined}
            </AgentIconTile>
            {agent.enabled && !isRunning && (
              <span className="absolute -top-0.5 -right-0.5 flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-feedback-success-primary opacity-50" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-feedback-success-primary" />
              </span>
            )}
            {isRunning && (
              <span className="absolute -top-0.5 -right-0.5 flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-50" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-accent" />
              </span>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 min-w-0">
              {/* A cut name is named whole by the design-system tooltip; the company-managed mark is
                  the icon, as the badge label does not fit this narrow widget on one line. */}
              <TruncatedText text={agent.name} className="content-compact font-medium" />
              {agent.is_company_managed && <CompanyManagedIcon label="Company-managed agent" />}
              {isShared && <SharedBadge size="sm" ownerName={agent.owner_name} />}
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <Tip content={t_i18n("Assignments")}>
                <span className="inline-flex items-center gap-0.5 content-caption text-default-secondary">
                  <Zap className="h-2.5 w-2.5" />
                  {formatCompactNumber(agent.assignment_count)}
                </span>
              </Tip>
              <Tip content={t_i18n("Runs this week")}>
                <span className={cn("inline-flex items-center gap-0.5 content-caption", agent.runs_this_week > 0 ? "text-accent" : "text-default-secondary")}>
                  <Activity className="h-2.5 w-2.5" />
                  {formatCompactNumber(agent.runs_this_week)}
                </span>
              </Tip>
              <Tip content={t_i18n("Chat queries this week")}>
                <span className={cn("inline-flex items-center gap-0.5 content-caption", (agent.chat_queries_this_week ?? 0) > 0 ? "text-accent" : "text-default-secondary")}>
                  <MessageSquare className="h-2.5 w-2.5" />
                  {formatCompactNumber(agent.chat_queries_this_week ?? 0)}
                </span>
              </Tip>
            </div>
          </div>
          <div className="shrink-0 flex items-center">
            {agent.last_run ? (
              <>
                {agent.last_run.status === "completed" && <CheckCircle2 className="h-3 w-3 text-feedback-success-primary" />}
                {agent.last_run.status === "failed" && <XCircle className="h-3 w-3 text-feedback-error-primary" />}
                {agent.last_run.status === "running" && <Spinner size="sm" tone="inherit" className="text-accent" label={t_i18n("Running")} />}
                {!["completed", "failed", "running"].includes(agent.last_run.status) && <Clock className="h-3 w-3 text-default-secondary" />}
              </>
            ) : (
              <ArrowUpRight className="h-3 w-3 text-default-disabled group-hover:text-accent transition-colors" />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Conversation Card — matches AgentCard look & feel
// ---------------------------------------------------------------------------
function ConversationCard({ conversation, onClick }: { conversation: ConversationListItem; onClick: () => void }) {
  const { t_i18n, intl } = useFormatter();

  return (
    <div
      className="group relative cursor-pointer transition-all duration-300 overflow-hidden hover:bg-elevation-hover h-14"
      onClick={onClick}
    >
      {/* The row is 56 px: its content is centred in it rather than padded from the top, so the
          title line and the stats line always fit and nothing is clipped (design standards s.4). */}
      <div className="relative flex h-full items-center px-3">
        <div className="flex w-full items-center gap-2 min-w-0">
          {/* Icon */}
          <div className="relative shrink-0">
            <AgentIconTile icon={conversation.agent_icon} fallback={MessageSquare} />
          </div>
          <div className="min-w-0 flex-1">
            {/* A cut title is named whole by the design-system tooltip. */}
            <TruncatedText text={conversation.title} className="content-compact font-medium" />
            <div className="flex items-center gap-2 mt-0.5">
              <Tip content={t_i18n("Messages")}>
                <span className={cn("inline-flex items-center gap-0.5 content-caption", conversation.message_count > 0 ? "text-accent" : "text-default-secondary")}>
                  <MessageSquare className="h-2.5 w-2.5" />
                  {formatCompactNumber(conversation.message_count)}
                </span>
              </Tip>
              <Tip content={t_i18n("Tools enabled")}>
                <span className={cn("inline-flex items-center gap-0.5 content-caption", conversation.tool_count > 0 ? "text-accent" : "text-default-secondary")}>
                  <Wrench className="h-2.5 w-2.5" />
                  {formatCompactNumber(conversation.tool_count)}
                </span>
              </Tip>
              {conversation.agent_name && (
                <Tip content={conversation.agent_name}>
                  <span className="inline-flex items-center gap-0.5 content-caption text-default-secondary truncate">
                    <Bot className="h-2.5 w-2.5 shrink-0" />
                    <span className="truncate">{conversation.agent_name}</span>
                  </span>
                </Tip>
              )}
            </div>
          </div>
          {/* The time and the arrow share one line, so the right column is never taller than the row. */}
          <div className="shrink-0 flex items-center gap-1">
            <span className="content-caption text-default-secondary whitespace-nowrap">{formatTimeAgo(intl, conversation.updated_at)}</span>
            <ArrowUpRight className="h-3 w-3 text-default-disabled group-hover:text-accent transition-colors" />
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI tile — one figure of the dashboard, drawn at the density its grid cell
// allows (`kpiDensity`): no secondary line at three rows, the comparison badge
// next to the value when narrow.
// ---------------------------------------------------------------------------
function KpiTile({
  label, value, current, previous, sub, icon, onClick,
}: {
  label: string;
  value: number;
  current: number;
  previous: number;
  sub: ReactNode;
  icon: ReactNode;
  onClick: () => void;
}) {
  const { t_i18n } = useFormatter();
  const size = useWidgetSize();
  const density = kpiDensity(size);
  const layout = kpiLayout(size);
  const badge = <DiffBadge current={current} previous={previous} label={t_i18n("vs last month")} />;
  return (
    <Card
      className={cn("h-full flex flex-col cursor-pointer transition-all duration-300 hover:bg-elevation-hover", layout.card, BUTTON_ROLE_FOCUS_RING)}
      {...buttonRoleProps(onClick)}
    >
      <CardContent className={cn("flex justify-between", layout.content)}>
        <div className="min-w-0">
          <div className={cn("flex gap-2", layout.labelRow)} data-kpi-label-row>
            <p
              className={cn("content-caption font-medium text-default-secondary uppercase", layout.label)}
              title={label}
            >
              {label}
            </p>
            {!density.badgeByValue && badge}
          </div>
          <div className="flex items-center gap-1.5 mt-1">
            {/* Compact on the card, the exact count on hover (#10476). */}
            <p className="text-3xl font-bold tabular-nums" title={exactMetricValue(value)}>
              <AnimatedNumber value={value} />
            </p>
            {density.badgeByValue && badge}
          </div>
          {!density.hideSub && <div className="mt-1.5">{sub}</div>}
        </div>
        {icon}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main Dashboard (Copilot mode)
//
// The XTM One ⇄ Copilot mode branch lives at the route level in App.tsx,
// where platformConfig is already resolved — this module is the Copilot
// dashboard chunk only, and pages/xtm-one/XtmOneDashboard is its sibling
// chunk. Nesting the XTM One dashboard as a second lazy() inside this chunk
// caused two sequential chunk downloads (two skeleton flashes) on the home
// route.
// ---------------------------------------------------------------------------
export default function Dashboard() {
  const { t_i18n, intl } = useFormatter();
  useTick(REFETCH_INTERVAL_SLOW); // keep relative timestamps (timeAgo) fresh
  const { data: stats, isLoading } = useDashboardStats();
  const { data: recentRuns } = useAllAssignmentRuns(10, 0, undefined, undefined, undefined, "mine");
  const { data: integrations } = useIntegrations();
  const { data: user } = useMe();
  const { data: conversations } = useConversations(10);
  const navigate = useNavigate();

  // Determine connected integration logos (exclude bot/channel providers)
  const connectedLogos = useMemo(() => {
    if (!integrations) return [];
    return integrations
      .filter((i) => i.status === "connected")
      .map((i) => i.provider)
      .filter((v, idx, arr) => arr.indexOf(v) === idx);
  }, [integrations]);

  // Resolved logo entries for the inline integrations row. Computed
  // off ``connectedLogos`` once so the rendering condition, the
  // ``.slice(0, 5)`` map, and the ``+N`` overflow count all agree on
  // the same filtered list — without this, a connected provider whose
  // key isn't in ``PROVIDER_LOGOS`` (e.g. ``filigran_ai`` before round
  // 1 of this PR) would (a) render the wrapper div empty, and (b)
  // get included in the ``+N`` count even though it was silently
  // skipped from the visible row.
  const connectedLogoEntries = useMemo(
    () =>
      connectedLogos
        .map((provider) => ({ provider, src: PROVIDER_LOGOS[provider] }))
        .filter((entry): entry is { provider: string; src: string } => Boolean(entry.src)),
    [connectedLogos],
  );


  // The greeting's clock, read again every 30 s outside render.
  const now = new Date(useNow());
  const hour = now.getHours();
  const timeGreeting = hour < 12 ? t_i18n("Good morning") : hour < 17 ? t_i18n("Good afternoon") : t_i18n("Good evening");
  const firstName = user?.full_name?.split(" ")[0] ?? "";
  const greeting = firstName ? t_i18n("{greeting}, {name}", { greeting: timeGreeting, name: firstName }) : timeGreeting;

  // The dashboard this person built (their saved layout, every widget exactly
  // once), and whether they are editing it.
  const updateProfile = useUpdateProfile();
  const { layout: dashboardLayout, onChange: onLayoutChange } = useDashboardLayoutState({
    key: "copilot",
    saved: user?.copilot_dashboard_layout,
    registry: COPILOT_WIDGETS,
    write: (layout, settled) => updateProfile.mutate({ copilot_dashboard_layout: layout }, { onSettled: settled }),
  });
  const [customizing, setCustomizing] = useState(false);
  // The Fleets widget is drawn at the height of its content (#12063), never saved so.
  const [fleetsPx, setFleetsPx] = useState<number | null>(null);
  const drawnLayout = useMemo(
    () => (customizing ? dashboardLayout : withContentHeight<CopilotWidgetId>(dashboardLayout, "fleets", fleetsPx)),
    [customizing, dashboardLayout, fleetsPx],
  );

  if (isLoading || !stats) return <PageSkeleton cards={6} />;

  const hasRunning = stats.runs.running > 0;

  // Every widget of the dashboard grid; each fills the cell it is given.
  const widgets: Record<CopilotWidgetId, ReactNode> = {
    // ─── Hero header with live status ─ drawn at the density its size allows
    //     (`copilotHeroDensity`): the orbit beside the text, or its count as a
    //     chip in the header line.
    hero: (
      <WidgetSized>
        {(size) => {
          const d = copilotHeroDensity(size);
          return (
            <div
              className={cn(
                "@container relative h-full overflow-hidden rounded-sm bg-gradient-to-br from-[var(--bg-elevation-default)] via-[var(--bg-elevation-default)] to-accent/[0.03]",
                // Without the orbit the header is short: tighter all round.
                d.orbit ? "p-6 @3xl:p-8" : "p-5",
              )}
            >
              {/* Ambient glow */}
              <div className="absolute top-0 right-0 w-96 h-96 bg-accent/[0.03] rounded-full blur-3xl -translate-y-1/2 translate-x-1/3 pointer-events-none" />
              <div className="absolute bottom-0 left-0 w-64 h-64 bg-accent/[0.02] rounded-full blur-3xl translate-y-1/2 -translate-x-1/3 pointer-events-none" />

              <div className="relative space-y-4">
                <div
                  className={cn(
                    "flex justify-between gap-6",
                    d.orbit ? "flex-col @xl:flex-row items-start @xl:items-center" : "flex-col",
                  )}
                >
                  <div className="space-y-3 min-w-0 flex-1 w-full">
                    <div className="flex items-center gap-2 min-w-0">
                      {hasRunning && (
                        <span className="relative flex h-2 w-2 shrink-0">
                          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75" />
                          <span className="relative inline-flex rounded-full h-2 w-2 bg-accent" />
                        </span>
                      )}
                      <p className="content-compact text-accent font-medium uppercase tracking-wider truncate">
                        {hasRunning ? t_i18n("Agents Active") : t_i18n("Welcome to your Enterprise Agentic AI Platform")}
                      </p>
                      {/* No room for the orbit: the count it carries. */}
                      {!d.orbit && stats.integrations_connected > 0 && (
                        <span className="ml-auto shrink-0 bg-accent/20 text-accent content-caption font-bold rounded-sm px-2 py-0.5 border border-accent/30">
                          {t_i18n("{count} connected", { count: formatCompactNumber(stats.integrations_connected) })}
                        </span>
                      )}
                    </div>
                    <h1 className={cn("font-bold", d.orbit ? "text-3xl @3xl:text-4xl" : "text-2xl")}>{greeting}</h1>

                    {/* Subtitle — right below the title */}
                    <p className={cn("text-default-secondary content-base", d.orbit && "@3xl:text-base", d.subtitle === "clamped" && "line-clamp-2")}>
                      {hasRunning
                        ? t_i18n("{count} {noun} running right now. Your agents are covering your ecosystem.", {
                            count: formatCompactNumber(stats.runs.running),
                            noun: t_i18n(pluralize(stats.runs.running, "assignment")),
                          })
                        : t_i18n("{agents} watching over {integrations}.", {
                            agents: `${formatCompactNumber(stats.agents.active)} ${t_i18n(pluralize(stats.agents.active, "agent"))}`,
                            integrations: `${formatCompactNumber(stats.integrations_connected)} ${t_i18n(pluralize(stats.integrations_connected, "integration"))}`,
                          })
                      }
                    </p>

                    {/* CTA button */}
                    <Button
                      size="sm"
                      className="gap-1.5 bg-accent text-negative-primary font-medium hover:bg-accent/90 shadow-[0_0_15px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)] hover:shadow-[0_0_20px_color-mix(in_srgb,_var(--color-accent)_calc(0.4_*_100%),_transparent)] transition-all duration-300"
                      onClick={() => navigate("/chat")}
                    >
                      <MessageSquare className="h-3.5 w-3.5" />
                      {t_i18n("Start chatting")}
                    </Button>

                    {/* Live activity ticker */}
                    {d.ticker && recentRuns && recentRuns.length > 0 && (
                      <div className="pt-1">
                        <LiveActivityTicker runs={recentRuns} />
                      </div>
                    )}
                  </div>

                  {/* Ecosystem orbit visualization */}
                  {d.orbit && (
                    <EcosystemOrbit
                      integrationCount={stats.integrations_connected}
                      logos={connectedLogos}
                    />
                  )}
                </div>
              </div>
            </div>
          );
        }}
      </WidgetSized>
    ),

    // ─── KPI cards ────────────────────────────────────────────────────
    kpi_agents: (
      <KpiTile
        label={t_i18n("Agents")}
        value={stats.agents.active}
        current={stats.agents.active}
        previous={stats.diffs.agents_prev_month}
        sub={<p className="text-[11px] text-default-secondary">{t_i18n("{count} active assignments", { count: formatCompactNumber(stats.assignments.active) })}</p>}
        icon={<Bot className="h-8 w-8 text-default-disabled shrink-0" />}
        onClick={() => navigate("/agents")}
      />
    ),
    kpi_conversations: (
      <KpiTile
        label={t_i18n("Conversations")}
        value={stats.conversation_count}
        current={stats.conversation_count}
        previous={stats.diffs.conversations_prev_month}
        sub={<p className="text-[11px] text-default-secondary">{t_i18n("{count, plural, one {{shown} message} other {{shown} messages}}", { count: stats.message_count, shown: formatCompactNumber(stats.message_count) })}</p>}
        icon={<MessageSquare className="h-8 w-8 text-default-disabled shrink-0" />}
        onClick={() => navigate("/chat")}
      />
    ),
    kpi_executions: (
      <KpiTile
        label={t_i18n("Agentic Executions")}
        value={stats.runs.this_month}
        current={stats.runs.this_month}
        previous={stats.diffs.runs_prev_month_count}
        sub={
          hasRunning ? (
            <div className="flex items-center gap-1 text-accent text-[11px]">
              <Spinner size="sm" tone="inherit" /> {t_i18n("{count} running now", { count: formatCompactNumber(stats.runs.running) })}
            </div>
          ) : (
            <p className="text-[11px] text-default-secondary">{t_i18n("last 30 days")}</p>
          )
        }
        icon={
          <div className="relative shrink-0">
            <BarChart3 className="h-8 w-8 text-default-disabled" />
            {hasRunning && <div className="absolute top-0 right-0 h-2 w-2 rounded-full bg-accent animate-pulse" />}
          </div>
        }
        onClick={() => navigate("/operations/executions")}
      />
    ),
    kpi_integrations: (
      <KpiTile
        label={t_i18n("Integrations")}
        value={stats.integrations_connected}
        current={stats.integrations_connected}
        previous={stats.diffs.integrations_prev_month}
        sub={
          connectedLogoEntries.length > 0 ? (
            <div className="flex items-center gap-1 -space-x-1">
              {connectedLogoEntries.slice(0, 5).map(({ provider, src }) => (
                <img
                  key={provider}
                  src={src}
                  alt={provider}
                  className="h-4 w-4 rounded-full bg-elevation-hover p-0.5 ring-1 ring-focus object-contain"
                  onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                />
              ))}
              {connectedLogoEntries.length > 5 && (
                <span className="content-caption text-default-secondary ml-2">
                  +{formatCompactNumber(connectedLogoEntries.length - 5)}
                </span>
              )}
            </div>
          ) : (
            <p className="text-[11px] text-default-secondary">{t_i18n("connected services")}</p>
          )
        }
        icon={<Link2 className="h-8 w-8 text-default-disabled shrink-0" />}
        onClick={() => navigate("/integrations")}
      />
    ),
    kpi_documents: (
      <KpiTile
        label={t_i18n("Documents")}
        value={stats.document_count}
        current={stats.document_count}
        previous={stats.diffs.documents_prev_month}
        sub={
          <p className="text-[11px] text-default-secondary">
            {formatCompactNumber(stats.knowledge_bases)} {t_i18n(pluralize(stats.knowledge_bases, "knowledge base"))}
          </p>
        }
        icon={<FileText className="h-8 w-8 text-default-disabled shrink-0" />}
        onClick={() => navigate("/knowledge")}
      />
    ),

    // ─── Agents (all, sorted alphabetically); the list scrolls inside ──
    agents: (
      <Card className="h-full overflow-hidden flex flex-col">
        <CardContent className="p-4 space-y-3 flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between">
            <h2 className="content-base font-semibold flex items-center gap-2">
              <Bot className="h-4 w-4 text-accent" /> {t_i18n("Agents")}
            </h2>
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" className="h-7 content-compact gap-1.5" onClick={() => navigate("/agents/new")}>
                <Plus className="h-3 w-3" /> {t_i18n("New")}
              </Button>
              <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/agents")}>
                <ArrowUpRight className="h-3 w-3" /> {t_i18n("All")}
              </Button>
            </div>
          </div>

          {stats.agent_summaries.length > 0 ? (
            <div className="divide-y divide-[var(--border-elevation-subtle)] stagger-children min-h-0 flex-1 overflow-y-auto">
              {[...stats.agent_summaries]
                .sort((a, b) => a.name.localeCompare(b.name))
                .slice(0, 10)
                .map((agent: AgentSummary) => (
                  <AgentCard
                    key={agent.id}
                    agent={agent}
                    userId={user?.id}
                    onClick={() => navigate(`/agents/${agent.id}`)}
                  />
                ))}
            </div>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-center">
              <div className="relative inline-flex mb-3">
                <Bot className="h-10 w-10 text-default-disabled" />
              </div>
              <p className="content-base text-default-secondary">{t_i18n("No agents yet")}</p>
              <p className="content-compact text-default-secondary mt-1 mb-3">{t_i18n("Create your first agent to start automating.")}</p>
              <Button size="sm" className="gap-1.5 bg-accent text-negative-primary hover:bg-accent/90" onClick={() => navigate("/agents/new")}>
                <Plus className="h-3.5 w-3.5" /> {t_i18n("Create Agent")}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    ),
    // ─── Last Conversations ───────────────────────────────────────────
    conversations: (
      <Card className="h-full overflow-hidden flex flex-col">
        <CardContent className="p-4 space-y-3 flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between">
            <h2 className="content-base font-semibold flex items-center gap-2">
              <MessageSquare className="h-4 w-4 text-accent" /> {t_i18n("Last Conversations")}
            </h2>
            <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/chat")}>
              <ArrowUpRight className="h-3 w-3" /> {t_i18n("All")}
            </Button>
          </div>

          {conversations && conversations.length > 0 ? (
            <div className="divide-y divide-[var(--border-elevation-subtle)] stagger-children min-h-0 flex-1 overflow-y-auto">
              {conversations.slice(0, 10).map((conv: ConversationListItem) => (
                <ConversationCard
                  key={conv.id}
                  conversation={conv}
                  onClick={() => navigate(`/chat?c=${conv.id}`)}
                />
              ))}
            </div>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-center">
              <div className="relative inline-flex mb-3">
                <MessageSquare className="h-10 w-10 text-default-disabled" />
              </div>
              <p className="content-base text-default-secondary">{t_i18n("No conversations yet")}</p>
              <p className="content-compact text-default-secondary mt-1 mb-3">{t_i18n("Start chatting with your agents.")}</p>
              <Button size="sm" className="gap-1.5 bg-accent text-negative-primary hover:bg-accent/90" onClick={() => navigate("/chat")}>
                <MessageSquare className="h-3.5 w-3.5" /> {t_i18n("Start Chatting")}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    ),
    // ─── Live Activity Feed ───────────────────────────────────────────
    live_activity: (
      <Card className="h-full overflow-hidden flex flex-col">
        <CardContent className="p-4 space-y-3 flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between">
            <h2 className="content-base font-semibold flex items-center gap-2">
              <Activity className="h-4 w-4 text-accent" /> {t_i18n("Live Activity")}
            </h2>
            <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/operations/executions")}>
              <ArrowUpRight className="h-3 w-3" /> {t_i18n("History")}
            </Button>
          </div>

          {recentRuns && recentRuns.length > 0 ? (
            <div className="divide-y divide-[var(--border-elevation-subtle)] stagger-children min-h-0 flex-1 overflow-y-auto">
              {recentRuns.slice(0, 10).map((run) => (
                <RunCard key={run.id} run={run} compact card={false} className="h-14 overflow-hidden" />
              ))}
            </div>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-center">
              <Zap className="h-8 w-8 text-default-disabled mb-2" />
              <p className="content-compact text-default-secondary">{t_i18n("No activity yet")}</p>
            </div>
          )}
        </CardContent>
      </Card>
    ),
    // ─── Latest Results & Digests: the cards scroll inside the widget ──
    digests: (
      <Card className="@container h-full overflow-hidden flex flex-col">
        <CardContent className="p-4 flex flex-col flex-1 min-h-0">
          <div className="flex items-center justify-between mb-3">
            <h2 className="content-base font-semibold flex items-center gap-2">
              <BarChart3 className="h-4 w-4 text-accent" /> {t_i18n("Latest Results & Digests")}
            </h2>
            {stats.recent_digests.length > 0 && (
              <Button variant="ghost" size="sm" className="h-7 content-compact gap-1" onClick={() => navigate("/operations/executions?filter=digests")}>
                <ArrowUpRight className="h-3 w-3" /> {t_i18n("View all")}
              </Button>
            )}
          </div>

          {stats.recent_digests.length > 0 ? (
            <div className="grid gap-4 @xl:grid-cols-2 @4xl:grid-cols-3 content-start min-h-0 flex-1 overflow-y-auto">
              {stats.recent_digests.slice(0, 9).map((digest: DigestItem) => {
                return (
                  <Card
                    key={digest.id}
                    className="cursor-pointer hover:bg-elevation-hover transition-all duration-300 group"
                    onClick={() => navigate(`/operations/executions/${digest.id}`)}
                  >
                    <CardContent className="pt-5 pb-4 space-y-3">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <AgentIconTile icon={digest.agent_icon} size="digest" />
                          <div>
                            <p className="content-compact font-semibold">{digest.assignment_name}</p>
                            <p className="content-caption text-default-secondary">{digest.agent_name}</p>
                          </div>
                        </div>
                        <span className="content-caption text-default-secondary shrink-0">{formatTimeAgo(intl, digest.created_at)}</span>
                      </div>
                      <div className="rounded-sm bg-elevation-hover p-3 group-hover:bg-elevation-hover transition-colors line-clamp-4 overflow-hidden text-[11px] text-default-secondary leading-relaxed [&_.chat-markdown]:contents [&_p]:mb-1 [&_p:last-child]:mb-0 [&_h1]:content-compact [&_h1]:font-semibold [&_h1]:mb-1 [&_h2]:content-compact [&_h2]:font-semibold [&_h2]:mb-1 [&_h3]:text-[11px] [&_h3]:font-semibold [&_h3]:mb-1 [&_ul]:mb-1 [&_ol]:mb-1 [&_li]:text-[11px] [&_code]:content-caption [&_pre]:hidden [&_table]:hidden [&_img]:hidden [&_hr]:hidden [&_blockquote]:border-l [&_blockquote]:border-accent/20 [&_blockquote]:pl-2 [&_blockquote]:my-1">
                        <ChatMarkdown content={digest.summary} />
                      </div>
                      <div className="flex items-center justify-between">
                        {digest.actions_count > 0 && (
                          <span className="content-caption text-accent">{t_i18n("{count} {noun} taken", { count: formatCompactNumber(digest.actions_count), noun: t_i18n(pluralize(digest.actions_count, "action")) })}</span>
                        )}
                        <span className="content-caption text-accent ml-auto group-hover:underline">{t_i18n("Read more")} →</span>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
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
    // The Value card (#9836 R6b, design 10.8): the Value page's report, the shared RoiFigure.
    value: <RoiValueCard />,
    // Top value drivers (#12009): the agents of the same report ranked by hours saved.
    value_drivers: <RoiValueDriversCard />,
    // Fleets (#12063): the shared fleet tile, every fleet by default, domain neutral.
    fleets: <CopilotFleetsWidget onContentHeight={setFleetsPx} />,
  };

  // Each widget's name, for its edit overlay and the catalog of removed ones.
  const widgetLabels: Record<CopilotWidgetId, string> = {
    hero: t_i18n("Overview"),
    kpi_agents: t_i18n("Agents"),
    kpi_conversations: t_i18n("Conversations"),
    kpi_executions: t_i18n("Agentic Executions"),
    kpi_integrations: t_i18n("Integrations"),
    kpi_documents: t_i18n("Documents"),
    agents: t_i18n("Agent list"),
    conversations: t_i18n("Last Conversations"),
    live_activity: t_i18n("Live Activity"),
    digests: t_i18n("Latest Results & Digests"),
    value: t_i18n("Value"),
    value_drivers: t_i18n("Top value drivers"),
    fleets: t_i18n("Fleets"),
  };

  return (
    <div className="space-y-8">
      <DashboardNotices />

      {/* The "Customize" button turns the grid below into a live editor of
          this person's dashboard. `space-y-8` spaces this page with a BOTTOM
          margin on each child, so the row's own `mb-4` replaces it: the button
          sits close above the grid. A negative one would pull the grid over
          the row, and a grid widget, positioned, paints above it. The row
          keeps the button's height (22.5px) while it is hidden in edit mode,
          so the grid does not jump on Customize and Done. */}
      <div className="flex min-h-[22.5px] justify-end mb-4">
        {!customizing && (
          <CustomizeButton
            onClick={() => setCustomizing(true)}
            tooltip={t_i18n("Build your dashboard: move, resize, remove and add widgets")}
          />
        )}
      </div>

      <DashboardGrid
        registry={COPILOT_WIDGETS}
        layout={drawnLayout}
        widgets={widgets}
        labels={widgetLabels}
        title={t_i18n("Customize your dashboard")}
        editing={customizing}
        onChange={onLayoutChange}
        onDone={() => setCustomizing(false)}
      />
    </div>
  );
}

