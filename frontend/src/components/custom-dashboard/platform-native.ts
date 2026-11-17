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
 * What every platform source editor shares about a stored widget
 * configuration: its filter groups, and how two configurations compare.
 *
 * The editors never go through a form model of their own: they read and write
 * the stored configuration in place (`platform-form.ts`, from the description
 * the platform's adapter serves) and `dashboard_platform.check_native`
 * validates it on the server. A key no control shows is written back as it
 * was, so editing a widget never loses anything.
 *
 * Pure: `platform-native.test.ts` runs it on `node --test`.
 */

type Raw = Record<string, unknown>;

const record = (value: unknown): Raw => (typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Raw) : {});

// ── Filter groups ───────────────────────────────────────────────────────────

/** One filter: a key written as a list (`["name"]`) or a string, as the platform writes it. */
export interface PlatformFilter {
  key: string[] | string;
  values: unknown[];
  operator: string;
  mode: "and" | "or";
  id?: string;
}

/** A filter group; groups nest under `filterGroups` where the platform nests them. */
export interface PlatformFilterGroup {
  mode: "and" | "or";
  filters: PlatformFilter[];
  filterGroups?: PlatformFilterGroup[];
}

/** The empty group, nesting or not. */
export function emptyGroup(nested: boolean): PlatformFilterGroup {
  return nested ? { mode: "and", filters: [], filterGroups: [] } : { mode: "and", filters: [] };
}

/** A stored group as a group (a missing one is the empty group); its filters are kept as they are. */
export function asGroup(value: unknown, nested: boolean): PlatformFilterGroup {
  const raw = record(value);
  if (!Array.isArray(raw.filters)) return emptyGroup(nested);
  return raw as unknown as PlatformFilterGroup;
}

/** The key a filter is about (the first of a list). */
export function filterKey(filter: PlatformFilter): string {
  return Array.isArray(filter.key) ? (filter.key[0] ?? "") : filter.key;
}

/** Operators that take no value. */
export function isValueless(operator: string): boolean {
  return ["nil", "not_nil", "empty", "not_empty"].includes(operator);
}

/** A random id (`uuid` v4), for the platforms that give each filter one. */
export function newId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `f-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

// ── Comparing ───────────────────────────────────────────────────────────────

/**
 * A configuration as the server stores it, for comparing two: a key that is
 * absent, empty (`""`, `[]`, an empty filter group) or `false` is left out, and
 * object keys are sorted. The server writes those defaults; the editors may not.
 */
export function canonicalNative(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalNative);
  if (typeof value !== "object" || value === null) return value;
  const out: Raw = {};
  for (const key of Object.keys(value).sort()) {
    const item = canonicalNative((value as Raw)[key]);
    const empty =
      item === undefined || item === null || item === "" || item === false ||
      (Array.isArray(item) && item.length === 0) ||
      (typeof item === "object" && item !== null && !Array.isArray(item) && Object.keys(item).length === 1 && (item as Raw).mode === "and");
    if (!empty) out[key] = item;
  }
  return out;
}

/** Whether two configurations are the same widget (`canonicalNative`). */
export function sameNative(a: unknown, b: unknown): boolean {
  return JSON.stringify(canonicalNative(a)) === JSON.stringify(canonicalNative(b));
}
