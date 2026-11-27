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
 * The absolute layout of a dashboard: a canvas in pixels,
 * and every edit the studio makes to it.
 *
 * A tab whose layout is `absolute` (`backend/app/services/dashboard_canvas.py`)
 * has a size, a way to fit the screen (`display`), a background, and places
 * blocks at `{x, y, w, h}` and lines at `{from, to}` - each end a point or a
 * connection to the middle of a side of a block (`{item, port}`). A block is a
 * visualization drawing data or content (a widget) or an element drawing none:
 * a rectangle, an ellipse, an icon, an image, markdown. The structure's order
 * is the stacking order: the last entry is drawn on top.
 *
 * Reading is defensive like `spec.ts` (nothing throws, what is not understood
 * is left out); every edit is a pure function from a definition to a new one,
 * so the studio keeps its undo history as a list of definitions
 * (`History<T>`) and saves the difference (`definitionOperations`). The
 * backend validates what is saved.
 *
 * Pure, with no import: tested on `node --test`.
 */

type Raw = Record<string, unknown>;

const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const record = (value: unknown): Raw => (isRecord(value) ? value : {});
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** The largest canvas, in pixels (`MAX_CANVAS`). */
export const MAX_CANVAS = 10_000;
const DEFAULT_CANVAS = { width: 1440, height: 960 } as const;
/** A canvas never shrinks below this (`ge=100`). */
export const MIN_CANVAS = 100;
/** How far an arrow key moves a block, and with Shift. */
export const NUDGE = 10;
export const FINE_NUDGE = 1;
/** Where a pasted copy lands, from its original. */
export const PASTE_OFFSET = 20;

export const DISPLAY_MODES = ["auto", "actual-size", "fit-to-width"] as const;
export type CanvasDisplay = (typeof DISPLAY_MODES)[number];
const PORTS = ["n", "e", "s", "w"] as const;
export type Port = (typeof PORTS)[number];

/** The element kinds a canvas holds besides widgets, and their visualization types. */
const ELEMENT_TYPES = {
  rectangle: "xtm.shape.rectangle",
  ellipse: "xtm.shape.ellipse",
  line: "xtm.shape.line",
  icon: "xtm.icon",
  image: "xtm.image",
  markdown: "xtm.markdown",
} as const;
export type ElementKind = keyof typeof ELEMENT_TYPES;
const TYPE_TO_KIND: Record<string, ElementKind> = Object.fromEntries(
  Object.entries(ELEMENT_TYPES).map(([kind, type]) => [type, kind as ElementKind]),
);

/** Whether a visualization type is a canvas element (drawn by the canvas, not as a widget). */
export function isElementType(type: unknown): boolean {
  return typeof type === "string" && type in TYPE_TO_KIND;
}

interface CanvasBackgroundImage {
  fileId: string;
  sizeType: "auto" | "contain" | "cover";
  opacity: number;
}

export interface CanvasOptions {
  width: number;
  height: number;
  display: CanvasDisplay;
  backgroundColor?: string;
  backgroundImage?: CanvasBackgroundImage;
}

export type DashStyle = "solid" | "dashed" | "dotted";

interface Stroke {
  strokeColor?: string;
  strokeWidth: number;
  strokeOpacity: number;
  strokeDashStyle: DashStyle;
}

export type CanvasElement =
  | ({ kind: "rectangle"; fillColor?: string; fillOpacity: number; rx: number } & Stroke)
  | ({ kind: "ellipse"; fillColor?: string; fillOpacity: number } & Stroke)
  | ({ kind: "line"; fromArrow: boolean; toArrow: boolean } & Stroke)
  | { kind: "icon"; icon?: string; fileId?: string; color?: string; opacity: number; alt?: string }
  | { kind: "image"; fileId?: string; alt?: string; fit: "contain" | "cover" | "fill"; opacity: number }
  | {
      kind: "markdown";
      markdown: string;
      fontColor?: string;
      backgroundColor?: string;
      fontSize: "small" | "default" | "large" | "extraLarge";
      fontFamily: "sans" | "serif" | "mono";
      textAlign: "left" | "center" | "right";
    };

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}
export interface Point {
  x: number;
  y: number;
}
export type LineEnd = Point | { item: string; port: Port };

interface CanvasBlock extends Box {
  id: string;
  type: "block";
  /** A canvas element; none for a widget (drawn from the dashboard's widgets by its id). */
  element?: CanvasElement;
}

interface CanvasLine {
  id: string;
  type: "line";
  from: LineEnd;
  to: LineEnd;
  element: Extract<CanvasElement, { kind: "line" }>;
}

export type CanvasItem = CanvasBlock | CanvasLine;

/** A tab's canvas: its options and what it places, bottom first. */
export interface CanvasModel {
  options: CanvasOptions;
  items: CanvasItem[];
}

// ── Reading ──────────────────────────────────────────────────────────────────

const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;
const FILE_ID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const ICON_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

const color = (value: unknown): string | undefined => (typeof value === "string" && HEX_RE.test(value) ? value : undefined);
const fileId = (value: unknown): string | undefined =>
  typeof value === "string" && FILE_ID_RE.test(value) ? value.toLowerCase() : undefined;
const text = (value: unknown, max: number): string | undefined =>
  typeof value === "string" && value.trim() ? value.slice(0, max) : undefined;
function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}
function int(value: unknown, min: number, max: number, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, Math.round(value))) : fallback;
}
function unit(value: unknown, fallback = 1): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : fallback;
}

