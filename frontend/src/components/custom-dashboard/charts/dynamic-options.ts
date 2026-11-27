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
 * The dynamic options syntax, read on the closed data.
 *
 * An option whose value starts with `>` is an expression: a source, then
 * selectors and formatters piped one after the other, e.g.
 * `> primary | seriesByName('count') | lastPoint() | rangeValue(countRanges)`.
 *
 * - Sources: `primary` (the visualization's data; `table` is its alias),
 *   `annotation`, or the name of another option of the visualization
 *   (`> sparklineValues | lastPoint()`), resolved up to {@link MAX_DEPTH} deep.
 * - Selectors: `seriesByName('name')`, `seriesByIndex(n)`, `firstPoint()`,
 *   `lastPoint()`, `delta(n)` (the last point minus the point at index `n`,
 *   negative from the end), `min()`, `max()`, `sum()`.
 * - Formatters, applied to a point or to every point of a series:
 *   `rangeValue(config)`, `matchValue(config)`, `gradient(config)`,
 *   `formatByType(config)`, `prefix('text')`. A config argument names an
 *   entry of the visualization's `context`.
 *
 * The data is a frame of fields, read from the closed shapes: a `series` as
 * `x` then one field per series, a `distribution` as `label` and `value`, a
 * `rows` as its columns, a `number` as `value` and `change`.
 *
 * The expression is tokenized and parsed here, never handed to `eval` or
 * `Function`; what cannot be read gives `undefined` and the option falls back
 * to its default. `backend/app/services/dashboard_visualization_options.py`
 * checks the same grammar on write.
 *
 * Pure, relative imports only: runs on `node --test`.
 */
import { mixColor, resolveColor } from "./colors.ts";

export type Cell = number | string | null;
export interface Field {
  name: string;
  values: Cell[];
}
export type DosValue = { kind: "frame"; fields: Field[] } | { kind: "series"; field: Field } | { kind: "point"; value: Cell };

type Arg = { kind: "string"; value: string } | { kind: "number"; value: number } | { kind: "ref"; name: string };
interface Call {
  name: string;
  args: Arg[];
}
export interface Expression {
  source: string;
  calls: Call[];
}

const MAX_EXPRESSION_CHARS = 500;
const MAX_CALLS = 10;
/** How deep an option may name another option. */
const MAX_DEPTH = 4;
/** The most points a literal array or an evaluated series holds. */
const MAX_VALUES = 1000;
const MAX_TEXT = 200;

/** The functions, with their arguments' kinds. */
const FUNCTIONS: Record<string, readonly ("string" | "number" | "ref")[]> = {
  seriesByName: ["string"],
  seriesByIndex: ["number"],
  firstPoint: [],
  lastPoint: [],
  delta: ["number"],
  min: [],
  max: [],
  sum: [],
  rangeValue: ["ref"],
  matchValue: ["ref"],
  gradient: ["ref"],
  formatByType: ["ref"],
  prefix: ["string"],
};
type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);

const isExpression = (value: unknown): value is string => typeof value === "string" && value.trimStart().startsWith(">");

// ── Parsing ──────────────────────────────────────────────────────────────────

const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]{0,59}$/;
const TOKEN_RE = /\s*(?:(\|)|(\()|(\))|(,)|('(?:[^'\\]|\\.){0,200}'|"(?:[^"\\]|\\.){0,200}")|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*))/y;

type Token = { type: "pipe" | "open" | "close" | "comma" } | { type: "string"; value: string } | { type: "number"; value: number } | { type: "ident"; value: string };

function tokenize(text: string): Token[] | string {
  const tokens: Token[] = [];
  TOKEN_RE.lastIndex = 0;
  let index = 0;
  while (index < text.length) {
    if (/^\s*$/.test(text.slice(index))) break;
    TOKEN_RE.lastIndex = index;
    const m = TOKEN_RE.exec(text);
    if (!m) return `unexpected text at ${index}`;
    index = TOKEN_RE.lastIndex;
    if (m[1]) tokens.push({ type: "pipe" });
    else if (m[2]) tokens.push({ type: "open" });
    else if (m[3]) tokens.push({ type: "close" });
    else if (m[4]) tokens.push({ type: "comma" });
    else if (m[5]) tokens.push({ type: "string", value: m[5].slice(1, -1).replace(/\\(.)/g, "$1") });
    else if (m[6]) tokens.push({ type: "number", value: Number(m[6]) });
    else tokens.push({ type: "ident", value: m[7] });
  }
  return tokens;
}

