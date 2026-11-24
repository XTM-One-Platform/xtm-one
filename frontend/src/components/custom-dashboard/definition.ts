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
 * A version 2 dashboard definition read as widget records, tab by tab.
 *
 * The definition (`backend/app/services/dashboard_definition.py`) keeps data
 * sources, visualizations and layouts apart. The
 * renderer draws widgets, so each visualization placed in a tab becomes the
 * record `normalizeWidget` reads: a content type (`xtm.<kind>`) its kind with
 * its options as fields; a data type on a platform source a `platform` widget;
 * a data type on a static source a `data` widget, fetched by its id like a
 * platform one. The definition's defaults (`defaults.visualizations.global`,
 * then `defaults.visualizations.<type>`) are merged under each visualization's
 * own options.
 *
 * A canvas tab (`absolute`) also carries its canvas (`canvas.ts`): its size,
 * background and every item in pixels, the elements that draw no data
 * (shapes, lines, icons, images, markdown) among them; those are no widgets.
 *
 * Read defensively, like the rest of `spec.ts`: nothing here throws, and what
 * is not understood is left for `normalizeWidget` to show as unsupported.
 * Pure, with relative imports only: `spec.ts` runs on `node --test`.
 */
import { isElementType, readCanvas, type CanvasModel } from "./canvas.ts";

import { readInputs, type ReadInputs } from "./inputs.ts";

type Raw = Record<string, unknown>;

const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);

const record = (value: unknown): Raw => (isRecord(value) ? value : {});

/** A data visualization's type and the platform widget view it draws (`TYPE_TO_VIEW`). */
export const TYPE_TO_VIEW: Record<string, string | undefined> = {
  "xtm.singlevalue": "number",
  "xtm.list": "list",
  "xtm.distribution": "distribution",
  "xtm.column": "bar",
  "xtm.bar": "hbar",
  "xtm.line": "line",
  "xtm.area": "area",
  "xtm.pie": "donut",
  "xtm.table": "table",
  "xtm.scatter": "scatter",
  "xtm.bubble": "bubble",
  "xtm.punchcard": "punchcard",
  "xtm.singlevalueicon": "icon",
  "xtm.singlevalueradial": "radial",
  "xtm.fillergauge": "filler",
  "xtm.markergauge": "marker",
  "xtm.map": "map",
  "xtm.choropleth.svg": "choroplethsvg",
  "xtm.linkgraph": "linkgraph",
  "xtm.networkgraph": "networkgraph",
  "xtm.sankey": "sankey",
  "xtm.parallelcoordinates": "parallel",
  "xtm.timeline": "timeline",
  "xtm.treemap": "treemap",
  "xtm.radar": "radar",
  "xtm.polararea": "polar",
  "xtm.wordcloud": "wordcloud",
  "xtm.heatmap": "heatmap",
  "xtm.outcomematrix": "outcomematrix",
  "xtm.pathgraph": "pathgraph",
  "xtm.exposureconsole": "exposureconsole",
  "xtm.scoregauge": "scoregauge",
  "xtm.ringgauge": "ringgauge",
  "xtm.domaincards": "domaincards",
  "xtm.platformlink": undefined,
  "xtm.answer": "text",
};

/**
 * The validation views XTM One drew before it reproduced its modules' own, and
 * the view each became (`dashboard_definition.LEGACY_TYPES`). The server
 * upgrades a stored definition on read, a platform widget to its module's own
 * view; this reader takes the plain replacement, for a definition that did not
 * come through it.
 */
const LEGACY_TYPES: Readonly<Record<string, string>> = {
  "xtm.validationgates": "xtm.exposureconsole",
  "xtm.exposuregauge": "xtm.scoregauge",
  "xtm.resiliencerings": "xtm.ringgauge",
  "xtm.outcomecards": "xtm.domaincards",
  "xtm.outcomegrid": "xtm.outcomematrix",
};

/** How often a source is read again, in seconds (`dashboard_definition.refresh_seconds`); 30 s at least. */
export function refreshSeconds(value: unknown): number | undefined {
  if (typeof value !== "string") return undefined;
  const match = /^(\d{1,5})([smh])$/.exec(value);
  if (!match) return undefined;
  const seconds = Number(match[1]) * { s: 1, m: 60, h: 3600 }[match[2] as "s" | "m" | "h"];
  return seconds >= 30 && seconds <= 86_400 ? seconds : undefined;
}

