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
 * A custom dashboard as the renderer draws it: the stored spec, read defensively.
 *
 * The backend validates every write (`app/services/custom_dashboard_spec.py`),
 * but what this module reads is a stored row: written under an older catalog,
 * truncated, or simply not what the types promise. So nothing here trusts its
 * input. `normalizeDashboard` never throws: every string is bounded, every list
 * capped, every tone and width read against its own list, every link re-checked
 * (`isSafeDashboardHref`), and a widget that cannot be drawn becomes an
 * `unsupported` placeholder instead of failing the page. The components in
 * `widgets.tsx` then only ever meet the shapes declared here.
 *
 * Pure module (no React, no `@/` import at runtime): `spec.test.ts` runs it on
 * `node --test`.
 */
import type { CanvasModel } from "./canvas.ts";
import { DATA_SOURCE_TYPES, readDefinition } from "./definition.ts";
import type { ReadInputs } from "./inputs.ts";
import { readConditions, readHandlers, readVisibility, type EventHandler, type Visibility } from "./interactions.ts";
import { placeWidgets, readCell, type GridCell } from "./grid.ts";

/** The widget catalog, in the backend's order (`DASHBOARD_WIDGET_KINDS`,
 *  pinned by `backend/tests/test_custom_dashboards.py`, which reads this source).
 *  @public */
export const DASHBOARD_WIDGET_KINDS = [
  "score",
  "stats",
  "stages",
  "decision",
  "feed",
  "callout",
  "ranked_list",
  "steps",
  "checklist",
  "progress",
  "prompts",
  "text",
  "platform",
] as const;

/** Where a `platform` widget reads its data (`dashboard_platform` in the backend). */
const PLATFORM_SOURCES = ["opencti", "openaev", "opencrq"] as const;
/** How a `platform` widget is drawn. */
export const PLATFORM_VIEWS = [
  "number",
  "list",
  "distribution",
  "bar",
  "hbar",
  "line",
  "area",
  "donut",
  "table",
  "scatter",
  "bubble",
  "punchcard",
  "icon",
  "radial",
  "filler",
  "marker",
  // Maps, graphs and flows (`viz/`).
  "map",
  "choroplethsvg",
  "linkgraph",
  "networkgraph",
  "sankey",
  "parallel",
  "timeline",
  "treemap",
  "attackflow",
  // Radar, polar area, word cloud and heatmap (`charts/`).
  "radar",
  "polar",
  "wordcloud",
  "heatmap",
  // Validation outcomes (`charts/OutcomeViews`).
  "outcomematrix",
  "pathgraph",
  "exposureconsole",
  "scoregauge",
  "ringgauge",
  "domaincards",
] as const;
export type PlatformSource = (typeof PLATFORM_SOURCES)[number];
/** The platforms' names, as they spell them. */
export const PLATFORM_NAMES: Record<PlatformSource, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };
export type PlatformView = (typeof PLATFORM_VIEWS)[number];
/** How a data widget is drawn: a platform view, or an answer in words (`xtm.answer`). */
export const DATA_VIEWS = [...PLATFORM_VIEWS, "text"] as const;
export type DataView = (typeof DATA_VIEWS)[number];
type DataSourceType = (typeof DATA_SOURCE_TYPES)[number];

/** The colours a widget may ask for, by meaning (`DASHBOARD_TONES`, read by
 *  `backend/tests/test_custom_dashboards.py`). @public */
export const DASHBOARD_TONES = ["neutral", "accent", "ai", "success", "warning", "danger", "info"] as const;

export type DashboardWidgetKind = (typeof DASHBOARD_WIDGET_KINDS)[number];
export type DashboardTone = (typeof DASHBOARD_TONES)[number];
export type DashboardWidth = "full" | "half" | "third";
export type DashboardStageState = "done" | "active" | "pending" | "blocked";
type DashboardTrendDirection = "up" | "down" | "flat";

/** At most this many widgets are drawn, whatever the row holds (`MAX_VISUALIZATIONS`). */
export const MAX_RENDERED_WIDGETS = 60;

/** A control: open a platform page (`href`) or put a question in the
 *  assistant's composer (`prompt`). Exactly one of the two. */
export interface DashboardAction {
  label: string;
  href?: string;
  prompt?: string;
}