/** An absolute layout's options, read defensively, defaults filled. */
function readCanvasOptions(raw: unknown): CanvasOptions {
  const options = record(raw);
  const image = record(options.backgroundImage);
  const imageId = fileId(image.fileId);
  return {
    width: int(options.width, MIN_CANVAS, MAX_CANVAS, DEFAULT_CANVAS.width),
    height: int(options.height, MIN_CANVAS, MAX_CANVAS, DEFAULT_CANVAS.height),
    display: oneOf(options.display, DISPLAY_MODES, "auto"),
    backgroundColor: color(options.backgroundColor),
    backgroundImage: imageId
      ? { fileId: imageId, sizeType: oneOf(image.sizeType, ["auto", "contain", "cover"] as const, "cover"), opacity: unit(image.opacity) }
      : undefined,
  };
}

function stroke(options: Raw): Stroke {
  return {
    strokeColor: color(options.strokeColor),
    strokeWidth: int(options.strokeWidth, 0, 50, 1),
    strokeOpacity: unit(options.strokeOpacity),
    strokeDashStyle: oneOf(options.strokeDashStyle, ["solid", "dashed", "dotted"] as const, "solid"),
  };
}

/** A canvas element of visualization `type`, read defensively; `undefined` for a type that is none. */
export function readElement(type: unknown, raw: unknown): CanvasElement | undefined {
  const kind = typeof type === "string" ? TYPE_TO_KIND[type] : undefined;
  const options = record(raw);
  switch (kind) {
    case "rectangle":
      return { kind, ...stroke(options), fillColor: color(options.fillColor), fillOpacity: unit(options.fillOpacity), rx: int(options.rx, 0, 1000, 0) };
    case "ellipse":
      return { kind, ...stroke(options), fillColor: color(options.fillColor), fillOpacity: unit(options.fillOpacity) };
    case "line":
      return { kind, ...stroke(options), fromArrow: options.fromArrow === true, toArrow: options.toArrow === true };
    case "icon": {
      const icon = typeof options.icon === "string" && ICON_RE.test(options.icon) ? options.icon : undefined;
      return { kind, icon, fileId: icon ? undefined : fileId(options.fileId), color: color(options.color), opacity: unit(options.opacity), alt: text(options.alt, 300) };
    }
    case "image":
      return { kind, fileId: fileId(options.fileId), alt: text(options.alt, 300), fit: oneOf(options.fit, ["contain", "cover", "fill"] as const, "contain"), opacity: unit(options.opacity) };
    case "markdown":
      return {
        kind,
        markdown: typeof options.markdown === "string" ? options.markdown.slice(0, 5000) : "",
        fontColor: color(options.fontColor),
        backgroundColor: color(options.backgroundColor),
        fontSize: oneOf(options.fontSize, ["small", "default", "large", "extraLarge"] as const, "default"),
        fontFamily: oneOf(options.fontFamily, ["sans", "serif", "mono"] as const, "sans"),
        textAlign: oneOf(options.textAlign, ["left", "center", "right"] as const, "left"),
      };
    default:
      return undefined;
  }
}

function readBox(raw: unknown): Box | undefined {
  const p = record(raw);
  if (![p.x, p.y, p.w, p.h].every((v) => typeof v === "number" && Number.isFinite(v))) return undefined;
  return {
    x: int(p.x, 0, MAX_CANVAS, 0),
    y: int(p.y, 0, MAX_CANVAS * 4, 0),
    w: int(p.w, 1, MAX_CANVAS, 1),
    h: int(p.h, 1, MAX_CANVAS, 1),
  };
}

function readEnd(raw: unknown): LineEnd | undefined {
  const end = record(raw);
  if (typeof end.item === "string") return { item: end.item, port: oneOf(end.port, PORTS, "e") };
  if (typeof end.x === "number" && typeof end.y === "number") {
    return { x: int(end.x, 0, MAX_CANVAS, 0), y: int(end.y, 0, MAX_CANVAS, 0) };
  }
  return undefined;
}

/**
 * A layout definition's canvas: its options and its items in stacking order.
 * `isWidget(id)` says whether a block that is no element is drawn (a widget the
 * renderer knows); the rest are left out, like a line whose ends are unreadable.
 */
export function readCanvas(layout: unknown, visualizations: unknown, isWidget: (id: string) => boolean): CanvasModel {
  const definition = record(layout);
  const vizs = record(visualizations);
  const items: CanvasItem[] = [];
  const seen = new Set<string>();
  for (const raw of Array.isArray(definition.structure) ? definition.structure : []) {
    const entry = record(raw);
    const id = entry.item;
    if (typeof id !== "string" || seen.has(id)) continue;
    const viz = record(vizs[id]);
    const element = readElement(viz.type, viz.options);
    if (entry.type === "line") {
      const position = record(entry.position);
      const from = readEnd(position.from);
      const to = readEnd(position.to);
      if (element?.kind !== "line" || !from || !to) continue;
      seen.add(id);
      items.push({ id, type: "line", from, to, element });
      continue;
    }
    if ((entry.type ?? "block") !== "block") continue;
    const box = readBox(entry.position);
    if (!box || element?.kind === "line" || (!element && !isWidget(id))) continue;
    seen.add(id);
    items.push({ id, type: "block", ...box, element });
  }
  return { options: readCanvasOptions(definition.options), items };
}