/** A source and every source it derives from, as text: what its data depends on. */
export function chainOf(sid: string, sources: Raw): unknown[] {
  const chain: unknown[] = [];
  let current: string | undefined = sid;
  const seen = new Set<string>();
  while (current && !seen.has(current) && chain.length < 6) {
    seen.add(current);
    const source = record(sources[current]);
    chain.push([current, source]);
    current = source.type === "derived" ? (record(source.options).extends as string | undefined) : undefined;
  }
  return chain;
}

const MAX_TABS = 10;

/** A tab of the definition, its visualizations as widget records. */
interface DefinitionTab {
  id: string;
  label: string;
  type: "grid" | "absolute";
  records: Raw[];
  /** An absolute layout's canvas: its options and its items in pixels, bottom first. */
  canvas?: CanvasModel;
}

export interface ReadDefinition {
  title: unknown;
  description: unknown;
  tabs: DefinitionTab[];
  showTabBar: boolean;
  tabBarPosition: "top" | "left";
  /** The dashboard's own theme; none follows the viewer's. */
  theme?: "light" | "dark";
  appProperties: { hideEdit: boolean; hideExport: boolean; hideOpenInPlatform: boolean; collapseNavigation: boolean };
  /** The data sources as stored, by id. */
  sources: Raw;
  /** Its inputs, token defaults, computed tokens and submit switch. */
  inputs: ReadInputs;
}

/** The columns a `width` stands for, for a reader without the grid (`width_of`). */
function widthOf(columns: number): "full" | "half" | "third" {
  if (columns <= 4) return "third";
  if (columns <= 6) return "half";
  return "full";
}

/** The options a visualization of `type` is drawn with: the defaults, then its own. */
function mergedOptions(defaults: Raw, type: string, own: unknown): Raw {
  const visualizations = record(defaults.visualizations);
  return {
    ...record(record(visualizations.global).options),
    ...record(record(visualizations[type]).options),
    ...record(own),
  };
}

/**
 * The options a data visualization is drawn with once its data came: how the
 * data's module draws it (`presentation`, generic options) laid under the
 * visualization's own, only when it is drawn with the presentation's view. A
 * widget drawn another way keeps its own look; any option can still be set.
 */
export function presentedOptions(options: Raw, view: string | undefined, presentation: unknown): Raw {
  const preset = record(presentation);
  const presetView = typeof preset.view === "string" ? TYPE_TO_VIEW[preset.view] : undefined;
  if (!view || presetView !== view) return options;
  return { ...record(preset.options), ...options };
}

/** One visualization as a widget record, or `undefined` when it draws nothing a widget can. */
function widgetRecord(vid: string, viz: Raw, sources: Raw, defaults: Raw): Raw | undefined {
  const written = typeof viz.type === "string" ? viz.type : "";
  const type = LEGACY_TYPES[written] ?? written;
  const options = mergedOptions(defaults, type, viz.options);
  const title = typeof viz.title === "string" ? viz.title : undefined;
  if (type.startsWith("xtm.") && !(type in TYPE_TO_VIEW)) {
    // A content visualization: its options are the widget's fields.
    return { ...options, kind: type.slice(4), id: vid, title };
  }
  if (!(type in TYPE_TO_VIEW)) return { kind: type || "unknown", id: vid, title };
  const sid = record(viz.dataSources).primary;
  const source = typeof sid === "string" ? record(sources[sid]) : {};
  const sourceOptions = record(source.options);
  const view = TYPE_TO_VIEW[type];
  const link = options.link;
  // What a drawing of data carries besides its data: the panel chrome, the
  // expressions' context, the source of its annotations.
  const annotationId = record(viz.dataSources).annotation;
  const annotation = typeof annotationId === "string" && isRecord(sources[annotationId]) ? sources[annotationId] : undefined;
  const drawing = {
    description: viz.description,
    context: record(viz.context),
    annotation: annotation ? { id: annotationId, fingerprint: JSON.stringify([annotationId, annotation]) } : undefined,
    showProgressBar: viz.showProgressBar === true,
    showLastUpdated: viz.showLastUpdated === true,
  };
  if (source.type === "platform") {
    return {
      kind: "platform",
      id: vid,
      title,
      source: sourceOptions.source,
      integration_id: sourceOptions.integration_id,
      native: sourceOptions.native,
      view,
      link,
      options,
      sourceId: sid,
      refresh: refreshSeconds(sourceOptions.refresh),
      chain: chainOf(sid as string, sources),
      ...drawing,
    };
  }
  if (typeof sid === "string" && ["static", "assignment", "ask", "variable", "derived"].includes(source.type as string)) {
    // Read again when the source, a source it derives from or the view changes, not on every save.
    return {
      kind: "data",
      id: vid,
      title,
      view,
      link,
      options,
      sourceId: sid,
      sourceType: source.type,
      refresh: refreshSeconds(sourceOptions.refresh),
      // What its tokens are looked for in (`tokens.tokensFor`).
      chain: chainOf(sid, sources),
      fingerprint: JSON.stringify([chainOf(sid, sources), type]),
      ...drawing,
    };
  }
  return { kind: String(source.type ?? "data"), id: vid, title };
}

