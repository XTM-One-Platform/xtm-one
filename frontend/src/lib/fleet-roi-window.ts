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
 * The one window the fleet page's ROI view and its cost card share (#9785,
 * design dev-docs/roi-outcomes.md 6.3 "Window", R2-A2): the URL parameter
 * `window` (`7d`, `30d`, `90d`; `30d` when absent or unknown), so the two never
 * show different periods and a shared link keeps the period. Pure, so
 * `yarn test:unit` covers it.
 */

export type FleetWindow = "7d" | "30d" | "90d";

export const FLEET_WINDOW_PARAM = "window";

export const DEFAULT_FLEET_WINDOW: FleetWindow = "30d";

/** The control's options, in order. Translated by `SegmentedFilter`. @i18n-keys */
export const FLEET_WINDOW_LABELS = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
} as const satisfies Record<FleetWindow, string>;

export const FLEET_WINDOWS = Object.keys(FLEET_WINDOW_LABELS) as FleetWindow[];

export function readFleetWindow(params: URLSearchParams): FleetWindow {
  const value = params.get(FLEET_WINDOW_PARAM);
  return FLEET_WINDOWS.find((w) => w === value) ?? DEFAULT_FLEET_WINDOW;
}

/** The query with `next` as the window: the default leaves the URL clean. */
export function withFleetWindow(params: URLSearchParams, next: FleetWindow): URLSearchParams {
  const out = new URLSearchParams(params);
  if (next === DEFAULT_FLEET_WINDOW) out.delete(FLEET_WINDOW_PARAM);
  else out.set(FLEET_WINDOW_PARAM, next);
  return out;
}

/** The days `GET /fleets/{id}/cost?days=` takes for a window. */
export function fleetWindowDays(window: FleetWindow): number {
  return Number.parseInt(window, 10);
}

/** The next longer window, for the "widen the window" action; `null` at 90 days. */
export function widerFleetWindow(window: FleetWindow): FleetWindow | null {
  return FLEET_WINDOWS[FLEET_WINDOWS.indexOf(window) + 1] ?? null;
}
