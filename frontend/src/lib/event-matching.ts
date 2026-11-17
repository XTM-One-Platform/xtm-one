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
 * Event matching of integration-event and webhook triggers.
 *
 * A trigger saved before exact event matching keeps the prefix match it always
 * had: `pull_request` also runs on `pull_request_review`. It keeps it through
 * every save that leaves its events alone, and matches exactly from the save
 * that chooses them again or asks for it. `settle_event_matching` in
 * `backend/app/models/assignment.py` decides; the helpers below only let the
 * trigger card say beforehand what a save will do.
 */

/** Trigger types that select events (`EVENT_MATCHING_TRIGGER_TYPES`). */
const EVENT_TRIGGER_TYPES = new Set(["integration_event", "webhook"]);

/** The `trigger_config` keys a trigger chooses its events with (`EVENT_SELECTION_KEYS`). */
const EVENT_SELECTION_KEYS = ["provider", "event", "events", "path"] as const;

/** A stored or pending `trigger_config` (`TriggerConfig`, or any JSON object). */
type ConfigLike = object | null | undefined;

function field(config: ConfigLike, key: string): unknown {
  return config ? (config as Record<string, unknown>)[key] : undefined;
}

/**
 * These are the catalog events a legacy trigger matches only by prefix; exact
 * matching still takes `pull_request.opened`, below the dot.
 */
export function legacySiblingEvents(selected: string, events: readonly string[]): string[] {
  if (!selected) return [];
  return events.filter(
    (event) => event !== selected && event.startsWith(selected) && !event.startsWith(`${selected}.`),
  );
}

/**
 * Whether a legacy trigger's `events` allow-list is matched by prefix. Every
 * receiver that reads `events` applies it to webhook AND integration-event
 * triggers (the HubSpot and per-assignment URLs), so the trigger type is
 * deliberately not part of the question.
 */
export function hasPrefixAllowList(events: unknown): boolean {
  return Array.isArray(events) ? events.length > 0 : !!events;
}

/**
 * The event a legacy integration-event trigger names under the old
 * `event_type` key, which is not read until the trigger matches
 * exactly by its own marker; `null` when there is none, or `event` names one.
 */
export function unreadLegacyEventType(config: ConfigLike): string | null {
  if (!config || field(config, "event_matching") === "exact") return null;
  const legacyValue = field(config, "event_type");
  const currentValue = field(config, "event");
  const legacy = typeof legacyValue === "string" ? legacyValue.trim() : "";
  const current = typeof currentValue === "string" ? currentValue.trim() : "";
  return legacy && !current ? legacy : null;
}

/** Python's falsiness: what a legacy trigger reads as no selection at all. */
function selectsNothing(value: unknown): boolean {
  if (value === undefined || value === null || value === false || value === 0 || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === "object" && Object.keys(value as object).length === 0;
}

/** A JSON value in one spelling whatever its key order; `undefined` inside reads as `null`. */
function stableJson(value: unknown): string {
  if (value === undefined || value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** One selection value as `_event_selection` compares it: `config.get(key) or None`. */
function canonical(value: unknown): string {
  return selectsNothing(value) ? "null" : stableJson(value);
}

/**
 * Whether saving *next* switches a trigger stored as *stored* from its prefix
 * match to exact matching: it asks for it, turns another trigger type into an
 * event trigger, or chooses its events again. A trigger already marked exact
 * is not switched.
 */
export function saveSwitchesToExact(
  storedType: string,
  stored: ConfigLike,
  nextType: string,
  next: ConfigLike,
): boolean {
  if (!EVENT_TRIGGER_TYPES.has(nextType) || field(stored, "event_matching") === "exact") return false;
  if (field(next, "event_matching") === "exact" || storedType !== nextType) return true;
  return EVENT_SELECTION_KEYS.some((key) => canonical(field(stored, key)) !== canonical(field(next, key)));
}
