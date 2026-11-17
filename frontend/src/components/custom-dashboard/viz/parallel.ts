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
 * Parallel coordinates: each column of a table an axis, each row a line
 * across them. A column whose every filled cell is a figure is a numeric
 * axis (from its least to its largest value, bottom to top); any other is a
 * categorical one (its values in the order they first appear, at most
 * `MAX_CATEGORIES`, the rest on an "other" step). An empty cell breaks the
 * row's line there.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetRowsData } from "@/types";

import { cellNumber, cellText, dataColumns } from "./shapes.ts";

const MAX_AXES = 8;
export const MAX_CATEGORIES = 30;
export const OTHER = "…";

export type ParallelAxis =
  | { key: string; label: string; x: number; kind: "number"; min: number; max: number }
  | { key: string; label: string; x: number; kind: "category"; values: string[] };

interface ParallelLine {
  row: number;
  /** The line's points, `null` where its cell is empty. */
  points: ({ x: number; y: number } | null)[];
  /** The SVG path through them, broken at an empty cell. */
  path: string;
}

export interface ParallelLayout {
  axes: ParallelAxis[];
  lines: ParallelLine[];
}

const round = (value: number) => Math.round(value * 100) / 100;

/** Where `value` sits on `axis`, 0 (bottom) to 1 (top); `null` for an empty cell. */
export function axisPosition(axis: ParallelAxis, value: string | undefined): number | null {
  const text = (value ?? "").trim();
  if (!text) return null;
  if (axis.kind === "number") {
    const parsed = cellNumber(text);
    if (parsed === null) return null;
    return axis.max === axis.min ? 0.5 : (parsed - axis.min) / (axis.max - axis.min);
  }
  const index = axis.values.indexOf(axis.values.includes(text) ? text : OTHER);
  if (index < 0) return null;
  return axis.values.length === 1 ? 0.5 : 1 - index / (axis.values.length - 1);
}

export function parallelLayout(data: WidgetRowsData, width: number, height: number, fields?: unknown): ParallelLayout {
  const keys = dataColumns(data);
  const asked = Array.isArray(fields) ? fields.filter((f): f is string => typeof f === "string" && keys.includes(f)) : [];
  const used = (asked.length >= 2 ? asked : keys).slice(0, MAX_AXES);
  if (used.length < 2) return { axes: [], lines: [] };
  const labels = new Map(data.columns.map((column) => [column.key, column.label || column.key]));
  const step = width / (used.length - 1);
  const axes: ParallelAxis[] = used.map((key, index) => {
    const cells = data.rows.map((row) => cellText(row[key]).trim()).filter(Boolean);
    const numbers = cells.map(cellNumber);
    const label = labels.get(key) ?? key;
    const x = round(index * step);
    if (cells.length > 0 && numbers.every((n) => n !== null)) {
      const values = numbers as number[];
      return { key, label, x, kind: "number", min: Math.min(...values), max: Math.max(...values) };
    }
    const values: string[] = [];
    for (const cell of cells) if (!values.includes(cell)) values.push(cell);
    return {
      key,
      label,
      x,
      kind: "category",
      values: values.length > MAX_CATEGORIES ? [...values.slice(0, MAX_CATEGORIES - 1), OTHER] : values,
    };
  });
  const lines = data.rows.map((row, index) => {
    const points = axes.map((axis) => {
      const at = axisPosition(axis, cellText(row[axis.key]));
      return at === null ? null : { x: axis.x, y: round(height - at * height) };
    });
    let path = "";
    let drawing = false;
    for (const point of points) {
      if (!point) {
        drawing = false;
        continue;
      }
      path += `${drawing ? "L" : "M"}${point.x},${point.y}`;
      drawing = true;
    }
    return { row: index, points, path };
  });
  return { axes, lines };
}