export interface DashboardStat {
  label: string;
  value: string;
  caption?: string;
  tone: DashboardTone;
}

interface DashboardBadge {
  label: string;
  tone: DashboardTone;
}

/** What a drawing of data carries besides its data (`definition.ts`): the panel chrome, the
 *  context its dynamic options read, the data source of its annotations. */
interface DataDrawing {
  description?: string;
  context: Record<string, unknown>;
  annotation?: { id: string; fingerprint: string };
  showProgressBar: boolean;
  showLastUpdated: boolean;
}

interface WidgetCommon {
  id: string;
  title?: string;
  width: DashboardWidth;
  /** Its place on the 12-column grid (`grid.ts`): stored, or placed when read. */
  layout: GridCell;
  link?: DashboardAction;
  /** What a click on it does, in order (a definition's `eventHandlers`). */
  handlers?: EventHandler[];
  /** When it shows (`containerOptions.visibility`). */
  visibility?: Visibility;
}

export type DashboardWidget =
  | (WidgetCommon & {
      kind: "score";
      label: string;
      value: number;
      max?: number;
      unit?: string;
      trend?: { direction: DashboardTrendDirection; label: string; tone: DashboardTone };
      series: number[];
      seriesLabel?: string;
      stats: DashboardStat[];
    })
  | (WidgetCommon & { kind: "stats"; items: DashboardStat[] })
  | (WidgetCommon & {
      kind: "stages";
      items: { label: string; state: DashboardStageState; status?: string; progress?: number }[];
    })
  | (WidgetCommon & {
      kind: "decision";
      eyebrow?: string;
      time?: string;
      text: string;
      tags: string[];
      actions: DashboardAction[];
    })
  | (WidgetCommon & {
      kind: "feed";
      items: { badge?: string; title: string; time?: string; text?: string; href?: string }[];
    })
  | (WidgetCommon & { kind: "callout"; eyebrow?: string; text: string; tone: DashboardTone; actions: DashboardAction[] })
  | (WidgetCommon & {
      kind: "ranked_list";
      numbered: boolean;
      items: { title: string; meta?: string; badge?: DashboardBadge; href?: string }[];
    })
  | (WidgetCommon & {
      kind: "steps";
      caption?: string;
      items: { label: string; detail?: string; tone: DashboardTone }[];
    })
  | (WidgetCommon & {
      kind: "checklist";
      eyebrow?: string;
      items: { label: string; done: boolean }[];
      actions: DashboardAction[];
    })
  | (WidgetCommon & {
      kind: "progress";
      status?: DashboardBadge;
      text?: string;
      progress?: number;
      progressLabel?: string;
      lines: { label: string; tone: DashboardTone }[];
      actions: DashboardAction[];
    })
  | (WidgetCommon & { kind: "prompts"; placeholder?: string; suggestions: string[] })
  /** `format: "markdown"`: GitHub-flavoured Markdown (an imported OpenCTI text); else paragraphs with inline markup. */
  | (WidgetCommon & { kind: "text"; body: string; format?: "markdown" })
  /** A widget of OpenCTI, OpenAEV or OpenCRQ, drawn from the platform's data
   *  (read as the viewer). No `view`: a type this build does not draw (an
   *  OpenCTI heatmap), shown as a link to the platform. */
  | (WidgetCommon & {
      kind: "platform";
      source: PlatformSource;
      integrationId: string;
      view?: PlatformView;
      size: "s" | "m" | "l";
      /** The platform's own name for its type: "donut", "vertical-barchart", "findings_trend". */
      nativeType: string;
      /** What the widget reads, as text: its data is read again when this changes, not on every save. */
      fingerprint: string;
      /** Its visualization's options, the definition's defaults merged in. */
      options: Record<string, unknown>;
      /** The data source it draws (a definition's), and how often it is read again (seconds). */
      sourceId?: string;
      refresh?: number;
      /** The source and those it derives from, as stored: where its tokens are looked for. */
      chain?: unknown;
    } & DataDrawing)
  /** A visualization drawing a data source of XTM One's own (static data, an
   *  assignment's or an agent's answer, a variable, a derived source): read
   *  like a platform widget, by its id, never named after a platform. */
  | (WidgetCommon & {
      kind: "data";
      view?: DataView;
      fingerprint: string;
      options: Record<string, unknown>;
      sourceId: string;
      sourceType: DataSourceType;
      refresh?: number;
      chain?: unknown;
    } & DataDrawing)
  /** An input placed among the widgets (its definition is the dashboard's `inputs`). */
  | (WidgetCommon & { kind: "input"; inputId: string })
  /** A widget this build cannot draw: an unknown kind, or one missing what it
   *  needs. Drawn as a placeholder naming the kind. */
  | (WidgetCommon & { kind: "unsupported"; originalKind: string });

