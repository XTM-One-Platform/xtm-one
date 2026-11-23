// SPDX-License-Identifier: Apache-2.0

/**
 * The "CTEM signal" trigger of the assignment form (#1409): a tile of its own
 * in the trigger picker, saved as the `platform_event` trigger underneath (one
 * mechanism). It subscribes to `ctem_signal.recorded`, `ctem_signal.triaged`
 * or both, with optional any-of filters on the signal's routing fields. An
 * assignment saved that way reopens on this tile: {@link isCtemSignalTrigger}.
 */

import type { TriggerConfig } from "@/types";

const CTEM_SIGNAL_EVENTS = ["ctem_signal.recorded", "ctem_signal.triaged"] as const;

/** The payload fields the tile filters on, each an any-of list; empty = every signal. */
const CTEM_SIGNAL_FILTER_KEYS = ["trigger_path", "lens", "source_provider", "subject_kind", "verdict"] as const;
export type CtemSignalFilterKey = (typeof CTEM_SIGNAL_FILTER_KEYS)[number];

/** Which events the tile subscribes to. */
export type CtemSignalEventChoice = "recorded" | "triaged" | "both";

export interface CtemSignalDraft {
  on: CtemSignalEventChoice;
  filters: Record<CtemSignalFilterKey, string[]>;
}

/** The lenses and subject kinds a signal carries (`backend/app/models/ctem_signal.py`). */
export const CTEM_SIGNAL_LENSES = ["inside", "outside", "world"] as const;
export const CTEM_SIGNAL_SUBJECT_KINDS = ["asset", "software", "identity", "third_party", "vulnerability", "threat", "code"] as const;
/** The scope gate's verdicts (`services/ctem_signals.VERDICTS`). */
export const CTEM_SIGNAL_VERDICTS = ["confirmed", "to_validate", "watchlist", "discarded"] as const;

function emptyFilters(): Record<CtemSignalFilterKey, string[]> {
  return { trigger_path: [], lens: [], source_provider: [], subject_kind: [], verdict: [] };
}

function eventsOf(config: TriggerConfig | undefined): string[] {
  const raw = (config as Record<string, unknown> | undefined)?.event;
  if (Array.isArray(raw)) return raw.filter((e): e is string => typeof e === "string");
  return typeof raw === "string" && raw ? [raw] : [];
}

/**
 * Whether a stored `platform_event` trigger is one the CTEM signal tile shows: it
 * subscribes to signal events only, filters on the tile's keys only (each a value
 * or a list of values), and holds no one-off watch, deadline or scope it would drop.
 */
export function isCtemSignalTrigger(triggerType: string, config: TriggerConfig | undefined): boolean {
  if (triggerType !== "platform_event" || !config) return false;
  const events = eventsOf(config);
  if (events.length === 0 || !events.every((e) => (CTEM_SIGNAL_EVENTS as readonly string[]).includes(e))) return false;
  const raw = config as Record<string, unknown>;
  if (raw.once === true || raw.expires_at || raw.scope === "all") return false;
  const filter = (raw.filter ?? {}) as Record<string, unknown>;
  return Object.entries(filter).every(
    ([key, value]) =>
      (CTEM_SIGNAL_FILTER_KEYS as readonly string[]).includes(key) &&
      (typeof value === "string" || (Array.isArray(value) && value.every((v) => typeof v === "string"))),
  );
}

/** The draft the tile edits for a stored trigger (a default one for anything else). */
export function ctemSignalDraft(triggerType: string, config: TriggerConfig | undefined): CtemSignalDraft {
  if (!isCtemSignalTrigger(triggerType, config)) return { on: "both", filters: emptyFilters() };
  const events = eventsOf(config);
  const on: CtemSignalEventChoice =
    events.length === 2 || events.length === 0 ? "both" : events[0] === "ctem_signal.triaged" ? "triaged" : "recorded";
  const filters = emptyFilters();
  const stored = ((config as Record<string, unknown>).filter ?? {}) as Record<string, string | string[]>;
  for (const key of CTEM_SIGNAL_FILTER_KEYS) {
    const value = stored[key];
    if (value !== undefined) filters[key] = Array.isArray(value) ? [...value] : [value];
  }
  return { on, filters };
}

/** The `platform_event` trigger config the tile saves: the event(s) and an any-of list per filled key. */
export function ctemSignalConfig(draft: CtemSignalDraft): TriggerConfig {
  const events: string[] =
    draft.on === "recorded" ? ["ctem_signal.recorded"] : draft.on === "triaged" ? ["ctem_signal.triaged"] : [...CTEM_SIGNAL_EVENTS];
  const config: Record<string, unknown> = { event: events.length === 1 ? events[0] : events };
  const filter: Record<string, string[]> = {};
  for (const key of CTEM_SIGNAL_FILTER_KEYS) {
    const values = draft.filters[key].filter(Boolean);
    if (values.length > 0) filter[key] = values;
  }
  if (Object.keys(filter).length > 0) config.filter = filter;
  return config as unknown as TriggerConfig;
}

/** The tile of the trigger picker beside the trigger types: it saves a `platform_event` trigger. */
export const CTEM_SIGNAL_TILE = "ctem_signal";

/** The picker tile a stored trigger opens on: `ctem_signal` for a CTEM signal trigger, else its own type. */
export function triggerTile(triggerType: string, config: TriggerConfig | undefined): string {
  return isCtemSignalTrigger(triggerType, config) ? CTEM_SIGNAL_TILE : triggerType;
}

/** The tiles of the trigger picker: the trigger types, the CTEM signal tile right after Platform event on XTM One. */
export function triggerTiles(types: readonly string[], ctem: boolean): string[] {
  if (!ctem) return [...types];
  const at = types.indexOf("platform_event");
  return at < 0 ? [...types, CTEM_SIGNAL_TILE] : [...types.slice(0, at + 1), CTEM_SIGNAL_TILE, ...types.slice(at + 1)];
}
