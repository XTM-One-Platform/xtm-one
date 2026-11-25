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
 * The fleet About page (#10123): the first thing a person sees in a fleet, as
 * a dashboard, never a document. Status and live figures first (hero, KPI
 * row), then the mini graph next to "How it works" (50/50 from 1280 px, the
 * graph expands to full width in place), the member cards, and what the fleet
 * creates next to what it needs. Every figure is read live from the platform
 * (health, assignments, ROI), never written by the AI; each tile links to the
 * tab that holds the detail, so nothing here repeats another tab's table.
 * Reading the page starts no run, calls no model and uses no quota.
 */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Crown,
  Gauge,
  Inbox,
  Maximize2,
  Minimize2,
  Settings2,
  ShieldCheck,
  XCircle,
  Zap,
} from "lucide-react";

import { RoiFigure } from "@/components/roi/RoiFigure";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompactFigure } from "@/components/ui/compact-figure";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Sparkline } from "@/components/ui/sparkline";
import { StatCard } from "@/components/ui/stat-card";
import { Tip } from "@/components/ui/tooltip";
import { useAssignments } from "@/hooks/api/useAssignmentApi";
import { useFleetApprovalGates, type FleetDocumentation, type FleetHealth, type FleetHealthStarts } from "@/hooks/api/useFleetApi";
import { useFleetRoiReport } from "@/hooks/api/useRoiReportApi";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { useFormatter } from "@/i18n";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { moduleDisplayName } from "@/lib/fleet-card-facts";
import type { FleetView } from "@/lib/fleet-operations";
import { fleetOptIn } from "@/lib/fleet-activity";
import { scopeHeroFigures } from "@/lib/roi-outcomes";
import { valueIsEmpty } from "@/lib/roi-value";
import { exactCount } from "@/lib/number-format";
import { cn, formatCompactNumber, timeAgo } from "@/lib/utils";

import {
  aboutStatus,
  checklistGroups,
  clampChips,
  INTEGRATION_CARD_PROVIDERS_MAX,
  lastRunAt,
  memberCards,
  moduleLabel,
  quotaEstimate,
  quotaRange,
  shownAutomationCount,
  setupPrerequisites,
  successRate,
  triggerKinds,
} from "./fleet-about-showcase";
import { autonomyChipLabel, effectiveAutonomyLevel } from "./fleet-autonomy";
import { FleetAboutPrerequisites, FleetPrerequisitesChip } from "./FleetAboutPrerequisites";
import { FleetAboutSteps } from "./FleetAboutSteps";
import { FleetAboutSuggestions } from "./FleetAboutSuggestions";
import { FleetHealthSettingsLink, FleetModuleLogos, useFleetStartsLine } from "./FleetHealthStarts";
import { FleetModuleMark as ModuleMark } from "./FleetModuleMark";
import { FleetOptInBadge } from "./FleetOptInBadge";
import { FleetTriggerList } from "./FleetTriggerList";

/** The member status chip's variant, as on the Members tab. */
const MEMBER_STATUS_VARIANT = {
  failing: "destructive",
  stale: "warning",
  disabled: "secondary",
  idle: "outline",
  ok: "success",
} as const;

/** The member status chip's label. @i18n-keys */
const MEMBER_STATUS_LABEL = {
  failing: "Failing",
  stale: "Stale items",
  disabled: "Paused",
  idle: "Idle",
  ok: "On",
} as const;

