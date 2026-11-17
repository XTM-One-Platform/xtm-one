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
 * UpcomingTimeline - a mini-Gantt of what will happen, and when.
 *
 * One lane per scheduled assignment (recurring: glowing markers at each
 * projected fire time within the selected window - the first is the
 * authoritative backend-computed `next_run_at`, later ones are projected from
 * the cron, visual only) and one lane per scheduled work item (one-off:
 * a single diamond at its time; a due one sits on the now-line). The two kinds
 * are told apart by their marker and their tag. Theme-aware (uses tokens, no
 * hardcoded surface colors).
 */

import { useMemo } from "react";
import { Bot } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { ICON_MAP } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { humanizeCron } from "@/lib/cron";
import { buildTimelineLanes, type TimelineLane } from "@/lib/scheduled-work-items";
import { useFormatter } from "@/i18n";
import type { AssignmentSchedule, ScheduledWorkItem } from "@/types";

interface UpcomingTimelineProps {
  /** Schedule-type assignments only (with cron + next_run_at). */
  schedules: AssignmentSchedule[];
  /** Scheduled work items: one-off actions. */
  oneOffs?: ScheduledWorkItem[];
  /** Window length in hours (e.g. 1, 24, 168). */
  windowHours: number;
  /** Re-render seed so projections refresh as time passes (e.g. from useTick). */
  nowMs: number;
  activeAgentIds?: Set<string>;
  onSelect?: (assignmentId: string) => void;
  onSelectOneOff?: (item: ScheduledWorkItem) => void;
}

/**
 * Time options in the viewer's language: a 24 h clock outside English,
 * as the schedule rows read.
 */
function clockOptions(locale: string): Intl.DateTimeFormatOptions {
  const lang = (locale || "en").split("-")[0].toLowerCase();
  return lang === "en" ? {} : { hourCycle: "h23" };
}

function buildGridlines(windowHours: number, now: Date, locale: string): { pct: number; label: string }[] {
  const lines: { pct: number; label: string }[] = [];
  const windowMs = windowHours * 3_600_000;
  if (windowHours <= 1) {
    // Every 15 minutes.
    for (let m = 15; m <= windowHours * 60; m += 15) {
      lines.push({ pct: (m / (windowHours * 60)) * 100, label: `+${m}m` });
    }
  } else if (windowHours <= 24) {
    const step = windowHours <= 6 ? 1 : 3;
    for (let h = step; h <= windowHours; h += step) {
      const at = new Date(now.getTime() + h * 3_600_000);
      lines.push({
        pct: ((h * 3_600_000) / windowMs) * 100,
        label: at.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", ...clockOptions(locale) }),
      });
    }
  } else {
    const days = Math.round(windowHours / 24);
    for (let d = 1; d <= days; d += 1) {
      const at = new Date(now.getTime() + d * 86_400_000);
      lines.push({
        pct: ((d * 86_400_000) / windowMs) * 100,
        label: at.toLocaleDateString(locale, { weekday: "short" }),
      });
    }
  }
  return lines;
}

function laneDescription(lane: TimelineLane, t_i18n: (key: string) => string, locale: string): string {
  if (lane.kind === "one-off") return t_i18n("One-off");
  return lane.cron ? humanizeCron(lane.cron, locale) : t_i18n("Scheduled");
}

