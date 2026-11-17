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
 * The geometry and options of the radar, polar area, word cloud and heatmap
 * (`RadarChart`, `PolarAreaChart`, `WordCloud`, `HeatmapChart`): drawn as
 * OpenCTI draws its radar, polar area, word cloud and heatmap widgets, from
 * the closed data frames (`dynamic-options.frameOf`).
 *
 * Pure, relative imports only: runs on `node --test`.
 */
import { LEGEND_DISPLAYS, LEGEND_TRUNCATIONS, type LegendDisplay, type LegendTruncation } from "./chart-options.ts";
import { resolveColor } from "./colors.ts";
import { toNumber, type Field } from "./dynamic-options.ts";

type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const oneOf = <T extends string>(value: unknown, allowed: readonly T[], fallback: T): T =>
  (allowed as readonly unknown[]).includes(value) ? (value as T) : fallback;
const num = (value: unknown, min: number, max: number): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : undefined;
const colors = (value: unknown): (string | undefined)[] =>
  (Array.isArray(value) ? value : []).slice(0, 20).map((v) => resolveColor(v));
const colorMap = (value: unknown): Record<string, string> =>
  Object.fromEntries(
    Object.entries(isRecord(value) ? value : {})
      .slice(0, 50)
      .flatMap(([key, v]) => {
        const color = resolveColor(v);
        return color ? [[key, color]] : [];
      }),
  );

// ── Options ──────────────────────────────────────────────────────────────────

export interface RadarOptions {
  areaOpacity: number;
  lineWidth: number;
  legendDisplay: LegendDisplay;
  legendTruncation: LegendTruncation;
  seriesColors: (string | undefined)[];
  seriesColorsByField: Record<string, string>;
}

export function readRadarOptions(raw: unknown): RadarOptions {
  const o = isRecord(raw) ? raw : {};
  return {
    // OpenCTI's radar: a light fill under a thin line.
    areaOpacity: num(o.areaOpacity, 0, 1) ?? 0.2,
    lineWidth: num(o.lineWidth, 1, 6) ?? 1,
    legendDisplay: oneOf(o.legendDisplay, LEGEND_DISPLAYS, "bottom"),
    legendTruncation: oneOf(o.legendTruncation, LEGEND_TRUNCATIONS, "ellipsisEnd"),
    seriesColors: colors(o.seriesColors),
    seriesColorsByField: colorMap(o.seriesColorsByField),
  };
}

export interface WordCloudOptions {
  fontSizeMin: number;
  fontSizeMax: number;
  seriesColors: (string | undefined)[];
}

export function readWordCloudOptions(raw: unknown): WordCloudOptions {
  const o = isRecord(raw) ? raw : {};
  // OpenCTI's word cloud: 20 to 50 pixels.
  const min = num(o.fontSizeMin, 8, 32) ?? 20;
  const max = num(o.fontSizeMax, 12, 96) ?? 50;
  return { fontSizeMin: Math.min(min, max), fontSizeMax: Math.max(min, max), seriesColors: colors(o.seriesColors) };
}

export interface HeatmapOptions {
  colorMin?: string;
  colorMax?: string;
  dataValuesDisplay: "off" | "all";
  /** The colour ranges' legend: below, or none. */
  legendDisplay: LegendDisplay;
}

export function readHeatmapOptions(raw: unknown): HeatmapOptions {
  const o = isRecord(raw) ? raw : {};
  return {
    colorMin: resolveColor(o.colorMin),
    colorMax: resolveColor(o.colorMax),
    dataValuesDisplay: oneOf(o.dataValuesDisplay, ["off", "all"] as const, "off"),
    legendDisplay: oneOf(o.legendDisplay, LEGEND_DISPLAYS, "bottom"),
  };
}

// ── Labels and values ────────────────────────────────────────────────────────

export interface Item {
  label: string;
  value: number;
}

/** A distribution's items, or a series' totals by series. */
export function itemsOf(shape: string, fields: Field[]): Item[] {
  if (shape === "series") {
    return fields.slice(1).map((f) => ({ label: f.name, value: f.values.reduce<number>((sum, v) => sum + (toNumber(v) ?? 0), 0) }));
  }
  return (fields[0]?.values ?? []).map((label, i) => ({ label: String(label ?? ""), value: toNumber(fields[1]?.values[i]) ?? 0 }));
}

// ── Polar area ───────────────────────────────────────────────────────────────

export interface Sector {
  label: string;
  value: number;
  index: number;
  /** The SVG path of the sector, centred on (0, 0), its radius at most 1. */
  path: string;
  /** Where its label sits (the middle of its arc, a little out). */
  labelAt: { x: number; y: number };
}

const point = (angle: number, radius: number) => ({ x: radius * Math.sin(angle), y: -radius * Math.cos(angle) });
const fixed = (n: number) => Number(n.toFixed(4));

