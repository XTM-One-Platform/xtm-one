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
 * The one form engine of the platform source editors: it edits a platform
 * widget's stored configuration from the description the platform's adapter
 * serves (`GET /custom-dashboards/platform-forms`, `dashboard_platform/forms.py`).
 * It knows no platform: its types, fields, conditions, value sources and the
 * way each field reads and writes the configuration all come from the
 * description.
 *
 * - Expressions are JSON: an object whose one key starts with `$` is an
 *   operation (`evaluate`), anything else a literal evaluated item by item.
 *   Conditions are objects keyed by their operator (`holds`).
 * - A scope is where a field reads and writes: the configuration itself, or
 *   one item of a list of it, with the variables, parameters and schema
 *   answers (`sources`) its fields read.
 * - A value field writes in place, at its path or through its binding (the
 *   values of one filter of a group, a parameter of a `compose`), then runs
 *   its `onChange` effects. A key no field writes is kept as it was.
 *
 * Pure: `platform-form.test.ts` runs it on `node --test`.
 */

import type {
  PlatformFilterDialect,
  PlatformFormCond,
  PlatformFormDescription,
  PlatformFormEffect,
  PlatformFormExpr,
  PlatformFormField,
  PlatformFormMatch,
  PlatformFormSource,
} from "@/types";

import { asGroup, filterKey, newId, type PlatformFilter, type PlatformFilterGroup } from "./platform-native.ts";

type Raw = Record<string, unknown>;

/** A filter key as the platform's schema describes it. */
export interface FilterKeyOption {
  key?: string;
  label: string;
  type?: string;
  multiple?: boolean;
  values?: string[];
  dynamic?: boolean;
  operators?: string[];
}

/** Where fields read and write. */
export interface Scope {
  form: PlatformFormDescription;
  /** The configuration being edited. */
  root: Raw;
  /** Where this scope's object is in `root`. */
  path: string[];
  vars: Raw;
  params: Raw;
  /** The answers of the schema questions in reach; `null` until one is answered. */
  sources: Record<string, unknown[] | null>;
  /** In effects: the configuration before the change, and the value written. */
  old?: Raw;
  value?: unknown;
  /** Inside a `compose`: where its value is built, and how. */
  compose?: { path: string[]; build: PlatformFormExpr };
  /** Whether one of its schema questions is being answered. */
  fetching?: boolean;
}

const isObject = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const split = (path: string | undefined): string[] => (path ? path.split(".") : []);

// ── Paths ───────────────────────────────────────────────────────────────────

/** Where a field's `path` is in the configuration. */
export function pathOf(scope: Scope, path: string | undefined): string[] {
  return [...scope.path, ...split(path)];
}

export function getIn(value: unknown, parts: string[]): unknown {
  let current = value;
  for (const part of parts) {
    if (Array.isArray(current)) current = /^\d+$/.test(part) ? current[Number(part)] : undefined;
    else if (isObject(current)) current = current[part];
    else return undefined;
  }
  return current;
}

/** `target` with `value` at `parts`, copied along the way; `undefined` removes the key. */
export function setIn(target: unknown, parts: string[], value: unknown): unknown {
  if (parts.length === 0) return value;
  const [head, ...rest] = parts;
  const index = /^\d+$/.test(head) ? Number(head) : null;
  if (Array.isArray(target) && index !== null) {
    const copy = [...target];
    copy[index] = setIn(target[index], rest, value);
    return copy;
  }
  const copy: Raw = isObject(target) ? { ...target } : {};
  const next = setIn(copy[head], rest, value);
  if (next === undefined) delete copy[head];
  else copy[head] = next;
  return copy;
}

// ── Expressions and conditions ──────────────────────────────────────────────

const OPERATIONS = new Set([
  "$", "$root", "$old", "$value", "$attr", "$oldAttr", "$param", "$var", "$source", "$template", "$uuid", "$cond",
  "$coalesce", "$or", "$concat", "$unique", "$len", "$get", "$map", "$find", "$lower", "$filterValues", "$if",
]); // prettier-ignore

function operationOf(expr: Raw): string | null {
  const keys = Object.keys(expr).filter((key) => key.startsWith("$"));
  return keys.length === 1 && OPERATIONS.has(keys[0]) ? keys[0] : null;
}

/** The type the configuration is, and what the description says it takes. */
function typeOf(form: PlatformFormDescription, root: unknown) {
  const name = getIn(root, split(form.typePath));
  return form.types.find((type) => type.value === name);
}