/** A definition (`version: 2`) as tabs of widget records. Never throws. */
export function readDefinition(raw: Raw): ReadDefinition {
  const visualizations = record(raw.visualizations);
  const sources = record(raw.dataSources);
  const defaults = record(raw.defaults);
  const layout = record(raw.layout);
  const definitions = record(layout.layoutDefinitions);
  const tabsRaw = record(layout.tabs);
  const items = Array.isArray(tabsRaw.items) ? tabsRaw.items.slice(0, MAX_TABS) : [];
  const seen = new Set<string>();
  const tabs: DefinitionTab[] = [];
  for (const item of items) {
    const tab = record(item);
    const id = typeof tab.layoutId === "string" ? tab.layoutId : "";
    if (!id || seen.has(id) || !isRecord(definitions[id])) continue;
    seen.add(id);
    const definition = record(definitions[id]);
    const type = definition.type === "absolute" ? "absolute" : "grid";
    const records: Raw[] = [];
    for (const entry of Array.isArray(definition.structure) ? definition.structure : []) {
      const place = record(entry);
      if (place.type === "input" && typeof place.item === "string" && isRecord(record(raw.inputs)[place.item])) {
        // An input placed in the tab: drawn as a widget of its own.
        const position = record(place.position);
        const widget: Raw = { kind: "input", id: `input-${place.item}`, inputId: place.item };
        if (type === "grid") widget.layout = { x: position.x, y: position.y, w: position.w, h: position.h };
        records.push(widget);
        continue;
      }
      if ((place.type ?? "block") !== "block" || typeof place.item !== "string") continue;
      const viz = visualizations[place.item];
      // A canvas element draws no widget: the canvas draws it.
      if (!isRecord(viz) || isElementType(viz.type)) continue;
      const widget = widgetRecord(place.item, viz, sources, defaults);
      if (!widget) continue;
      // What a click does and when it shows (`interactions.ts`), read by `normalizeWidget`.
      widget.handlers = viz.eventHandlers;
      widget.visibility = viz.containerOptions;
      const position = record(place.position);
      // An absolute layout's pixels are no grid cell: the widget is placed in rows.
      if (type === "grid") {
        widget.layout = { x: position.x, y: position.y, w: position.w, h: position.h };
        if (typeof position.w === "number") widget.width = widthOf(position.w);
      }
      records.push(widget);
    }
    const label = typeof tab.label === "string" && tab.label.trim() ? tab.label.trim().slice(0, 60) : id;
    const drawn = new Set(records.map((widget) => widget.id));
    const canvas = type === "absolute" ? readCanvas(definition, visualizations, (vid) => drawn.has(vid)) : undefined;
    tabs.push({ id, label, type, records, ...(canvas ? { canvas } : {}) });
  }
  if (tabs.length === 0) tabs.push({ id: "layout_1", label: "Overview", type: "grid", records: [] });
  const props = record(raw.applicationProperties);
  const flag = (name: string) => props[name] === true;
  const tabOptions = record(tabsRaw.options);
  return {
    title: raw.title,
    description: raw.description,
    sources,
    inputs: readInputs(raw),
    tabs,
    // A single tab shows no bar unless asked; several always do unless told not to.
    showTabBar: tabOptions.showTabBar === false ? false : tabs.length > 1 || tabOptions.showTabBar === true,
    tabBarPosition: tabOptions.barPosition === "left" ? "left" : "top",
    ...(raw.theme === "light" || raw.theme === "dark" ? { theme: raw.theme } : {}),
    appProperties: {
      hideEdit: flag("hideEdit"),
      hideExport: flag("hideExport"),
      hideOpenInPlatform: flag("hideOpenInPlatform"),
      collapseNavigation: flag("collapseNavigation"),
    },
  };
}