// ── Geometry ─────────────────────────────────────────────────────────────────

/**
 * How much the canvas is scaled to be shown in a `viewport` (its container).
 * `auto` scales down to the viewport (never up), `actual-size` shows its pixels
 * (scrolled), `fit-to-width` scales to the viewport's width, up or down. A
 * `zoom` (the editor's) replaces the display's scale.
 */
export function canvasScale(options: Pick<CanvasOptions, "width" | "height" | "display">, viewport: { width: number; height: number }, zoom?: number | null): number {
  if (typeof zoom === "number" && zoom > 0) return zoom;
  const byWidth = viewport.width > 0 ? viewport.width / options.width : 1;
  const byHeight = viewport.height > 0 ? viewport.height / options.height : byWidth;
  switch (options.display) {
    case "actual-size":
      return 1;
    case "fit-to-width":
      return byWidth;
    default:
      return Math.min(1, byWidth, byHeight);
  }
}

/** The middle of a side of a box. */
function portPoint(box: Box, port: Port): Point {
  switch (port) {
    case "n":
      return { x: box.x + Math.floor(box.w / 2), y: box.y };
    case "e":
      return { x: box.x + box.w, y: box.y + Math.floor(box.h / 2) };
    case "s":
      return { x: box.x + Math.floor(box.w / 2), y: box.y + box.h };
    default:
      return { x: box.x, y: box.y + Math.floor(box.h / 2) };
  }
}

export const isConnection = (end: LineEnd): end is { item: string; port: Port } => "item" in end;

/** Where a line end is: its point, or the port of the block it holds to (the origin when that block is gone). */
export function endPoint(end: LineEnd, blocks: ReadonlyMap<string, Box>): Point {
  if (!isConnection(end)) return end;
  const box = blocks.get(end.item);
  return box ? portPoint(box, end.port) : { x: 0, y: 0 };
}

export function blockMap(items: readonly CanvasItem[]): Map<string, Box> {
  return new Map(items.filter((item): item is CanvasBlock => item.type === "block").map((item) => [item.id, item]));
}

/** The box an item covers (a line: the box of its two ends). */
function itemBox(item: CanvasItem, blocks: ReadonlyMap<string, Box>): Box {
  if (item.type === "block") return { x: item.x, y: item.y, w: item.w, h: item.h };
  const a = endPoint(item.from, blocks);
  const b = endPoint(item.to, blocks);
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
}

/** The items a rectangle drawn on the canvas (a marquee) touches. */
export function itemsIn(items: readonly CanvasItem[], area: Box): string[] {
  const blocks = blockMap(items);
  return items
    .filter((item) => {
      const box = itemBox(item, blocks);
      return box.x <= area.x + area.w && box.x + box.w >= area.x && box.y <= area.y + area.h && box.y + box.h >= area.y;
    })
    .map((item) => item.id);
}

/** The port of a block nearest to a point, within `reach` pixels; `null` when none is. */
export function nearestPort(items: readonly CanvasItem[], point: Point, reach: number, except?: string): { item: string; port: Port } | null {
  let best: { item: string; port: Port; distance: number } | null = null;
  for (const item of items) {
    if (item.type !== "block" || item.id === except) continue;
    for (const port of PORTS) {
      const at = portPoint(item, port);
      const distance = Math.hypot(at.x - point.x, at.y - point.y);
      if (distance <= reach && (!best || distance < best.distance)) best = { item: item.id, port, distance };
    }
  }
  return best ? { item: best.item, port: best.port } : null;
}

// ── Previews ─────────────────────────────────────────────────────────────────
//
// While a drag is under way the stage shows the items where they would land;
// the definition changes once, when the drag ends.

/** Items moved by `(dx, dy)`: the blocks, and the free ends of the lines (held ends follow their block). */
export function shiftItems(items: readonly CanvasItem[], ids: ReadonlySet<string>, dx: number, dy: number): CanvasItem[] {
  if (dx === 0 && dy === 0) return [...items];
  return items.map((item) => {
    if (!ids.has(item.id)) return item;
    if (item.type === "block") return { ...item, x: item.x + dx, y: item.y + dy };
    const shift = (end: LineEnd): LineEnd => (isConnection(end) ? end : { x: end.x + dx, y: end.y + dy });
    return { ...item, from: shift(item.from), to: shift(item.to) };
  });
}

/** Items with one block at `box`. */
export function withBox(items: readonly CanvasItem[], id: string, box: Box): CanvasItem[] {
  return items.map((item) => (item.id === id && item.type === "block" ? { ...item, ...box } : item));
}

/** Items with one end of a line at `end`. */
export function withLineEnd(items: readonly CanvasItem[], id: string, which: "from" | "to", end: LineEnd): CanvasItem[] {
  return items.map((item) => (item.id === id && item.type === "line" ? { ...item, [which]: end } : item));
}

export type Handle = "n" | "ne" | "e" | "se" | "s" | "sw" | "w" | "nw";
export const HANDLES: readonly Handle[] = ["n", "ne", "e", "se", "s", "sw", "w", "nw"];
const MIN_SIDE = 8;

