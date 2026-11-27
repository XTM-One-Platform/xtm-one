// SPDX-License-Identifier: Apache-2.0

/**
 * The resync windows of the purge dialog (#2578): one field per route, its default
 * filled in; what the field holds becomes the `resync_windows` the server validates.
 * Pure, so the parsing is unit-tested.
 */
import type { CtemSignalResyncRoute, CtemSignalResyncRouteResult } from "@/hooks/api/useCtemSignalsApi";

/** The text a field starts with: the default in days, empty for no age limit. */
export function initialWindowText(routes: CtemSignalResyncRoute[]): Record<string, string> {
  return Object.fromEntries(routes.filter((r) => r.windowed).map((r) => [r.key, r.default === null ? "" : String(r.default)]));
}

/**
 * One field's value: a whole number of days from 1 to max, null for an empty inside-state
 * field (no age limit), "invalid" otherwise (an empty world field included).
 */
export function parseWindow(text: string, route: Pick<CtemSignalResyncRoute, "group">, max: number): number | null | "invalid" {
  const trimmed = text.trim();
  if (!trimmed) return route.group === "inside" ? null : "invalid";
  if (!/^\d+$/.test(trimmed)) return "invalid";
  const days = Number(trimmed);
  return days >= 1 && days <= max ? days : "invalid";
}

/** The windows to send, or null while a field is invalid. */
export function windowsToSend(
  routes: CtemSignalResyncRoute[],
  texts: Record<string, string>,
  max: number,
): Record<string, number | null> | null {
  const out: Record<string, number | null> = {};
  for (const route of routes) {
    if (!route.windowed) continue;
    const value = parseWindow(texts[route.key] ?? "", route, max);
    if (value === "invalid") return null;
    out[route.key] = value;
  }
  return out;
}

/** How one route of a finished resync reads: its state and the counts it carries. */
export type RouteOutcome =
  | { kind: "skipped"; reason: string }
  | { kind: "error"; reason: string; read: number; recorded: number }
  | { kind: "done"; read: number; recorded: number; capped: boolean; note?: string };

export function routeOutcome(result: CtemSignalResyncRouteResult): RouteOutcome {
  const read = result.read ?? 0;
  const recorded = result.recorded ?? 0;
  if (result.skipped) return { kind: "skipped", reason: result.skipped };
  const reason = result.error ?? result.errors?.[0];
  if (reason) return { kind: "error", reason, read, recorded };
  return { kind: "done", read, recorded, capped: !!result.capped, note: result.note };
}
