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
 * A definition's inputs and tokens as the page uses them, read defensively.
 *
 * The server validates every write (`dashboard_definition.Input`); this reads
 * a stored row, so an input it does not understand is left out rather than
 * drawn wrong. Also read here: the token defaults (`defaults.tokens.default`),
 * the computed tokens (`expressions.tokens`) and the submit switch
 * (`layout.options`). Pure: `inputs.test.ts` runs it on `node --test`.
 */
import type { TokenValue, Tokens } from "./tokens.ts";
import { cellText } from "./viz/shapes.ts";

type Raw = Record<string, unknown>;
const isRecord = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const record = (value: unknown): Raw => (isRecord(value) ? value : {});
const text = (value: unknown, max: number): string | undefined =>
  typeof value === "string" && value.trim() ? value.slice(0, max) : undefined;
const finite = (value: unknown): number | undefined => (typeof value === "number" && Number.isFinite(value) ? value : undefined);

const INPUT_KINDS = ["timerange", "dropdown", "multiselect", "text", "number", "button", "entity"] as const;
export type InputKind = (typeof INPUT_KINDS)[number];

export type InputAction =
  | { type: "setToken"; token: string; value: string }
  | { type: "unsetToken"; token: string }
  | { type: "link"; url: string; newTab: boolean }
  | { type: "switchTab"; layoutId: string };

export interface DashboardInput {
  id: string;
  kind: InputKind;
  title?: string;
  token?: string;
  defaultValue?: TokenValue;
  items: { label: string; value: string }[];
  /** A dropdown's or multiselect's data source, with the fields giving its values and labels. */
  sourceId?: string;
  valueField?: string;
  labelField?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  /** A button's label and actions. */
  label?: string;
  actions: InputAction[];
  /** An entity input: the platform and integration searched, the types its entity may be. */
  entitySource?: string;
  integrationId?: string;
  entityTypes?: string[];
}

const TOKEN_NAME = /^[A-Za-z_][A-Za-z0-9_.]{0,59}$/;

function action(value: unknown): InputAction | undefined {
  const raw = record(value);
  const token = typeof raw.token === "string" && TOKEN_NAME.test(raw.token) ? raw.token : undefined;
  if (raw.type === "setToken" && token && typeof raw.value === "string") return { type: "setToken", token, value: raw.value.slice(0, 500) };
  if (raw.type === "unsetToken" && token) return { type: "unsetToken", token };
  if (raw.type === "link" && typeof raw.url === "string") return { type: "link", url: raw.url.slice(0, 1000), newTab: raw.newTab === true };
  if (raw.type === "switchTab" && typeof raw.layoutId === "string") return { type: "switchTab", layoutId: raw.layoutId };
  return undefined;
}

function tokenValue(value: unknown): TokenValue | undefined {
  if (typeof value === "string") return value.slice(0, 500);
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === "string").slice(0, 50);
  return undefined;
}

/** The operation keeping *value* as a stored input's default (the whole input is written: an id may hold a dot),
 *  or `null` when the definition has no such input. */
export function keepInputOperation(definition: unknown, inputId: string, value: TokenValue): { op: "set"; path: string; value: unknown } | null {
  const stored = record(record(record(definition).inputs)[inputId]);
  if (!Object.keys(stored).length) return null;
  return { op: "set", path: `inputs.${inputId}`, value: { ...stored, defaultValue: value } };
}

/** One stored input as the page draws it, or `undefined` when it cannot be. */
export function readInput(id: string, value: unknown): DashboardInput | undefined {
  const raw = record(value);
  const kind = typeof raw.type === "string" ? raw.type.replace(/^input\./, "") : "";
  if (!(INPUT_KINDS as readonly string[]).includes(kind)) return undefined;
  const options = record(raw.options);
  const token = typeof raw.token === "string" && TOKEN_NAME.test(raw.token) ? raw.token : undefined;
  if (kind !== "button" && !token) return undefined;
  const items = (Array.isArray(options.items) ? options.items : [])
    .slice(0, 200)
    .map((item) => {
      if (typeof item === "string") return { label: item, value: item };
      const entry = record(item);
      return typeof entry.value === "string" ? { label: text(entry.label, 120) ?? entry.value, value: entry.value } : undefined;
    })
    .filter((item): item is { label: string; value: string } => !!item);
  const actions = (Array.isArray(options.actions) ? options.actions : []).slice(0, 10).map(action).filter((a): a is InputAction => !!a);
  const primary = record(raw.dataSources).primary;
  return {
    id,
    kind: kind as InputKind,
    title: text(raw.title, 120),
    token,
    defaultValue: tokenValue(raw.defaultValue),
    items,
    sourceId: typeof primary === "string" ? primary : undefined,
    valueField: text(options.valueField, 100),
    labelField: text(options.labelField, 100),
    placeholder: text(options.placeholder, 120),
    min: finite(options.min),
    max: finite(options.max),
    step: finite(options.step),
    label: text(options.label, 60),
    actions,
    ...(kind === "entity"
      ? {
          entitySource: text(options.source, 20),
          integrationId: text(options.integration_id, 60),
          entityTypes: (Array.isArray(options.entityTypes) ? options.entityTypes : [])
            .filter((t): t is string => typeof t === "string" && /^[A-Za-z0-9_.-]{1,100}$/.test(t))
            .slice(0, 10),
        }
      : {}),
  };
}

export interface ReadInputs {
  inputs: DashboardInput[];
  /** The inputs drawn above the tabs, in order. */
  globalInputs: string[];
  /** The tokens' values before anything is set: the definition's defaults, then each input's. */
  defaults: Tokens;
  /** `expressions.tokens`: computed tokens. */
  expressions: Record<string, string>;
  submitButton: boolean;
  submitOnLoad: boolean;
}