/** A box resized by dragging one of its handles by `(dx, dy)`, never under `MIN_SIDE` pixels. */
export function resizeBox(box: Box, handle: Handle, dx: number, dy: number): Box {
  let { x, y, w, h } = box;
  if (handle.includes("e")) w = Math.max(MIN_SIDE, w + dx);
  if (handle.includes("s")) h = Math.max(MIN_SIDE, h + dy);
  if (handle.includes("w")) {
    const next = Math.max(MIN_SIDE, w - dx);
    x += w - next;
    w = next;
  }
  if (handle.includes("n")) {
    const next = Math.max(MIN_SIDE, h - dy);
    y += h - next;
    h = next;
  }
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

/**
 * The grid size (`WidgetSize`) a widget drawn in a box of `h` pixels gets, so
 * it sizes itself as on the grid (rows of 20 px, 16 px apart; columns of a
 * 1440 px page).
 */
export function cellOfBox(box: { w: number; h: number }): { cols: number; rows: number } {
  return { cols: Math.max(1, Math.round(box.w / 120)), rows: Math.max(1, (box.h + 16) / 36) };
}

// ── Editing ──────────────────────────────────────────────────────────────────
//
// Each edit takes the definition (as the API returned it) and returns a new
// one; nothing is mutated. A layout is named by its id (`layoutId`).

function layoutOf(definition: Raw, layoutId: string): Raw | undefined {
  const layout = record(record(definition.layout).layoutDefinitions)[layoutId];
  return isRecord(layout) ? layout : undefined;
}

function structureOf(layout: Raw): Raw[] {
  return Array.isArray(layout.structure) ? (layout.structure.filter(isRecord) as Raw[]) : [];
}

/** A copy of the definition with one layout's structure replaced. */
function withStructure(definition: Raw, layoutId: string, structure: Raw[]): Raw {
  const next = clone(definition);
  const layout = layoutOf(next, layoutId);
  if (layout) layout.structure = structure;
  return next;
}

function blocksOf(structure: readonly Raw[]): Map<string, Box> {
  const blocks = new Map<string, Box>();
  for (const entry of structure) {
    if ((entry.type ?? "block") !== "block" || typeof entry.item !== "string") continue;
    const box = readBox(entry.position);
    if (box) blocks.set(entry.item, box);
  }
  return blocks;
}

function clampBox(box: Box, canvas: CanvasOptions): Box {
  const w = Math.min(Math.max(1, Math.round(box.w)), MAX_CANVAS);
  const h = Math.min(Math.max(1, Math.round(box.h)), MAX_CANVAS);
  return {
    x: Math.min(Math.max(0, Math.round(box.x)), Math.max(0, canvas.width - w)),
    y: Math.min(Math.max(0, Math.round(box.y)), Math.max(0, canvas.height - h)),
    w,
    h,
  };
}

function clampPoint(point: Point, canvas: CanvasOptions): Point {
  return {
    x: Math.min(Math.max(0, Math.round(point.x)), canvas.width),
    y: Math.min(Math.max(0, Math.round(point.y)), canvas.height),
  };
}

/** Every id the definition already uses for a visualization, an input or a data source. */
function takenIds(definition: Raw): Set<string> {
  return new Set([
    ...Object.keys(record(definition.visualizations)),
    ...Object.keys(record(definition.inputs)),
    ...Object.keys(record(definition.dataSources)),
  ]);
}

/** A new id from `base` (`rectangle`, `rectangle_2`, ...), not in `taken`; added to it. */
export function freshId(taken: Set<string>, base: string): string {
  const stem = (base.replace(/[^A-Za-z0-9_.-]/g, "_").replace(/^[^A-Za-z0-9]+/, "") || "item").slice(0, 50);
  let id = stem;
  for (let n = 2; taken.has(id); n += 1) id = `${stem}_${n}`;
  taken.add(id);
  return id;
}

/** Move items by `(dx, dy)`, kept on the canvas: a line's free ends move with it, its held ends stay. */
export function moveItems(definition: Raw, layoutId: string, ids: readonly string[], dx: number, dy: number): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout || ids.length === 0 || (dx === 0 && dy === 0)) return definition;
  const canvas = readCanvasOptions(layout.options);
  const moving = new Set(ids);
  const structure = structureOf(layout).map((entry) => {
    if (typeof entry.item !== "string" || !moving.has(entry.item)) return entry;
    if (entry.type === "line") {
      const position = record(entry.position);
      const shift = (end: unknown) => {
        const read = readEnd(end);
        return read && !isConnection(read) ? clampPoint({ x: read.x + dx, y: read.y + dy }, canvas) : end;
      };
      return { ...entry, position: { from: shift(position.from), to: shift(position.to) } };
    }
    const box = readBox(entry.position);
    return box ? { ...entry, position: clampBox({ ...box, x: box.x + dx, y: box.y + dy }, canvas) } : entry;
  });
  return withStructure(definition, layoutId, structure);
}

/** Place one block at `box` (a drag of a handle, the position fields), kept on the canvas. */
export function setBox(definition: Raw, layoutId: string, id: string, box: Box): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return definition;
  const canvas = readCanvasOptions(layout.options);
  const structure = structureOf(layout).map((entry) =>
    entry.item === id && (entry.type ?? "block") === "block" ? { ...entry, position: clampBox(box, canvas) } : entry,
  );
  return withStructure(definition, layoutId, structure);
}

/** Grow or shrink blocks by `(dw, dh)` (Alt + an arrow), never below a pixel. */
export function resizeItems(definition: Raw, layoutId: string, ids: readonly string[], dw: number, dh: number): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return definition;
  let next = definition;
  for (const [id, box] of blocksOf(structureOf(layout))) {
    if (ids.includes(id)) next = setBox(next, layoutId, id, { ...box, w: Math.max(1, box.w + dw), h: Math.max(1, box.h + dh) });
  }
  return next;
}

