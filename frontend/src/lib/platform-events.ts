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
 * The `platform_event` trigger: an assignment that starts when
 * something happens inside XTM One (a work item note, a run that fails, a
 * render that finishes). The catalog is served by
 * `GET /assignments/platform-events`, the same one the agent builder's
 * `list_platform_events` tool reads.
 */

import type { TriggerConfig } from "@/types";

/** One event of the catalog. */
export interface PlatformEventSpec {
  event: string;
  description: string;
  /** The payload fields a filter can name (dotted paths). */
  filter_keys: string[];
}

/** One row of the filter editor: a payload field and the value wanted. */
export interface PlatformFilterRow {
  key: string;
  value: string;
}

/**
 * The name a person reads for each platform event, in place of its key (UX #9882
 * r2 B2). Every key of the backend `PLATFORM_EVENTS` catalog has one: pinned by
 * backend/tests/test_platform_event_labels.py. Read through `platformEventLabel`. @i18n-keys
 */
const PLATFORM_EVENT_LABELS = {
  "work_item.note_added": "Work item note added",
  "work_item.status_changed": "Work item status changed",
  "work_item.claimed": "Work item claimed",
  "work_item.claim_released": "Work item claim released",
  "work_item.completed": "Work item completed",
  "work_item.released": "Work item released",
  "work_item.release_exhausted": "Work item no longer dispatched",
  "work_item.past_due": "Work item past due",
  "work_item.needs_human": "Work item waits for a person",
  "work_item.wait_cycle": "Work items wait on each other",
  "run.completed": "Run completed",
  "run.failed": "Run failed",
  "run.orphaned": "Run ended unfinished",
  "run.friction_recorded": "Friction recorded",
  "run.completed_with_undisposed_frictions": "Run ended with open frictions",
  "a2a.delegation.completed": "Partner answer arrived",
  "background_task.completed": "Background task completed",
  "background_task.failed": "Background task failed",
  "video.render.finished": "Video render finished",
  "video.render.failed": "Video render failed",
  "video.scene_preview.finished": "Scene preview finished",
  "video.scene_preview.failed": "Scene preview failed",
  "video.clip.ready": "AI shot ready",
  "video.clip.failed": "AI shot failed",
  "video.music.ready": "AI music ready",
  "video.music.failed": "AI music failed",
  "agent_account.owner_sent": "Mailbox owner sent an email",
  "platform.release.deployed": "New release deployed",
  "integration.version.changed": "Connected platform upgraded",
  "ctem_case.opened": "CTEM case opened",
  "ctem_case.reopened": "CTEM case reopened",
  "ctem_case.resolved": "CTEM case resolved",
  "ctem_case.sla_breached": "CTEM case past its SLA",
  "ctem_case.validation_finished": "CTEM case validation finished",
  "ctem_case.retest_requested": "CTEM case re-test requested",
  "ctem_signal.recorded": "CTEM signal recorded",
  "ctem_signal.triaged": "CTEM signal triaged",
  "platform.default_model_route.changed": "Default model route changed",
  "timer.due": "Timer due",
} as const;

/** The catalog label of a platform event (an English key for `t_i18n`), or null for an event it does not list. */
export function platformEventLabel(event: string): string | null {
  return Object.prototype.hasOwnProperty.call(PLATFORM_EVENT_LABELS, event)
    ? PLATFORM_EVENT_LABELS[event as keyof typeof PLATFORM_EVENT_LABELS]
    : null;
}

/** The family of an event, its name up to the first dot: `work_item`, `run`, `video`. */
export function platformEventFamily(event: string): string {
  const dot = event.indexOf(".");
  return dot > 0 ? event.slice(0, dot) : event;
}

/** The rows the editor shows for a stored filter: one per key, a scalar value as text. */
export function platformFilterRows(filter: Record<string, unknown> | null | undefined): PlatformFilterRow[] {
  return Object.entries(filter ?? {}).map(([key, value]) => ({
    key,
    value: typeof value === "string" ? value : JSON.stringify(value),
  }));
}

/**
 * The filter a set of rows saves: rows without a field or a value are left
 * out, the last row of a repeated field wins, and `undefined` when nothing
 * is left (the trigger then takes every event of its kind in its scope).
 */
