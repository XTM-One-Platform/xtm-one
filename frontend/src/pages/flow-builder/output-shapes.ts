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
 * The shape of what a node publishes, so the builder can offer
 * `{{step.fields.severity}}` rather than leaving users to guess the key.
 *
 * Two sources, merged:
 * - the DECLARED structure, resolved by the server: an agent's JSON output
 *   schema (or the Structured output node wired into it), a manual
 *   trigger's parameters, an event type's fields;
 * - the OBSERVED one: the node's output in the flow's last run, every item
 *   of a list merged (n8n's schema view does the same).
 *
 * The declared structure wins on a conflict: it is the contract the next run
 * follows, where the last run may predate a schema change.
 *
 * No React and no `@/` import, so `output-shapes.test.ts` runs under
 * `node --test`.
 */

export type ShapeType =
  | "text"
  | "number"
  | "boolean"
  | "object"
  | "list"
  | "null"
  | "any";

export interface Shape {
  type: ShapeType;
  /** An object's keys. */
  fields?: Record<string, Shape>;
  /** A list's items, every item merged into one shape. */
  items?: Shape;
  description?: string;
  /** A scalar value the last run produced, shortened. */
  sample?: string | number | boolean | null;
  /** Part of a declared structure. */
  declared?: boolean;
  /** Seen in the last run. */
  observed?: boolean;
}

/** Bounds: a 5 MB payload must not freeze the drawer. */
const MAX_DEPTH = 5;
const MAX_KEYS = 60;
const MAX_ITEMS_SCANNED = 20;
const MAX_SAMPLE_CHARS = 80;
const MAX_PATHS = 80;

/** `{{node.a.b}}` splits on dots and stops at `}}`: such a key cannot be read. */
const ADDRESSABLE_KEY = /^[^.{}\s]+$/;

// ── From a value (the last run) ─────────────────────────────────────────────

export function shapeOfValue(value: unknown, depth = 0): Shape {
  if (value === null || value === undefined) {
    return { type: "null", sample: null, observed: true };
  }
  if (typeof value === "string") {
    const sample =
      value.length > MAX_SAMPLE_CHARS ? `${value.slice(0, MAX_SAMPLE_CHARS)}…` : value;
    return { type: "text", sample, observed: true };
  }
  if (typeof value === "number") return { type: "number", sample: value, observed: true };
  if (typeof value === "boolean") return { type: "boolean", sample: value, observed: true };
  if (Array.isArray(value)) {
    const shape: Shape = { type: "list", observed: true };
    if (depth < MAX_DEPTH) {
      let items: Shape | undefined;
      for (const item of value.slice(0, MAX_ITEMS_SCANNED)) {
        items = mergeShapes(items, shapeOfValue(item, depth + 1));
      }
      if (items) shape.items = items;
    }
    return shape;
  }
  if (typeof value === "object") {
    const shape: Shape = { type: "object", observed: true };
    if (depth < MAX_DEPTH) {
      const fields: Record<string, Shape> = {};
      for (const [key, child] of Object.entries(value as Record<string, unknown>).slice(
        0,
        MAX_KEYS,
      )) {
        fields[key] = shapeOfValue(child, depth + 1);
      }
      shape.fields = fields;
    }
    return shape;
  }
  return { type: "any", observed: true };
}

// ── From a JSON Schema (declared) ───────────────────────────────────────────

const SCHEMA_TYPES: Record<string, ShapeType> = {
  string: "text",
  number: "number",
  integer: "number",
  boolean: "boolean",
  object: "object",
  array: "list",
  null: "null",
};

export function shapeOfSchema(schema: unknown, depth = 0): Shape {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
    return { type: "any", declared: true };
  }
  const node = schema as Record<string, unknown>;
  const description =
    typeof node.description === "string" && node.description.trim()
      ? node.description.trim()
      : undefined;
  // `"type": ["string", "null"]` is a nullable string.
  const rawTypes = Array.isArray(node.type) ? node.type : [node.type];
  const named = rawTypes.filter((t): t is string => typeof t === "string" && t !== "null");
  let type: ShapeType =
    named.length === 1
      ? (SCHEMA_TYPES[named[0]] ?? "any")
      : !named.length && rawTypes.includes("null")
        ? "null"
        : "any";
  if (!named.length && node.properties && typeof node.properties === "object") type = "object";
  if (!named.length && node.items !== undefined) type = "list";

  // A union without a type of its own: its branches, merged.
  const branches = [node.anyOf, node.oneOf].find(Array.isArray) as unknown[] | undefined;
  if (!named.length && type === "any" && branches?.length) {
    let merged: Shape | undefined;
    for (const branch of branches) {
      const branchShape = shapeOfSchema(branch, depth);
      if (branchShape.type === "null") continue;
      merged = mergeShapes(merged, branchShape);
    }
    if (merged) return { ...merged, description: description ?? merged.description };
  }

  const shape: Shape = { type, declared: true };
  if (description) shape.description = description;
  if (depth >= MAX_DEPTH) return shape;
  if (type === "object" && node.properties && typeof node.properties === "object") {
    const fields: Record<string, Shape> = {};
    for (const [key, child] of Object.entries(node.properties as Record<string, unknown>).slice(
      0,
      MAX_KEYS,
    )) {
      fields[key] = shapeOfSchema(child, depth + 1);
    }
    shape.fields = fields;
  }
  if (type === "list" && node.items && typeof node.items === "object") {
    shape.items = shapeOfSchema(node.items, depth + 1);
  }
  return shape;
}