/**
 * The rings of a polar area: round values from 0 up to at least the largest, about `count` steps
 * of 1, 2, 2.5 or 5 times a power of ten (as a chart axis rounds its scale).
 */
export function polarRings(largest: number, count = 5): number[] {
  if (!(largest > 0)) return [];
  const raw = largest / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * power >= raw) ?? 10) * power;
  const rings: number[] = [];
  for (let at = step; at < largest + step - step * 1e-9; at += step) rings.push(Number(at.toPrecision(12)));
  return rings;
}

/** A polar area: one equal angle per item, starting at the top and going clockwise, its radius its
 *  value's share of `scaleMax` (the outer ring; the largest value when unset). */
export function polarSectors(items: Item[], scaleMax?: number): Sector[] {
  const max = scaleMax && scaleMax > 0 ? scaleMax : Math.max(0, ...items.map((i) => i.value));
  const step = items.length ? (2 * Math.PI) / items.length : 0;
  return items.map((item, index) => {
    const radius = max > 0 ? Math.min(1, Math.max(0, item.value) / max) : 0;
    const start = index * step;
    const end = start + step;
    const a = point(start, radius);
    const b = point(end, radius);
    const large = step > Math.PI ? 1 : 0;
    const path =
      items.length === 1
        ? `M 0 ${fixed(-radius)} A ${fixed(radius)} ${fixed(radius)} 0 1 1 0 ${fixed(radius)} A ${fixed(radius)} ${fixed(radius)} 0 1 1 0 ${fixed(-radius)} Z`
        : `M 0 0 L ${fixed(a.x)} ${fixed(a.y)} A ${fixed(radius)} ${fixed(radius)} 0 ${large} 1 ${fixed(b.x)} ${fixed(b.y)} Z`;
    const middle = point(start + step / 2, Math.max(0.35, radius) * 0.7);
    return { label: item.label, value: item.value, index, path, labelAt: { x: fixed(middle.x), y: fixed(middle.y) } };
  });
}

// ── Heatmap ──────────────────────────────────────────────────────────────────

export interface HeatmapGrid {
  rows: string[];
  columns: string[];
  /** `cells[row][column]`: the value, or `null` where the series has no point. */
  cells: (number | null)[][];
  min: number;
  max: number;
}

/** A series frame as a heatmap: one row per series, one column per x, in the frame's order. */
export function heatmapGrid(shape: string, fields: Field[]): HeatmapGrid {
  if (shape === "distribution") {
    // A distribution is one row: its labels are the columns.
    const items = itemsOf(shape, fields);
    const values = items.map((i) => i.value);
    return { rows: [""], columns: items.map((i) => i.label), cells: [values], min: Math.min(0, ...values), max: Math.max(0, ...values) };
  }
  const columns = (fields[0]?.values ?? []).map((x) => String(x ?? ""));
  const cells = fields.slice(1).map((f) => columns.map((_, i) => toNumber(f.values[i]) ?? null));
  const all = cells.flat().filter((v): v is number => v !== null);
  return { rows: fields.slice(1).map((f) => f.name), columns, cells, min: all.length ? Math.min(...all) : 0, max: all.length ? Math.max(...all) : 0 };
}

