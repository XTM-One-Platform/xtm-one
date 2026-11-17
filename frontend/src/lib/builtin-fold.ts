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
 * The built-in fold of a list that mixes the platform's seeded catalogue with
 * the organisation's own items (Agents, Fleets; issue #11831).
 *
 * Company holds both the company-managed items the organisation made and the
 * built-in ones the platform seeded. While built-ins are hidden, Company lists
 * the organisation's own items only, its count is theirs, and one quiet row at
 * the end of the list says how many built-ins are hidden with a Show link. The
 * choice is the reader's, per list: `?builtin=show|hide` in the URL first, then
 * the one stored for that user and list, then the default (hidden when the
 * organisation has company items of its own, shown otherwise: a fresh install
 * keeps today's view).
 *
 * The landing segment keeps the order of every visibility filter (#7469): with
 * no `?vis=`, a list lands on Mine when Mine holds items, else on Company when
 * it holds company items of the organisation's own (built-ins hidden), else on
 * Company when it holds built-ins only (built-ins shown, before Shared), else on
 * Shared, then All.
 *
 * Pure module (no React): unit-tested on vitest.
 */

import { landingVisibility, segmentOf, type Visibility, type VisibilityFields } from "@/lib/visibility";

export type BuiltinChoice = "show" | "hide";

/** The URL parameter holding the choice. */
const BUILTIN_PARAM = "builtin";

/**
 * The key of the Properties filter option that mirrors the choice: checked =
 * built-ins shown. Never a `has:` predicate: `splitBuiltinSelection` takes it
 * out of the property keys before they filter.
 */
export const BUILTIN_PROPERTY_KEY = "builtin";

/** The choice a URL names, or `null`. */
export function readBuiltinParam(params: URLSearchParams): BuiltinChoice | null {
  const raw = params.get(BUILTIN_PARAM)?.trim().toLowerCase();
  return raw === "show" || raw === "hide" ? raw : null;
}

/** *params* with the choice written to `?builtin=`. */
export function writeBuiltinParam(params: URLSearchParams, choice: BuiltinChoice): URLSearchParams {
  const next = new URLSearchParams(params);
  next.set(BUILTIN_PARAM, choice);
  return next;
}

/** The local storage key of one user's choice on one list. */
export function builtinStorageKey(list: string, userId: string): string {
  return `xtm-one:builtin-fold:${list}:${userId}`;
}

/** The stored choice, or `null` (none, or storage unavailable). */
export function readStoredBuiltin(list: string, userId: string | undefined): BuiltinChoice | null {
  if (!userId) return null;
  try {
    const raw = globalThis.localStorage?.getItem(builtinStorageKey(list, userId));
    return raw === "show" || raw === "hide" ? raw : null;
  } catch {
    return null;
  }
}

/** Stores the choice; a storage that refuses is ignored (the URL still carries it). */
export function storeBuiltin(list: string, userId: string | undefined, choice: BuiltinChoice): void {
  if (!userId) return;
  try {
    globalThis.localStorage?.setItem(builtinStorageKey(list, userId), choice);
  } catch {
    // Private mode or a full storage: the choice lives in the URL only.
  }
}

/** How many company items are the organisation's own and how many are built-in. */
export function companySplit<T extends VisibilityFields>(
  items: readonly T[],
  userId: string | undefined,
  isBuiltin: (item: T) => boolean,
): { custom: number; builtin: number } {
  let custom = 0;
  let builtin = 0;
  for (const item of items) {
    if (segmentOf(item, userId) !== "company") continue;
    if (isBuiltin(item)) builtin += 1;
    else custom += 1;
  }
  return { custom, builtin };
}

/**
 * The company split of a list that keeps some built-ins drawn whatever the fold
 * (the Agentic Flow map keeps the General Assistant's stand-in, #11945):
 * `landing` counts a kept built-in as built-in, so a catalogue of built-ins only
 * lands on Company with built-ins shown, as on the Agents list; `fold` counts it
 * as drawn, so the Company count the fold lowers and its hidden count leave it in.
 */
export function companySplitKeeping<T extends VisibilityFields & { id: string }>(
  items: readonly T[],
  userId: string | undefined,
  isBuiltin: (item: T) => boolean,
  keep: ReadonlySet<string>,
): { landing: { custom: number; builtin: number }; fold: { custom: number; builtin: number } } {
  return {
    landing: companySplit(items, userId, isBuiltin),
    fold: companySplit(items, userId, (item) => isBuiltin(item) && !keep.has(item.id)),
  };
}

/**
 * The segment a list lands on with no `?vis=`, in the order Mine, Company,
 * Shared, All of every list:
 * 1. Mine when it holds items;
 * 2. Company when the organisation has company items of its own (built-ins hidden);
 * 3. Company when it holds built-ins only (built-ins shown), before Shared;
 * 4. the first of Shared and All holding items (`landingVisibility`).
 * *company* is the `companySplit` of the whole list, so step 3 never depends on
 * a Company count that the fold may have lowered.
 */
export function builtinLanding(
  counts: Partial<Record<Visibility, number>>,
  company: { custom: number; builtin: number },
): Visibility {
  if ((counts.mine ?? 0) > 0) return "mine";
  if (company.custom > 0) return "company";
  if (company.builtin > 0) return "company";
  return landingVisibility({ ...counts, mine: 0, company: 0 });
}

/** The choice when nothing names one: hidden when the organisation has company items of its own. */
export function defaultBuiltinChoice(customCompany: number): BuiltinChoice {
  return customCompany > 0 ? "hide" : "show";
}

/** What the fold does to the rows of one segment. */
export interface BuiltinFoldResult<T> {
  /** The rows to list. */
  shown: T[];
  /** Built-in rows taken out of the list (0 when nothing is hidden). */
  hidden: number;
  /**
   * The search matches built-ins only: they are listed anyway, with a hint,
   * never an empty state.
   */
  revealedBySearch: boolean;
}

/**
 * Applies the fold to *rows* (already narrowed by the search, tags, properties
 * and the segment): only Company with built-ins hidden takes anything out.
 */
export function foldBuiltins<T>(
  rows: readonly T[],
  segment: Visibility,
  choice: BuiltinChoice,
  isBuiltin: (item: T) => boolean,
  searching: boolean,
): BuiltinFoldResult<T> {
  if (segment !== "company" || choice === "show") return { shown: [...rows], hidden: 0, revealedBySearch: false };
  const shown = rows.filter((row) => !isBuiltin(row));
  const hidden = rows.length - shown.length;
  if (searching && shown.length === 0 && hidden > 0) return { shown: [...rows], hidden: 0, revealedBySearch: true };
  return { shown, hidden, revealedBySearch: false };
}

/** The Properties filter's checked keys with the built-in option checked when built-ins are shown. */
export function mergeBuiltinSelection(selected: readonly string[], choice: BuiltinChoice): string[] {
  const rest = selected.filter((key) => key !== BUILTIN_PROPERTY_KEY);
  return choice === "show" ? [...rest, BUILTIN_PROPERTY_KEY] : rest;
}

/** Splits the Properties filter's keys into the `has:` keys and the built-in choice. */
export function splitBuiltinSelection(keys: readonly string[]): { properties: string[]; choice: BuiltinChoice } {
  return {
    properties: keys.filter((key) => key !== BUILTIN_PROPERTY_KEY),
    choice: keys.includes(BUILTIN_PROPERTY_KEY) ? "show" : "hide",
  };
}