/** Set one end of a line: a point, or a connection to a block's port. */
export function setLineEnd(definition: Raw, layoutId: string, id: string, which: "from" | "to", end: LineEnd): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return definition;
  const canvas = readCanvasOptions(layout.options);
  const value = isConnection(end) ? { item: end.item, port: end.port } : clampPoint(end, canvas);
  const structure = structureOf(layout).map((entry) =>
    entry.item === id && entry.type === "line" ? { ...entry, position: { ...record(entry.position), [which]: value } } : entry,
  );
  return withStructure(definition, layoutId, structure);
}

/** Place blocks of a grid layout (the grid's drag or resize: `{id, x, y, w, h}` in cells). */
export function placeBlocks(definition: Raw, layoutId: string, places: readonly ({ id: string } & Box)[]): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return definition;
  const byId = new Map(places.map((place) => [place.id, place]));
  const structure = structureOf(layout).map((entry) => {
    const place = byId.get(entry.item as string);
    return place && (entry.type ?? "block") === "block" ? { ...entry, position: { x: place.x, y: place.y, w: place.w, h: place.h } } : entry;
  });
  return withStructure(definition, layoutId, structure);
}

export type AlignMode = "left" | "center" | "right" | "top" | "middle" | "bottom";

/** Align blocks on the box around them (two or more; lines are left where they are). */
export function alignItems(definition: Raw, layoutId: string, ids: readonly string[], mode: AlignMode): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return definition;
  const blocks = [...blocksOf(structureOf(layout))].filter(([id]) => ids.includes(id));
  if (blocks.length < 2) return definition;
  const left = Math.min(...blocks.map(([, b]) => b.x));
  const right = Math.max(...blocks.map(([, b]) => b.x + b.w));
  const top = Math.min(...blocks.map(([, b]) => b.y));
  const bottom = Math.max(...blocks.map(([, b]) => b.y + b.h));
  let next = definition;
  for (const [id, b] of blocks) {
    const box = { ...b };
    if (mode === "left") box.x = left;
    if (mode === "right") box.x = right - b.w;
    if (mode === "center") box.x = Math.round((left + right) / 2 - b.w / 2);
    if (mode === "top") box.y = top;
    if (mode === "bottom") box.y = bottom - b.h;
    if (mode === "middle") box.y = Math.round((top + bottom) / 2 - b.h / 2);
    next = setBox(next, layoutId, id, box);
  }
  return next;
}

/** Space blocks evenly between the first and the last along an axis (three or more). */
export function distributeItems(definition: Raw, layoutId: string, ids: readonly string[], axis: "horizontal" | "vertical"): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return definition;
  const start = axis === "horizontal" ? "x" : "y";
  const size = axis === "horizontal" ? "w" : "h";
  const blocks = [...blocksOf(structureOf(layout))]
    .filter(([id]) => ids.includes(id))
    .sort(([, a], [, b]) => a[start] - b[start]);
  if (blocks.length < 3) return definition;
  const first = blocks[0][1];
  const last = blocks[blocks.length - 1][1];
  const span = last[start] + last[size] - first[start];
  const used = blocks.reduce((sum, [, b]) => sum + b[size], 0);
  const gap = (span - used) / (blocks.length - 1);
  let next = definition;
  let cursor = first[start];
  for (const [id, b] of blocks) {
    next = setBox(next, layoutId, id, { ...b, [start]: Math.round(cursor) });
    cursor += b[size] + gap;
  }
  return next;
}

export type LayerMove = "front" | "back" | "forward" | "backward";

/** Bring items to the front (last drawn) or send them to the back, all the way or one step. */
export function reorderItems(definition: Raw, layoutId: string, ids: readonly string[], move: LayerMove): Raw {
  const layout = layoutOf(definition, layoutId);
  if (!layout || ids.length === 0) return definition;
  const structure = structureOf(layout);
  const chosen = new Set(ids);
  const picked = structure.filter((e) => chosen.has(e.item as string));
  const rest = structure.filter((e) => !chosen.has(e.item as string));
  if (move === "front") return withStructure(definition, layoutId, [...rest, ...picked]);
  if (move === "back") return withStructure(definition, layoutId, [...picked, ...rest]);
  const order = [...structure];
  const step = move === "forward" ? 1 : -1;
  const indices = order.map((e, i) => (chosen.has(e.item as string) ? i : -1)).filter((i) => i >= 0);
  for (const i of step > 0 ? indices.reverse() : indices) {
    const j = i + step;
    if (j < 0 || j >= order.length || chosen.has(order[j].item as string)) continue;
    [order[i], order[j]] = [order[j], order[i]];
  }
  return withStructure(definition, layoutId, order);
}

/** Turn every line end held to one of `ids` into the point it is at (they are leaving the layout). */
function detach(structure: Raw[], ids: ReadonlySet<string>): Raw[] {
  const blocks = blocksOf(structure);
  return structure.map((entry) => {
    if (entry.type !== "line") return entry;
    const position = { ...record(entry.position) };
    for (const key of ["from", "to"] as const) {
      const end = readEnd(position[key]);
      if (end && isConnection(end) && ids.has(end.item)) position[key] = endPoint(end, blocks);
    }
    return { ...entry, position };
  });
}