function withVar(scope: Scope, name: string, value: unknown): Scope {
  return { ...scope, vars: { ...scope.vars, [name]: value } };
}

function deepEqual(a: unknown, b: unknown): boolean {
  const x = a === undefined ? null : a;
  const y = b === undefined ? null : b;
  if (Array.isArray(x) && Array.isArray(y)) return x.length === y.length && x.every((item, i) => deepEqual(item, y[i]));
  if (isObject(x) && isObject(y)) {
    const keys = new Set([...Object.keys(x), ...Object.keys(y)]);
    return [...keys].every((key) => deepEqual(x[key], y[key]));
  }
  return x === y;
}

function stringsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Whether a filter is the one a match is about. */
export function matches(filter: PlatformFilter, match: PlatformFormMatch, scope: Scope): boolean {
  const key = evaluate(match.key, scope);
  if (match.operator && filter.operator !== match.operator) return false;
  if (match.exact) return Array.isArray(filter.key) ? filter.key.length === 1 && filter.key[0] === key : filter.key === key;
  return filterKey(filter) === key;
}

function filterValuesOf(group: unknown, match: PlatformFormMatch, scope: Scope): string[] {
  const filters = isObject(group) && Array.isArray(group.filters) ? (group.filters as PlatformFilter[]) : [];
  const found = filters.find((filter) => isObject(filter) && matches(filter, match, scope));
  return stringsOf(found?.values);
}