/** An expression read, or why it cannot be. Never throws. */
export function parseExpression(text: string): { ok: true; expression: Expression } | { ok: false; error: string } {
  if (!isExpression(text)) return { ok: false, error: "an expression starts with '>'" };
  if (text.length > MAX_EXPRESSION_CHARS) return { ok: false, error: `an expression holds at most ${MAX_EXPRESSION_CHARS} characters` };
  const tokens = tokenize(text.trimStart().slice(1));
  if (typeof tokens === "string") return { ok: false, error: tokens };
  let at = 0;
  const source = tokens[at++];
  if (!source || source.type !== "ident") return { ok: false, error: "an expression starts with a source name" };
  const calls: Call[] = [];
  while (at < tokens.length) {
    if (tokens[at++].type !== "pipe") return { ok: false, error: "steps are separated by '|'" };
    const name = tokens[at++];
    if (!name || name.type !== "ident" || !Object.prototype.hasOwnProperty.call(FUNCTIONS, name.value)) {
      return { ok: false, error: `unknown function ${name && name.type === "ident" ? name.value : ""}`.trim() };
    }
    if (tokens[at++]?.type !== "open") return { ok: false, error: `${name.value} needs '('` };
    const args: Arg[] = [];
    while (tokens[at] && tokens[at].type !== "close") {
      const token = tokens[at++];
      if (token.type === "string") args.push({ kind: "string", value: token.value });
      else if (token.type === "number") args.push({ kind: "number", value: token.value });
      else if (token.type === "ident") args.push({ kind: "ref", name: token.value });
      else return { ok: false, error: `unexpected token in ${name.value}()` };
      if (tokens[at]?.type === "comma") at++;
      else if (tokens[at]?.type !== "close") return { ok: false, error: `${name.value}() needs ')'` };
    }
    if (tokens[at++]?.type !== "close") return { ok: false, error: `${name.value}() needs ')'` };
    const expected = FUNCTIONS[name.value];
    if (args.length !== expected.length || args.some((arg, i) => arg.kind !== expected[i])) {
      return { ok: false, error: `${name.value}() takes (${expected.join(", ")})` };
    }
    calls.push({ name: name.value, args });
    if (calls.length > MAX_CALLS) return { ok: false, error: `an expression has at most ${MAX_CALLS} steps` };
  }
  if (!IDENT_RE.test(source.value)) return { ok: false, error: "a source name is a short identifier" };
  return { ok: true, expression: { source: source.value, calls } };
}

// ── The closed data as a frame ───────────────────────────────────────────────

