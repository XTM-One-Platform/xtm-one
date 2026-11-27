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
 * A data source's result as CSV (the per-visualization export).
 *
 * The data is the closed shape the server answered (`WidgetData`), never
 * anything else. Each cell is quoted when it must be, and a cell a
 * spreadsheet would run as a formula (`=`, `+`, `-`, `@`, a tab or a carriage
 * return first) is prefixed with `'` unless it is a plain number: a row of a
 * platform or an agent's answer cannot become `=HYPERLINK(...)` in the file.
 * Pure: `csv.test.ts` runs it on `node --test`.
 */
import type { WidgetData } from "../../types";
import { cellText } from "./viz/shapes.ts";

type Ok = Extract<WidgetData, { status: "ok" }>;

const NUMBER = /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

/** One cell, safe for a spreadsheet and for CSV. */
export function csvCell(value: unknown): string {
  let text = value === null || value === undefined ? "" : String(value);
  if (/^[=+\-@\t\r]/.test(text) && !NUMBER.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) || text !== text.trim() ? `"${text.replace(/"/g, '""')}"` : text;
}

function lines(rows: unknown[][]): string {
  return rows.map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

/** The result as CSV: one row per value, item, point (one column per series) or row; the text in one cell. */
export function toCsv(result: Ok): string {
  switch (result.shape) {
    case "number":
      return lines([["value", "change"], [result.data.value, result.data.change ?? ""]]);
    case "distribution":
      return lines([["label", "value"], ...result.data.items.map((item) => [item.label, item.value])]);
    case "series": {
      const series = result.data.series;
      const axis = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))];
      const at = series.map((s) => new Map(s.points.map((p) => [p.x, p.y])));
      return lines([["x", ...series.map((s) => s.name)], ...axis.map((x) => [x, ...at.map((m) => m.get(x) ?? "")])]);
    }
    case "rows": {
      const columns = result.data.columns;
      return lines([columns.map((c) => c.label || c.key), ...result.data.rows.map((row) => columns.map((c) => cellText(row[c.key])))]);
    }
    case "text":
      return lines([["text"], [result.data.text]]);
    case "geo":
      return lines([
        ["kind", "label", "code", "lat", "lon", "value", "group"],
        ...result.data.points.map((p) => ["point", p.label, "", p.lat, p.lon, p.value ?? "", p.group ?? ""]),
        ...result.data.regions.map((r) => ["region", r.label, r.code, "", "", r.value, ""]),
      ]);
    case "graph":
      return lines([["source", "target", "label", "value"], ...result.data.edges.map((e) => [e.source, e.target, e.label ?? "", e.value ?? ""])]);
    case "events":
      return lines([["label", "start", "end", "lane", "value"], ...result.data.events.map((e) => [e.label, e.start, e.end ?? "", e.lane ?? "", e.value ?? ""])]);
    case "tree":
      return lines([["path", "value"], ...result.data.items.map((item) => [item.path.join(" / "), item.value])]);
    case "outcomes":
      return lines([
        ["group", "label", "part", "success", "failed", "pending"],
        ...result.data.items.flatMap((item) => [
          [item.group ?? "", item.label, "", item.success, item.failed, item.pending],
          ...item.parts.map((part) => [item.group ?? "", item.label, part.label, part.success, part.failed, part.pending]),
        ]),
      ]);
  }
}

/** A file name for the export: the visualization's title or id, kept to safe characters. */
export function csvFileName(name: string): string {
  const base = name.normalize("NFKD").replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
  return `${base || "data"}.csv`;
}
