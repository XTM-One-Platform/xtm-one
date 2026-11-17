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
 * Scheduled work items: the pure helpers every surface shares - the
 * chat card, Operations > Work Items, Agentic Flow > Schedule and the agent's
 * "Coming up" card.
 *
 * Times are shown in the viewer's time zone (their profile's, else the
 * browser's) and in UTC, and a time picked in a form is sent WITH its UTC
 * offset: the backend refuses one without.
 */
import { occurrencesInWindow } from "@/lib/cron";
import type {
  AssignmentSchedule,
  ScheduledWorkItem,
  WorkItem,
  WorkItemScheduleState,
  WorkItemScheduled,
} from "@/types";

/** Read by `t_i18n(SCHEDULE_STATE_LABELS[state])`. @i18n-keys */
export const SCHEDULE_STATE_LABELS = {
  scheduled: "Scheduled",
  due: "Due",
  running: "Running",
  done: "Done",
  blocked: "Blocked",
  missed: "Missed",
  cancelled: "Cancelled",
} as const;

/**
 * The Badge status variant of each state: the design system's alert surfaces
 * for an outcome (success, warning, error) and its neutral fills otherwise,
 * never a tint of the accent or of a feedback hue picked here.
 */
export const SCHEDULE_STATE_VARIANT: Record<
  WorkItemScheduleState,
  "default" | "secondary" | "success" | "warning" | "destructive"
> = {
  scheduled: "default",
  due: "warning",
  running: "default",
  done: "success",
  blocked: "destructive",
  missed: "warning",
  cancelled: "secondary",
};

/** States an item can still be cancelled or rescheduled in: it has not run. */
const MOVABLE: ReadonlySet<WorkItemScheduleState> = new Set(["scheduled", "due", "blocked", "missed"]);

/**
 * The outcome of an item platform restarts cut past every resume. An
 * `interrupted` item is one an operator stopped (an emergency stop, a
 * cancelled run): it keeps its controls. A restart-cut item is otherwise
 * known by its `restarts` counts.
 */
const RESTART_OUTCOMES: ReadonlySet<string> = new Set(["resume_exhausted"]);

export function isScheduledItem(item: WorkItem): item is WorkItem & { scheduled: WorkItemScheduled } {
  return !!item.scheduled;
}

export function canChangeSchedule(state: WorkItemScheduleState): boolean {
  return MOVABLE.has(state);
}

/** Whether a platform restart cut the item while it ran. */
export function cutByRestart(scheduled: Pick<WorkItemScheduled, "outcome_code" | "restarts">): boolean {
  const restarts = scheduled.restarts;
  return (
    RESTART_OUTCOMES.has(scheduled.outcome_code ?? "") || !!(restarts && (restarts.resumed > 0 || restarts.requeued > 0))
  );
}

/**
 * Whether Reschedule is offered: the item has not run, and no platform restart
 * cut it. A rollout never hands an action back to the person.
 */
export function canReschedule(scheduled: Pick<WorkItemScheduled, "state" | "outcome_code" | "restarts">): boolean {
  return canChangeSchedule(scheduled.state) && !cutByRestart(scheduled);
}

function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** The zone times are shown in: the profile's when it is a real zone, else the browser's. */
export function viewerTimeZone(profileZone?: string | null): string {
  if (profileZone) {
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: profileZone });
      return profileZone;
    } catch {
      // An unknown zone in a profile falls back to the browser's.
    }
  }
  return browserTimeZone();
}

const UTC_ZONES = new Set(["UTC", "Etc/UTC", "Etc/UCT", "UCT", "Etc/GMT", "GMT", "Etc/Universal", "Universal", "Etc/Zulu", "Zulu"]);

/** True when *timeZone* is UTC under one of its names: a time shown in it and
 *  again in UTC would read the same twice. */
export function isUtcZone(timeZone: string): boolean {
  return UTC_ZONES.has(timeZone);
}

/** `Thu 8 Oct 2026, 09:00 CEST` in *timeZone*. */
export function formatInZone(iso: string, timeZone: string, locale?: string): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone,
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).format(new Date(iso));
}

/**
 * `Mon 2 Nov, 13:20` in *timeZone* (the locale's order): the absolute time of a
 * schedule list row, the year and the zone name left to its tooltip.
 */
export function formatCompactInZone(iso: string, timeZone: string, locale?: string): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone,
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