export function UpcomingTimeline({
  schedules,
  oneOffs = [],
  windowHours,
  nowMs,
  activeAgentIds,
  onSelect,
  onSelectOneOff,
}: UpcomingTimelineProps) {
  const { t_i18n, locale } = useFormatter();
  const { lanes, gridlines, now } = useMemo(() => {
    const nowDate = new Date(nowMs);
    return {
      lanes: buildTimelineLanes(schedules, oneOffs, windowHours, nowMs),
      gridlines: buildGridlines(windowHours, nowDate, locale),
      now: nowDate,
    };
  }, [schedules, oneOffs, windowHours, nowMs, locale]);

  const windowMs = windowHours * 3_600_000;

  if (lanes.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center rounded-sm border border-dashed border-elevation-subtle content-base text-default-secondary">
        {t_i18n("Nothing is scheduled in this window.")}
      </div>
    );
  }

  return (
    <div className="space-y-1">
      {/* Axis header */}
      <div className="flex items-stretch content-caption text-default-secondary">
        <div className="w-44 shrink-0 pr-2 text-right uppercase tracking-wider" data-testid="timeline-now">
          {t_i18n("now")}
        </div>
        <div className="relative h-4 flex-1">
          {gridlines.map((g, i) => (
            <span
              key={i}
              className="absolute -translate-x-1/2 whitespace-nowrap"
              style={{ left: `${g.pct}%` }}
            >
              {g.label}
            </span>
          ))}
        </div>
      </div>

      {/* Lanes - one tooltip per lane (markers are cheap spans). */}
      <div className="space-y-px">
        {lanes.map((lane) => {
          const Icon = (lane.agentIcon && ICON_MAP[lane.agentIcon]) || Bot;
          const isActive = lane.agentId ? activeAgentIds?.has(lane.agentId) : false;
          const oneOff = lane.kind === "one-off";
          const nextAt = lane.occurrences[0];
          return (
            <Tip
              key={lane.key}
              side="top"
              content={
                <div className="content-compact">
                  <div className="font-medium">{lane.label}</div>
                  <div className="text-default-secondary">
                    {laneDescription(lane, t_i18n, locale)}
                    {" - "}
                    {nextAt.toLocaleString(locale, {
                      weekday: "short",
                      hour: "2-digit",
                      minute: "2-digit",
                      ...clockOptions(locale),
                    })}
                  </div>
                </div>
              }
            >
              <button
                type="button"
                data-lane-kind={lane.kind}
                onClick={() =>
                  lane.kind === "one-off"
                    ? onSelectOneOff?.(lane.item)
                    : onSelect?.(lane.schedule.assignment_id)
                }
                className="group flex w-full items-center rounded-sm py-1 text-left transition-colors hover:bg-elevation-hover"
              >
                <div className="flex w-44 shrink-0 items-center gap-1.5 pr-2">
                  <Icon
                    className={cn(
                      "h-3.5 w-3.5 shrink-0",
                      isActive ? "text-accent" : "text-default-secondary",
                    )}
                  />
                  <span className="truncate content-compact">{lane.label}</span>
                </div>
                <div className="relative h-6 flex-1 rounded-sm bg-elevation-hover">
                  {/* Gridlines */}
                  {gridlines.map((g, i) => (
                    <span
                      key={i}
                      className="absolute top-0 h-full w-px bg-elevation-hover"
                      style={{ left: `${g.pct}%` }}
                    />
                  ))}
                  {/* Occurrence markers */}
                  {lane.occurrences.map((d, i) => {
                    const pct = Math.max(
                      0,
                      Math.min(100, ((d.getTime() - now.getTime()) / windowMs) * 100),
                    );
                    const isFirst = i === 0;
                    return (
                      <span
                        key={i}
                        className={cn(
                          "absolute top-1/2 -translate-x-1/2 -translate-y-1/2",
                          oneOff
                            ? "h-2.5 w-2.5 rotate-45 rounded-[2px] bg-feedback-alert-primary ring-2 ring-feedback-alert-primary/30"
                            : isFirst
                              ? "h-2.5 w-2.5 rounded-full bg-accent shadow-[0_0_8px_color-mix(in_srgb,_var(--color-accent)_calc(0.7_*_100%),_transparent)]"
                              : "h-1.5 w-1.5 rounded-full bg-accent/50",
                        )}
                        style={{ left: `${pct}%` }}
                      />
                    );
                  })}
                </div>
              </button>
            </Tip>
          );
        })}
      </div>
    </div>
  );
}