/** Where a value sits between the grid's lowest and highest (0..1); a zero is 0, as OpenCTI keeps zero cells apart. */
export function heatShare(value: number, min: number, max: number): number {
  if (value <= 0 || max <= min) return value > 0 ? 1 : 0;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

// ── Word cloud ───────────────────────────────────────────────────────────────

export interface PlacedWord {
  text: string;
  value: number;
  index: number;
  size: number;
  /** The word's centre, from the cloud's centre. */
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A word's width at a font size, estimated (no measuring in a pure module). */
export function wordWidth(text: string, size: number): number {
  return Math.ceil(text.length * size * 0.56) + 4;
}

/**
 * The words placed on a rectangular spiral from the centre, the largest first,
 * each sized on a log scale of its value between `min` and `max` pixels (the
 * smallest value at `min`, the largest at `max`, one value alone half way),
 * truncated to a whole pixel; the spiral steps 4 px down for `4 × width /
 * height` across, as d3-cloud's does (the layout OpenCTI's word cloud uses).
 * When some find no room, the cloud is laid out again a little smaller (down
 * to 60%); a word with no positive value, or still with no room, is not
 * placed. Deterministic: the same words always land in the same places.
 */
export function wordCloudLayout(items: Item[], width: number, height: number, min: number, max: number): PlacedWord[] {
  const wanted = items.filter((w) => Number.isFinite(w.value) && w.value > 0 && w.label.trim()).length;
  let best: PlacedWord[] = [];
  for (const scale of [1, 0.85, 0.72, 0.6]) {
    const placed = placeWords(items, width, height, Math.max(8, Math.round(min * scale)), Math.max(10, Math.round(max * scale)));
    if (placed.length > best.length) best = placed;
    if (best.length === wanted) break;
  }
  return best;
}

function placeWords(items: Item[], width: number, height: number, min: number, max: number): PlacedWord[] {
  const words = items
    .map((item, index) => ({ ...item, index }))
    .filter((w) => Number.isFinite(w.value) && w.value > 0 && w.label.trim())
    .sort((a, b) => b.value - a.value || a.index - b.index);
  if (!words.length || width <= 10 || height <= 10) return [];
  const logs = words.map((w) => Math.log(w.value));
  const low = Math.min(...logs);
  const high = Math.max(...logs);
  const placed: PlacedWord[] = [];
  const halfW = width / 2;
  const halfH = height / 2;
  const fits = (x: number, y: number, w: number, h: number) =>
    x - w / 2 >= -halfW && x + w / 2 <= halfW && y - h / 2 >= -halfH && y + h / 2 <= halfH &&
    placed.every((p) => Math.abs(p.x - x) * 2 >= p.width + w || Math.abs(p.y - y) * 2 >= p.height + h); // prettier-ignore
  words.forEach((word, n) => {
    const share = high > low ? (logs[n] - low) / (high - low) : 0.5;
    let size = Math.trunc(min + share * (max - min) + 1e-9);
    // A word too wide for the cloud shrinks to fit, never under the smallest size.
    while (size > min && wordWidth(word.label, size) > width) size -= 1;
    const w = wordWidth(word.label, size);
    const h = Math.ceil(size * 1.15);
    // A rectangular spiral, wider than tall like the cloud.
    const stepY = 4;
    const stepX = (4 * width) / height;
    let x = 0;
    let y = 0;
    let dx = stepX;
    let dy = 0;
    let leg = 1;
    // Enough steps for the spiral to reach the cloud's corners.
    const reach = Math.max(halfW / stepX, halfH / stepY) + 2;
    const steps = Math.ceil(4 * reach * reach) + 100;
    for (let turn = 0, walked = 0; turn < steps; turn += 1) {
      if (fits(x, y, w, h)) {
        placed.push({ text: word.label, value: word.value, index: word.index, size, x, y, width: w, height: h });
        return;
      }
      x += dx;
      y += dy;
      walked += 1;
      if (walked >= leg) {
        walked = 0;
        [dx, dy] = dx !== 0 ? [0, dx > 0 ? stepY : -stepY] : [dy > 0 ? -stepX : stepX, 0];
        if (dy === 0) leg += 1;
      }
    }
  });
  return placed;
}

// ── Label room ───────────────────────────────────────────────────────────────

/** About how wide a label is drawn, in px: 0.6 em a character. */
export const labelPixels = (text: string, fontSize: number): number => Math.ceil(text.length * fontSize * 0.6);

/**
 * How a radar fits its box: how many characters its labels keep (at most
 * `maxChars`, as the chart library cuts them) and its radius, so that the
 * labels beside the shape stay inside the box. With two axes or fewer no
 * label sits beside the shape (one above, one below): the radius is then
 * ApexCharts' own (`Radar.js`: the box's shorter side over 2.1, less the
 * stroke and the longest label over 1.75).
 */
export function radarFit(labels: string[], width: number, height: number, fontSize: number, maxChars: number): { chars: number; radius: number } {
  if (labels.length <= 2) {
    const chars = Math.max(4, Math.min(maxChars, Math.floor((width - 20) / (fontSize * 0.6))));
    const longest = Math.min(chars, Math.max(0, ...labels.map((l) => l.length))) * fontSize * 0.6;
    const apex = Math.min(width, height) / 2.1 - 1 - longest / 1.75;
    return { chars, radius: Math.max(30, Math.floor(Math.min(apex, height / 2 - fontSize - 12))) };
  }
  const half = width / 2;
  // The labels keep what leaves the shape a radius of 30 px at least: a smaller shape before shorter labels.
  const room = Math.max(0, half - 30 - 10);
  const chars = Math.max(4, Math.min(maxChars, Math.floor(room / (fontSize * 0.6))));
  const longest = Math.min(chars, Math.max(0, ...labels.map((l) => l.length))) * fontSize * 0.6;
  const byWidth = half - longest - 10;
  const byHeight = height / 2 - fontSize - 12;
  return { chars, radius: Math.max(30, Math.floor(Math.min(byWidth, byHeight))) };
}

/**
 * The x labels an axis writes, by index: from the first, each label whose box
 * (centred on its place, kept inside `start`..`end`) starts 8 px past the box
 * of the last one written.
 */
export function axisLabelPlaces(centers: number[], widths: number[], start: number, end: number): { index: number; x: number }[] {
  const kept: { index: number; x: number }[] = [];
  let right = -Infinity;
  centers.forEach((center, index) => {
    const half = widths[index] / 2;
    const x = Math.min(end - half, Math.max(start + half, center));
    if (x - half >= right + 8) {
      kept.push({ index, x });
      right = x + half;
    }
  });
  return kept;
}