/** A tab of the dashboard: its layout and the widgets placed in it. */
export interface DashboardTab {
  id: string;
  label: string;
  /** How its widgets are placed: on the 12-column grid, or in pixels on a canvas. */
  type: "grid" | "absolute";
  widgets: DashboardWidget[];
  /** An absolute tab's canvas (`canvas.ts`): its options and items, the widgets among them by id. */
  canvas?: CanvasModel;
}

/** The definition's switches for the page around the dashboard (`applicationProperties`). */
interface DashboardAppProperties {
  hideEdit: boolean;
  hideExport: boolean;
  hideOpenInPlatform: boolean;
  collapseNavigation: boolean;
}

export interface NormalizedDashboard {
  title: string;
  subtitle?: string;
  /** The first tab's widgets: what a reader without tabs (the phone, a chat card) draws. */
  widgets: DashboardWidget[];
  tabs: DashboardTab[];
  /** Whether the tab bar shows (a single tab hides it unless asked). */
  showTabBar: boolean;
  /** Where the tab bar sits. */
  tabBarPosition?: "top" | "left";
  /** The dashboard's own theme; none follows the viewer's. */
  theme?: "light" | "dark";
  appProperties: DashboardAppProperties;
  /** The definition's data sources as stored, by id (none for a widget list). */
  sources: Record<string, unknown>;
  /** Its inputs and tokens (none for a widget list). */
  inputs: ReadInputs;
  /** `expressions.conditions`: what a visualization's visibility names. */
  conditions: Record<string, string>;
}

// ── Links ────────────────────────────────────────────────────────────────────