/**
 * Remove items: their visualizations, their places in every layout, and a data
 * source one of them brought along (`ds_<id>`) that nothing reads any more.
 */
export function removeItems(definition: Raw, ids: readonly string[]): Raw {
  if (ids.length === 0) return definition;
  const next = clone(definition);
  const gone = new Set(ids);
  const visualizations = record(next.visualizations);
  const sources = record(next.dataSources);
  const dropped: string[] = [];
  for (const id of ids) {
    const sid = record(record(visualizations[id]).dataSources).primary;
    if (sid === `ds_${id}`) dropped.push(sid);
    delete visualizations[id];
  }
  for (const layout of Object.values(record(record(next.layout).layoutDefinitions))) {
    if (!isRecord(layout)) continue;
    layout.structure = detach(structureOf(layout), gone).filter((e) => !gone.has(e.item as string));
  }
  const readers = new Set([
    ...Object.values(visualizations).map((v) => record(record(v).dataSources).primary),
    ...Object.values(record(next.inputs)).map((v) => record(record(v).dataSources).primary),
  ]);
  for (const sid of dropped) if (!readers.has(sid)) delete sources[sid];
  return next;
}

/** The options an element starts with. `markdown` is its first text, in the person's language. */
function defaultElementOptions(kind: ElementKind, markdown = ""): Raw {
  switch (kind) {
    case "rectangle":
      return { fillOpacity: 0.15, strokeWidth: 2, rx: 8 };
    case "ellipse":
      return { fillOpacity: 0.15, strokeWidth: 2 };
    case "line":
      return { strokeWidth: 2, toArrow: true };
    case "icon":
      return { icon: "shield-check" };
    case "image":
      return {};
    case "markdown":
      return { markdown };
  }
}

const DEFAULT_SIZE: Record<ElementKind, [number, number]> = {
  rectangle: [240, 140],
  ellipse: [160, 160],
  line: [200, 0],
  icon: [64, 64],
  image: [320, 200],
  markdown: [320, 120],
};

/** Add an element at `at` (its top left; a line's start), on top of the others. Returns the new id. */
export function addElement(definition: Raw, layoutId: string, kind: ElementKind, at: Point, markdown = ""): { definition: Raw; id: string } {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return { definition, id: "" };
  const canvas = readCanvasOptions(layout.options);
  const next = clone(definition);
  const id = freshId(takenIds(next), kind);
  next.visualizations = { ...record(next.visualizations), [id]: { type: ELEMENT_TYPES[kind], options: defaultElementOptions(kind, markdown) } };
  const [w, h] = DEFAULT_SIZE[kind];
  const entry =
    kind === "line"
      ? { item: id, type: "line", position: { from: clampPoint(at, canvas), to: clampPoint({ x: at.x + w, y: at.y + h }, canvas) } }
      : { item: id, type: "block", position: clampBox({ x: at.x, y: at.y, w, h }, canvas) };
  const target = layoutOf(next, layoutId) as Raw;
  target.structure = [...structureOf(target), entry];
  return { definition: next, id };
}

/** Change some options of one element (a key set to `undefined` is removed). */
export function setElementOptions(definition: Raw, id: string, patch: Raw): Raw {
  const viz = record(definition.visualizations)[id];
  if (!isRecord(viz)) return definition;
  const next = clone(definition);
  const options = { ...record(viz.options) };
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) delete options[key];
    else options[key] = value;
  }
  record(next.visualizations)[id] = { ...clone(viz), options };
  return next;
}

/** Change some options of a canvas (a key set to `undefined` is removed). */
export function setCanvasOptions(definition: Raw, layoutId: string, patch: Raw): Raw {
  if (!layoutOf(definition, layoutId)) return definition;
  const next = clone(definition);
  const layout = layoutOf(next, layoutId) as Raw;
  const options = { ...record(layout.options) };
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) delete options[key];
    else options[key] = value;
  }
  layout.options = options;
  return next;
}

// ── Clipboard ────────────────────────────────────────────────────────────────

/** What a copy holds: the elements, the data sources they read, their places. */
export interface CanvasClipboard {
  visualizations: Record<string, Raw>;
  dataSources: Record<string, Raw>;
  entries: Raw[];
}

/** Copy items of a layout, with the data sources they read. */
export function copyItems(definition: Raw, layoutId: string, ids: readonly string[]): CanvasClipboard | null {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return null;
  const chosen = new Set(ids);
  const entries = structureOf(layout).filter((e) => chosen.has(e.item as string));
  if (entries.length === 0) return null;
  const visualizations: Record<string, Raw> = {};
  const dataSources: Record<string, Raw> = {};
  const sources = record(definition.dataSources);
  for (const entry of entries) {
    const viz = record(record(definition.visualizations)[entry.item as string]);
    visualizations[entry.item as string] = clone(viz);
    const sid = record(viz.dataSources).primary;
    if (typeof sid === "string" && isRecord(sources[sid])) dataSources[sid] = clone(sources[sid]);
  }
  return clone({ visualizations, dataSources, entries });
}

/**
 * Paste a copy into a layout, `offset` pixels from where it was, on top.
 * Every element gets a new id; a line held to a pasted block holds to its
 * copy, one held to anything else lets go. `newDataSources`: each copy reads
 * a copy of its data source ("clone with a new data source") instead
 * of the same one. Returns the new ids.
 */
