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
 * What a click on a visualization does, and when a visualization shows.
 *
 * A visualization's `eventHandlers` (`dashboard_definition.EventHandler`) run
 * in order on a click - or a brush over a series - and :func:`planHandlers`
 * says what they amount to: tokens to set or unset (from what the click gives:
 * `name`, `value`, `x`, `y`, `row.<field>.value`, or a value written with
 * the page's and the click's tokens - `$click.value$`), a link to follow (a
 * page of the web app, another dashboard opened with `?form.<token>=`, an
 * http(s) address or the platform's page, opened in a new tab), a tab to
 * show. :func:`isVisible` reads `containerOptions.visibility` against the
 * definition's `expressions.conditions`.
 *
 * Pure: `interactions.test.ts` runs it on `node --test`.
 */
import type { WidgetData } from "../../types";
import { fill, holds, type TokenValue, type Tokens } from "./tokens.ts";

type Raw = Record<string, unknown>;
const record = (value: unknown): Raw => (typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Raw) : {});

/** What a click (or a brush) gives. */
export interface ClickEvent {
  /** The series, slice or column clicked. */
  name?: string;
  value?: string;
  x?: string;
  y?: string;
  /** The row of a table or a list clicked (its `_url`, a link into the platform built by the server). */
  row?: Record<string, string>;
  /** A brush: the first and the last x it covers. */
  range?: { start: string; end: string };
}

export interface EventHandler {
  type: string;
  options: Raw;
}

export interface Visibility {
  showConditions: string[];
  hideConditions: string[];
  hideWhenNoData: boolean;
}

const HANDLER_TYPES = new Set([
  "drilldown.setToken",
  "drilldown.unsetToken",
  "drilldown.linkToUrl",
  "drilldown.linkToDashboard",
  "drilldown.openInPlatform",
  "drilldown.switchTab",
  "drilldown.setTimeRange",
]);
const TOKEN_NAME = /^[A-Za-z_][A-Za-z0-9_.]{0,59}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A visualization's handlers as stored, those this page knows kept in order. */
export function readHandlers(value: unknown): EventHandler[] {
  return (Array.isArray(value) ? value : [])
    .slice(0, 10)
    .map((item) => record(item))
    .filter((item) => typeof item.type === "string" && HANDLER_TYPES.has(item.type))
    .map((item) => ({ type: item.type as string, options: record(item.options) }));
}

export function readVisibility(value: unknown): Visibility | undefined {
  const raw = record(record(value).visibility);
  const ids = (list: unknown) => (Array.isArray(list) ? list.filter((id): id is string => typeof id === "string").slice(0, 10) : []);
  const visibility = { showConditions: ids(raw.showConditions), hideConditions: ids(raw.hideConditions), hideWhenNoData: raw.hideWhenNoData === true };
  return visibility.showConditions.length || visibility.hideConditions.length || visibility.hideWhenNoData ? visibility : undefined;
}

/** The click's own tokens (`click.name`, `click.value`, `click.x`, `click.y`, `click.row.<field>`). */
export function clickTokens(event: ClickEvent): Tokens {
  const out: Tokens = {};
  for (const key of ["name", "value", "x", "y"] as const) if (event[key] !== undefined && event[key] !== "") out[`click.${key}`] = String(event[key]);
  for (const [field, value] of Object.entries(event.row ?? {})) if (field !== "_url" && value !== "") out[`click.row.${field}`] = value;
  return out;
}

function fromClick(key: string, event: ClickEvent): string | undefined {
  if (key === "name" || key === "value" || key === "x" || key === "y") return event[key];
  const field = /^row\.(.+)\.value$/.exec(key)?.[1];
  return field ? event.row?.[field] : undefined;
}

export interface HandlerPlan {
  /** Tokens to set (a value) or unset (`undefined`), in the order the handlers gave them. */
  tokens: [string, TokenValue | undefined][];
  link?: { url: string; newTab: boolean };
  tab?: string;
}