// ── Merging ─────────────────────────────────────────────────────────────────

/**
 * One shape holding both. `prefer` decides a type conflict: `"first"` keeps
 * the first shape (declared over observed), `"union"` widens to `any`
 * (two items of one list that disagree).
 */
export function mergeShapes(
  a: Shape | undefined,
  b: Shape | undefined,
  prefer: "first" | "union" = "union",
): Shape {
  if (!a) return b ?? { type: "any" };
  if (!b) return a;
  const extras = {
    description: a.description ?? b.description,
    declared: a.declared || b.declared,
    observed: a.observed || b.observed,
  };
  // A null says nothing about the type: the other side's wins.
  if (a.type === "null" && b.type !== "null") return compact({ ...b, ...extras });
  if (b.type === "null" && a.type !== "null") return compact({ ...a, ...extras });
  if (a.type !== b.type) {
    if (prefer === "union") return compact({ type: "any", ...extras });
    // A declared `any` (a `json` parameter, a schema property without a
    // type) says nothing of the structure: what the run produced fills it.
    return compact(a.type === "any" ? { ...b, ...extras } : { ...a, ...extras });
  }
  const merged: Shape = {
    type: a.type,
    ...extras,
    sample: a.sample !== undefined ? a.sample : b.sample,
  };
  if (a.fields || b.fields) {
    const fields: Record<string, Shape> = { ...(b.fields ?? {}) };
    for (const [key, shape] of Object.entries(a.fields ?? {})) {
      fields[key] = mergeShapes(shape, b.fields?.[key], prefer);
    }
    merged.fields = fields;
  }
  if (a.items || b.items) merged.items = mergeShapes(a.items, b.items, prefer);
  return compact(merged);
}

/** Drops the unset optional keys, so two equal shapes compare equal. */
function compact(shape: Shape): Shape {
  const out = { ...shape };
  if (out.description === undefined) delete out.description;
  if (out.sample === undefined) delete out.sample;
  if (!out.declared) delete out.declared;
  if (!out.observed) delete out.observed;
  return out;
}

// ── Paths ───────────────────────────────────────────────────────────────────

interface ShapePath {
  /** Dotted path below the output, e.g. `severity` or `hits.0.title`. */
  path: string;
  shape: Shape;
}

/**
 * Every readable path below *shape*, parents before children. A list offers
 * its first item (`hits.0.title`), the index the run-time lookup accepts.
 */
export function shapePaths(shape: Shape, limit = MAX_PATHS): ShapePath[] {
  const out: ShapePath[] = [];
  const walk = (current: Shape, prefix: string, depth: number) => {
    if (depth > MAX_DEPTH || out.length >= limit) return;
    if (current.type === "object" && current.fields) {
      for (const [key, child] of Object.entries(current.fields)) {
        if (!ADDRESSABLE_KEY.test(key)) continue;
        if (out.length >= limit) return;
        const path = prefix ? `${prefix}.${key}` : key;
        out.push({ path, shape: child });
        walk(child, path, depth + 1);
      }
    } else if (current.type === "list" && current.items) {
      const items = current.items;
      if (items.type !== "object" && items.type !== "list") return;
      walk(items, prefix ? `${prefix}.0` : "0", depth + 1);
    }
  };
  walk(shape, "", 0);
  return out;
}

// ── Per node ────────────────────────────────────────────────────────────────

/**
 * Output name → its declared shape. The schemas come from the server
 * (`POST /flows/output-schemas`), which applies the run's own rules: which
 * agent answers in JSON, which wired schema wins, what a trigger or an event
 * type declares. Never re-spelled here.
 */
export function declaredOutputShapes(
  schemas: Record<string, unknown> | undefined,
): Record<string, Shape> {
  const out: Record<string, Shape> = {};
  for (const [name, schema] of Object.entries(schemas ?? {})) {
    out[name] = shapeOfSchema(schema);
  }
  return out;
}

/** Output name → its observed shape. `_`-prefixed keys are walker bookkeeping. */
export function observedOutputShapes(
  output: Record<string, unknown> | null | undefined,
): Record<string, Shape> {
  const out: Record<string, Shape> = {};
  for (const [name, value] of Object.entries(output ?? {})) {
    if (name.startsWith("_")) continue;
    out[name] = shapeOfValue(value);
  }
  return out;
}

/** Declared and observed, merged per output. */
export function nodeOutputShapes(
  declared: Record<string, Shape>,
  observed: Record<string, Shape>,
): Record<string, Shape> {
  const out: Record<string, Shape> = {};
  for (const name of new Set([...Object.keys(declared), ...Object.keys(observed)])) {
    out[name] = mergeShapes(declared[name], observed[name], "first");
  }
  return out;
}
