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
 * The fleet page's views and its Operations tab's lists, as path routes
 * (Sam, 8 Nov 2026, #10203 S2): the path says where you are, the query only
 * keeps state (filters, search, sort, pagination, `?window=`, `?focus=`).
 *
 *   /fleets/:fleetId                      About, the page with no tab
 *   /fleets/:fleetId/graph                Graph
 *   /fleets/:fleetId/data-flow            Data flow
 *   /fleets/:fleetId/members              Members
 *   /fleets/:fleetId/approval             Approval
 *   /fleets/:fleetId/schedule             Schedule
 *   /fleets/:fleetId/operations/:list?    Operations (runs when no list)
 *   /fleets/:fleetId/roi                  ROI (managers only)
 *
 * The links written before (`?view=<tab>`, `?ops=<list>`, `?mode=flow`,
 * `?view=graph&mode=flow`) are read once by `legacyFleetLocation` and
 * replaced by their path; an unknown tab or list lands on something that
 * exists (About, the runs).
 */

export type FleetView = "graph" | "flow" | "about" | "members" | "approval" | "schedule" | "roi" | "operations";

/**
 * The fleet page's tabs in order (Sam, 8 Nov 2026, #10176): About / Graph /
 * Data flow / Members / Approval / Schedule / Operations / ROI. Eight tabs at
 * most: a ninth fails review (a test holds the count). ROI shows to managers only.
 */
export const FLEET_TABS: readonly FleetView[] = ["about", "graph", "flow", "members", "approval", "schedule", "operations", "roi"];

/** Catalog keys of the tab labels. @i18n-keys */
export const FLEET_TAB_LABELS = {
  about: "About",
  graph: "Graph",
  flow: "Data flow",
  members: "Members",
  approval: "Approval",
  schedule: "Schedule",
  operations: "Operations",
  roi: "ROI",
} as const satisfies Record<FleetView, string>;

/** The path segment of each view; About is the page with none. */
const FLEET_VIEW_SEGMENTS = {
  about: "",
  graph: "graph",
  flow: "data-flow",
  members: "members",
  approval: "approval",
  schedule: "schedule",
  operations: "operations",
  roi: "roi",
} as const satisfies Record<FleetView, string>;

/** The views the legacy `?view=` named. */
const LEGACY_VIEWS: readonly FleetView[] = ["graph", "flow", "schedule", "members", "approval", "roi", "about"];

/** The view a fleet opens on: its first tab (Sam, 8 Nov 2026). */
export const FLEET_DEFAULT_VIEW: FleetView = "about";

export type FleetOpsList = "runs" | "background-tasks" | "work-items" | "state" | "worksets";

const LISTS: readonly FleetOpsList[] = ["runs", "background-tasks", "work-items", "state", "worksets"];

export function readFleetOpsList(value: string | null): FleetOpsList {
  return LISTS.find((l) => l === value) ?? "runs";
}

/** The view a `:tab` path segment names; none or an unknown one is About. */
export function readFleetTab(segment: string | undefined): FleetView {
  const found = FLEET_TABS.find((v) => FLEET_VIEW_SEGMENTS[v] === segment && segment !== "");
  return found ?? FLEET_DEFAULT_VIEW;
}

/** Whether a `:tab` segment names a tab (an absent one is About, a known place too). */
export function isKnownFleetTab(segment: string | undefined): boolean {
  return segment === undefined || FLEET_TABS.some((v) => v !== FLEET_DEFAULT_VIEW && FLEET_VIEW_SEGMENTS[v] === segment);
}

/** The path of a fleet's view (an Operations list optional), with no query. */
export function fleetViewPath(fleetId: string, view: FleetView, list?: FleetOpsList): string {
  const base = `/fleets/${fleetId}`;
  if (view === FLEET_DEFAULT_VIEW) return base;
  if (view === "operations") return `${base}/operations/${list ?? "runs"}`;
  return `${base}/${FLEET_VIEW_SEGMENTS[view]}`;
}

/**
 * The view a legacy query named, or null when it names none: `?ops=<list>`
 * (Operations), `?view=<tab>`, the Graph tab's former switch
 * `?view=graph&mode=flow` and `?mode=flow` (Data flow, #9829 / #9980).
 */
export function readLegacyFleetView(params: URLSearchParams): FleetView | null {
  if (params.has("ops")) return "operations";
  const view = params.get("view");
  if ((view === null || view === "graph") && params.get("mode") === "flow") return "flow";
  if (view === null) return null;
  return LEGACY_VIEWS.find((v) => v === view) ?? FLEET_DEFAULT_VIEW;
}

/**
 * Where a legacy fleet link goes: its path and the query left once `view`,
 * `ops` and `mode` are taken out (the lists' filters and `?window=` kept).
 * Null when the query names no view: nothing to redirect.
 */
export function legacyFleetLocation(fleetId: string, params: URLSearchParams): string | null {
  const view = readLegacyFleetView(params);
  if (view === null) return null;
  const list = view === "operations" ? readFleetOpsList(params.get("ops")) : undefined;
  const rest = new URLSearchParams(params);
  for (const key of ["view", "ops", "mode"]) rest.delete(key);
  // A list embedded in Operations owned `?view=` (the work items' Stale / Scheduled segment): kept.
  if (view === "operations" && params.get("view")) rest.set("view", params.get("view") as string);
  const query = rest.toString();
  return `${fleetViewPath(fleetId, view, list)}${query ? `?${query}` : ""}`;
}

/**
 * Whether an Operations list shows the fleet picker: not when it is already
 * locked on a fleet (the fleet page) or on a workspace (the workspace page).
 */
export function offersFleetPicker(scope: { fleet?: string; workspace?: string }): boolean {
  return !scope.fleet && !scope.workspace;
}

/**
 * Whether the reader can access a fleet at all: `GET /fleets` lists
 * the fleets the Fleets tab shows (Mine, Shared directly or through a group,
 * Company), archived ones left out. "unknown" while it loads; a refusal (no
 * licence) or a failure counts as none.
 */
type FleetAccess = "unknown" | "none" | "some";

export function fleetAccess(fleets: readonly unknown[] | undefined, failed: boolean): FleetAccess {
  if (failed) return "none";
  if (fleets === undefined) return "unknown";
  return fleets.length > 0 ? "some" : "none";
}

/**
 * The `?fleet=` a list applies: ignored once the reader is known to access no
 * fleet (unfiltered result, no picker), kept while access loads so a shared
 * link is not read twice.
 */
export function effectiveFleetParam(fromUrl: string | undefined, access: FleetAccess): string | undefined {
  return access === "none" ? undefined : fromUrl;
}

/**
 * Whether a list must wait before asking: the URL names a fleet and
 * the reader's access is not known yet, so the `fleet=` sent now may be
 * dropped a moment later. Without `?fleet=` nothing waits.
 */
export function fleetParamPending(fromUrl: string | undefined, access: FleetAccess): boolean {
  return fromUrl !== undefined && access === "unknown";
}