function Section({ id, title, action, children, className }: { id: string; title: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    // A flex column: in a row of two sections, each card fills the row's height (UX r1 B3).
    <section id={id} aria-labelledby={`${id}-title`} className={cn("flex min-w-0 flex-col gap-3", className)} data-testid={id}>
      <div className="flex min-h-8 items-center justify-between gap-2">
        <h2 id={`${id}-title`} className="content-base font-semibold">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/** A tile's foot link to the tab that holds the detail: one way, on its own line. */
function TabLink({ label, onClick, testId }: { label: string; onClick?: () => void; testId: string }) {
  if (!onClick) return null;
  return (
    <button type="button" onClick={onClick} className="inline-flex items-center gap-1 content-caption text-accent underline-offset-2 hover:underline" data-testid={testId}>
      {label}
      <ArrowRight className="h-3 w-3" aria-hidden />
    </button>
  );
}

/**
 * The fleet's flow diagram as the About view draws it: the page's own graph,
 * a fixed mini drawing (#9757), in a card, and the one way to the Graph tab in
 * its header. Shared by the showcase and the undocumented fleet's About (the
 * flow needs no documentation). `action` adds the Expand toggle; `className`
 * places the section in its row.
 */
export function FleetAboutFlow({ flow, onOpenGraph, action, className }: { flow: ReactNode; onOpenGraph?: () => void; action?: ReactNode; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Section
      id="about-diagram"
      className={className}
      title={t_i18n("Flow")}
      action={
        <div className="flex items-center gap-1">
          {action}
          {onOpenGraph && (
            <Button type="button" variant="ghost" size="sm" className="h-8 gap-1.5" onClick={onOpenGraph} data-testid="about-open-full-graph">
              {t_i18n("Open the full graph")}
              <ArrowRight className="h-4 w-4" aria-hidden />
            </Button>
          )}
        </div>
      }
    >
      {/* A flex column: the graph fills the card, no dead band under the drawing. */}
      <Card className="relative flex flex-1 flex-col overflow-hidden" data-testid="about-flow-diagram">
        <CardContent className="flex min-h-0 flex-1 flex-col justify-center p-0">{flow}</CardContent>
      </Card>
    </Section>
  );
}

/**
 * The Platform Health fleet's starts line, Settings link and Modules row on its About page (#2194), the same
 * component and keys as its list card: it starts from Settings schedules and signals, never its manual assignments (#974).
 */
function AboutHealthStarts({ healthStarts }: { healthStarts: FleetHealthStarts }) {
  const { t_i18n } = useFormatter();
  const startsLine = useFleetStartsLine({ events: healthStarts.events, schedules: healthStarts.schedules }, healthStarts);
  const names = healthStarts.modules.map((m) => moduleDisplayName(m));
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 content-compact text-default-secondary" data-testid="about-health-starts">
      <p className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5" data-testid="about-health-starts-line">
        <Zap className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="min-w-0">{startsLine}</span>
        <FleetHealthSettingsLink healthStarts={healthStarts} testId="about-health-settings" />
      </p>
      {/* Copilot mode: Platform Health covers no module, so the row is left out. */}
      {healthStarts.modules.length > 0 && (
        <div className="flex min-w-0 items-center gap-2" data-testid="about-health-modules">
          <span className="shrink-0">{t_i18n("Modules")}</span>
          <div className="flex h-6 min-w-0 items-center gap-2" role="group" aria-label={t_i18n("Modules: {names}", { names: names.join(", ") })}>
            <FleetModuleLogos modules={healthStarts.modules} testId="about-health-modules" />
          </div>
        </div>
      )}
    </div>
  );
}

export function FleetAboutShowcase({
  fleetId,
  doc,
  health,
  healthStarts,
  orchestratorId,
  onManageAutomations,
  onOpenView,
  flow,
  onOpenGraph,
  readOnly = false,
}: {
  /** A fleet nobody may write (Platform Health, `read_only_reason`): no Set up, no Fix with AI, the lock stated instead (UX r1 B3 on #464). */
  readOnly?: boolean;
  /** The fleet's id: the Fix with AI request names it (#10258). */
  fleetId: string;
  doc: FleetDocumentation;
  health: FleetHealth | null | undefined;
  /** The Platform Health fleet only (`Fleet.health_starts`, #974): its starts line, Settings link and modules (#2194). */
  healthStarts?: FleetHealthStarts | null;
  /** The fleet's orchestrator: its card comes first and is highlighted. */
  orchestratorId?: string | null;
  /** Opens the members view, where each member's automations are switched on. Starts no run. */
  onManageAutomations: () => void;
  /** Opens another tab of the fleet: a tile's link to its detail. */
  onOpenView?: (view: FleetView) => void;
  /** The fleet's flow diagram (the page's own `AgentFlowGraph`), never a second graph component. */
  flow?: ReactNode;
  /** Opens the Graph tab: the Flow section's header link. */
  onOpenGraph?: () => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const { data: assignments } = useAssignments();
  // No ROI read on Community Edition (#9836 R6c): the Value tile shows its empty state.
  const roiLocked = useEnterpriseLockedReason();
  const roi = useFleetRoiReport(fleetId, "30d", { enabled: !roiLocked });
  // The instructions position (#10168) names a fleet that gates nothing (#10439).
  const gates = useFleetApprovalGates(fleetId);
  const [expanded, setExpanded] = useState(false);
  const [metShown, setMetShown] = useState(false);
  const [stepsExpanded, setStepsExpanded] = useState(false);
  const s = doc.structured ?? {};
  // One count source with the header pill and the CTEM loop setup (UX r1 B2 on #1092): the health's automations
  // (Platform Health counts itself there, #974); the members' assignments only while the health has no count.
  const automations = shownAutomationCount(health, assignments);
  // One status source with the header pill (UX r1 B2): the health's automations, as `FleetHeaderStatus` reads them.
  const status = fleetOptIn(health?.automations) ?? aboutStatus(health, automations);
  const last = lastRunAt(health);
  const lastMember = health?.members.find((m) => m.last_run_at === last)?.name;
  const stats = health?.run_stats;
  const rate = successRate(stats);
  const estimate = quotaEstimate(quotaRange(s));
  const openItems = health?.workspace_open_work_items ?? 0;
  const waiting = stats?.awaiting_approval ?? 0;
  const cards = memberCards(health, s, assignments, orchestratorId);
  const kinds = triggerKinds(s);
  const states = doc.prerequisite_states ?? [];
  const checklist = checklistGroups(states);
  // The checklist derived from the configuration (#10258) wins over the documented one.
  const live = doc.live_prerequisites ?? [];
  const setupPrereq = setupPrerequisites(live, states);
  const memberName = (agentId: string) => health?.members.find((m) => m.agent_id === agentId)?.name ?? "";
  const agentIdOf = (name: string) => health?.members.find((m) => m.name.trim().toLowerCase() === name.trim().toLowerCase())?.agent_id;
  const level = effectiveAutonomyLevel(doc.autonomy_level);
  const go = (view: FleetView) => (onOpenView ? () => onOpenView(view) : undefined);
  const labelOf = (module: string | undefined) => {
    const label = moduleLabel(module, INTEGRATION_PROVIDER_NAMES);
    return label.translate ? t_i18n(label.text) : label.text;
  };
  // Compact figures on the tile (#10476), the exact estimate on hover.
  const quotaText = (figure: (value: number) => string) =>
    !estimate
      ? t_i18n("None yet")
      : estimate.kind === "about"
        ? t_i18n("About {count}", { count: figure(estimate.value) })
        : estimate.kind === "named"
          ? estimate.state === "manual"
            ? t_i18n("Started by hand only")
            : t_i18n("Event-driven")
          : estimate.kind === "floor"
            ? t_i18n("{count} or more", { count: figure(estimate.value) })
            : t_i18n("{min} to {max}", { min: figure(estimate.min), max: figure(estimate.max) });
  const quotaShown = quotaText(formatCompactNumber);
  const quotaExact = quotaText((value) => exactCount(locale, value));
  const runsShown = stats ? formatCompactNumber(stats.runs) : "";

  return (
    <div className="space-y-6" data-testid="fleet-about-showcase">
      <Card data-testid="about-hero">
        <CardContent className="space-y-3 p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              {status && <FleetOptInBadge optIn={status} />}
              <FleetPrerequisitesChip fleetId={fleetId} lines={live} readOnly={readOnly} />
              <Badge variant="outline" className="gap-1" data-testid="about-autonomy">
                <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
                {autonomyChipLabel(level, doc.autonomy_catalog, gates.data?.instructions_position, t_i18n)}
              </Badge>
              {/* What the AI found in the configuration, as actions, for the people who manage the fleet (#10246). */}
              <FleetAboutSuggestions
                fleetId={fleetId}
                findings={doc.findings ?? []}
                dismissed={doc.dismissed_findings ?? []}
                agentIdOf={agentIdOf}
                onOpenMembers={onManageAutomations}
              />
            </div>
            {!readOnly && (
              <Button type="button" size="sm" className="h-8 gap-1.5" onClick={onManageAutomations} data-testid="about-primary-action">
                <Settings2 className="h-4 w-4" aria-hidden />
                {status === "not_set_up" ? t_i18n("Set up") : t_i18n("Manage automations")}
              </Button>
            )}
          </div>
          {s.purpose_line && <p className="content-base text-default-primary" data-testid="about-purpose-line">{s.purpose_line}</p>}
          {(s.serves?.length ?? 0) > 0 && (
            <ul className="flex flex-wrap items-center gap-1.5" aria-label={t_i18n("Who it serves")} data-testid="about-serves">
              {(s.serves ?? []).map((who) => (
                <li key={who}>
                  <Badge variant="secondary">{who}</Badge>
                </li>
              ))}
            </ul>
          )}
          {healthStarts && <AboutHealthStarts healthStarts={healthStarts} />}
        </CardContent>
      </Card>

      {status === "not_set_up" ? (
        // A fleet not set up has no figure to show: one compact setup row, never six tiles of zeros (UX r1 B1, mockup R2 B3).
        <Card data-testid="about-setup-row">
          <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-3 p-4">
            <p className="flex min-w-0 flex-1 items-center gap-2 content-compact font-medium">
              <CircleDashed className="h-5 w-5 shrink-0 text-default-secondary" aria-hidden />
              {readOnly
                ? t_i18n("Platform Health is built in and read-only.")
                : t_i18n("Not set up yet: nothing runs until an automation is switched on")}
            </p>
            <dl className="flex flex-wrap items-center gap-x-8 gap-y-2 content-compact">
              <div data-testid="about-setup-automations">
                <dt className="content-caption text-default-secondary">{t_i18n("Automations")}</dt>
                <dd className="font-semibold">
                  {t_i18n("{count, plural, one {# to switch on} other {# to switch on}}", { count: automations?.total ?? 0 })}
                </dd>
              </div>
              <div data-testid="about-setup-quota">
                <dt className="content-caption text-default-secondary">{t_i18n("Quota estimate")}</dt>
                <dd className="font-semibold">
                  <CompactFigure exact={quotaExact}>{quotaShown}</CompactFigure>
                </dd>
              </div>
              {setupPrereq && (
                <div data-testid="about-setup-prerequisites">
                  <dt className="content-caption text-default-secondary">{t_i18n("Prerequisites")}</dt>
                  <dd className="font-semibold">
                    {t_i18n("{met} of {total} met", { met: setupPrereq.met, total: setupPrereq.total })}
                  </dd>
                </div>
              )}
            </dl>
            <TabLink label={t_i18n("Schedule")} onClick={go("schedule")} testId="about-setup-link-schedule" />
          </CardContent>
        </Card>
      ) : (
      <div className="grid auto-rows-fr gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6" data-testid="about-kpis">
        <div className="flex flex-col gap-1">
          <StatCard
            className="flex-1"
            icon={CheckCircle2}
            label={t_i18n("Automations on")}
            value={automations && automations.total > 0 ? t_i18n("{on} of {total}", { on: automations.on, total: automations.total }) : t_i18n("None yet")}
            subtitle={automations && automations.total > 0 ? t_i18n("Switched-off automations start no run") : t_i18n("No automation yet: set one up to start")}
          />
          <TabLink label={t_i18n("Schedule")} onClick={go("schedule")} testId="about-kpi-link-schedule" />
        </div>
        <div className="flex flex-col gap-1">
          <StatCard
            className="flex-1"
            icon={Activity}
            label={t_i18n("Runs, last 30 days")}
            value={stats ? stats.runs : "-"}
            subtitle={
              <span className="block space-y-1">
                <span className="block">{rate === null ? t_i18n("No finished run yet") : t_i18n("{rate}% succeeded", { rate })}</span>
                {stats && stats.runs > 0 && <Sparkline values={stats.by_day} className="h-6 text-accent" />}
              </span>
            }
          />
          <TabLink label={t_i18n("Operations")} onClick={go("operations")} testId="about-kpi-link-operations" />
        </div>
        <div className="flex flex-col gap-1">
          <StatCard
            className="flex-1"
            icon={CalendarClock}
            label={t_i18n("Last run")}
            value={last ? timeAgo(last, locale) : t_i18n("Never run")}
            subtitle={last && lastMember ? lastMember : t_i18n("Nothing ran yet: switch on an automation to start")}
          />
          <TabLink label={t_i18n("Operations")} onClick={go("operations")} testId="about-kpi-link-last-run" />
        </div>
        <div className="flex flex-col gap-1">
          <StatCard
            className="flex-1"
            icon={Inbox}
            label={t_i18n("Open work items")}
            value={openItems}
            subtitle={t_i18n("{count, plural, one {# waiting for approval} other {# waiting for approval}}", { count: waiting })}
          />
          <TabLink label={t_i18n("Operations")} onClick={go("operations")} testId="about-kpi-link-items" />
        </div>
        <div className="flex flex-col gap-1" data-testid="about-kpi-value">
          {roi.data ? (
            <RoiFigure
              // The ROI tab's hero (the ledger's totals, the viewer's own share under k), so the caption holds (#12077 L6b3 UX r2 B7).
              figures={scopeHeroFigures(roi.data)}
              metric={roi.data.rate_label === "assumption" ? "hours" : "value"}
              currency={roi.data.currency}
              window="30d"
              costMasked={roi.data.cost_masked}
              layout="card"
              caption={
                // A zero says why (UX r1 B5): no unit was counted in the window.
                valueIsEmpty(roi.data)
                  ? t_i18n("No unit counted in the last 30 days: the value starts with the first finished run")
                  : t_i18n("Last 30 days, same figure as the ROI tab")
              }
            />
          ) : (
            <StatCard className="flex-1" icon={Gauge} label={t_i18n("Value, last 30 days")} value="-" subtitle={t_i18n("Not measured: no run yet")} />
          )}
          <TabLink label={t_i18n("ROI")} onClick={go("roi")} testId="about-kpi-link-roi" />
        </div>
        <div className="flex flex-col gap-1">
          <StatCard
            className="flex-1"
            icon={Gauge}
            label={t_i18n("Quota estimate")}
            value={quotaShown}
            exact={quotaExact}
            subtitle={
              stats && estimate
                ? t_i18n("{count, plural, one {{shown} run in the last 30 days} other {{shown} runs in the last 30 days}}", { count: stats.runs, shown: runsShown })
                : t_i18n("Runs a month, an estimate, never a commitment")
            }
          />
          <TabLink label={t_i18n("ROI")} onClick={go("roi")} testId="about-kpi-link-quota" />
        </div>
      </div>
      )}

      <div className={cn("grid gap-6", !expanded && "xl:grid-cols-2")} data-testid="about-graph-row" data-expanded={expanded || undefined}>
        {flow && (
          <FleetAboutFlow
            flow={flow}
            onOpenGraph={onOpenGraph}
            // Folded, both cards share the row's height; unfolded, the diagram keeps its own height and stays in view (#10333 UX r2 B1).
            className={stepsExpanded ? "xl:sticky xl:top-[calc(var(--fds-header-height,68px)+1rem)] xl:self-start" : undefined}
            action={
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="hidden h-8 gap-1.5 xl:inline-flex"
                aria-pressed={expanded}
                onClick={() => setExpanded((v) => !v)}
                data-testid="about-graph-expand"
              >
                {expanded ? <Minimize2 className="h-4 w-4" aria-hidden /> : <Maximize2 className="h-4 w-4" aria-hidden />}
                {expanded ? t_i18n("Collapse") : t_i18n("Expand")}
              </Button>
            }
          />
        )}
        {((s.steps?.length ?? 0) > 0 || kinds.length > 0 || (s.triggers?.length ?? 0) > 0) && (
          <Section id="about-flow" title={t_i18n("How it works")}>
            <Card className="flex-1">
              <CardContent className="flex flex-col gap-3 p-4">
                {/* What starts it (#1319): every documented trigger, the same list as the setup wizard's first step. */}
                {(s.triggers?.length ?? 0) > 0 && (
                  <div className="space-y-1.5 content-compact" data-testid="about-triggers">
                    <h3 className="content-caption font-semibold text-default-secondary">{t_i18n("What starts it")}</h3>
                    <FleetTriggerList triggers={s.triggers} testId="about-trigger" />
                  </div>
                )}
                <FleetAboutSteps structured={s} onExpandedChange={setStepsExpanded} />
              </CardContent>
            </Card>
          </Section>
        )}
      </div>

      {cards.length > 0 && (
        <Section
          id="about-members"
          title={t_i18n("Members")}
          action={<TabLink label={t_i18n("Members tab")} onClick={go("members")} testId="about-members-link" />}
        >
          <ul className="grid auto-rows-fr gap-3 md:grid-cols-2 2xl:grid-cols-4" data-testid="about-member-cards">
            {cards.map((m) => {
              const providers = clampChips(m.providers, INTEGRATION_CARD_PROVIDERS_MAX);
              return (
                <li key={m.agentId}>
                  <Card className={cn("relative h-full transition-colors hover:bg-elevation-hover", m.orchestrator && "border-accent")} data-testid="about-member-card">
                    <CardContent className="flex h-full flex-col gap-2 p-4">
                      <div className="flex items-center gap-2">
                        {m.orchestrator && <Crown className="h-4 w-4 shrink-0 text-accent" aria-label={t_i18n("Orchestrator")} />}
                        {/* The stretched link makes the whole card open the agent, with no nested interactive element. */}
                        <Link to={`/agents/${m.agentId}`} className="min-w-0 flex-1 truncate content-compact font-semibold after:absolute after:inset-0" title={m.name}>
                          {m.name}
                        </Link>
                        <Badge variant={MEMBER_STATUS_VARIANT[m.status]} className="shrink-0">
                          {m.status === "stale"
                            ? t_i18n("{count, plural, one {# stale item} other {# stale items}}", { count: m.staleItems })
                            : t_i18n(MEMBER_STATUS_LABEL[m.status])}
                        </Badge>
                      </div>
                      {m.roleLine && <p className="truncate content-compact text-default-secondary" title={m.roleLine}>{m.roleLine}</p>}
                      {m.providers.length > 0 && (
                        <ul className="flex items-center gap-1.5" aria-label={t_i18n("Integrations")}>
                          {providers.shown.map((p) => (
                            <li key={p}>
                              <Tip content={labelOf(p)}>
                                <span className="inline-flex">
                                  <ProviderLogo provider={p} className="h-5 w-5" />
                                </span>
                              </Tip>
                            </li>
                          ))}
                          {providers.rest > 0 && <li className="content-caption text-default-secondary">{t_i18n("+{count} more", { count: providers.rest })}</li>}
                        </ul>
                      )}
                      <p className="mt-auto content-caption text-default-secondary">
                        {t_i18n("{on} of {total} on", { on: m.automationsOn, total: m.automationsTotal })}
                        {" · "}
                        {m.lastRunAt ? timeAgo(m.lastRunAt, locale) : t_i18n("Never run")}
                      </p>
                    </CardContent>
                  </Card>
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      {((s.objects?.length ?? 0) > 0 || live.length > 0 || states.length > 0) && (
        <div className="grid items-start gap-6 xl:grid-cols-2" data-testid="about-bottom-row">
          {(s.objects?.length ?? 0) > 0 && (
            <Section id="about-objects" title={t_i18n("Objects it creates")}>
              <Card>
                <CardContent className="space-y-3 p-4">
                  {(s.objects ?? []).map((o, i) => (
                    <div key={i} className="flex flex-wrap items-center gap-2">
                      <ModuleMark module={o.module} className="h-4 w-4 shrink-0" />
                      <span className="content-compact font-semibold">{labelOf(o.module)}</span>
                      {o.objects.map((name) => (
                        <Badge key={name} variant="secondary">{name}</Badge>
                      ))}
                    </div>
                  ))}
                </CardContent>
              </Card>
            </Section>
          )}
          {(live.length > 0 || states.length > 0) && (
            <Section id="about-prerequisites" title={t_i18n("Prerequisites")}>
              {live.length > 0 ? (
                // The checklist derived from the configuration (#10258) wins over the documented one.
                <FleetAboutPrerequisites fleetId={fleetId} lines={live} memberName={memberName} readOnly={readOnly} />
              ) : (
              <Card>
                <CardContent className="p-0">
                  <ul className="divide-y divide-[var(--border-elevation-subtle-soft)]" data-testid="about-checklist">
                    {[...checklist.open, ...(metShown ? checklist.met : [])].map((p, i) => (
                      <li key={i} className="flex min-h-11 items-center gap-3 px-4 py-1 content-compact">
                        {p.met === true ? (
                          <CheckCircle2 className="h-5 w-5 shrink-0 text-feedback-success-primary" aria-hidden />
                        ) : p.met === false ? (
                          <XCircle className="h-5 w-5 shrink-0 text-feedback-warning-tertiary" aria-hidden />
                        ) : (
                          <CircleDashed className="h-5 w-5 shrink-0 text-default-secondary" aria-hidden />
                        )}
                        <span className="min-w-0 flex-1">
                          {p.label}
                          <span className="sr-only"> {p.met === true ? t_i18n("Met") : p.met === false ? t_i18n("Not met") : t_i18n("Check by hand")}</span>
                        </span>
                        {p.met === false && p.fix_route && (
                          <Button asChild variant="outline" size="sm" className="shrink-0">
                            <Link to={p.fix_route}>{t_i18n("Set up")}</Link>
                          </Button>
                        )}
                      </li>
                    ))}
                    {checklist.met.length > 0 && (
                      <li className="px-2 py-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-8 gap-1.5"
                          aria-expanded={metShown}
                          onClick={() => setMetShown((v) => !v)}
                          data-testid="about-checklist-met"
                        >
                          <CheckCircle2 className="h-4 w-4 text-feedback-success-primary" aria-hidden />
                          {t_i18n("{count, plural, one {# met} other {# met}}", { count: checklist.met.length })}
                          <ChevronDown className={cn("h-4 w-4 transition-transform", metShown && "rotate-180")} aria-hidden />
                        </Button>
                      </li>
                    )}
                  </ul>
                </CardContent>
              </Card>
              )}
            </Section>
          )}
        </div>
      )}
    </div>
  );
}
