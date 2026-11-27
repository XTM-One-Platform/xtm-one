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
 * The closed shapes read as the maps, graphs and flows draw them.
 *
 * A data source answers in one closed shape (`dashboard_live`); a view may
 * draw more than one: a graph from a table's source and target columns, a
 * timeline from a table's date column, a treemap from a distribution. The
 * conversions live here, pure, so every view reads one decision. A cell is
 * read as text (`cellText`: a chip as its label) and `cellNumber` reads the
 * figures back.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetDistributionData, WidgetEventsData, WidgetGraphData, WidgetRowsData, WidgetTreeData } from "@/types";

type Options = Record<string, unknown>;

/** The most stages a link graph reads from a table. */
const MAX_STAGES = 5;

const optionText = (options: Options, key: string): string | undefined =>
  typeof options[key] === "string" && (options[key] as string).trim() ? (options[key] as string).trim() : undefined;

/** The data columns of a table: never the row link. */
export function dataColumns(rows: WidgetRowsData): string[] {
  return rows.columns.map((column) => column.key).filter((key) => key !== "_url");
}

/** A column named by `options[key]` if the table has it, else the first of `fallbacks` it has. */
function pickColumn(rows: WidgetRowsData, options: Options, key: string, fallbacks: (string | undefined)[]): string | undefined {
  const keys = dataColumns(rows);
  const asked = optionText(options, key);
  if (asked) return keys.includes(asked) ? asked : undefined;
  return fallbacks.find((candidate): candidate is string => candidate !== undefined && keys.includes(candidate));
}

/** A rows cell as text: text as it is, a number written, a chip as its label, chips as their labels joined; empty when none. */
export function cellText(cell: unknown): string {
  if (typeof cell === "string") return cell;
  if (typeof cell === "number") return Number.isFinite(cell) ? String(cell) : "";
  const label = (chip: unknown) =>
    typeof chip === "object" && chip !== null && typeof (chip as { label?: unknown }).label === "string" ? (chip as { label: string }).label : "";
  if (Array.isArray(cell)) return cell.map(label).filter(Boolean).join(", ");
  return label(cell);
}

/** A row's cells as text, as a click hands them to the dashboard's interactions. */
export function textRow(row: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(Object.entries(row).flatMap(([key, cell]) => (cell === null || cell === undefined ? [] : [[key, cellText(cell)]])));
}

/** The keys a closed shape keeps a link under: a row's `_url`, an item's, an event's, an outcome's or an action's `link`. */
const LINK_KEYS = new Set(["_url", "link"]);

/** Data with every link into a platform taken out, at any depth (a dashboard that hides them); its `open_url` is kept: the frame hides it. */
export function withoutLinks<T>(value: T): T {
  if (Array.isArray(value)) return value.map((v) => withoutLinks(v)) as T;
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !LINK_KEYS.has(key)).map(([key, v]) => [key, withoutLinks(v)])) as T;
  }
  return value;
}