export function platformFilterFromRows(rows: readonly PlatformFilterRow[]): Record<string, string> | undefined {
  const out: Record<string, string> = {};
  for (const { key, value } of rows) {
    const k = key.trim();
    const v = value.trim();
    if (k && v) out[k] = v;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Whether a trigger's deadline (`expires_at`, standing trigger or one-off watch) is still ahead:
 * `active` before it, `ended` once it passed; `null` without a deadline. A deadline that does not
 * parse is `ended`, as the platform reads it (it never delivers to it).
 */
export function platformDeadlineState(
  expiresAt: string | null | undefined,
  now: Date = new Date(),
): "active" | "ended" | null {
  if (!expiresAt) return null;
  const deadline = Date.parse(expiresAt);
  return Number.isFinite(deadline) && deadline > now.getTime() ? "active" : "ended";
}

/** A deadline a draft may save: none, or one that parses and is still ahead. */
export function platformDeadlineSavable(expiresAt: string | undefined, now: Date = new Date()): boolean {
  return !expiresAt || platformDeadlineState(expiresAt, now) === "active";
}

/**
 * A one-off watch's state: `armed` until its first event, then `fired`; `expired` when its deadline
 * passed (or does not parse) before any event, the platform no longer delivering to it; `null` for
 * a standing trigger (its deadline reads through {@link platformDeadlineState}).
 */
export function platformWatchState(
  config: { once?: boolean; fired_event_id?: string; expires_at?: string } | null | undefined,
  now: Date = new Date(),
): "armed" | "fired" | "expired" | null {
  if (config?.once !== true) return null;
  if (config.fired_event_id) return "fired";
  return platformDeadlineState(config.expires_at, now) === "ended" ? "expired" : "armed";
}

/**
 * Whether a platform event trigger no longer starts runs: a one-off watch that
 * {@link platformWatchState fired} (it starts nothing more until saved again) or
 * {@link platformWatchState expired}, or any trigger whose {@link platformDeadlineState deadline
 * ended}. The assignment page and the agent Overview assignment list show it as "Ended" (status
 * tile, header badge) instead of "Active"; `false` for every other trigger type.
 */
export function platformTriggerEnded(
  triggerType: string,
  config: { once?: boolean; fired_event_id?: string; expires_at?: string } | null | undefined,
  now: Date = new Date(),
): boolean {
  if (triggerType !== "platform_event" || !config) return false;
  const watch = platformWatchState(config, now);
  return watch === "fired" || watch === "expired" || platformDeadlineState(config.expires_at, now) === "ended";
}

/** What the platform event editor holds while the trigger is edited. */
export interface PlatformEventDraft {
  /** The events subscribed to: the editor picks one, a list saved by the agent builder is kept until changed. */
  events: string[];
  rows: PlatformFilterRow[];
  scope: "own" | "all";
  once: boolean;
  /** The subscription's deadline (an ISO instant with its offset), optional: past it the trigger no longer starts a run. */
  expiresAt?: string;
}

export function platformEventDraft(config: TriggerConfig | undefined): PlatformEventDraft {
  // The agent builder may save a list of events; the TriggerConfig type names one.
  const raw = config?.event as string | string[] | undefined;
  const events = Array.isArray(raw) ? raw : raw ? [raw] : [];
  return {
    events,
    rows: platformFilterRows(config?.filter as Record<string, unknown> | undefined),
    scope: config?.scope === "all" ? "all" : "own",
    once: config?.once === true,
    ...(typeof config?.expires_at === "string" && config.expires_at ? { expiresAt: config.expires_at } : {}),
  };
}

/** The trigger config a draft saves; a saved watch is armed again (no `fired_event_id`), its deadline kept. */
export function platformEventConfig(draft: PlatformEventDraft): TriggerConfig {
  const config: TriggerConfig = {};
  if (draft.events.length === 1) config.event = draft.events[0];
  if (draft.events.length > 1) (config as unknown as Record<string, unknown>).event = draft.events;
  // A platform event filter names payload fields, not the integration filter keys TriggerConfig types.
  const filter = platformFilterFromRows(draft.rows);
  if (filter) config.filter = filter as unknown as TriggerConfig["filter"];
  if (draft.scope === "all") config.scope = "all";
  if (draft.once) config.once = true;
  if (draft.expiresAt) config.expires_at = draft.expiresAt;
  return config;
}