/** `t_i18n` of `useFormatter()`: a catalog key and its values, in the viewer's language. */
type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * `in 3d 4h`, `in 2h 5m`, `in 14m`, `in <1m`, `due now` once passed, `-`
 * without a time, in the viewer's language through `t`.
 * `bare` drops the "in" / "dans" / "en" prefix (`3d 4h`, `14m`) where a label
 * already says the time is ahead, as the "Next trigger" KPI card does; its
 * passed form is the short `due` (fr "imminent"), which fits a 6-column card
 * at 1280 where fr "maintenant" does not.
 */
export function formatCountdown(
  iso: string | null,
  nowMs: number,
  t: Translate,
  { bare = false }: { bare?: boolean } = {},
): string {
  if (!iso) return "-";
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return "-";
  let diff = Math.round((at - nowMs) / 1000);
  if (diff <= 0) return bare ? t("due") : t("due now");
  const days = Math.floor(diff / 86400);
  diff %= 86400;
  const hours = Math.floor(diff / 3600);
  diff %= 3600;
  const minutes = Math.floor(diff / 60);
  if (bare) {
    if (days > 0) return t("{days}d {hours}h", { days, hours });
    if (hours > 0) return t("{hours}h {minutes}m", { hours, minutes });
    if (minutes > 0) return t("{count, plural, one {#m} other {#m}}", { count: minutes });
    return t("<1m");
  }
  if (days > 0) return t("in {days}d {hours}h", { days, hours });
  if (hours > 0) return t("in {hours}h {minutes}m", { hours, minutes });
  if (minutes > 0) return t("{count, plural, one {in #m} other {in #m}}", { count: minutes });
  return t("in <1m");
}

/**
 * `just now`, `5m ago`, `2h ago`, `3d ago` in the viewer's language through `t`:
 * the compact localised counterpart of `timeAgo` for the schedule rows.
 * Named apart from `formatTimeAgo` of `lib/relative-time`, the sentence
 * form every other row reads ("5 minutes ago").
 */
export function formatCompactTimeAgo(iso: string, t: Translate, nowMs: number = Date.now()): string {
  const mins = Math.floor((nowMs - new Date(iso).getTime()) / 60000);
  if (mins < 1) return t("just now");
  if (mins < 60) return t("{count, plural, one {#m ago} other {#m ago}}", { count: mins });
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return t("{count, plural, one {#h ago} other {#h ago}}", { count: hrs });
  return t("{count, plural, one {#d ago} other {#d ago}}", { count: Math.floor(hrs / 24) });
}

/**
 * The soonest upcoming run of an assignment: the earlier of an already-queued
 * delayed run (`pending_run_at`) and the next cron fire (`next_run_at`). A
 * schedule assignment can carry BOTH - a queued/delayed execution that is due
 * now (or earlier than its next cron tick) must win, otherwise the row hides the
 * imminent run behind the later cron time. `pending` says which one it is.
 */
export function soonestRun(s: AssignmentSchedule): { iso: string; pending: boolean } | null {
  const scheduledIso = s.trigger_type === "schedule" ? s.next_run_at : null;
  const pendingIso = s.pending_run_at;
  if (pendingIso && scheduledIso) {
    return new Date(pendingIso).getTime() <= new Date(scheduledIso).getTime()
      ? { iso: pendingIso, pending: true }
      : { iso: scheduledIso, pending: false };
  }
  if (pendingIso) return { iso: pendingIso, pending: true };
  if (scheduledIso) return { iso: scheduledIso, pending: false };
  return null;
}