const INTERNAL_PATH_RE = /^\/(?![/\\])[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]{0,300}$/;
const API_PATH_RE = /^\/api(?:[/?#]|$)/i;

/** Whether `href` is a page of this web app (the backend's `check_internal_path`):
 *  a root-relative path, never a scheme, a `//host`, a backslash or the API. */
export function isSafeDashboardHref(href: unknown): href is string {
  return typeof href === "string" && INTERNAL_PATH_RE.test(href) && !API_PATH_RE.test(href);
}

// ── Readers: every one total, none throws ────────────────────────────────────

type Raw = Record<string, unknown>;

const isRecord = (value: unknown): value is Raw =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A trimmed, bounded string; a number reads as written. */
function text(value: unknown, max: number): string | undefined {
  let out: string | undefined;
  if (typeof value === "string") out = value.trim();
  else if (typeof value === "number" && Number.isFinite(value)) out = String(value);
  if (!out) return undefined;
  return out.length > max ? `${out.slice(0, max - 1)}…` : out;
}

function finite(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function fraction(value: unknown): number | undefined {
  const n = finite(value);
  return n === undefined ? undefined : Math.min(1, Math.max(0, n));
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

const tone = (value: unknown): DashboardTone => oneOf(value, DASHBOARD_TONES, "neutral");

/** The items of `value` `read` accepts, at most `max` of them. */
function list<T>(value: unknown, max: number, read: (item: unknown) => T | undefined): T[] {
  if (!Array.isArray(value)) return [];
  const out: T[] = [];
  for (const item of value) {
    const read_ = read(item);
    if (read_ !== undefined) out.push(read_);
    if (out.length >= max) break;
  }
  return out;
}

function href(value: unknown): string | undefined {
  return isSafeDashboardHref(value) ? value : undefined;
}

function action(value: unknown): DashboardAction | undefined {
  if (!isRecord(value)) return undefined;
  const label = text(value.label, 80);
  if (!label) return undefined;
  const target = href(value.href);
  if (target) return { label, href: target };
  const prompt = text(value.prompt, 500);
  return prompt ? { label, prompt } : undefined;
}

function stat(value: unknown): DashboardStat | undefined {
  if (!isRecord(value)) return undefined;
  const label = text(value.label, 80);
  const shown = text(value.value, 24);
  if (!label || !shown) return undefined;
  return { label, value: shown, caption: text(value.caption, 200), tone: tone(value.tone) };
}

function badge(value: unknown): DashboardBadge | undefined {
  if (!isRecord(value)) return undefined;
  const label = text(value.label, 24);
  return label ? { label, tone: tone(value.tone) } : undefined;
}

const actions = (value: unknown) => list(value, 2, action);

// ── Widgets ──────────────────────────────────────────────────────────────────

/** The fields of one kind, or `undefined` when the widget lacks what it needs. */
const READERS: { [K in DashboardWidgetKind]: (raw: Raw) => Omit<Extract<DashboardWidget, { kind: K }>, keyof WidgetCommon | "kind"> | undefined } = {
  score: (raw) => {
    const label = text(raw.label, 80);
    const value = finite(raw.value);
    if (!label || value === undefined) return undefined;
    const trend = isRecord(raw.trend) ? raw.trend : undefined;
    const trendLabel = trend ? text(trend.label, 80) : undefined;
    const max = finite(raw.max);
    const unit = text(raw.unit, 12);
    return {
      label,
      value,
      max,
      // "/100" next to a max of 100 says it twice: the max already reads "/100".
      unit: unit !== undefined && max !== undefined && unit.replace(/\s+/g, "") === `/${max}` ? undefined : unit,
      trend:
        trend && trendLabel
          ? { direction: oneOf(trend.direction, ["up", "down", "flat"] as const, "flat"), label: trendLabel, tone: tone(trend.tone) }
          : undefined,
      series: list(raw.series, 60, finite),
      seriesLabel: text(raw.series_label, 80),
      stats: list(raw.stats, 4, stat),
    };
  },
  stats: (raw) => {
    const items = list(raw.items, 6, stat);
    return items.length ? { items } : undefined;
  },
  stages: (raw) => {
    const items = list(raw.items, 8, (item) => {
      if (!isRecord(item)) return undefined;
      const label = text(item.label, 80);
      if (!label) return undefined;
      return {
        label,
        state: oneOf(item.state, ["done", "active", "pending", "blocked"] as const, "pending"),
        status: text(item.status, 80),
        progress: fraction(item.progress),
      };
    });
    return items.length ? { items } : undefined;
  },
  decision: (raw) => {
    const body = text(raw.text, 600);
    if (!body) return undefined;
    return {
      eyebrow: text(raw.eyebrow, 80),
      time: text(raw.time, 80),
      text: body,
      tags: list(raw.tags, 4, (tag) => text(tag, 80)),
      actions: actions(raw.actions),
    };
  },
  feed: (raw) => {
    const items = list(raw.items, 12, (item) => {
      if (!isRecord(item)) return undefined;
      const title = text(item.title, 80);
      if (!title) return undefined;
      return {
        badge: text(item.badge, 6),
        title,
        time: text(item.time, 80),
        text: text(item.text, 600),
        href: href(item.href),
      };
    });
    return items.length ? { items } : undefined;
  },
  callout: (raw) => {
    const body = text(raw.text, 600);
    if (!body) return undefined;
    return { eyebrow: text(raw.eyebrow, 80), text: body, tone: tone(raw.tone), actions: actions(raw.actions) };
  },
  ranked_list: (raw) => {
    const items = list(raw.items, 12, (item) => {
      if (!isRecord(item)) return undefined;
      const title = text(item.title, 80);
      if (!title) return undefined;
      return { title, meta: text(item.meta, 200), badge: badge(item.badge), href: href(item.href) };
    });
    return items.length ? { items, numbered: raw.numbered !== false } : undefined;
  },
  steps: (raw) => {
    const items = list(raw.items, 12, (item) => {
      if (!isRecord(item)) return undefined;
      const label = text(item.label, 80);
      return label ? { label, detail: text(item.detail, 200), tone: tone(item.tone) } : undefined;
    });
    return items.length ? { items, caption: text(raw.caption, 80) } : undefined;
  },
  checklist: (raw) => {
    const items = list(raw.items, 20, (item) => {
      if (!isRecord(item)) return undefined;
      const label = text(item.label, 200);
      return label ? { label, done: item.done === true } : undefined;
    });
    return items.length ? { items, eyebrow: text(raw.eyebrow, 80), actions: actions(raw.actions) } : undefined;
  },
  progress: (raw) => ({
    status: badge(raw.status),
    text: text(raw.text, 600),
    progress: fraction(raw.progress),
    progressLabel: text(raw.progress_label, 80),
    lines: list(raw.lines, 8, (line) => {
      if (!isRecord(line)) return undefined;
      const label = text(line.label, 200);
      return label ? { label, tone: tone(line.tone) } : undefined;
    }),
    actions: actions(raw.actions),
  }),
  prompts: (raw) => {
    const suggestions = list(raw.suggestions, 6, (s) => text(s, 200));
    return suggestions.length ? { suggestions, placeholder: text(raw.placeholder, 80) } : undefined;
  },
  text: (raw) => {
    const body = text(raw.body, 2000);
    return body ? { body, ...(raw.format === "markdown" ? { format: "markdown" as const } : {}) } : undefined;
  },
  platform: (raw) => {
    const source = (PLATFORM_SOURCES as readonly string[]).includes(raw.source as string)
      ? (raw.source as PlatformSource)
      : undefined;
    const integrationId = text(raw.integration_id, 64);
    const native = isRecord(raw.native) ? raw.native : undefined;
    if (!source || !integrationId || !native) return undefined;
    const nativeType = text(native.type ?? native.widget_type ?? native.metric, 60) ?? source;
    const view = (PLATFORM_VIEWS as readonly string[]).includes(raw.view as string) ? (raw.view as PlatformView) : undefined;
    const fingerprint = JSON.stringify([source, integrationId, native]);
    const options = isRecord(raw.options) ? raw.options : {};
    const sourceId = text(raw.sourceId, 60);
    return {
      source,
      integrationId,
      view,
      size: oneOf(raw.size, ["s", "m", "l"] as const, "m"),
      nativeType,
      fingerprint,
      options,
      sourceId,
      refresh: refresh(raw.refresh),
      chain: raw.chain,
      ...dataDrawing(raw),
    };
  },
};

function dataDrawing(raw: Raw): DataDrawing {
  const annotation = isRecord(raw.annotation) ? raw.annotation : undefined;
  const annotationId = annotation ? text(annotation.id, 60) : undefined;
  const annotationFingerprint = annotation ? text(annotation.fingerprint, 20_000) : undefined;
  return {
    description: text(raw.description, 200),
    context: isRecord(raw.context) ? raw.context : {},
    annotation: annotationId && annotationFingerprint ? { id: annotationId, fingerprint: annotationFingerprint } : undefined,
    showProgressBar: raw.showProgressBar === true,
    showLastUpdated: raw.showLastUpdated === true,
  };
}

/** A `data` widget: a data source of the definition XTM One reads itself (`readDefinition`). */
function dataFields(raw: Record<string, unknown>) {
  const view = (DATA_VIEWS as readonly string[]).includes(raw.view as string) ? (raw.view as DataView) : undefined;
  const fingerprint = text(raw.fingerprint, 20_000);
  const sourceId = text(raw.sourceId, 60);
  const sourceType = (DATA_SOURCE_TYPES as readonly string[]).includes(raw.sourceType as string)
    ? (raw.sourceType as DataSourceType)
    : undefined;
  if (!fingerprint || !sourceId || !sourceType) return undefined;
  return {
    view,
    fingerprint,
    options: isRecord(raw.options) ? raw.options : {},
    sourceId,
    sourceType,
    refresh: refresh(raw.refresh),
    chain: raw.chain,
    ...dataDrawing(raw),
  };
}

/** A refresh in seconds, 30 s to a day (`refreshSeconds`). */
function refresh(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 30 && value <= 86_400 ? value : undefined;
}

const isKind = (value: unknown): value is DashboardWidgetKind =>
  typeof value === "string" && (DASHBOARD_WIDGET_KINDS as readonly string[]).includes(value);

// An element id of a definition (`ELEMENT_ID_RE`), which a widget id also is.
const WIDGET_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$/;

function normalizeWidget(raw: unknown, index: number, taken: Set<string>): DashboardWidget {
  const record = isRecord(raw) ? raw : {};
  // A missing or repeated id would make two widgets one React key.
  let id = typeof record.id === "string" && WIDGET_ID_RE.test(record.id) ? record.id : `widget-${index + 1}`;
  while (taken.has(id)) id = `${id}-${index + 1}`;
  taken.add(id);
  const common: WidgetCommon = {
    id,
    title: text(record.title, 120),
    width: oneOf(record.width, ["full", "half", "third"] as const, "full"),
    // Placed by `placeWidgets` when the stored one is missing or off the grid.
    layout: readCell(record.layout) as GridCell,
    link: action(record.link),
  };
  const handlers = readHandlers(record.handlers);
  if (handlers.length) common.handlers = handlers;
  const visibility = readVisibility(record.visibility);
  if (visibility) common.visibility = visibility;
  const kind = record.kind;
  if (kind === "input" && typeof record.inputId === "string") {
    return { ...common, kind: "input", inputId: record.inputId };
  }
  if (kind === "data") {
    const data = dataFields(record);
    return data ? { ...common, kind: "data", ...data } : { ...common, kind: "unsupported", originalKind: "data" };
  }
  const fields = isKind(kind) ? READERS[kind](record) : undefined;
  if (!isKind(kind) || !fields) {
    return { ...common, kind: "unsupported", originalKind: text(kind, 40) ?? "unknown" };
  }
  return { ...common, kind, ...fields } as DashboardWidget;
}

const NO_INPUTS: ReadInputs = { inputs: [], globalInputs: [], defaults: {}, expressions: {}, submitButton: false, submitOnLoad: true };

const NO_APP_PROPERTIES: DashboardAppProperties = {
  hideEdit: false,
  hideExport: false,
  hideOpenInPlatform: false,
  collapseNavigation: false,
};

/** The stored dashboard as the renderer draws it: a definition (version 2) or a widget list. Never throws. */
export function normalizeDashboard(raw: unknown): NormalizedDashboard {
  const record = isRecord(raw) ? raw : {};
  if (record.version === 2) {
    const definition = readDefinition(record);
    const taken = new Set<string>();
    let drawn = 0;
    const tabs: DashboardTab[] = definition.tabs.map((tab) => {
      const records = tab.records.slice(0, Math.max(0, MAX_RENDERED_WIDGETS - drawn));
      drawn += records.length;
      return { ...tab, widgets: placeWidgets(records.map((widget, index) => normalizeWidget(widget, index, taken))) };
    });
    return {
      title: text(definition.title, 120) ?? "",
      subtitle: text(definition.description, 200),
      widgets: tabs[0]?.widgets ?? [],
      tabs,
      showTabBar: definition.showTabBar,
      tabBarPosition: definition.tabBarPosition,
      ...(definition.theme ? { theme: definition.theme } : {}),
      appProperties: definition.appProperties,
      sources: definition.sources,
      inputs: definition.inputs,
      conditions: readConditions(record),
    };
  }
  const taken = new Set<string>();
  const widgets = placeWidgets(
    Array.isArray(record.widgets)
      ? record.widgets.slice(0, MAX_RENDERED_WIDGETS).map((widget, index) => normalizeWidget(widget, index, taken))
      : [],
  );
  return {
    title: text(record.title, 120) ?? "",
    subtitle: text(record.subtitle, 200),
    widgets,
    tabs: [{ id: "layout_1", label: "Overview", type: "grid", widgets }],
    showTabBar: false,
    appProperties: NO_APP_PROPERTIES,
    sources: {},
    inputs: NO_INPUTS,
    conditions: {},
  };
}

/** Points of a sparkline in a `width` x `height` box, oldest on the left; the
 *  lowest value at the bottom. Empty under two finite points. */
export function sparklinePoints(series: readonly number[], width: number, height: number, pad = 4): [number, number][] {
  const values = series.filter((v) => Number.isFinite(v));
  if (values.length < 2) return [];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = (width - pad * 2) / (values.length - 1);
  return values.map((v, i) => [pad + i * step, pad + (height - pad * 2) * (1 - (v - min) / span)]);
}