/** A figure written in a cell (`1,500,000`, ` 42 `, `-3.5`), or `null`. */
export function cellNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[\s,\u00a0\u202f]/g, "");
  if (!/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(cleaned)) return null;
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A moment in milliseconds: an ISO date (UTC midnight) or date-time; `null` when it is not one. */
export function parseMoment(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  const date = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (date) {
    const ms = Date.UTC(Number(date[1]), Number(date[2]) - 1, Number(date[3]));
    // `Date.UTC` rolls 2026-02-30 over to March: refuse what does not round-trip.
    return new Date(ms).toISOString().slice(0, 10) === text ? ms : null;
  }
  if (!/^\d{4}-\d{2}-\d{2}T/.test(text)) return null;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * A graph from a table: one edge per row from its source cell to its target
 * cell, rows naming the same pair summed (`value` column, else 1 each).
 * Source and target default to columns named so, else the first two.
 */
export function rowsToGraph(rows: WidgetRowsData, options: Options = {}): WidgetGraphData {
  const keys = dataColumns(rows);
  const source = pickColumn(rows, options, "sourceField", ["source", "from", keys[0]]);
  const target = pickColumn(rows, options, "targetField", ["target", "to", keys.find((key) => key !== source)]);
  const valueKey = pickColumn(rows, options, "valueField", ["value", "count"]);
  if (!source || !target || source === target) return { nodes: [], edges: [] };
  const nodes = new Map<string, WidgetGraphData["nodes"][number]>();
  const edges = new Map<string, WidgetGraphData["edges"][number]>();
  for (const row of rows.rows) {
    const from = cellText(row[source]).trim();
    const to = cellText(row[target]).trim();
    if (!from || !to) continue;
    for (const id of [from, to]) if (!nodes.has(id)) nodes.set(id, { id, label: id, group: null, value: null });
    const value = valueKey ? (cellNumber(row[valueKey]) ?? 0) : 1;
    const pair = JSON.stringify([from, to]);
    const edge = edges.get(pair);
    if (edge) edge.value = (edge.value ?? 0) + value;
    else edges.set(pair, { source: from, target: to, label: null, value });
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

/**
 * A link graph from a table: each column a stage, each value of
 * it a node of that stage, and a link between the values of two adjacent
 * columns of a row, counted. `options.fields` picks the stages.
 */
export function rowsToStages(rows: WidgetRowsData, options: Options = {}): WidgetGraphData {
  const keys = dataColumns(rows);
  const asked = Array.isArray(options.fields) ? options.fields.filter((f): f is string => typeof f === "string" && keys.includes(f)) : [];
  const stages = (asked.length >= 2 ? asked : keys).slice(0, MAX_STAGES);
  const labels = new Map(rows.columns.map((column) => [column.key, column.label || column.key]));
  const nodes = new Map<string, WidgetGraphData["nodes"][number]>();
  const edges = new Map<string, WidgetGraphData["edges"][number]>();
  const nodeId = (stage: number, value: string) => `${stage}\u001f${value}`;
  for (const row of rows.rows) {
    let previous: string | null = null;
    stages.forEach((key, stage) => {
      const value = cellText(row[key]).trim();
      if (!value) {
        previous = null;
        return;
      }
      const id = nodeId(stage, value);
      if (!nodes.has(id)) nodes.set(id, { id, label: value, group: labels.get(key) ?? key, value: null });
      if (previous) {
        const pair = JSON.stringify([previous, id]);
        const edge = edges.get(pair);
        if (edge) edge.value = (edge.value ?? 0) + 1;
        else edges.set(pair, { source: previous, target: id, label: null, value: 1 });
      }
      previous = id;
    });
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

/**
 * Events from a table: a label, a start (a `date` / `start` column, else the
 * first whose cells are dates), an optional end and lane, and a second line
 * (a `secondary` column). A row's link, entity type, icon and colour are the
 * event's. A row without a readable start is left out.
 */
export function rowsToEvents(rows: WidgetRowsData, options: Options = {}): WidgetEventsData {
  const keys = dataColumns(rows);
  const dated = keys.find((key) => rows.rows.some((row) => parseMoment(row[key]) !== null));
  const start = pickColumn(rows, options, "startField", ["start", "date", "time", dated]);
  if (!start) return { events: [] };
  const end = pickColumn(rows, options, "endField", ["end"]);
  const lane = pickColumn(rows, options, "laneField", ["lane", "type"]);
  const label = pickColumn(rows, options, "labelField", ["label", "name", keys.find((key) => key !== start)]);
  const secondaryKey = pickColumn(rows, options, "secondaryField", ["secondary"]);
  const events: WidgetEventsData["events"] = [];
  for (const row of rows.rows) {
    const begin = cellText(row[start]);
    if (parseMoment(begin) === null) continue;
    const finish = end && parseMoment(row[end]) !== null ? cellText(row[end]) : null;
    const secondary = secondaryKey ? cellText(row[secondaryKey]) : "";
    events.push({
      label: (label ? cellText(row[label]) : "") || "-",
      start: begin,
      end: finish,
      lane: lane ? cellText(row[lane]) || null : null,
      value: null,
      ...(secondary ? { secondary } : {}),
      ...(row._url ? { link: row._url } : {}),
      ...(row._entityType ? { entityType: row._entityType } : {}),
      ...(row._icon ? { icon: row._icon } : {}),
      ...(row._color ? { color: row._color } : {}),
    });
  }
  return { events };
}

/** A distribution as a one-level tree. */
export function distributionToTree(data: WidgetDistributionData): WidgetTreeData {
  return { items: data.items.filter((item) => item.value > 0).map((item) => ({ path: [item.label], value: item.value })) };
}