/** A cell read as a number: a finite number, or a numeric text. */
export function toNumber(cell: unknown): number | null {
  if (typeof cell === "number") return Number.isFinite(cell) ? cell : null;
  if (typeof cell === "string" && /^\s*-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s*$/.test(cell)) {
    const n = Number(cell);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** A rows cell as a value: text and numbers as they are, a chip as its label, chips as their labels joined. */
function cellValue(cell: unknown): Cell {
  if (typeof cell === "string" || (typeof cell === "number" && Number.isFinite(cell))) return cell;
  if (isRecord(cell)) return typeof cell.label === "string" ? cell.label : null;
  if (Array.isArray(cell)) return cell.flatMap((chip) => (isRecord(chip) && typeof chip.label === "string" ? [chip.label] : [])).join(", ");
  return null;
}

/** The closed shapes (`dashboard_live`) as fields. Anything else: no field. */
const DATE_X = /^\d{4}-\d{2}(?:-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?)?$/;

/** The x values of series in the order they are drawn: in time when every one is a date (buckets
 *  one series has and another lacks fall in their place), else in the order the data gives them
 *  (a radar's axes, a chart's categories, as the module sent them). */
export function seriesXOrder(xs: Iterable<string>): string[] {
  const seen = [...new Set(xs)];
  return seen.length && seen.every((x) => DATE_X.test(x)) ? seen.sort((a, b) => a.localeCompare(b)) : seen;
}

export function frameOf(shape: unknown, data: unknown): Field[] {
  const raw = isRecord(data) ? data : {};
  if (shape === "number") {
    return [
      { name: "value", values: [toNumber(raw.value)] },
      { name: "change", values: [toNumber(raw.change)] },
    ];
  }
  if (shape === "series") {
    const series = (Array.isArray(raw.series) ? raw.series : []).filter(isRecord);
    const xs = new Set<string>();
    const maps = series.map((s) => {
      const map = new Map<string, number | null>();
      for (const point of Array.isArray(s.points) ? s.points : []) {
        if (!isRecord(point)) continue;
        const x = String(point.x ?? "");
        xs.add(x);
        map.set(x, toNumber(point.y));
      }
      return map;
    });
    const order = seriesXOrder(xs);
    return [
      { name: "x", values: order },
      ...series.map((s, i) => ({ name: String(s.name ?? `series ${i + 1}`), values: order.map((x) => maps[i].get(x) ?? null) })),
    ];
  }
  if (shape === "distribution") {
    const items = (Array.isArray(raw.items) ? raw.items : []).filter(isRecord);
    return [
      { name: "label", values: items.map((item) => String(item.label ?? "")) },
      { name: "value", values: items.map((item) => toNumber(item.value)) },
    ];
  }
  if (shape === "outcomes") {
    const items = (Array.isArray(raw.items) ? raw.items : []).filter(isRecord);
    return [
      { name: "label", values: items.map((item) => String(item.label ?? "")) },
      ...(["success", "failed", "pending"] as const).map((name) => ({ name, values: items.map((item) => toNumber(item[name])) })),
    ];
  }
  if (shape === "rows") {
    const columns = (Array.isArray(raw.columns) ? raw.columns : []).filter(isRecord);
    const rows = (Array.isArray(raw.rows) ? raw.rows : []).filter(isRecord);
    return columns.map((column) => {
      const key = String(column.key ?? "");
      return { name: key, values: rows.map((row) => cellValue(row[key])) };
    });
  }
  return [];
}

// ── Formatting ───────────────────────────────────────────────────────────────

export interface NumberFormat {
  precision?: number;
  thousandSeparated?: boolean;
  unit?: string;
  unitPosition?: "before" | "after";
  abbreviate?: boolean;
}

const ABBREVIATIONS: [number, string][] = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"],
];

/** A number as text: rounded to `precision` decimals, optionally abbreviated (12.3K) and with
 *  thousand separators, its unit before or after it. */
export function formatNumber(value: number, format: NumberFormat = {}): string {
  if (!Number.isFinite(value)) return "";
  const precision = Math.min(6, Math.max(0, Math.trunc(format.precision ?? 0)));
  let scaled = value;
  let suffix = "";
  if (format.abbreviate) {
    const found = ABBREVIATIONS.find(([size]) => Math.abs(value) >= size);
    if (found) {
      scaled = value / found[0];
      suffix = found[1];
    }
  }
  // An abbreviated value keeps a decimal (1.2K), never a trailing zero (1K).
  const fixed = suffix ? Math.abs(scaled).toFixed(Math.max(1, precision)).replace(/\.?0+$/, "") : Math.abs(scaled).toFixed(precision);
  const [whole, decimals] = fixed.split(".");
  const grouped = format.thousandSeparated === false ? whole : whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = scaled < 0 && Number(`${whole}.${decimals ?? 0}`) !== 0 ? "-" : "";
  const number = `${sign}${grouped}${decimals ? `.${decimals}` : ""}${suffix}`;
  const unit = typeof format.unit === "string" ? format.unit.slice(0, 12) : "";
  if (!unit) return number;
  if (format.unitPosition === "before") return `${unit}${number}`;
  // A symbol sticks to the number (73%), a word is spaced from it (42 ms).
  return /^[^\p{L}]$/u.test(unit) ? `${number}${unit}` : `${number} ${unit}`;
}

function readNumberFormat(raw: unknown): NumberFormat {
  const config = isRecord(raw) ? raw : {};
  return {
    precision: typeof config.precision === "number" ? config.precision : undefined,
    thousandSeparated: typeof config.thousandSeparated === "boolean" ? config.thousandSeparated : undefined,
    unit: typeof config.unit === "string" ? config.unit : undefined,
    unitPosition: config.unitPosition === "before" ? "before" : "after",
    abbreviate: config.abbreviate === true,
  };
}

// ── Evaluation ───────────────────────────────────────────────────────────────

export interface DosScope {
  /** The visualization's data, as fields (`frameOf`). */
  primary?: Field[];
  annotation?: Field[];
  /** The visualization's options: a source may name one. */
  options?: Raw;
  /** The visualization's `context`: a formatter's config argument names one. */
  context?: Raw;
}

const text = (value: unknown): Cell =>
  typeof value === "number" && Number.isFinite(value) ? value : typeof value === "string" ? value.slice(0, MAX_TEXT) : null;

/** A literal option value as a data value: an array a series, a scalar a point. */
function literal(value: unknown): DosValue | undefined {
  if (Array.isArray(value)) return { kind: "series", field: { name: "", values: value.slice(0, MAX_VALUES).map(text) } };
  if (typeof value === "number" || typeof value === "string") return { kind: "point", value: text(value) };
  return undefined;
}

function resolveSource(name: string, scope: DosScope, depth: number): DosValue | undefined {
  if (name === "primary" || name === "table") return scope.primary ? { kind: "frame", fields: scope.primary } : undefined;
  if (name === "annotation") return scope.annotation ? { kind: "frame", fields: scope.annotation } : undefined;
  const options = scope.options ?? {};
  if (!Object.prototype.hasOwnProperty.call(options, name)) return undefined;
  const value = options[name];
  if (isExpression(value)) return depth < MAX_DEPTH ? evaluateAt(value, scope, depth + 1) : undefined;
  return literal(value);
}

function configOf(arg: Arg, scope: DosScope): unknown {
  if (arg.kind !== "ref") return undefined;
  const context = scope.context ?? {};
  return Object.prototype.hasOwnProperty.call(context, arg.name) ? context[arg.name] : undefined;
}

/** One formatter, on one cell. */
function formatCell(call: Call, cell: Cell, scope: DosScope): Cell {
  if (call.name === "prefix") {
    const prefix = call.args[0].kind === "string" ? call.args[0].value : "";
    return cell === null ? null : `${prefix}${cell}`.slice(0, MAX_TEXT);
  }
  const config = configOf(call.args[0], scope);
  if (call.name === "formatByType") {
    const n = toNumber(cell);
    return n !== null ? formatNumber(n, readNumberFormat(isRecord(config) ? config.number : undefined)) : cell;
  }
  if (call.name === "matchValue") {
    for (const rule of Array.isArray(config) ? config.slice(0, 50) : []) {
      if (isRecord(rule) && cell !== null && (rule.match === cell || String(rule.match) === String(cell))) return text(rule.value);
    }
    return null;
  }
  const n = toNumber(cell);
  if (n === null) return null;
  if (call.name === "rangeValue") {
    for (const rule of Array.isArray(config) ? config.slice(0, 50) : []) {
      if (!isRecord(rule)) continue;
      const from = typeof rule.from === "number" ? rule.from : -Infinity;
      const to = typeof rule.to === "number" ? rule.to : Infinity;
      if (n >= from && n < to) return text(rule.value);
    }
    return null;
  }
  // gradient: {stops: number[], colors: colour[]}, ascending stops.
  const gradient = isRecord(config) ? config : {};
  const stops = (Array.isArray(gradient.stops) ? gradient.stops : []).slice(0, 10).filter((s): s is number => typeof s === "number" && Number.isFinite(s));
  const colors = (Array.isArray(gradient.colors) ? gradient.colors : []).slice(0, 10).map(resolveColor);
  const count = Math.min(stops.length, colors.length);
  if (count === 0 || colors.slice(0, count).some((c) => c === undefined)) return null;
  const resolved = colors as string[];
  if (n <= stops[0]) return resolved[0];
  for (let i = 1; i < count; i++) {
    if (n <= stops[i]) {
      const span = stops[i] - stops[i - 1];
      return mixColor(resolved[i - 1], resolved[i], span > 0 ? (n - stops[i - 1]) / span : 1);
    }
  }
  return resolved[count - 1];
}

function numbers(field: Field): number[] {
  return field.values.map(toNumber).filter((n): n is number => n !== null);
}

function step(value: DosValue, call: Call, scope: DosScope): DosValue | undefined {
  const arg = call.args[0];
  switch (call.name) {
    case "seriesByName": {
      if (value.kind !== "frame" || arg.kind !== "string") return undefined;
      const field = value.fields.find((f) => f.name === arg.value);
      return field ? { kind: "series", field } : undefined;
    }
    case "seriesByIndex": {
      if (value.kind !== "frame" || arg.kind !== "number") return undefined;
      const field = value.fields[Math.trunc(arg.value)];
      return field ? { kind: "series", field } : undefined;
    }
    case "firstPoint":
    case "lastPoint": {
      if (value.kind !== "series" || value.field.values.length === 0) return undefined;
      const values = value.field.values;
      return { kind: "point", value: call.name === "firstPoint" ? values[0] : values[values.length - 1] };
    }
    case "delta": {
      if (value.kind !== "series" || arg.kind !== "number") return undefined;
      const values = value.field.values.map(toNumber);
      const at = Math.trunc(arg.value);
      const index = at < 0 ? values.length + at : at;
      const last = values[values.length - 1];
      const then = values[index];
      if (last == null || then == null || index < 0 || index >= values.length) return undefined;
      return { kind: "point", value: last - then };
    }
    case "min":
    case "max":
    case "sum": {
      if (value.kind !== "series") return undefined;
      const all = numbers(value.field);
      if (all.length === 0) return undefined;
      const result = call.name === "sum" ? all.reduce((a, b) => a + b, 0) : call.name === "min" ? Math.min(...all) : Math.max(...all);
      return { kind: "point", value: result };
    }
    default: {
      if (value.kind === "point") return { kind: "point", value: formatCell(call, value.value, scope) };
      if (value.kind === "series") {
        return { kind: "series", field: { name: value.field.name, values: value.field.values.map((cell) => formatCell(call, cell, scope)) } };
      }
      return undefined;
    }
  }
}

function evaluateAt(textValue: string, scope: DosScope, depth: number): DosValue | undefined {
  const parsed = parseExpression(textValue);
  if (!parsed.ok) return undefined;
  let value = resolveSource(parsed.expression.source, scope, depth);
  for (const call of parsed.expression.calls) {
    if (!value) return undefined;
    value = step(value, call, scope);
  }
  return value;
}

/** An expression's value on `scope`, or `undefined` when it does not read. Never throws. */
export function evaluate(expression: string, scope: DosScope): DosValue | undefined {
  return evaluateAt(expression, scope, 0);
}

/** An option as one value: an expression's point, or the literal itself. */
export function optionValue(value: unknown, scope: DosScope): Cell | undefined {
  const result = isExpression(value) ? evaluate(value, scope) : literal(value);
  return result?.kind === "point" ? result.value : undefined;
}

/** An option as a list of values: an expression's series, or a literal array. */
export function optionValues(value: unknown, scope: DosScope): Cell[] | undefined {
  const result = isExpression(value) ? evaluate(value, scope) : literal(value);
  return result?.kind === "series" ? result.field.values.slice(0, MAX_VALUES) : undefined;
}

/** An option as a colour (`resolveColor`), or `undefined`. */
export function optionColor(value: unknown, scope: DosScope): string | undefined {
  return resolveColor(optionValue(value, scope));
}

/** An option as one colour per point, or `undefined` when it is not a list. */
export function optionColors(value: unknown, scope: DosScope): (string | undefined)[] | undefined {
  return optionValues(value, scope)?.map(resolveColor);
}