/** What `handlers` amount to for `event`, read with the page's `tokens` (and the platform page `openUrl`, if any). */
export function planHandlers(handlers: EventHandler[], event: ClickEvent, tokens: Tokens, openUrl?: string | null): HandlerPlan {
  const plan: HandlerPlan = { tokens: [] };
  const withClick = { ...tokens, ...clickTokens(event) };
  for (const handler of handlers) {
    const options = handler.options;
    switch (handler.type) {
      case "drilldown.setToken":
        for (const entry of Array.isArray(options.tokens) ? options.tokens : []) {
          const item = record(entry);
          if (typeof item.token !== "string" || !TOKEN_NAME.test(item.token)) continue;
          const value =
            typeof item.key === "string" ? fromClick(item.key, event) : typeof item.value === "string" ? (fill(item.value, withClick) ?? undefined) : undefined;
          if (value !== undefined) {
            plan.tokens.push([item.token, value.slice(0, 500)]);
            withClick[item.token] = value;
          }
        }
        break;
      case "drilldown.unsetToken":
        for (const entry of Array.isArray(options.tokens) ? options.tokens : []) {
          const token = record(entry).token;
          if (typeof token === "string" && TOKEN_NAME.test(token)) {
            plan.tokens.push([token, undefined]);
            delete withClick[token];
          }
        }
        break;
      case "drilldown.linkToUrl": {
        const url = typeof options.url === "string" ? fill(options.url, withClick, "url") : null;
        if (url) plan.link = { url, newTab: options.newTab === true };
        break;
      }
      case "drilldown.linkToDashboard": {
        const id = typeof options.dashboardId === "string" && UUID.test(options.dashboardId) ? options.dashboardId : null;
        if (!id) break;
        const params = new URLSearchParams();
        for (const entry of Array.isArray(options.tokens) ? options.tokens : []) {
          const item = record(entry);
          const value = typeof item.value === "string" ? fill(item.value, withClick) : null;
          if (typeof item.token === "string" && TOKEN_NAME.test(item.token) && value) params.append(`form.${item.token}`, value.slice(0, 500));
        }
        const query = params.toString();
        plan.link = { url: `/dashboards/${id}${query ? `?${query}` : ""}`, newTab: options.newTab === true };
        break;
      }
      case "drilldown.openInPlatform": {
        const url = event.row?._url ?? openUrl ?? undefined;
        if (url) plan.link = { url, newTab: true };
        break;
      }
      case "drilldown.switchTab":
        if (typeof options.layoutId === "string") plan.tab = options.layoutId;
        break;
      case "drilldown.setTimeRange":
        if (event.range && typeof options.token === "string" && /^[A-Za-z_][A-Za-z0-9_]{0,49}$/.test(options.token)) {
          plan.tokens.push([`${options.token}.earliest`, event.range.start], [`${options.token}.latest`, event.range.end]);
        }
        break;
    }
  }
  return plan;
}

/** Whether a visualization shows: every show condition holds, no hide condition does, and it has data unless asked not to care. */
export function isVisible(visibility: Visibility | undefined, conditions: Record<string, string>, tokens: Tokens, empty: boolean): boolean {
  if (!visibility) return true;
  if (visibility.hideWhenNoData && empty) return false;
  const expression = (id: string) => conditions[id];
  if (visibility.showConditions.some((id) => !expression(id) || !holds(expression(id), tokens))) return false;
  return !visibility.hideConditions.some((id) => expression(id) && holds(expression(id), tokens));
}

/** The definition's conditions (`expressions.conditions.<id>.value`). */
export function readConditions(definition: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [id, condition] of Object.entries(record(record(record(definition).expressions).conditions)).slice(0, 40)) {
    const value = record(condition).value;
    if (typeof value === "string") out[id] = value.slice(0, 300);
  }
  return out;
}

