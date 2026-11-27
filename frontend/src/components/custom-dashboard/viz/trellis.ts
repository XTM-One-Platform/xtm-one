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
 * Trellis: one chart split into small multiples by a field (a
 * "trellis layout"). What a split is depends on the closed shape:
 *
 * - `series`: one panel per series (the field is `series`);
 * - `tree`: one panel per top-level name, a distribution of what is under it
 *   (the field is `group`);
 * - `rows`: one panel per value of the `splitBy` column, a distribution of
 *   its `labelField` cells (the first other column) by its `valueField`
 *   figures (the first numeric column; else each row counts one).
 *
 * Panels are sorted (`name`, `-name`, `total`, `-total`), at most
 * `MAX_PANELS`; with a shared scale (the default) every panel's value axis
 * runs from the least to the largest value of them all, so panels compare.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetChartData, WidgetDistributionData, WidgetSeriesData, WidgetStudioData } from "@/types";

import { cellNumber, cellText, dataColumns } from "./shapes.ts";

export const MAX_PANELS = 24;
const MAX_COLUMNS = 6;
/** The least width of a panel when the columns are chosen for the room (px). */
const PANEL_MIN_WIDTH = 220;

type TrellisSort = "name" | "-name" | "total" | "-total";

export interface TrellisOptions {
  splitBy: string;
  columns: number | "auto";
  sharedScale: boolean;
  sort: TrellisSort;
  labelField?: string;
  valueField?: string;
}

export interface TrellisPanel {
  key: string;
  data: Extract<WidgetChartData, { shape: "series" | "distribution" }>;
  total: number;
}

export interface Trellis {
  panels: TrellisPanel[];
  /** The value axis every panel shares, or `null` when each keeps its own. */
  domain: [number, number] | null;
  /** Panels the split had beyond `MAX_PANELS`. */
  hidden: number;
}

/** A visualization's `trellis` option, or `null` when it asks for none. */
export function trellisOptions(options: Record<string, unknown>): TrellisOptions | null {
  const raw = options.trellis;
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const t = raw as Record<string, unknown>;
  if (t.enabled === false) return null;
  const splitBy = typeof t.splitBy === "string" && t.splitBy.trim() ? t.splitBy.trim() : "";
  if (!splitBy && t.enabled !== true) return null;
  const columns = typeof t.columns === "number" && Number.isInteger(t.columns) ? Math.min(MAX_COLUMNS, Math.max(1, t.columns)) : "auto";
  const sort = (["name", "-name", "total", "-total"] as const).find((s) => s === t.sort) ?? "name";
  const text = (value: unknown) => (typeof value === "string" && value.trim() ? value.trim() : undefined);
  return {
    splitBy,
    columns,
    sharedScale: t.sharedScale !== false,
    sort,
    labelField: text(t.labelField),
    valueField: text(t.valueField),
  };
}

const sum = (values: number[]) => values.reduce((a, b) => a + b, 0);

function split(data: WidgetChartData | WidgetStudioData, options: TrellisOptions): TrellisPanel[] | null {
  if (data.shape === "series") {
    return data.data.series.map((series) => ({
      key: series.name,
      data: { shape: "series", data: { series: [series] } satisfies WidgetSeriesData },
      total: sum(series.points.map((p) => p.y)),
    }));
  }
  if (data.shape === "tree") {
    const groups = new Map<string, WidgetDistributionData["items"]>();
    for (const item of data.data.items) {
      const [head, ...rest] = item.path;
      if (head === undefined) continue;
      const items = groups.get(head) ?? [];
      const label = rest.length ? rest.join(" / ") : head;
      const same = items.find((i) => i.label === label);
      if (same) same.value += item.value;
      else items.push({ label, value: item.value });
      groups.set(head, items);
    }
    return [...groups].map(([key, items]) => ({ key, data: { shape: "distribution", data: { items } }, total: sum(items.map((i) => i.value)) }));
  }
  if (data.shape === "rows") {
    const keys = dataColumns(data.data);
    if (!keys.includes(options.splitBy)) return null;
    const others = keys.filter((key) => key !== options.splitBy);
    const label = options.labelField && others.includes(options.labelField) ? options.labelField : others[0];
    const numeric = others.filter((key) => key !== label && data.data.rows.some((row) => cellNumber(row[key]) !== null));
    const value = options.valueField && others.includes(options.valueField) ? options.valueField : numeric[0];
    if (!label) return null;
    const groups = new Map<string, WidgetDistributionData["items"]>();
    for (const row of data.data.rows) {
      const key = cellText(row[options.splitBy]).trim() || "-";
      const name = cellText(row[label]).trim() || "-";
      const figure = value ? (cellNumber(row[value]) ?? 0) : 1;
      const items = groups.get(key) ?? [];
      const same = items.find((i) => i.label === name);
      if (same) same.value += figure;
      else items.push({ label: name, value: figure });
      groups.set(key, items);
    }
    return [...groups].map(([key, items]) => ({ key, data: { shape: "distribution", data: { items } }, total: sum(items.map((i) => i.value)) }));
  }
  return null;
}

/** The panels of a trellis over `data`, or `null` when its shape cannot be split. */
export function trellisPanels(data: WidgetChartData | WidgetStudioData, options: TrellisOptions): Trellis | null {
  const panels = split(data, options);
  if (!panels) return null;
  const order: Record<TrellisSort, (a: TrellisPanel, b: TrellisPanel) => number> = {
    name: (a, b) => a.key.localeCompare(b.key),
    "-name": (a, b) => b.key.localeCompare(a.key),
    total: (a, b) => a.total - b.total || a.key.localeCompare(b.key),
    "-total": (a, b) => b.total - a.total || a.key.localeCompare(b.key),
  };
  panels.sort(order[options.sort]);
  const shown = panels.slice(0, MAX_PANELS);
  let domain: [number, number] | null = null;
  if (options.sharedScale) {
    const values = shown.flatMap((panel) =>
      panel.data.shape === "series" ? panel.data.data.series.flatMap((s) => s.points.map((p) => p.y)) : panel.data.data.items.map((i) => i.value),
    );
    domain = values.length ? [Math.min(0, ...values), Math.max(0, ...values)] : null;
  }
  return { panels: shown, domain, hidden: panels.length - shown.length };
}

/** How many columns the panels take: the asked number, else as many as `PANEL_MIN_WIDTH` allows. */
export function trellisColumns(count: number, width: number, asked: number | "auto"): number {
  if (count <= 0) return 1;
  if (asked !== "auto") return Math.min(asked, count);
  return Math.max(1, Math.min(count, MAX_COLUMNS, Math.floor(width / PANEL_MIN_WIDTH)));
}
