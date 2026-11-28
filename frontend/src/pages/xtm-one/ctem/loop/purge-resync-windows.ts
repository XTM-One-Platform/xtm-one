// SPDX-License-Identifier: Apache-2.0

/**
 * The resync choices of the purge dialog: one row per route (#2578), each ticked or not and
 * with its window in hours or days (#2745); what the rows hold becomes the `resync_sources`
 * the server validates. Pure, so the parsing is unit-tested.
 */
import type { CtemSignalResyncRoute, CtemSignalResyncRouteResult } from "@/hooks/api/useCtemSignalsApi";

export type WindowUnit = "days" | "hours";

/** One row of the dialog: ticked or not, the window as typed, its unit. */
export interface ResyncChoice {
  enabled: boolean;
  text: string;
  unit: WindowUnit;
}

/** The per-route entry the server reads (`resync_sources`). */
export interface ResyncSource {
  enabled: boolean;
  window?: number | null;
  unit?: WindowUnit;
}

/** Why a route a purge left unticked was not read, as the server words it (#2745). */
export const NOT_SELECTED = "not selected in the purge dialog";

/** A route the administrator may untick: every route the resync reads, and the passes. */
export function isSelectable(route: Pick<CtemSignalResyncRoute, "selectable" | "windowed" | "note">): boolean {
  return route.selectable ?? (route.windowed || !route.note);
}

/** What every row starts with: ticked, its default in days, empty for no age limit. */
export function initialChoices(routes: CtemSignalResyncRoute[]): Record<string, ResyncChoice> {
  return Object.fromEntries(
    routes
      .filter(isSelectable)
      .map((r) => [r.key, { enabled: true, text: r.windowed && r.default !== null ? String(r.default) : "", unit: "days" as const }]),
  );
}

/** The largest window a unit allows, *maxDays* being the server's bound in days. */
export function maxFor(unit: WindowUnit, maxDays: number): number {
  return unit === "hours" ? maxDays * 24 : maxDays;
}

/**
 * One field's value: a whole number of the unit from 1 to its bound, null for an empty
 * inside-state field (no age limit), "invalid" otherwise (an empty world field included).
 */
export function parseWindow(
  text: string,
  route: Pick<CtemSignalResyncRoute, "group">,
  maxDays: number,
  unit: WindowUnit = "days",
): number | null | "invalid" {
  const trimmed = text.trim();
  if (!trimmed) return route.group === "inside" ? null : "invalid";
  if (!/^\d+$/.test(trimmed)) return "invalid";
  const value = Number(trimmed);
  return value >= 1 && value <= maxFor(unit, maxDays) ? value : "invalid";
}

/** The sources to send, or null while a ticked field is invalid. */
export function sourcesToSend(
  routes: CtemSignalResyncRoute[],
  choices: Record<string, ResyncChoice>,
  maxDays: number,
): Record<string, ResyncSource> | null {
  const out: Record<string, ResyncSource> = {};
  for (const route of routes) {
    if (!isSelectable(route)) continue;
    const choice = choices[route.key];
    if (!choice) continue;
    if (!choice.enabled) {
      out[route.key] = { enabled: false };
      continue;
    }
    if (!route.windowed) {
      out[route.key] = { enabled: true };
      continue;
    }
    const value = parseWindow(choice.text, route, maxDays, choice.unit);
    if (value === "invalid") return null;
    out[route.key] = { enabled: true, window: value, unit: choice.unit };
  }
  return out;
}

/** How one route of a finished resync reads: its state and the counts it carries. */
export type RouteOutcome =
  | { kind: "skipped"; reason: string; unselected: boolean }
  | { kind: "error"; reason: string; read: number; recorded: number }
  | { kind: "done"; read: number; recorded: number; capped: boolean; note?: string };

export function routeOutcome(result: CtemSignalResyncRouteResult): RouteOutcome {
  const read = result.read ?? 0;
  const recorded = result.recorded ?? 0;
  if (result.skipped) return { kind: "skipped", reason: result.skipped, unselected: result.skipped === NOT_SELECTED };
  const reason = result.error ?? result.errors?.[0];
  if (reason) return { kind: "error", reason, read, recorded };
  return { kind: "done", read, recorded, capped: !!result.capped, note: result.note };
}
