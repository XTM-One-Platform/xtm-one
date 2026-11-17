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
 * The colours of the maps, graphs and flows: the dashboards' chart palette
 * (`EVALUATION_CHART_COLORS`, as `PlatformChart` draws series), and the
 * theme's surface and text tokens, set as `style` so SVG reads them live.
 */
import { EVALUATION_CHART_COLORS } from "@/lib/constants";

/**
 * The palette for categories, the violet last: a theme whose accent is violet
 * (XTM One's) would otherwise draw the first and the third category alike.
 */
const CATEGORICAL = [
  EVALUATION_CHART_COLORS[0],
  ...EVALUATION_CHART_COLORS.slice(1).filter((colour) => colour !== "#a78bfa"),
  "#a78bfa",
];

/** The `index`-th categorical colour; `null` (no group) is the accent. */
export function vizColor(index: number | null): string {
  if (index === null || index < 0) return CATEGORICAL[0];
  return CATEGORICAL[index % CATEGORICAL.length];
}

export const VIZ_TEXT = { fill: "var(--text-default-primary)", fontSize: 11 } as const;
export const VIZ_MUTED = { fill: "var(--text-default-secondary)", fontSize: 11 } as const;
export const VIZ_LINE = "var(--border-elevation-subtle)";
export const VIZ_SURFACE = "var(--bg-elevation-default)";
/** A country or a region without a value. */
export const VIZ_EMPTY = "var(--bg-elevation-hover)";
