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
 * The view state of an agent graph page, kept across reload and navigation.
 *
 * What a page shows (layout, visibility segment, Filters dropdown, Forces
 * sliders, Fleets layer) lives in the URL query, so a link reproduces the
 * view, and in a per-user preference (localStorage, one key per page: the
 * map, each fleet), used when the URL names none of the view's keys.
 *
 * Reading order: the URL when it carries at least one view key (a key it
 * leaves out then takes the page's default, never the preference, so a shared
 * link shows the same view to everyone); else the stored preference; else the
 * page's defaults. Writing: every control writes both.
 *
 * The visibility segment is the exception: it lives in the URL only, never in
 * the preference. With no `?vis=` the page lands on every visit by the landing
 * rule of the lists (`builtinLanding`), so a segment picked on an earlier visit
 * never outlives the counts it was picked on (an empty Mine, #12071).
 *
 * Pure module (no React, no DOM; the stored string is passed in): unit-tested
 * in agent-graph-view-state.test.ts.
 */

import { AGENT_KINDS, DEFAULT_GRAPH_FILTERS, type AgentGraphFilters, type AgentKind, type EnabledFilter } from "@/lib/agent-graph-filters";
import { FORCE_LIMITS, GRAPH_LAYOUTS, type GraphForces, type GraphLayout } from "@/lib/agent-graph-layout";
import { namedVisibility, readVisibility, writeVisibility, type Visibility } from "@/lib/visibility";

export interface GraphViewState {
  layout: GraphLayout;
  /**
   * True when the user picked the layout on the toolbar (or the URL names
   * one): the stored layout keeps winning on the next visit. Otherwise the
   * page opens on its default layout, so a default that was only written
   * along with another change never outlives a change of the default.
   */
  layoutChosen?: boolean;
  /**
   * The visibility segment; absent on a page that offers none (a fleet). Read
   * from `?vis=` only: the preference never restores it (`parseStoredView`).
   */
  visibility?: Visibility;
  filters: AgentGraphFilters;
  /** null = the automatic forces, scaled with the node count. */
  forces: GraphForces | null;
  fleetsLayer: boolean;
}

/** The query keys of the view. `vis` is shared with every list page (`lib/visibility.ts`). */
const P = {
  layout: "layout",
  vis: "vis",
  kinds: "kinds",
  enabled: "enabled",
  items: "items",
  inflight: "inflight",
  fleets: "fleets",
  forces: "forces",
} as const;

const VIEW_KEYS: readonly string[] = Object.values(P);
// The [DEPRECATED] aliases `readVisibility` still reads count as a view key.
const VISIBILITY_ALIASES: readonly string[] = ["visibility", "ownership", "scope"];
const ENABLED: readonly EnabledFilter[] = ["all", "enabled", "disabled"];

/**
 * True when the URL names at least one key of the view. On a page with no
 * segment (a fleet), `vis` and its aliases belong to the lists of its other
 * tabs, so they do not count.
 */
export function urlHasView(params: URLSearchParams, defaults: GraphViewState): boolean {
  const keys = defaults.visibility !== undefined ? [...VIEW_KEYS, ...VISIBILITY_ALIASES] : VIEW_KEYS.filter((k) => k !== P.vis);
  return keys.some((k) => params.has(k));
}

function toLayout(raw: string | null | undefined): GraphLayout | null {
  return raw && (GRAPH_LAYOUTS as readonly string[]).includes(raw) ? (raw as GraphLayout) : null;
}

function toKinds(raw: string | null | undefined): AgentKind[] | null {
  if (raw == null) return null;
  const kinds = raw.split(",").map((k) => k.trim());
  return AGENT_KINDS.filter((k) => kinds.includes(k));
}

function toEnabled(raw: unknown): EnabledFilter | null {
  return typeof raw === "string" && (ENABLED as readonly string[]).includes(raw) ? (raw as EnabledFilter) : null;
}

function toBool(raw: string | null | undefined): boolean | null {
  if (raw === "1" || raw === "true") return true;
  if (raw === "0" || raw === "false") return false;
  return null;
}

function inLimit(value: number, limit: { min: number; max: number }): boolean {
  return Number.isFinite(value) && value >= limit.min && value <= limit.max;
}

/** `charge,linkDistance,collide`, each inside the toolbar's bounds; anything else is null. */
export function parseForces(raw: string | null | undefined): GraphForces | null {
  if (!raw) return null;
  const parts = raw.split(",");
  if (parts.length !== 3 || parts.some((p) => p.trim() === "")) return null;
  const [charge, linkDistance, collide] = parts.map(Number);
  if (!inLimit(charge, FORCE_LIMITS.charge) || !inLimit(linkDistance, FORCE_LIMITS.linkDistance) || !inLimit(collide, FORCE_LIMITS.collide)) {
    return null;
  }
  return { charge, linkDistance, collide };
}

function formatForces(forces: GraphForces): string {
  return `${forces.charge},${forces.linkDistance},${forces.collide}`;
}

/** The view a URL carries, every key it leaves out (or carries invalid) at the page's default. */
export function readViewFromUrl(params: URLSearchParams, defaults: GraphViewState): GraphViewState {
  return {
    layout: toLayout(params.get(P.layout)) ?? defaults.layout,
    // The URL names a layout only when it differs from the default: a choice.
    ...(toLayout(params.get(P.layout)) ? { layoutChosen: true } : {}),
    ...(defaults.visibility !== undefined ? { visibility: readVisibility(params, undefined, defaults.visibility) } : {}),
    filters: {
      kinds: toKinds(params.get(P.kinds)) ?? defaults.filters.kinds,
      enabled: toEnabled(params.get(P.enabled)) ?? defaults.filters.enabled,
      withOpenWorkItems: toBool(params.get(P.items)) ?? defaults.filters.withOpenWorkItems,
      withRunsInFlight: toBool(params.get(P.inflight)) ?? defaults.filters.withRunsInFlight,
    },
    forces: parseForces(params.get(P.forces)) ?? defaults.forces,
    fleetsLayer: toBool(params.get(P.fleets)) ?? defaults.fleetsLayer,
  };
}

const bit = (value: boolean) => (value ? "1" : "0");

/**
 * *params* with *view* written: a value equal to the page's default is
 * removed, except the visibility segment, always written on a page that has
 * one (the `?vis=` contract of the list pages), so a page whose view is all
 * defaults still names its view and a reload never falls to a stale
 * preference. Every key that is not the view's is kept.
 */
export function writeViewToUrl(params: URLSearchParams, view: GraphViewState, defaults: GraphViewState): URLSearchParams {
  let next = new URLSearchParams(params);
  const put = (key: string, value: string | null) => {
    if (value === null) next.delete(key);
    else next.set(key, value);
  };
  put(P.layout, view.layout !== defaults.layout ? view.layout : null);
  const kinds = AGENT_KINDS.filter((k) => view.filters.kinds.includes(k)).join(",");
  const defaultKinds = AGENT_KINDS.filter((k) => defaults.filters.kinds.includes(k)).join(",");
  put(P.kinds, kinds !== defaultKinds ? kinds : null);
  put(P.enabled, view.filters.enabled !== defaults.filters.enabled ? view.filters.enabled : null);
  put(P.items, view.filters.withOpenWorkItems !== defaults.filters.withOpenWorkItems ? bit(view.filters.withOpenWorkItems) : null);
  put(P.inflight, view.filters.withRunsInFlight !== defaults.filters.withRunsInFlight ? bit(view.filters.withRunsInFlight) : null);
  put(P.fleets, view.fleetsLayer !== defaults.fleetsLayer ? bit(view.fleetsLayer) : null);
  const forces = view.forces ? formatForces(view.forces) : null;
  const defaultForces = defaults.forces ? formatForces(defaults.forces) : null;
  put(P.forces, forces !== defaultForces ? forces : null);
  if (defaults.visibility !== undefined && view.visibility !== undefined) next = writeVisibility(next, view.visibility);
  return next;
}

/** The stored preference, or null when absent, unreadable or not an object. */
export function parseStoredView(raw: string | null, defaults: GraphViewState): GraphViewState | null {
  if (!raw) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const f = (v.filters && typeof v.filters === "object" ? v.filters : {}) as Record<string, unknown>;
  const forces = v.forces && typeof v.forces === "object" ? (v.forces as Record<string, unknown>) : null;
  // The segment is never restored: a `visibility` or `visibilityChosen` an
  // older build stored is ignored, so no user stays on a segment picked on an
  // earlier visit (#12071); the page lands from its counts.
  const visibility = defaults.visibility;
  const storedLayout = toLayout(typeof v.layout === "string" ? v.layout : null);
  // A preference written before the flag: a layout other than the page's
  // default of that time was picked by hand. A fleet (no segment) then
  // opened Left to right, written with any other change of its view.
  const legacyDefault: GraphLayout = defaults.visibility === undefined ? "hierarchy-lr" : defaults.layout;
  const layoutChosen =
    storedLayout !== null && (typeof v.layoutChosen === "boolean" ? v.layoutChosen : storedLayout !== legacyDefault);
  return {
    layout: layoutChosen && storedLayout ? storedLayout : defaults.layout,
    ...(layoutChosen ? { layoutChosen: true } : {}),
    ...(visibility !== undefined ? { visibility } : {}),
    filters: {
      kinds: Array.isArray(f.kinds) ? AGENT_KINDS.filter((k) => (f.kinds as unknown[]).includes(k)) : defaults.filters.kinds,
      enabled: toEnabled(f.enabled) ?? defaults.filters.enabled,
      withOpenWorkItems: typeof f.withOpenWorkItems === "boolean" ? f.withOpenWorkItems : defaults.filters.withOpenWorkItems,
      withRunsInFlight: typeof f.withRunsInFlight === "boolean" ? f.withRunsInFlight : defaults.filters.withRunsInFlight,
    },
    forces: forces ? parseForces(`${forces.charge},${forces.linkDistance},${forces.collide}`) : defaults.forces,
    // A `mode` stored by the Graph tab's former Agents | Data flow switch
    // (#9829) is dropped: Data flow is a tab of its own since #9980.
    fleetsLayer: typeof v.fleetsLayer === "boolean" ? v.fleetsLayer : defaults.fleetsLayer,
  };
}

/** The preference: whether the layout was picked is always written; the segment never is (it lives in `?vis=`). */
export function serializeView(view: GraphViewState): string {
  // JSON.stringify drops an undefined key: the segment is never stored.
  return JSON.stringify({ ...view, visibility: undefined, layoutChosen: view.layoutChosen === true });
}

/**
 * Whether the URL names the map's segment: `?vis=` (or a [DEPRECATED] alias)
 * or a focus link, opened on All. Otherwise the map lands with `builtinLanding`
 * on every visit, Graph and Schedule, as the Agents and Fleets lists do.
 */
export function mapSegmentNamed(params: URLSearchParams): boolean {
  return params.has("focus") || namedVisibility(params) !== null;
}

/** The view a page shows: the URL when it names the view, else the stored preference, else the defaults. */
export function resolveGraphView(params: URLSearchParams, stored: string | null, defaults: GraphViewState): GraphViewState {
  if (urlHasView(params, defaults)) return readViewFromUrl(params, defaults);
  return parseStoredView(stored, defaults) ?? defaults;
}

/** The localStorage key of a page's preference, per user: `page` is "map" or "fleet:<id>". */
export function graphViewStorageKey(userId: string | undefined, page: string): string {
  return `xtmone.graphView.${userId ?? "anonymous"}.${page}`;
}

/**
 * The map's defaults: Mine, Force, no filter, automatic forces, Fleets layer
 * on. Mine is the segment before the agents load; the map then lands with
 * `builtinLanding` unless the URL names a segment (`mapSegmentNamed`).
 */
export const MAP_VIEW_DEFAULTS: GraphViewState = {
  layout: "force",
  visibility: "mine",
  filters: DEFAULT_GRAPH_FILTERS,
  forces: null,
  fleetsLayer: true,
};

/** A fleet's Graph tab: Force, as on the map; no segment, no filter. */
export const FLEET_VIEW_DEFAULTS: GraphViewState = {
  layout: "force",
  filters: DEFAULT_GRAPH_FILTERS,
  forces: null,
  fleetsLayer: true,
};