/** What the source editor edits: the whole definition (`path: null`), or one element of it by its path. */
export interface SourceScope {
  key: string;
  /** `visualizations.<id>`, `dataSources.<id>`, `defaults`; `null` for the whole definition. */
  path: string | null;
  kind: "dashboard" | "visualization" | "dataSource" | "defaults" | "input";
  /** The element's id, when it has one. */
  id?: string;
}

/**
 * The scopes the source editor offers: the dashboard, its defaults and, with a
 * widget picked, its visualization and data source - or the data source the
 * Data sources panel opened it on (`sourceId`).
 */
export function sourceScopes(definition: unknown, selectedId: string | null, sourceId?: string | null): SourceScope[] {
  const raw = record(definition);
  const scopes: SourceScope[] = [{ key: "dashboard", path: null, kind: "dashboard" }];
  const viz = selectedId ? record(raw.visualizations)[selectedId] : undefined;
  if (selectedId && isRecord(viz) && !sourceId) {
    scopes.push({ key: "visualization", path: `visualizations.${selectedId}`, kind: "visualization", id: selectedId });
    const sid = record(viz.dataSources).primary;
    if (typeof sid === "string" && isRecord(record(raw.dataSources)[sid])) {
      scopes.push({ key: "dataSource", path: `dataSources.${sid}`, kind: "dataSource", id: sid });
    }
  }
  if (sourceId?.startsWith("inputs.") && isRecord(record(raw.inputs)[sourceId.slice(7)])) {
    scopes.push({ key: "input", path: sourceId, kind: "input", id: sourceId.slice(7) });
  } else if (sourceId && isRecord(record(raw.dataSources)[sourceId])) {
    scopes.push({ key: "dataSource", path: `dataSources.${sourceId}`, kind: "dataSource", id: sourceId });
  }
  scopes.push({ key: "defaults", path: "defaults", kind: "defaults" });
  return scopes;
}

/** The element at `path` (`null`: the whole definition), as the editor shows it. */
export function sourceText(definition: unknown, path: string | null): string {
  if (path === null) return JSON.stringify(definition ?? {}, null, 2);
  let node: unknown = definition;
  // A path is a top-level key or `group.id`, split at its first dot: an id may hold dots itself.
  const dot = path.indexOf(".");
  const parts = dot < 0 ? [path] : [path.slice(0, dot), path.slice(dot + 1)];
  for (const part of parts) node = record(node)[part];
  return JSON.stringify(node ?? {}, null, 2);
}

/** The editor's text read as JSON: its value, or the parser's message. */
export function parseSource(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

// ── The Data sources panel ──────────────────────────────────────────────────

/** A data source of the definition as the panel lists it. */
export interface SourceEntry {
  id: string;
  name?: string;
  type: string;
  /** The visualizations drawing it and the derived sources reading it. */
  usedBy: string[];
}

/** Every data source, in the definition's order, with what uses it. */
export function sourceEntries(definition: unknown): SourceEntry[] {
  const raw = record(definition);
  const sources = record(raw.dataSources);
  const users = new Map<string, string[]>(Object.keys(sources).map((sid) => [sid, []]));
  for (const [vid, viz] of Object.entries(record(raw.visualizations))) {
    for (const sid of Object.values(record(record(viz).dataSources))) {
      if (typeof sid === "string") users.get(sid)?.push(vid);
    }
  }
  for (const [iid, input] of Object.entries(record(raw.inputs))) {
    const sid = record(record(input).dataSources).primary;
    if (typeof sid === "string") users.get(sid)?.push(iid);
  }
  for (const [sid, source] of Object.entries(sources)) {
    const parent = record(record(source).options).extends;
    if (record(source).type === "derived" && typeof parent === "string") users.get(parent)?.push(sid);
  }
  return Object.entries(sources).map(([id, source]) => {
    const entry = record(source);
    return {
      id,
      name: typeof entry.name === "string" && entry.name ? entry.name : undefined,
      type: typeof entry.type === "string" ? entry.type : "unknown",
      usedBy: users.get(id) ?? [],
    };
  });
}

/** An element id not taken yet in the definition: `base`, then `base_2`, `base_3`... */
export function freeId(definition: unknown, base: string): string {
  const raw = record(definition);
  const taken = new Set([
    ...Object.keys(record(raw.dataSources)),
    ...Object.keys(record(raw.visualizations)),
    ...Object.keys(record(raw.inputs)),
  ]);
  const clean = base.replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^[^A-Za-z0-9]+/, "").slice(0, 50) || "item";
  if (!taken.has(clean)) return clean;
  for (let n = 2; ; n += 1) if (!taken.has(`${clean}_${n}`)) return `${clean}_${n}`;
}