export function readInputs(definition: Raw): ReadInputs {
  const inputs = Object.entries(record(definition.inputs))
    .slice(0, 20)
    .map(([id, value]) => readInput(id, value))
    .filter((input): input is DashboardInput => !!input);
  const known = new Set(inputs.map((input) => input.id));
  const layout = record(definition.layout);
  const globalInputs = (Array.isArray(layout.globalInputs) ? layout.globalInputs : []).filter(
    (id): id is string => typeof id === "string" && known.has(id),
  );
  const defaults: Tokens = {};
  for (const [name, entry] of Object.entries(record(record(record(definition.defaults).tokens).default))) {
    const value = tokenValue(record(entry).value);
    if (TOKEN_NAME.test(name) && value !== undefined) defaults[name] = value;
  }
  for (const input of inputs) if (input.token && input.defaultValue !== undefined) defaults[input.token] = input.defaultValue;
  const expressions: Record<string, string> = {};
  for (const [name, expression] of Object.entries(record(record(definition.expressions).tokens))) {
    if (TOKEN_NAME.test(name) && typeof expression === "string") expressions[name] = expression.slice(0, 300);
  }
  const options = record(layout.options);
  return {
    inputs,
    globalInputs,
    defaults,
    expressions,
    submitButton: options.submitButton === true,
    submitOnLoad: options.submitOnDashboardLoad !== false,
  };
}

/** A dropdown's choices: its own items, then those of its data source's result (rows or a distribution). */
export function choicesOf(input: DashboardInput, result: unknown): { label: string; value: string }[] {
  const out = [...input.items];
  const data = record(record(result).data);
  if (Array.isArray(data.items)) {
    for (const item of data.items.slice(0, 200)) {
      const entry = record(item);
      const value = entry[input.valueField ?? "label"];
      const label = entry[input.labelField ?? input.valueField ?? "label"];
      if (value !== undefined && value !== null) out.push({ label: String(label ?? value), value: String(value) });
    }
  } else if (Array.isArray(data.rows)) {
    const columns = Array.isArray(data.columns) ? data.columns.map((c) => String(record(c).key)) : [];
    const valueKey = input.valueField ?? columns[0];
    const labelKey = input.labelField ?? valueKey;
    for (const row of data.rows.slice(0, 200)) {
      const entry = record(row);
      const value = valueKey ? cellText(entry[valueKey]) : "";
      if (value) out.push({ label: cellText(entry[labelKey ?? valueKey]) || value, value });
    }
  }
  const seen = new Set<string>();
  return out.filter((choice) => !seen.has(choice.value) && !!seen.add(choice.value));
}

// ── The token manager ───────────────────────────────────────────────────────

/** One token as the studio's token manager lists it. */
export interface TokenRow {
  name: string;
  /** Its value on the stage now. */
  value?: string;
  /** Its default (`defaults.tokens.default`, or the input's `defaultValue`). */
  defaultValue?: string;
  /** The elements naming it: `visualizations.x`, `dataSources.y`, `inputs.z`, `expressions.tokens.t`. */
  usedBy: string[];
  /** What sets it: an input, a button, a computed expression, the environment, a source's result. */
  setBy: string[];
}

const PLACEHOLDER = /\$([A-Za-z_][A-Za-z0-9_.:]{0,99})(?:\|[shun])?\$/g;

function namesInJson(value: unknown): Set<string> {
  const found = new Set<string>();
  const text = JSON.stringify(value ?? null);
  for (const match of text.matchAll(PLACEHOLDER)) found.add(match[1]);
  return found;
}

const show = (value: unknown): string | undefined =>
  value === undefined ? undefined : Array.isArray(value) ? value.join(", ") : String(value);

/** Every token the definition names or sets, with its value now, its default, what uses it and what sets it. */
export function tokenTable(definition: unknown, current: Record<string, unknown>): TokenRow[] {
  const raw = record(definition);
  const read = readInputs(raw);
  const rows = new Map<string, TokenRow>();
  const row = (name: string) => {
    if (!rows.has(name)) rows.set(name, { name, usedBy: [], setBy: [] });
    return rows.get(name) as TokenRow;
  };
  for (const group of ["visualizations", "dataSources", "inputs"] as const) {
    for (const [id, element] of Object.entries(record(raw[group]))) {
      for (const name of namesInJson(element)) row(name).usedBy.push(`${group}.${id}`);
    }
  }
  for (const [name, expression] of Object.entries(read.expressions)) {
    row(name).setBy.push(`expressions.tokens.${name}`);
    for (const used of namesInJson(expression)) row(used).usedBy.push(`expressions.tokens.${name}`);
  }
  for (const input of read.inputs) {
    if (input.token) row(input.token).setBy.push(`inputs.${input.id}`);
    for (const action of input.actions) {
      if (action.type === "setToken" || action.type === "unsetToken") row(action.token).setBy.push(`inputs.${input.id}`);
    }
  }
  for (const [name, value] of Object.entries(read.defaults)) row(name).defaultValue = show(value);
  for (const entry of rows.values()) {
    const [scope, rest] = entry.name.split(":");
    if (rest) entry.setBy.push(scope === "env" ? "environment" : `dataSources.${scope}`);
    entry.value = show(current[entry.name]);
    entry.setBy = [...new Set(entry.setBy)];
    entry.usedBy = [...new Set(entry.usedBy)];
  }
  return [...rows.values()].sort((a, b) => a.name.localeCompare(b.name));
}