export function evaluate(expr: PlatformFormExpr, scope: Scope): unknown {
  if (Array.isArray(expr)) {
    const out: unknown[] = [];
    for (const item of expr) {
      if (isObject(item) && operationOf(item) === "$if") {
        if (holds(item.$if as PlatformFormCond, scope)) out.push(evaluate(item.then, scope));
      } else {
        out.push(evaluate(item, scope));
      }
    }
    return out;
  }
  if (!isObject(expr)) return expr;
  const op = operationOf(expr);
  if (op === null) {
    const out: Raw = {};
    for (const [key, value] of Object.entries(expr)) {
      const evaluated = evaluate(value, scope);
      if (evaluated !== undefined) out[key] = evaluated;
    }
    return out;
  }
  const arg = expr[op];
  const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
  switch (op) {
    case "$":
      return getIn(scope.root, [...scope.path, ...split(arg as string)]);
    case "$root":
      return getIn(scope.root, split(arg as string));
    case "$old":
      return getIn(scope.old, split(arg as string));
    case "$value":
      return getIn(scope.value, split(arg as string));
    case "$attr":
      return getIn(typeOf(scope.form, scope.root)?.attrs, split(arg as string));
    case "$oldAttr":
      return getIn(typeOf(scope.form, scope.old)?.attrs, split(arg as string));
    case "$param":
      return getIn(scope.params, split(arg as string));
    case "$var": {
      const [name, ...rest] = split(arg as string);
      return getIn(scope.vars[name], rest);
    }
    case "$source":
      return scope.sources[arg as string] ?? null;
    case "$template": {
      const params = evaluate(expr.with ?? {}, scope) as Raw;
      return evaluate(scope.form.templates?.[arg as string], { ...scope, params });
    }
    case "$uuid":
      return newId();
    case "$cond":
      return evaluate(holds(arg as PlatformFormCond, scope) ? expr.then : expr.else, scope);
    case "$coalesce": {
      // JavaScript `??`: the first value set, else the last one.
      let value: unknown;
      for (const item of list(arg)) {
        value = evaluate(item, scope);
        if (value !== undefined && value !== null) return value;
      }
      return value;
    }
    case "$or": {
      let value: unknown;
      for (const item of list(arg)) {
        value = evaluate(item, scope);
        if (value) return value;
      }
      return value;
    }
    case "$concat":
      return list(arg).flatMap((item) => list(evaluate(item, scope)));
    case "$unique": {
      const seen = new Set<string>();
      return list(evaluate(arg, scope)).filter((item) => {
        if (item === undefined || item === null || item === "") return false;
        const key = JSON.stringify(item);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    }
    case "$len": {
      const value = evaluate(arg, scope);
      return Array.isArray(value) || typeof value === "string" ? value.length : 0;
    }
    case "$get": {
      const [table, key] = list(arg).map((item) => evaluate(item, scope));
      return key === undefined || key === null ? undefined : getIn(table, [String(key)]);
    }
    case "$map":
    case "$find": {
      const name = typeof expr.as === "string" ? expr.as : "item";
      const out: unknown[] = [];
      for (const item of list(evaluate(arg, scope))) {
        const inner = withVar(scope, name, item);
        if (expr.where && !holds(expr.where as PlatformFormCond, inner)) continue;
        const value = expr.to === undefined ? item : evaluate(expr.to, inner);
        if (op === "$find") return value;
        out.push(value);
      }
      return op === "$find" ? undefined : out;
    }
    case "$lower": {
      const value = evaluate(arg, scope);
      return typeof value === "string" ? value.toLowerCase() : "";
    }
    case "$filterValues": {
      const spec = arg as { group: PlatformFormExpr; match: PlatformFormMatch };
      return filterValuesOf(evaluate(spec.group, scope), spec.match, scope);
    }
    default:
      return undefined;
  }
}

export function holds(cond: PlatformFormCond | undefined, scope: Scope): boolean {
  if (!cond) return true;
  const pair = (value: unknown) => (Array.isArray(value) ? value.map((item) => evaluate(item, scope)) : [undefined, undefined]);
  if ("all" in cond) return (cond.all as PlatformFormCond[]).every((item) => holds(item, scope));
  if ("any" in cond) return (cond.any as PlatformFormCond[]).some((item) => holds(item, scope));
  if ("not" in cond) return !holds(cond.not as PlatformFormCond, scope);
  if ("eq" in cond) return deepEqual(...(pair(cond.eq) as [unknown, unknown]));
  if ("ne" in cond) return !deepEqual(...(pair(cond.ne) as [unknown, unknown]));
  if ("in" in cond) {
    const [value, among] = pair(cond.in);
    return Array.isArray(among) && among.some((item) => deepEqual(item, value));
  }
  if ("gt" in cond || "gte" in cond || "lt" in cond) {
    const [a, b] = pair(cond.gt ?? cond.gte ?? cond.lt);
    if (typeof a !== "number" || typeof b !== "number") return false;
    return "gt" in cond ? a > b : "gte" in cond ? a >= b : a < b;
  }
  if ("truthy" in cond) return Boolean(evaluate(cond.truthy, scope));
  if ("absent" in cond) {
    const value = evaluate(cond.absent, scope);
    return value === undefined || value === null;
  }
  if ("isArray" in cond) {
    const value = evaluate(cond.isArray, scope);
    if (!Array.isArray(value)) return false;
    if (cond.of === "object" && !value.every(isObject)) return false;
    return typeof cond.min !== "number" || value.length >= cond.min;
  }
  if ("isString" in cond) return typeof evaluate(cond.isString, scope) === "string";
  if ("endsWith" in cond) {
    const [value, suffix] = pair(cond.endsWith);
    return typeof value === "string" && typeof suffix === "string" && value.endsWith(suffix);
  }
  return false;
}

// ── Scopes ──────────────────────────────────────────────────────────────────

function withVars(scope: Scope, vars: Record<string, PlatformFormExpr> | undefined): Scope {
  let out = scope;
  for (const [name, expr] of Object.entries(vars ?? {})) out = withVar(out, name, evaluate(expr, out));
  return out;
}

/** The configuration's own scope; `sources` are the answers of its schema questions. */
export function rootScope(form: PlatformFormDescription, root: Raw, sources: Scope["sources"] = {}): Scope {
  return withVars({ form, root, path: [], vars: {}, params: {}, sources }, form.vars);
}

/** The scope of a list's item (`index`), a section's object or a compose's parameters. */
export function childScope(parent: Scope, field: PlatformFormField, index?: number): Scope {
  if (field.kind === "compose") {
    const params = evaluate(field.params ?? {}, parent) as Raw;
    return { ...parent, params, compose: { path: [...parent.path, ...split(field.path)], build: field.build } };
  }
  const at = [...parent.path, ...split(field.path)];
  const items = getIn(parent.root, at);
  const vars =
    index === undefined
      ? { ...parent.vars, index: 0, count: 1 }
      : { ...parent.vars, index, count: Array.isArray(items) ? items.length : 0 };
  const path = index === undefined ? at : [...at, String(index)];
  return withVars({ ...parent, path, vars }, field.vars);
}

/** The schema questions a scope asks, by source name; `null` for one it does not ask now. */
export function questionsOf(sources: Record<string, PlatformFormSource> | undefined, scope: Scope) {
  const out: Record<string, { kind: PlatformFormSource["kind"]; perspective?: string; types?: string[] } | null> = {};
  for (const [name, source] of Object.entries(sources ?? {})) {
    if (!holds(source.when, scope)) {
      out[name] = null;
      continue;
    }
    const perspective = evaluate(source.perspective, scope);
    const types = evaluate(source.types, scope);
    out[name] = {
      kind: source.kind,
      ...(typeof perspective === "string" ? { perspective } : {}),
      ...(Array.isArray(types) ? { types: stringsOf(types) } : {}),
    };
  }
  return out;
}

// ── Reading and writing ─────────────────────────────────────────────────────

/** What a value field shows. */
export function readField(field: PlatformFormField, scope: Scope): unknown {
  const bind = field.bind;
  if (bind && "filterValues" in bind) {
    const spec = bind.filterValues;
    const values = filterValuesOf(getIn(scope.root, [...scope.path, ...split(spec.group)]), spec.match, scope);
    return spec.single ? values[0] : values;
  }
  if (bind && "param" in bind) return scope.params[bind.param];
  return getIn(scope.root, [...scope.path, ...split(field.path)]);
}

/** A group with the values of one filter changed in place, added first, or removed when none is left. */
function writeFilterValues(
  value: unknown,
  spec: { match: PlatformFormMatch; new?: PlatformFormExpr; clearOthers?: boolean },
  scope: Scope,
  values: string[],
): PlatformFilterGroup {
  const group = asGroup(value, scope.form.filters?.nested ?? false);
  let filters = [...group.filters];
  if (spec.clearOthers) filters = filters.filter((filter) => matches(filter, spec.match, scope));
  const index = filters.findIndex((filter) => matches(filter, spec.match, scope));
  if (index >= 0) {
    if (values.length) filters[index] = { ...filters[index], values };
    else filters.splice(index, 1);
  } else if (values.length) {
    filters.unshift(evaluate(spec.new, { ...scope, params: { key: evaluate(spec.match.key, scope), values } }) as PlatformFilter);
  }
  return { ...group, mode: group.mode ?? "and", filters };
}

/**
 * What a text or number field stores for what was typed: a text cut to its
 * length (emptied, removed when the field says so), a number held to its
 * bounds (emptied, what its `empty` says: `null`, removed, its default, its least).
 */
function stored(field: PlatformFormField, scope: Scope, value: unknown): unknown {
  if (field.kind === "text") {
    const text = (typeof value === "string" ? value : "").slice(0, numberOf(evaluate(field.max, scope)) ?? 200);
    return field.empty === "remove" && !text ? undefined : text;
  }
  if (field.kind === "number") {
    const min = numberOf(evaluate(field.min, scope)) ?? Number.MIN_SAFE_INTEGER;
    const max = numberOf(evaluate(field.max, scope)) ?? Number.MAX_SAFE_INTEGER;
    const number = numberOf(value);
    if (number === undefined) {
      const empty = field.empty ?? "null";
      return empty === "null" ? null : empty === "default" ? numberOf(evaluate(field.default, scope)) : empty === "min" ? min : undefined;
    }
    return Math.max(min, Math.min(max, field.decimal ? number : Math.round(number)));
  }
  return value;
}

/** The configuration with `value` written by `field`, its effects run. */
export function writeField(field: PlatformFormField, scope: Scope, typed: unknown): Raw {
  const old = scope.root;
  const value = stored(field, scope, typed);
  let root: unknown = scope.root;
  const bind = field.bind;
  if (bind && "filterValues" in bind) {
    const spec = bind.filterValues;
    const at = [...scope.path, ...split(spec.group)];
    const values = spec.single ? (value === undefined || value === null || value === "" ? [] : [String(value)]) : stringsOf(value);
    root = setIn(root, at, writeFilterValues(getIn(root, at), spec, scope, values));
  } else if (bind && "param" in bind) {
    if (!scope.compose) return scope.root;
    const params = { ...scope.params, [bind.param]: value };
    root = setIn(root, scope.compose.path, evaluate(scope.compose.build, { ...scope, params }));
  } else {
    root = setIn(root, [...scope.path, ...split(field.path)], value);
  }
  if (field.onChange?.length) root = runEffects(field.onChange, { ...scope, root: root as Raw, old, value });
  return root as Raw;
}

function findField(fields: PlatformFormField[], id: string): PlatformFormField | undefined {
  for (const field of fields) {
    if (field.id === id) return field;
    if (field.kind === "row") {
      const found = findField(field.fields ?? [], id);
      if (found) return found;
    }
  }
  return undefined;
}

function runEffects(effects: PlatformFormEffect[], scope: Scope): Raw {
  let root = scope.root;
  for (const effect of effects) root = runEffect(effect, { ...scope, root });
  return root;
}

function runEffect(effect: PlatformFormEffect, scope: Scope): Raw {
  const at = (path: string) => [...scope.path, ...split(path)];
  if ("set" in effect) return setIn(scope.root, at(effect.set), evaluate(effect.value, scope)) as Raw;
  if ("remove" in effect) return setIn(scope.root, at(effect.remove), undefined) as Raw;
  if ("truncate" in effect) {
    const items = getIn(scope.root, at(effect.truncate));
    const max = evaluate(effect.max, scope);
    return setIn(scope.root, at(effect.truncate), (Array.isArray(items) ? items : []).slice(0, typeof max === "number" ? max : undefined)) as Raw;
  }
  if ("each" in effect) {
    const items = getIn(scope.root, at(effect.each));
    let root = scope.root;
    (Array.isArray(items) ? items : []).forEach((_, index) => {
      const item = { ...scope, root, path: [...at(effect.each), String(index)] };
      if (holds(effect.when, item)) root = runEffects(effect.do, item);
    });
    return root;
  }
  if ("if" in effect) return runEffects((holds(effect.if, scope) ? effect.then : effect.else) ?? [], scope);
  if ("change" in effect) {
    const target = findField(scope.form.fields, effect.change);
    if (!target) return scope.root;
    return writeField(target, rootScope(scope.form, scope.root, scope.sources), evaluate(effect.value, scope));
  }
  // rebuild: the current type's new widget, with what it carries over.
  const type = typeOf(scope.form, scope.root)?.value;
  let fresh: unknown = newWidget(scope.form, type, evaluate(effect.rebuild, scope) as Raw);
  for (const path of effect.carry ?? []) fresh = setIn(fresh, split(path), getIn(scope.root, split(path)));
  return fresh as Raw;
}

// ── Widgets ─────────────────────────────────────────────────────────────────

/** A new widget of `type` (the description's default type), as the platform's editor writes it. */
export function newWidget(form: PlatformFormDescription, type?: string, params: Raw = {}): Raw {
  const chosen = form.types.find((t) => t.value === type) ?? form.types.find((t) => t.value === form.default.type) ?? form.types[0];
  const scope: Scope = { form, root: {}, path: [], vars: {}, params: { ...(form.default.params ?? {}), ...params }, sources: {} };
  const out = evaluate(chosen?.new, scope);
  return isObject(out) ? out : {};
}

/**
 * Whether the form can show this stored configuration. It edits the
 * configuration in place and writes back every key it does not change, so
 * what it can show it keeps; anything else opens in the source editor.
 */
export function formCarries(form: PlatformFormDescription, native: unknown): boolean {
  if (!isObject(native) || !typeOf(form, native)) return false;
  return holds(form.carries, rootScope(form, native));
}

/**
 * The field shown under this name (its `id`, else its `path`) and its scope,
 * as a person reaches it in the form: `items` picks the item of each list on
 * the way, and a field its condition hides is not found.
 */
export function locate(form: PlatformFormDescription, native: Raw, name: string, items: number[] = []) {
  const walk = (fields: PlatformFormField[], scope: Scope, rest: number[]): { field: PlatformFormField; scope: Scope } | undefined => {
    for (const field of fields) {
      if (!holds(field.when, scope)) continue;
      if (field.id === name || (!field.id && field.path === name)) return { field, scope };
      if (!field.fields) continue;
      const kind = field.kind;
      const inner = kind === "row" ? scope : kind === "list" ? childScope(scope, field, rest[0] ?? 0) : childScope(scope, field);
      const found = walk(field.fields, inner, kind === "list" ? rest.slice(1) : rest);
      if (found) return found;
    }
    return undefined;
  };
  return walk(form.fields, rootScope(form, native), items);
}

/** `native` with the field shown as `name` set to `value` (`locate`). */
export function edit(form: PlatformFormDescription, native: Raw, name: string, value: unknown, items: number[] = []): Raw {
  const found = locate(form, native, name, items);
  if (!found) throw new Error(`No field ${name} is shown`);
  return writeField(found.field, found.scope, value);
}

/** How many items a list holds, and may hold. */
export function listBounds(field: PlatformFormField, scope: Scope) {
  const items = getIn(scope.root, pathOf(scope, field.path));
  return {
    count: Array.isArray(items) ? items.length : 0,
    min: numberOf(evaluate(field.min, scope)) ?? 1,
    max: numberOf(evaluate(field.max, scope)) ?? Number.MAX_SAFE_INTEGER,
  };
}

/** Whether a list's button `add` adds an item now. */
export function addable(field: PlatformFormField, scope: Scope, add: number): boolean {
  const button = field.add?.[add];
  if (!button || !holds(button.when, scope)) return false;
  const { count, max } = listBounds(field, scope);
  return count < max && !(button.disabled !== undefined && holds(button.disabled, scope));
}

/** A list with one more item (`add` names which of its buttons). */
export function addItem(field: PlatformFormField, scope: Scope, add: number): Raw {
  const at = [...scope.path, ...split(field.path)];
  const items = getIn(scope.root, at);
  const item = evaluate(field.add?.[add]?.item, scope);
  return setIn(scope.root, at, [...(Array.isArray(items) ? items : []), item]) as Raw;
}

export function removeItem(field: PlatformFormField, scope: Scope, index: number): Raw {
  const at = [...scope.path, ...split(field.path)];
  const items = getIn(scope.root, at);
  return setIn(scope.root, at, (Array.isArray(items) ? items : []).filter((_, i) => i !== index)) as Raw;
}

// ── Choices ─────────────────────────────────────────────────────────────────

/** A choice as a control shows it: `label` is an i18n key, `text` is shown as it is. */
export interface FormChoice {
  value: string;
  label: string;
  group?: string;
  /** Its tile's icon, a Lucide name. */
  icon?: string;
}

/** The choices an expression gives, labels translated by `t`. */
export function choicesOf(expr: PlatformFormExpr, scope: Scope, t: (key: string) => string): FormChoice[] {
  const out: FormChoice[] = [];
  const items = evaluate(expr, scope);
  for (const item of Array.isArray(items) ? items : []) {
    if (typeof item === "string") out.push({ value: item, label: item });
    else if (isObject(item) && typeof item.value === "string") {
      const label = typeof item.label === "string" ? t(item.label) : typeof item.text === "string" ? item.text : item.value;
      out.push({
        value: item.value,
        label,
        ...(typeof item.group === "string" ? { group: item.group } : {}),
        ...(typeof item.icon === "string" ? { icon: item.icon } : {}),
      });
    }
  }
  return out;
}

/** A number as stored (a number written as text included), or `undefined`. */
export function numberOf(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(parsed) ? parsed : undefined;
}

// ── Filter dialect ──────────────────────────────────────────────────────────

function ruleValue(rules: { when?: PlatformFormCond; value: PlatformFormExpr }[] | undefined, scope: Scope): unknown {
  for (const rule of rules ?? []) if (holds(rule.when, scope)) return evaluate(rule.value, scope);
  return undefined;
}

/** The scope a dialect's rules read: the filter key (`key`) and its lowercased type (`type`). */
function keyScope(form: PlatformFormDescription, key: FilterKeyOption | undefined): Scope {
  return { form, root: {}, path: [], vars: { key, type: (key?.type ?? "").toLowerCase() }, params: {}, sources: {} };
}

/** The operators the platform offers for a filter key. */
export function operatorsFor(form: PlatformFormDescription, key: FilterKeyOption | undefined): string[] {
  return stringsOf(ruleValue(form.filters?.operators, keyScope(form, key)));
}

/** The operator a new filter on this key starts with. */
export function defaultOperatorFor(form: PlatformFormDescription, key: FilterKeyOption | undefined): string {
  const chosen = ruleValue(form.filters?.defaultOperator, keyScope(form, key));
  return typeof chosen === "string" ? chosen : (operatorsFor(form, key)[0] ?? "eq");
}

/** Whether the values of this key are searched in the platform (rather than typed). */
export function searchedFor(form: PlatformFormDescription, key: FilterKeyOption | undefined): boolean {
  return holds(form.filters?.searched ?? { truthy: false }, keyScope(form, key));
}

/** A new filter on `key`, as the platform writes one. */
export function newFilter(dialect: PlatformFilterDialect, key: string, operator: string): PlatformFilter {
  return dialect.ids
    ? { id: newId(), key: dialect.keyForm === "list" ? [key] : key, mode: dialect.newFilterMode, values: [], operator }
    : { key: dialect.keyForm === "list" ? [key] : key, values: [], operator, mode: dialect.newFilterMode };
}