/** The visualization types a data source can be drawn with, by its shape. */
export const VISUALIZATION_TYPES_BY_SHAPE: Record<string, string[]> = {
  number: ["xtm.singlevalue"],
  series: ["xtm.line", "xtm.area", "xtm.column", "xtm.bar"],
  distribution: ["xtm.pie", "xtm.column", "xtm.bar", "xtm.distribution", "xtm.list"],
  rows: ["xtm.table", "xtm.list"],
  text: ["xtm.answer"],
};

/** The rows a new visualization of a type takes on the grid (`dashboard_grid._VIEW_ROWS`). */
/** The rows (OpenCTI's) a new visualization of `type` takes. */
function rowsOf(type: string): number {
  if (type === "xtm.singlevalue") return 3;
  return 4;
}

/**
 * The operations adding a visualization of `type` drawing `sourceId`, placed
 * below the others in the layout `layoutId` (the first one when unknown):
 * the visualization, then the layout holding it.
 */
export function addVisualizationOperations(
  definition: unknown,
  sourceId: string,
  type: string,
  title: string,
  layoutId: string | null,
): { id: string; operations: { op: "set"; path: string; value: unknown }[] } {
  const raw = record(definition);
  const layout = record(raw.layout);
  const definitions = record(layout.layoutDefinitions);
  const tabs = record(layout.tabs).items;
  const first = Array.isArray(tabs) ? record(tabs[0]).layoutId : undefined;
  const lid = layoutId && isRecord(definitions[layoutId]) ? layoutId : typeof first === "string" ? first : "layout_1";
  const target = record(definitions[lid]);
  const structure = Array.isArray(target.structure) ? target.structure : [];
  const bottom = structure.reduce((max: number, entry) => {
    const position = record(record(entry).position);
    return typeof position.y === "number" && typeof position.h === "number" ? Math.max(max, position.y + position.h) : max;
  }, 0);
  const absolute = target.type === "absolute";
  const id = freeId(definition, `viz_${sourceId}`);
  const position = absolute ? { x: 40, y: bottom + 40, w: 480, h: 320 } : { x: 0, y: bottom, w: 6, h: rowsOf(type) };
  const visualization: Raw = { type, dataSources: { primary: sourceId } };
  if (title.trim()) visualization.title = title.trim().slice(0, 120);
  return {
    id,
    operations: [
      { op: "set", path: `visualizations.${id}`, value: visualization },
      {
        op: "set",
        path: "layout",
        value: {
          ...layout,
          layoutDefinitions: { ...definitions, [lid]: { ...target, structure: [...structure, { item: id, type: "block", position }] } },
        },
      },
    ],
  };
}

// ── The Inputs panel ────────────────────────────────────────────────────────

/** The operations adding an input: the input, then the layout naming it (above the tabs). */
export function addInputOperations(
  definition: unknown,
  id: string,
  input: Raw,
): { op: "set"; path: string; value: unknown }[] {
  const layout = record(record(definition).layout);
  const global = Array.isArray(layout.globalInputs) ? layout.globalInputs : [];
  return [
    { op: "set", path: `inputs.${id}`, value: input },
    { op: "set", path: "layout", value: { ...layout, globalInputs: [...global.filter((g) => g !== id), id] } },
  ];
}

/** The operation turning the Submit button on or off (`layout.options`). */
export function submitButtonOperation(definition: unknown, on: boolean): { op: "set"; path: string; value: unknown } {
  const layout = record(record(definition).layout);
  return { op: "set", path: "layout", value: { ...layout, options: { ...record(layout.options), submitButton: on } } };
}

/** The operation writing a token's default (`defaults.tokens.default`), or removing it with `null`. */
export function tokenDefaultOperation(definition: unknown, name: string, value: string | null): { op: "set"; path: string; value: unknown } {
  const defaults = record(record(definition).defaults);
  const tokens = record(defaults.tokens);
  const entries = { ...record(tokens.default) };
  if (value === null) delete entries[name];
  else entries[name] = { value };
  return { op: "set", path: "defaults", value: { ...defaults, tokens: { ...tokens, default: entries } } };
}
