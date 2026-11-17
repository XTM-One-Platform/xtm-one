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
 * A timeline drawn `alternate` (`timelineStyle`): one entry per event, newest
 * first as they come, down a vertical line - its date on one side, a card on
 * the other, swapping sides from one entry to the next (the first card on the
 * right) - and between them an outlined dot in the event's colour holding
 * its icon. The card holds the event's label and, under it, its second line
 * cut to 150 characters (OpenCTI's timeline: MUI Lab's alternate
 * `Timeline`, `WidgetTimeline`).
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetEventsData } from "@/types";

const TIMELINE_STYLES = ["lanes", "alternate"] as const;
export type TimelineStyle = (typeof TIMELINE_STYLES)[number];

/** How long an entry's second line is, at most. */
export const SECONDARY_CHARS = 150;

export const readTimelineStyle = (options: Record<string, unknown>): TimelineStyle =>
  (TIMELINE_STYLES as readonly unknown[]).includes(options.timelineStyle) ? (options.timelineStyle as TimelineStyle) : "lanes";

/** A text cut to `max` characters, an ellipsis after it when it was longer. */
export function cutText(value: string | undefined | null, max = SECONDARY_CHARS): string {
  const text = (value ?? "").trim();
  return text.length > max ? `${text.slice(0, max).trimEnd()}…` : text;
}

export interface AlternateEntry {
  index: number;
  /** Where its card sits: the first on the right, then left, then right... */
  card: "right" | "left";
  label: string;
  secondary: string;
  start: string;
  color: string | undefined;
  icon: string | undefined;
  link: string | undefined;
}

/** The entries of an alternate timeline, in the events' own order. */
export function alternateEntries(data: WidgetEventsData): AlternateEntry[] {
  return data.events.map((event, index) => ({
    index,
    card: index % 2 === 0 ? "right" : "left",
    label: event.label,
    secondary: cutText(event.secondary),
    start: event.start,
    color: event.color,
    icon: event.icon,
    link: event.link,
  }));
}