export function pasteItems(
  definition: Raw,
  layoutId: string,
  clipboard: CanvasClipboard,
  { offset = PASTE_OFFSET, newDataSources = false }: { offset?: number; newDataSources?: boolean } = {},
): { definition: Raw; ids: string[] } {
  if (!layoutOf(definition, layoutId)) return { definition, ids: [] };
  const next = clone(definition);
  const layout = layoutOf(next, layoutId) as Raw;
  const canvas = readCanvasOptions(layout.options);
  const taken = takenIds(next);
  const renamed = new Map<string, string>();
  // A copy of a copy is `box_3`, not `box_2_2`.
  const stem = (id: string) => id.replace(/_\d+$/, "");
  for (const old of Object.keys(clipboard.visualizations)) renamed.set(old, freshId(taken, stem(old)));
  const sources = (next.dataSources = { ...record(next.dataSources) });
  const sourceIds = new Map<string, string>();
  for (const [sid, source] of Object.entries(clipboard.dataSources)) {
    // The same source when it is still here and the copies may share it.
    if (!newDataSources && isRecord(sources[sid])) sourceIds.set(sid, sid);
    else {
      const fresh = freshId(taken, stem(sid));
      sources[fresh] = clone(source);
      sourceIds.set(sid, fresh);
    }
  }
  const visualizations = (next.visualizations = { ...record(next.visualizations) });
  for (const [old, viz] of Object.entries(clipboard.visualizations)) {
    const copy = clone(viz);
    const sid = record(copy.dataSources).primary;
    if (typeof sid === "string" && sourceIds.has(sid)) copy.dataSources = { ...record(copy.dataSources), primary: sourceIds.get(sid) };
    visualizations[renamed.get(old) as string] = copy;
  }
  const existing = blocksOf(structureOf(layout));
  const copiedBlocks = blocksOf(clipboard.entries);
  const pasted = clipboard.entries.map((entry) => {
    const id = renamed.get(entry.item as string) as string;
    if (entry.type === "line") {
      const position = record(entry.position);
      const moved: Raw = {};
      for (const key of ["from", "to"] as const) {
        const end = readEnd(position[key]);
        if (!end) continue;
        if (isConnection(end) && renamed.has(end.item)) moved[key] = { item: renamed.get(end.item), port: end.port };
        else {
          const at = isConnection(end) ? endPoint(end, existing.has(end.item) ? existing : copiedBlocks) : end;
          moved[key] = clampPoint({ x: at.x + offset, y: at.y + offset }, canvas);
        }
      }
      return { item: id, type: "line", position: moved };
    }
    const box = readBox(entry.position) ?? { x: 0, y: 0, w: 100, h: 100 };
    return { item: id, type: "block", position: clampBox({ ...box, x: box.x + offset, y: box.y + offset }, canvas) };
  });
  layout.structure = [...structureOf(layout), ...pasted];
  return { definition: next, ids: pasted.map((e) => e.item) };
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

export const MAX_TABS = 10;

interface TabItem {
  layoutId: string;
  label: string;
}

function tabsOf(definition: Raw): TabItem[] {
  const items = record(record(definition.layout).tabs).items;
  return Array.isArray(items) ? (items.filter(isRecord) as unknown as TabItem[]) : [];
}

function withTabs(definition: Raw, items: TabItem[], layouts?: Raw): Raw {
  const next = clone(definition);
  const layout = (next.layout = { ...record(next.layout) });
  layout.tabs = { ...record(layout.tabs), items };
  if (layouts) layout.layoutDefinitions = layouts;
  return next;
}

/** Add a tab (a grid or a canvas), last. `null` when the dashboard already has `MAX_TABS`. */
export function addTab(definition: Raw, type: "grid" | "absolute", label: string): { definition: Raw; layoutId: string } | null {
  const tabs = tabsOf(definition);
  if (tabs.length >= MAX_TABS) return null;
  const layouts = { ...record(record(definition.layout).layoutDefinitions) };
  const layoutId = freshId(new Set(Object.keys(layouts)), `layout_${tabs.length + 1}`);
  layouts[layoutId] =
    type === "absolute"
      ? { type, options: { width: DEFAULT_CANVAS.width, height: DEFAULT_CANVAS.height }, structure: [] }
      : { type, structure: [] };
  return { definition: withTabs(definition, [...tabs, { layoutId, label: label.slice(0, 60) }], layouts), layoutId };
}

export function renameTab(definition: Raw, layoutId: string, label: string): Raw {
  const trimmed = label.trim().slice(0, 60);
  if (!trimmed) return definition;
  return withTabs(definition, tabsOf(definition).map((tab) => (tab.layoutId === layoutId ? { ...tab, label: trimmed } : tab)));
}

/** Move a tab one place left (`-1`) or right (`1`). */
export function moveTab(definition: Raw, layoutId: string, delta: -1 | 1): Raw {
  const tabs = [...tabsOf(definition)];
  const i = tabs.findIndex((tab) => tab.layoutId === layoutId);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= tabs.length) return definition;
  [tabs[i], tabs[j]] = [tabs[j], tabs[i]];
  return withTabs(definition, tabs);
}

