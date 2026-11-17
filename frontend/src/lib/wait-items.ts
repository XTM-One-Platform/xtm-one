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
 * What the chat's wait card shows of the work a `wait` watches: each
 * background task, assignment run, Video Studio job or A2A delegation named in
 * `until`, its state, and how long it has been running. The backend sends the
 * items on `wait_start` and again on `wait_status` whenever one of them
 * changes state.
 */

import type { WaitItem } from "@/types";

/** Read by `t_i18n(WAIT_ITEM_STATUS_LABELS[status])`. @i18n-keys */
const WAIT_ITEM_STATUS_LABELS = {
  pending: "Pending",
  queued: "Queued",
  delayed: "Delayed",
  running: "Running",
  awaiting_human: "Waiting for an answer",
  completed: "Completed",
  failed: "Failed",
  skipped: "Skipped",
  cancelled: "Cancelled",
  incomplete: "Incomplete",
  unknown: "Not found",
} as const;

/** Read by `t_i18n(WAIT_ITEM_KIND_LABELS[kind])`: the name of an item that has none of its own. @i18n-keys */
export const WAIT_ITEM_KIND_LABELS = {
  task: "Background task",
  run: "Assignment run",
  job: "Video Studio job",
  a2a: "Partner delegation",
} as const;

function isWaitItemKind(kind: unknown): kind is NonNullable<WaitItem["kind"]> {
  return kind === "task" || kind === "run" || kind === "job" || kind === "a2a";
}

/** The items a frame carries, or `undefined` when it carries none the card can show. */
export function readWaitItems(raw: unknown): WaitItem[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const items = raw.flatMap((entry): WaitItem[] => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    if (typeof item.id !== "string" || typeof item.status !== "string") return [];
    return [
      {
        id: item.id,
        kind: isWaitItemKind(item.kind) ? item.kind : null,
        label: typeof item.label === "string" && item.label.trim() ? item.label.trim() : null,
        status: item.status,
        started_at: typeof item.started_at === "string" ? item.started_at : null,
      },
    ];
  });
  return items.length > 0 ? items : undefined;
}

/** The i18n key of an item's state; a state this table does not know is shown as sent. */
export function waitItemStatusKey(status: string): string {
  return WAIT_ITEM_STATUS_LABELS[status as keyof typeof WAIT_ITEM_STATUS_LABELS] ?? status;
}

/** Seconds the item has been running at `nowMs`, or `null` when it is not running. */
export function waitItemRunningSeconds(item: WaitItem, nowMs: number): number | null {
  if (item.status !== "running" || !item.started_at) return null;
  const started = Date.parse(item.started_at);
  if (Number.isNaN(started)) return null;
  return Math.max(0, Math.floor((nowMs - started) / 1000));
}

/** The id as the card shows it when the item has no name: its first eight characters. */
export function shortWaitItemId(id: string): string {
  return id.slice(0, 8);
}
