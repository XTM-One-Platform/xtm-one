// SPDX-License-Identifier: Apache-2.0

/**
 * Cron display + projection helpers for the Agentic Flow Schedule tab.
 *
 * `humanizeCron` turns a cron expression into readable text (known presets
 * first, then `cronstrue`, raw string as last resort).
 *
 * `projectCronOccurrences` projects a cron's fire times across a time window
 * for the visual timeline. It is seeded from the backend-computed authoritative
 * first occurrence (`next_run_at`) so the first tick always matches the
 * inventory; subsequent ticks are client-projected and purely visual.
 */

import { CronExpressionParser } from "cron-parser";
import cronstrue from "cronstrue";
// Each registers its language on `cronstrue.locales`.
import "cronstrue/locales/fr";
import "cronstrue/locales/es";
import "cronstrue/locales/ja";
import { describeCron } from "@/lib/constants";

const JA_WEEKDAYS = ["日曜日", "月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日"];

/**
 * Japanese phrasing of the two commonest shapes, a daily and a weekly time:
 * `毎日 07:00`, `毎週月曜日 08:00`, `毎週月曜日・金曜日 08:00`. cronstrue's ja
 * output for them reads as machine text (`次において実施08:00月曜日 にのみ`).
 * Any other shape returns null and keeps cronstrue's wording.
 */
function japaneseDailyOrWeekly(cron: string): string | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dom, month, dow] = parts;
  if (!/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour) || dom !== "*" || month !== "*") return null;
  const m = Number(minute);
  const h = Number(hour);
  if (m > 59 || h > 23) return null;
  const time = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  if (dow === "*") return `毎日 ${time}`;
  if (!/^[0-7](,[0-7])*$/.test(dow)) return null;
  const days = [...new Set(dow.split(",").map((d) => Number(d) % 7))].sort((a, b) => a - b);
  return `毎週${days.map((d) => JA_WEEKDAYS[d]).join("・")} ${time}`;
}

/**
 * Human-readable description of a cron expression, in `locale` (a platform
 * locale such as `fr-fr`; English when absent). English reads the known preset
 * labels first; Japanese has its own phrasing for a daily or weekly time; the
 * other cases read cronstrue's own wording, in 24 h time.
 */
export function humanizeCron(cron: string | null | undefined, locale?: string): string {
  if (!cron) return "";
  const lang = (locale ?? "en").split("-")[0].toLowerCase();
  if (lang === "en") {
    const preset = describeCron(cron);
    if (preset !== cron) return preset; // matched a known preset label
  }
  if (lang === "ja") {
    const phrase = japaneseDailyOrWeekly(cron);
    if (phrase) return phrase;
  }
  try {
    return cronstrue.toString(cron, {
      verbose: false,
      locale: lang,
      use24HourTimeFormat: lang !== "en",
    });
  } catch {
    return cron;
  }
}

/**
 * The next occurrence of `cron` after `from`, read in `timeZone` (an IANA
 * zone such as `Europe/Paris`; UTC when absent), or null when the expression
 * does not parse. The Platform Health schedule table reads it for its "Next
 * run" column; the server's own `next_cron_run` decides when a cycle starts.
 * Only the 5-field form the server accepts is read: a 4-field or a 6-field
 * expression (which the parser would read with defaults or seconds) is null.
 */
export function nextCronRun(cron: string | null | undefined, timeZone?: string, from: Date = new Date()): Date | null {
  const expression = cron?.trim();
  if (!expression || expression.split(/\s+/).length !== 5) return null;
  try {
    return CronExpressionParser.parse(expression, { currentDate: from, tz: timeZone || "UTC" }).next().toDate();
  } catch {
    return null;
  }
}

/**
 * Project the fire times of `cron` within `(now, to]`, seeded from the
 * authoritative `firstIso` occurrence. Returns at most `max` dates.
 */
function projectCronOccurrences(
  cron: string | null | undefined,
  firstIso: string | null | undefined,
  to: Date,
  max = 60,
): Date[] {
  if (!firstIso) return [];
  const first = new Date(firstIso);
  if (Number.isNaN(first.getTime()) || first > to) return [];

  const out: Date[] = [first];
  if (!cron) return out;

  try {
    const interval = CronExpressionParser.parse(cron, { currentDate: first });
    while (out.length < max) {
      const next = interval.next().toDate();
      if (next > to) break;
      out.push(next);
    }
  } catch {
    // Unparseable by cron-parser - degrade to the single authoritative tick.
  }
  return out;
}

/**
 * Occurrences of `cron` within `[nowMs, to]`, seeded from the authoritative
 * `nextRunAt`. `next_run_at` is computed from `last_triggered_at` (not now), so
 * it can be overdue (in the past) when the scheduler is behind; clamp an overdue
 * seed to now so the due-now occurrence is still counted/shown at the now-line
 * instead of being filtered out. Shared by the Schedule "Next 24h" KPI and the
 * upcoming timeline so the two can never disagree.
 */
export function occurrencesInWindow(
  cron: string | null | undefined,
  nextRunAt: string | null | undefined,
  nowMs: number,
  to: Date,
  max = 60,
): Date[] {
  const seed =
    nextRunAt && new Date(nextRunAt).getTime() < nowMs
      ? new Date(nowMs).toISOString()
      : nextRunAt;
  return projectCronOccurrences(cron, seed, to, max).filter(
    (d) => d.getTime() >= nowMs,
  );
}