/** Remove a tab, its layout and what only it placed. `null` for the last tab. */
export function removeTab(definition: Raw, layoutId: string): Raw | null {
  const tabs = tabsOf(definition);
  if (tabs.length <= 1 || !tabs.some((tab) => tab.layoutId === layoutId)) return null;
  const layouts = { ...record(record(definition.layout).layoutDefinitions) };
  const placedHere = new Set(structureOf(record(layouts[layoutId])).map((e) => e.item as string));
  delete layouts[layoutId];
  const placedElsewhere = new Set(Object.values(layouts).flatMap((l) => structureOf(record(l)).map((e) => e.item as string)));
  const orphans = [...placedHere].filter((id) => !placedElsewhere.has(id) && id in record(definition.visualizations));
  const next = withTabs(definition, tabs.filter((tab) => tab.layoutId !== layoutId), layouts);
  return removeItems(next, orphans);
}

/** The pixels of a grid cell, and back (the grid's rows are OpenCTI's: 62 px and a 20 px gap, `OPENCTI_GRID_ROWS`). */
const CELL = { width: 120, height: 82 } as const;

/**
 * Switch a tab between a grid and a canvas. Places are converted (a column is
 * `canvas width / 12` pixels, a row 82); `null` when a canvas holding
 * elements a grid cannot draw (shapes, lines, icons, images, markdown) would
 * become a grid.
 */
export function setTabType(definition: Raw, layoutId: string, type: "grid" | "absolute"): Raw | null {
  const layout = layoutOf(definition, layoutId);
  if (!layout) return null;
  const current = layout.type === "absolute" ? "absolute" : "grid";
  if (current === type) return definition;
  const visualizations = record(definition.visualizations);
  const structure = structureOf(layout);
  if (type === "grid" && structure.some((e) => isElementType(record(visualizations[e.item as string]).type))) return null;
  const next = clone(definition);
  const target = layoutOf(next, layoutId) as Raw;
  if (type === "absolute") {
    const width = DEFAULT_CANVAS.width;
    const column = width / 12;
    const placed = structure.map((entry) => {
      const box = readBox(entry.position) ?? { x: 0, y: 0, w: 12, h: 4 };
      return { ...entry, position: { x: Math.round(box.x * column), y: box.y * CELL.height, w: Math.round(box.w * column), h: box.h * CELL.height } };
    });
    const bottom = Math.max(DEFAULT_CANVAS.height, ...placed.map((e) => (e.position as Box).y + (e.position as Box).h));
    target.type = "absolute";
    target.options = { width, height: Math.min(MAX_CANVAS, bottom) };
    target.structure = placed;
  } else {
    const canvas = readCanvasOptions(layout.options);
    const column = canvas.width / 12;
    target.type = "grid";
    target.options = {};
    target.structure = structure.map((entry) => {
      const box = readBox(entry.position) ?? { x: 0, y: 0, w: column * 12, h: CELL.height * 4 };
      const w = Math.min(12, Math.max(1, Math.round(box.w / column)));
      return {
        ...entry,
        position: {
          x: Math.min(12 - w, Math.max(0, Math.round(box.x / column))),
          y: Math.max(0, Math.round(box.y / CELL.height)),
          w,
          h: Math.min(40, Math.max(1, Math.round(box.h / CELL.height))),
        },
      };
    });
  }
  return next;
}

/** The tab bar's options: shown or not, on top or on the left. */
export function setTabOptions(definition: Raw, patch: { showTabBar?: boolean; barPosition?: "top" | "left" }): Raw {
  const next = clone(definition);
  const layout = (next.layout = { ...record(next.layout) });
  const tabs = (layout.tabs = { ...record(layout.tabs) });
  tabs.options = { ...record(tabs.options), ...patch };
  return next;
}

/** The dashboard's own theme, or none (it follows the viewer's). */
export function setTheme(definition: Raw, theme: "light" | "dark" | null): Raw {
  const next = clone(definition);
  if (theme) next.theme = theme;
  else delete next.theme;
  return next;
}

// ── Saving ───────────────────────────────────────────────────────────────────

/** One step of a saved edit (`CustomDashboardOperation`). */
export interface DefinitionOperation {
  op: "set" | "delete";
  path: string;
  value?: unknown;
}

const GROUPS = ["dataSources", "visualizations", "inputs"] as const;
const WHOLE = ["layout", "title", "description", "theme", "defaults", "expressions", "applicationProperties"] as const;

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * The `set` / `delete` operations that turn `before` into `after`: each
 * element changed or added is set whole, each gone is deleted. Applied
 * together and validated once by the server, so their order does not matter
 * to it; deletes of visualizations come first so a layout set after them is
 * the one that stays.
 */
export function definitionOperations(before: Raw, after: Raw): DefinitionOperation[] {
  const deletes: DefinitionOperation[] = [];
  const sets: DefinitionOperation[] = [];
  for (const group of GROUPS) {
    const was = record(before[group]);
    const now = record(after[group]);
    for (const id of Object.keys(was)) if (!(id in now)) deletes.push({ op: "delete", path: `${group}.${id}` });
    for (const [id, value] of Object.entries(now)) if (!same(was[id], value)) sets.push({ op: "set", path: `${group}.${id}`, value });
  }
  for (const key of WHOLE) {
    if (same(before[key], after[key])) continue;
    if (after[key] === undefined) deletes.push({ op: "delete", path: key });
    else sets.push({ op: "set", path: key, value: after[key] });
  }
  // A data source is deleted after the visualizations that read it were.
  deletes.sort((a, b) => Number(a.path.startsWith("dataSources.")) - Number(b.path.startsWith("dataSources.")));
  return [...deletes, ...sets];
}
