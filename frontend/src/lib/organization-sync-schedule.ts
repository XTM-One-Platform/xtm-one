// SPDX-License-Identifier: Apache-2.0

/**
 * The schedule of an organization profile sync (#11913), its default and its
 * one-line reading, shared by the import stepper and Settings > Organization > Syncs.
 */
import type { OrganizationSync } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";

export type SyncSchedule = Pick<OrganizationSync, "frequency" | "hour" | "minute" | "weekday" | "timezone">;

/** The browser's time zone, the default of a new schedule. */
export const browserTimeZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

/** A new schedule: daily at 06:00 in the browser's time zone. */
export const defaultSchedule = (): SyncSchedule => ({ frequency: "daily", hour: 6, minute: 0, weekday: 0, timezone: browserTimeZone() });

export const pad2 = (value: number) => String(value).padStart(2, "0");

/** The name of a weekday (0 Monday ... 6 Sunday) in the viewer's language; 1 January 2024 was a Monday. */
export const weekdayName = (locale: string, weekday: number) =>
  new Intl.DateTimeFormat(locale, { weekday: "long", timeZone: "UTC" }).format(new Date(Date.UTC(2024, 0, 1 + weekday)));

/** The schedule in one line: "Every day at 06:00 (Europe/Paris)". */
export function useScheduleLabel() {
  const { t_i18n, locale } = useFormatter();
  return (schedule: SyncSchedule): string => {
    const zone = schedule.timezone.replace(/_/g, " ");
    if (schedule.frequency === "hourly") return t_i18n("Every hour at minute {minute}", { minute: pad2(schedule.minute) });
    const time = `${pad2(schedule.hour)}:${pad2(schedule.minute)}`;
    if (schedule.frequency === "weekly") return t_i18n("Every {day} at {time} ({zone})", { day: weekdayName(locale, schedule.weekday), time, zone });
    return t_i18n("Every day at {time} ({zone})", { time, zone });
  };
}
