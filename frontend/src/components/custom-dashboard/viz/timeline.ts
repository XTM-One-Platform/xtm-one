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
 * The timeline: events on one time axis, in lanes (an event's `lane`, in the
 * order the lanes first appear), an event with an end as a bar, one without
 * as a dot. Events of a lane that overlap - their label included, beside
 * them on the right, or on the left near the axis' end - stack on rows of
 * their own, so none hides another or another's label. The axis' ticks fall on whole hours, days, weeks, months,
 * quarters or years, whichever leaves room for its labels.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetEventsData } from "@/types";

import { parseMoment } from "./shapes.ts";

interface TimelineItem {
  index: number;
  label: string;
  lane: number;
  /** The row inside the whole chart (lanes stacked, their rows in them). */
  row: number;
  x: number;
  /** 0 for a dot. */
  w: number;
  start: number;
  end: number | null;
  /** Where its label is written: after its end, or before its start. */
  labelSide: "right" | "left";
}

interface TimelineLane {
  name: string;
  /** Its first row and how many it takes. */
  row: number;
  rows: number;
}

export interface TimelineTick {
  x: number;
  at: number;
  unit: TickUnit;
}

export interface TimelineLayout {
  items: TimelineItem[];
  lanes: TimelineLane[];
  ticks: TimelineTick[];
  rows: number;
  domain: [number, number];
}

export type TickUnit = "hour" | "day" | "week" | "month" | "quarter" | "year";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
/** The least room between two ticks (px), and a dot's room on its row (px). */
const TICK_ROOM = 80;
const DOT_ROOM = 8;
/** A label's room (px): an estimate of its glyphs at the chart's 11 px, and the gap to its mark. */
const CHAR_PX = 6;
const LABEL_GAP = 6;
const MAX_LABEL_PX = 180;
export const labelRoom = (label: string): number => Math.min(MAX_LABEL_PX, label.length * CHAR_PX) + LABEL_GAP;

const UNITS: { unit: TickUnit; ms: number }[] = [
  { unit: "hour", ms: HOUR },
  { unit: "day", ms: DAY },
  { unit: "week", ms: 7 * DAY },
  { unit: "month", ms: 30 * DAY },
  { unit: "quarter", ms: 91 * DAY },
  { unit: "year", ms: 365 * DAY },
];

/** The first moment of `unit` at or after `ms` (UTC; weeks start on Monday). */
function ceilTo(ms: number, unit: TickUnit, step: number): number {
  const d = new Date(ms);
  switch (unit) {
    case "hour":
      return Math.ceil(ms / (HOUR * step)) * HOUR * step;
    case "day":
    case "week": {
      const day = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
      let at = day < ms ? day + DAY : day;
      if (unit === "week") while (new Date(at).getUTCDay() !== 1) at += DAY;
      return at;
    }
    case "month":
    case "quarter":
    case "year": {
      const months = unit === "month" ? step : unit === "quarter" ? 3 * step : 12 * step;
      let year = d.getUTCFullYear();
      let month = Math.ceil(d.getUTCMonth() / months) * months;
      let at = Date.UTC(year, month, 1);
      if (at < ms) {
        month += months;
        year += Math.floor(month / 12);
        at = Date.UTC(year, month % 12, 1);
      }
      return at;
    }
  }
}

function advance(ms: number, unit: TickUnit, step: number): number {
  const d = new Date(ms);
  switch (unit) {
    case "hour":
      return ms + HOUR * step;
    case "day":
      return ms + DAY * step;
    case "week":
      return ms + 7 * DAY * step;
    case "month":
      return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + step, 1);
    case "quarter":
      return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 3 * step, 1);
    case "year":
      return Date.UTC(d.getUTCFullYear() + step, 0, 1);
  }
}

/** Ticks over `[from, to]` drawn `width` px wide: the finest unit leaving `TICK_ROOM` px between them. */
export function timeTicks(from: number, to: number, width: number): TimelineTick[] {
  const span = Math.max(1, to - from);
  const most = Math.max(1, Math.floor(width / TICK_ROOM));
  let choice = UNITS[UNITS.length - 1];
  let step = Math.max(1, Math.ceil(span / (choice.ms * most)));
  for (const candidate of UNITS) {
    if (span / candidate.ms <= most) {
      choice = candidate;
      step = 1;
      break;
    }
  }
  const ticks: TimelineTick[] = [];
  for (let at = ceilTo(from, choice.unit, step); at <= to && ticks.length < 200; at = advance(at, choice.unit, step)) {
    ticks.push({ at, x: ((at - from) / span) * width, unit: choice.unit });
  }
  return ticks;
}

export function timelineLayout(data: WidgetEventsData, width: number): TimelineLayout {
  const read = data.events
    .map((event, index) => ({ event, index, start: parseMoment(event.start), end: parseMoment(event.end) }))
    .filter((e): e is typeof e & { start: number } => e.start !== null)
    .map((e) => ({ ...e, end: e.end !== null && e.end >= e.start ? e.end : null }));
  if (read.length === 0) return { items: [], lanes: [], ticks: [], rows: 0, domain: [0, 0] };
  let from = Math.min(...read.map((e) => e.start));
  let to = Math.max(...read.map((e) => e.end ?? e.start));
  if (to === from) {
    from -= DAY / 2;
    to += DAY / 2;
  } else {
    // A margin, so a dot at either end is drawn whole.
    const margin = (to - from) * 0.03;
    from -= margin;
    to += margin;
  }
  const span = to - from;
  const xOf = (ms: number) => ((ms - from) / span) * width;
  const laneNames: string[] = [];
  for (const e of read) {
    const name = e.event.lane ?? "";
    if (!laneNames.includes(name)) laneNames.push(name);
  }
  const lanes: TimelineLane[] = [];
  const items: TimelineItem[] = [];
  let row = 0;
  for (const name of laneNames) {
    // Greedy rows: each event on the first row whose last event ended before it starts.
    const ends: number[] = [];
    const lane = lanes.length;
    for (const e of read.filter((r) => (r.event.lane ?? "") === name).sort((a, b) => a.start - b.start)) {
      const x = xOf(e.start);
      const w = e.end !== null ? Math.max(2, xOf(e.end) - x) : 0;
      const markEnd = w > 0 ? x + w : x + DOT_ROOM;
      const room = labelRoom(e.event.label);
      // The label goes after the mark, or before it when the axis ends first.
      const labelSide = markEnd + room > width && x - room >= 0 ? "left" : "right";
      const left = labelSide === "left" ? x - room : w > 0 ? x : x - DOT_ROOM;
      const right = labelSide === "left" ? markEnd : markEnd + room;
      let slot = ends.findIndex((end) => end <= left);
      if (slot < 0) {
        slot = ends.length;
        ends.push(right);
      } else ends[slot] = right;
      items.push({ index: e.index, label: e.event.label, lane, row: row + slot, x, w, start: e.start, end: e.end, labelSide });
    }
    lanes.push({ name, row, rows: Math.max(1, ends.length) });
    row += Math.max(1, ends.length);
  }
  return { items, lanes, ticks: timeTicks(from, to, width), rows: row, domain: [from, to] };
}