/** `07:00 UTC`. */
export function formatUtcTime(iso: string): string {
  const at = new Date(iso);
  const hh = String(at.getUTCHours()).padStart(2, "0");
  const mm = String(at.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm} UTC`;
}

/**
 * A recurring trigger's next fire in the zone its cron is read in and in UTC
 * (`07:00 Europe/Paris, 06:00 UTC`), from the backend's `next_run_local` and
 * `next_run_at`. Null for a trigger read in UTC (no zone, or an unknown one:
 * its UTC time is what the row already says), for a trigger that is not a
 * schedule or has no next fire, and while a delayed run queued before that
 * fire is the next thing to happen (the row's countdown then names that run,
 * and a second, later "next run" beside it would contradict it).
 */
export function zonedNextRun(s: AssignmentSchedule): string | null {
  if (s.trigger_type !== "schedule" || !s.next_run_at || !s.next_run_local) return null;
  if (!s.timezone || s.timezone === "UTC" || s.timezone_warning) return null;
  if (s.pending_run_at && Date.parse(s.pending_run_at) <= Date.parse(s.next_run_at)) return null;
  const wall = /T(\d{2}:\d{2})/.exec(s.next_run_local)?.[1];
  if (!wall) return null;
  return `${wall} ${s.timezone}, ${formatUtcTime(s.next_run_at)}`;
}

/**
 * The zone a schedule's `timezone_warning` names (`timezone 'Mars/Olympus' not
 * recognised, UTC used` gives `Mars/Olympus`, `timezone 5 not recognised, UTC
 * used` gives `5`), `?` when it does not have that shape, null when there is
 * no warning.
 */
export function timezoneWarningZone(warning?: string | null): string | null {
  if (!warning) return null;
  return /^timezone '?(.*?)'? not recognised/.exec(warning)?.[1] || "?";
}

function partsIn(utcMs: number, timeZone: string): Record<string, number> {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const out: Record<string, number> = {};
  for (const part of parts) {
    if (part.type !== "literal") out[part.type] = Number(part.value);
  }
  return out;
}

/** Minutes *timeZone* is ahead of UTC at the instant *utcMs*. */
export function zoneOffsetMinutes(utcMs: number, timeZone: string): number {
  const p = partsIn(utcMs, timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return Math.round((asUtc - Math.floor(utcMs / 1000) * 1000) / 60000);
}

function offsetText(minutes: number): string {
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

/**
 * A wall time typed in an `<input type="datetime-local">` (`2026-10-25T09:00`),
 * meant in *timeZone*, as ISO-8601 WITH the offset that zone has at that
 * moment: `2026-10-25T09:00:00+01:00`. Daylight saving included.
 */
export function wallTimeToOffsetIso(value: string, timeZone: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!match) return null;
  const [, y, mo, d, h, mi] = match.map(Number);
  const asUtc = Date.UTC(y, mo - 1, d, h, mi);
  // The zone's offset at that wall time: read at the guess, then corrected
  // once (the guess may sit on the other side of a daylight saving change).
  let offset = zoneOffsetMinutes(asUtc - zoneOffsetMinutes(asUtc, timeZone) * 60000, timeZone);
  offset = zoneOffsetMinutes(asUtc - offset * 60000, timeZone);
  return `${match[0]}:00${offsetText(offset)}`;
}

/** An instant as the `datetime-local` value it reads as in *timeZone*. */
export function isoToWallTime(iso: string, timeZone: string): string {
  const p = partsIn(new Date(iso).getTime(), timeZone);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${p.year}-${pad(p.month)}-${pad(p.day)}T${pad(p.hour)}:${pad(p.minute)}`;
}

// ── The fleet's upcoming timeline: recurring and one-off, side by side ──

export type TimelineLane =
  | {
      kind: "recurring";
      key: string;
      label: string;
      agentId: string;
      agentIcon: string;
      cron: string | null;
      occurrences: Date[];
      schedule: AssignmentSchedule;
    }
  | {
      kind: "one-off";
      key: string;
      label: string;
      agentId: string | null;
      agentIcon: string | null;
      occurrences: Date[];
      item: ScheduledWorkItem;
    };

/**
 * The lanes of the upcoming timeline within the window: one per scheduled
 * assignment with its projected fires (recurring), one per scheduled work
 * item at its time (one-off; a due one sits on the now-line), soonest first.
 */
export function buildTimelineLanes(
  schedules: AssignmentSchedule[],
  oneOffs: ScheduledWorkItem[],
  windowHours: number,
  nowMs: number,
): TimelineLane[] {
  const windowEnd = new Date(nowMs + windowHours * 3_600_000);
  const lanes: TimelineLane[] = [];
  for (const s of schedules) {
    const occurrences = occurrencesInWindow(s.cron, s.next_run_at, nowMs, windowEnd);
    if (occurrences.length > 0) {
      lanes.push({
        kind: "recurring",
        key: `assignment:${s.assignment_id}`,
        label: s.assignment_name,
        agentId: s.agent_id,
        agentIcon: s.agent_icon,
        cron: s.cron,
        occurrences,
        schedule: s,
      });
    }
  }
  for (const item of oneOffs) {
    if (!item.due_at || !["scheduled", "due", "running"].includes(item.scheduled.state)) continue;
    const at = Math.max(new Date(item.due_at).getTime(), nowMs);
    if (at > windowEnd.getTime()) continue;
    lanes.push({
      kind: "one-off",
      key: `work-item:${item.id}`,
      label: item.title,
      agentId: item.owner_agent_id,
      agentIcon: item.agent_icon,
      occurrences: [new Date(at)],
      item,
    });
  }
  return lanes.sort((a, b) => a.occurrences[0].getTime() - b.occurrences[0].getTime());
}