/** The tokens a dashboard is opened with: `?form.<token>=value` (repeated for a list), bounded. */
export function formTokens(search: string): Tokens {
  const out: Tokens = {};
  const params = new URLSearchParams(search);
  for (const [key, value] of params) {
    const name = key.startsWith("form.") ? key.slice(5) : "";
    if (!TOKEN_NAME.test(name) || Object.keys(out).length >= 20) continue;
    const text = value.slice(0, 500);
    const previous = out[name];
    out[name] = previous === undefined ? text : (Array.isArray(previous) ? [...previous, text] : [previous, text]).slice(0, 50);
  }
  return out;
}

/** Whether a read has no data to show (an empty result, a source waiting or with no run): `undefined` while unknown. */
export function hasNoData(data: WidgetData | undefined): boolean | undefined {
  if (!data) return undefined;
  if (data.status === "error") return data.code === "waiting" || data.code === "no_result" || data.code === "not_run";
  switch (data.shape) {
    case "number":
      return false;
    case "series":
      return data.data.series.every((series) => series.points.length === 0);
    case "distribution":
      return data.data.items.length === 0;
    case "rows":
      return data.data.rows.length === 0;
    case "text":
      return data.data.text.trim() === "";
    case "geo":
      return data.data.points.length === 0 && data.data.regions.length === 0;
    case "graph":
      return data.data.nodes.length === 0;
    case "events":
      return data.data.events.length === 0;
    case "tree":
    case "outcomes":
      return data.data.items.length === 0;
  }
}

// ── The studio's editor ─────────────────────────────────────────────────────

type SetOperation = { op: "set"; path: string; value: unknown };

/** The operation writing a visualization's handlers (the rest of it kept). */
export function handlersOperation(definition: unknown, vid: string, handlers: EventHandler[]): SetOperation {
  const viz = record(record(record(definition).visualizations)[vid]);
  return { op: "set", path: `visualizations.${vid}`, value: { ...viz, eventHandlers: handlers } };
}

/** A visualization's visibility as the editor shows it: its own show and hide expressions, and the no-data switch. */
export function visibilityForm(definition: unknown, vid: string): { show: string; hide: string; hideWhenNoData: boolean } {
  const conditions = readConditions(definition);
  const visibility = readVisibility(record(record(record(definition).visualizations)[vid]).containerOptions);
  return {
    show: (visibility?.showConditions ?? []).map((id) => conditions[id]).filter(Boolean).join(" && "),
    hide: (visibility?.hideConditions ?? []).map((id) => conditions[id]).filter(Boolean).join(" || "),
    hideWhenNoData: visibility?.hideWhenNoData ?? false,
  };
}

/**
 * The operations writing a visualization's visibility: its own conditions
 * (`show_<id>`, `hide_<id>` in `expressions.conditions`, removed when empty),
 * then the visualization naming them.
 */
export function visibilityOperations(
  definition: unknown,
  vid: string,
  form: { show: string; hide: string; hideWhenNoData: boolean },
): SetOperation[] {
  const raw = record(definition);
  const expressions = record(raw.expressions);
  const conditions = { ...record(expressions.conditions) };
  const showId = `show_${vid}`.slice(0, 60);
  const hideId = `hide_${vid}`.slice(0, 60);
  delete conditions[showId];
  delete conditions[hideId];
  if (form.show.trim()) conditions[showId] = { name: `Show ${vid}`, value: form.show.trim() };
  if (form.hide.trim()) conditions[hideId] = { name: `Hide ${vid}`, value: form.hide.trim() };
  const viz = record(record(raw.visualizations)[vid]);
  const visibility = {
    showConditions: form.show.trim() ? [showId] : [],
    hideConditions: form.hide.trim() ? [hideId] : [],
    hideWhenNoData: form.hideWhenNoData,
  };
  return [
    { op: "set", path: "expressions", value: { ...expressions, conditions } },
    { op: "set", path: `visualizations.${vid}`, value: { ...viz, containerOptions: { ...record(viz.containerOptions), visibility } } },
  ];
}
