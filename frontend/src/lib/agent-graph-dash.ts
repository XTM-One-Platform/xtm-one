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
 * The marching dash of the agent graph's edges.
 *
 * A dashed edge is animated by moving its `stroke-dashoffset` in a loop. The
 * loop is seamless only when one cycle moves the dash by a whole number of
 * pattern periods: a fixed offset of 10 suited the handover dashes ("6 4",
 * "2 3") but not the A2A liaison dash "8 3 2 3" (period 16), which snapped
 * back 10/16 of a period at every cycle (#12382). Each dash pattern gets a
 * keyframe that moves it by exactly one period, at the speed the map always
 * had (10 drawing units per 3 s at rest, per 1.2 s live).
 */

/** Seconds the dash takes to move 10 drawing units, at rest and live. */
const SECONDS_PER_10_UNITS = { rest: 3, live: 1.2 } as const;

/**
 * Length of one period of an SVG dash array ("6 4" is 10, "8 3 2 3" is 16).
 * An odd list is repeated once, as SVG draws it ("5" is 10). Null for no dash
 * or a pattern with no length.
 */
export function dashPeriod(dash: string | null | undefined): number | null {
  if (!dash) return null;
  const parts = dash.split(/[\s,]+/).filter(Boolean).map(Number);
  if (parts.length === 0 || parts.some((n) => !Number.isFinite(n) || n < 0)) return null;
  const sum = parts.reduce((a, b) => a + b, 0) * (parts.length % 2 === 1 ? 2 : 1);
  return sum > 0 ? Math.round(sum * 1000) / 1000 : null;
}

/** The keyframe name that moves a dash of this period by one whole period. */
function dashKeyframeName(period: number): string {
  return `af-dash-${String(period).replace(".", "_")}`;
}

/**
 * The CSS `animation` of a marching dash, or undefined for a solid edge: one
 * period per cycle, the cycle's length scaled so every pattern marches at the
 * same speed.
 */
export function dashAnimation(dash: string | null | undefined, live: boolean): string | undefined {
  const period = dashPeriod(dash);
  if (period === null) return undefined;
  const seconds = Math.round(((live ? SECONDS_PER_10_UNITS.live : SECONDS_PER_10_UNITS.rest) * period) / 10 * 1000) / 1000;
  return `${dashKeyframeName(period)} ${seconds}s linear infinite`;
}

/** The `@keyframes` rules every dash of `dashes` needs, one per distinct period. */
export function dashKeyframes(dashes: ReadonlyArray<string | null | undefined>): string {
  const periods = [...new Set(dashes.map(dashPeriod).filter((p): p is number => p !== null))].sort((a, b) => a - b);
  return periods.map((p) => `@keyframes ${dashKeyframeName(p)}{to{stroke-dashoffset:-${p}}}`).join("\n");
}
