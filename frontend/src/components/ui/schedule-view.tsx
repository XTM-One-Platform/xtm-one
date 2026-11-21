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
 * ScheduleView - "what will happen, and when?" for a set of agents.
 *
 * KPI strip + an upcoming timeline (mini-Gantt) + an inventory, for the two
 * kinds of things that happen at a time: recurring assignment triggers
 * (`GET /assignments/schedule`) and one-off scheduled work items
 * (`GET /work-items/scheduled`), each typed as such. Next-run times come from
 * the backend; countdowns refresh on a timer.
 *
 * A view, not a page: the `Schedule` view of the agent-to-agent map
 * (`/flows/map/schedule`, the former Schedule tab) and of each fleet page
 * render it. `agentIds`
 * narrows every part to the agents shown (no value: every agent the caller
 * sees); `agentId` narrows it to one agent (the
 * link of an agent's "Coming up" card), with `onClearAgent` to widen it back.
 * Its rows are `schedule-row.tsx`: two lines of one height each.
 */

import { workItemPath } from "@/lib/task-routes";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CalendarClock,
  Clock,
  Zap,
  Activity,
  Hourglass,
  Bot,
  Play,
  Repeat,
  X,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { StatCard } from "@/components/ui/stat-card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageSkeleton } from "@/components/ui/skeleton";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { UpcomingTimeline } from "@/components/ui/upcoming-timeline";
import { OneOffRow, ScheduleRow } from "@/components/ui/schedule-row";
import {
  useAssignmentSchedule,
  useActiveAgentIds,
} from "@/hooks/api/useAssignmentApi";
import { useScheduledWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { REFETCH_INTERVAL_FAST } from "@/lib/constants";
import { occurrencesInWindow } from "@/lib/cron";
import { formatCountdown, soonestRun, viewerTimeZone } from "@/lib/scheduled-work-items";
import type { AssignmentSchedule, ScheduledWorkItem } from "@/types";

type WindowKey = "1" | "24" | "168";
const WINDOW_OPTIONS = [
  { key: "1" as WindowKey, label: "Next hour" },
  { key: "24" as WindowKey, label: "Next 24h" },
  { key: "168" as WindowKey, label: "Next 7d" },
];

function InventorySection({
  title,
  icon: Icon,
  items,
  nowMs,
  zone,
  onOpen,
}: {
  title: string;
  icon: React.ElementType;
  items: AssignmentSchedule[];
  nowMs: number;
  zone: string;
  onOpen: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 content-base font-medium">
        <Icon className="h-4 w-4 text-accent" />
        {title}
        <span className="content-compact text-default-secondary">{items.length}</span>
      </div>
      <div className="space-y-1.5">
        {items.map((s) => (
          <ScheduleRow
            key={s.assignment_id}
            s={s}
            nowMs={nowMs}
            zone={zone}
            onClick={() => onOpen(s.assignment_id)}
          />
        ))}
      </div>
    </div>
  );
}

export interface ScheduleViewProps {
  /** The agents whose schedule is shown; every visible agent when absent. */
  agentIds?: ReadonlySet<string> | null;
  /** One agent to narrow the view to (shown as a removable chip). */
  agentId?: string | null;
  /** Widens a view narrowed by `agentId` back to `agentIds`. */
  onClearAgent?: () => void;
  /**
   * A fleet whose schedule this is: the reads ask the server for that fleet's
   * home members (`?fleet=`) instead of filtering the first global page.
   */
  fleetId?: string | null;
}

export function ScheduleView({ agentIds = null, agentId = null, onClearAgent, fleetId = null }: ScheduleViewProps) {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const { data: allSchedules, isLoading } = useAssignmentSchedule(fleetId);
  const { data: upcomingOneOffs, isLoading: oneOffsLoading } = useScheduledWorkItems(undefined, "upcoming", fleetId);
  const { data: missedOneOffs } = useScheduledWorkItems(undefined, "missed", fleetId);
  const { data: activeAgentIdList } = useActiveAgentIds();
  const navigate = useNavigate();
  const agentFilter = agentId;
  const inScope = useCallback(
    (id: string | null | undefined) =>
      (!agentIds || (!!id && agentIds.has(id))) && (!agentFilter || id === agentFilter),
    [agentIds, agentFilter],
  );
  const [windowKey, setWindowKey] = useState<WindowKey>("24");
  // Tick "now" forward so countdowns and projections stay fresh between fetches.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), REFETCH_INTERVAL_FAST);
    return () => clearInterval(id);
  }, []);

  const activeAgentIds = useMemo(
    () => new Set(activeAgentIdList ?? []),
    [activeAgentIdList],
  );

  const schedules = useMemo(
    () => (allSchedules ?? []).filter((s) => inScope(s.agent_id)),
    [allSchedules, inScope],
  );
  const oneOffs = useMemo(() => {
    const seen = new Set<string>();
    return [...(upcomingOneOffs ?? []), ...(missedOneOffs ?? [])].filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return inScope(item.owner_agent_id);
    });
  }, [upcomingOneOffs, missedOneOffs, inScope]);
  const filteredAgentName =
    agentFilter
      ? schedules[0]?.agent_name ?? oneOffs[0]?.agent_name ?? null
      : null;

  const { scheduled, eventDriven, manual, nextScheduled, inFlightCount, triggers24h, waitingOneOffs } =
    useMemo(() => {
      const all = schedules;
      const sched = all.filter((s) => s.trigger_type === "schedule");
      const events = all.filter(
        (s) =>
          s.trigger_type === "webhook" || s.trigger_type === "integration_event",
      );
      const man = all.filter((s) => s.trigger_type === "manual");
      // Next trigger among scheduled assignments, considering BOTH the next cron
      // fire and any already-queued delayed run (pending_run_at) - whichever is
      // sooner - so a due-now pending run is not hidden behind a later cron tick.
      const next = sched
        .map((s) => ({ s, soon: soonestRun(s) }))
        .filter(
          (x): x is { s: AssignmentSchedule; soon: { iso: string; pending: boolean } } =>
            x.soon !== null,
        )
        .sort(
          (a, b) => new Date(a.soon.iso).getTime() - new Date(b.soon.iso).getTime(),
        )[0];
      // has_active_run covers any non-terminal run (pending / delayed /
      // running / awaiting_human), not just actively-executing ones - so this
      // is an "in flight" count, surfaced under that label below.
      const inFlight = all.filter((s) => s.has_active_run).length;
      const next24End = new Date(nowMs + 24 * 3_600_000);
      // Use the same clamp-overdue-to-now projection as the timeline so the KPI
      // counts due-now occurrences instead of dropping them (and never disagrees
      // with the timeline / row countdowns).
      const waiting = oneOffs.filter((i) => ["scheduled", "due", "running"].includes(i.scheduled.state));
      const oneOffs24 = waiting.filter(
        (i) => i.due_at && new Date(i.due_at).getTime() <= next24End.getTime(),
      ).length;
      const count24 = sched.reduce(
        (acc, s) =>
          acc + occurrencesInWindow(s.cron, s.next_run_at, nowMs, next24End).length,
        oneOffs24,
      );
      return {
        scheduled: sched,
        eventDriven: events,
        manual: man,
        nextScheduled: next,
        inFlightCount: inFlight,
        triggers24h: count24,
        waitingOneOffs: waiting,
      };
    }, [schedules, oneOffs, nowMs]);

  if (isLoading || oneOffsLoading) return <PageSkeleton cards={2} />;

  const clearAgent = () => onClearAgent?.();
  const openOneOff = (item: ScheduledWorkItem) => navigate(workItemPath(item.id));

  if (schedules.length === 0 && oneOffs.length === 0) {
    return (
      <div className="space-y-4">
        {agentFilter && onClearAgent && (
          <Button variant="ghost" size="sm" className="h-7 gap-1 content-compact" onClick={clearAgent}>
            <X className="h-3 w-3" /> {t_i18n("Show every agent")}
          </Button>
        )}
        <EmptyState
          icon={CalendarClock}
          title="Nothing is scheduled"
          description="Assignments run on a schedule or on events, and any agent can schedule a one-off action from a chat (&quot;post this tomorrow at 9:00&quot;). Both appear here, with exactly when they run."
          actionLabel="Go to Agents"
          onAction={() => navigate("/agents")}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {agentFilter && (
        <div className="flex items-center gap-2 content-base">
          <Badge variant="outline" className="gap-1.5">
            <Bot className="h-3 w-3" />
            {t_i18n("Agent: {name}", { name: filteredAgentName ?? agentFilter })}
          </Badge>
          {onClearAgent && (
            <Button variant="ghost" size="sm" className="h-7 gap-1 content-compact" onClick={clearAgent}>
              <X className="h-3 w-3" /> {t_i18n("Show every agent")}
            </Button>
          )}
        </div>
      )}

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard
          icon={Clock}
          label="Scheduled"
          value={scheduled.length}
          subtitle="cron-triggered"
        />
        <StatCard
          icon={CalendarClock}
          label="One-off"
          value={waitingOneOffs.length}
          subtitle="scheduled work items"
        />
        <StatCard
          icon={Hourglass}
          label="Next trigger"
          value={nextScheduled ? formatCountdown(nextScheduled.soon.iso, nowMs, t_i18n, { bare: true }) : "-"}
          subtitle={nextScheduled?.s.assignment_name ?? "no schedules"}
        />
        <StatCard
          icon={Activity}
          label="In flight"
          value={inFlightCount}
          subtitle="queued or running"
        />
        <StatCard
          icon={Repeat}
          label="Next 24h"
          value={triggers24h}
          subtitle="expected triggers"
        />
        <StatCard
          icon={Zap}
          label="Event-driven"
          value={eventDriven.length}
          subtitle="webhook + integration"
        />
      </div>

      {/* Upcoming timeline */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">{t_i18n("Upcoming")}</CardTitle>
          <SegmentedFilter
            options={WINDOW_OPTIONS}
            value={windowKey}
            onChange={setWindowKey}
            ariaLabel={t_i18n("Timeline window")}
          />
        </CardHeader>
        <CardContent>
          <UpcomingTimeline
            schedules={scheduled}
            oneOffs={waitingOneOffs}
            windowHours={Number(windowKey)}
            nowMs={nowMs}
            activeAgentIds={activeAgentIds}
            onSelect={(id) => navigate(`/agents/assignments/${id}`)}
            onSelectOneOff={openOneOff}
          />
        </CardContent>
      </Card>

      {/* Inventory */}
      <div className="space-y-5">
        {oneOffs.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 content-base font-medium">
              <CalendarClock className="h-4 w-4 text-feedback-warning-primary" />
              {t_i18n("One-off")}
              <span className="content-compact text-default-secondary">{oneOffs.length}</span>
            </div>
            <div className="space-y-1.5">
              {oneOffs.map((item) => (
                <OneOffRow
                  key={item.id}
                  item={item}
                  nowMs={nowMs}
                  zone={zone}
                  onOpen={() => openOneOff(item)}
                />
              ))}
            </div>
          </div>
        )}
        <InventorySection
          title={t_i18n("Recurring")}
          icon={Repeat}
          items={scheduled}
          nowMs={nowMs}
          zone={zone}
          onOpen={(id) => navigate(`/agents/assignments/${id}`)}
        />
        <InventorySection
          title={t_i18n("Event-driven")}
          icon={Zap}
          items={eventDriven}
          nowMs={nowMs}
          zone={zone}
          onOpen={(id) => navigate(`/agents/assignments/${id}`)}
        />
        <InventorySection
          title={t_i18n("Manual")}
          icon={Play}
          items={manual}
          nowMs={nowMs}
          zone={zone}
          onOpen={(id) => navigate(`/agents/assignments/${id}`)}
        />
      </div>
    </div>
  );
}
