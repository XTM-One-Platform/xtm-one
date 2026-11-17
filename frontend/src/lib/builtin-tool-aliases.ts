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
 * [DEPRECATED] built-in ids: a former id is still accepted on
 * an agent, an assignment or the General Assistant and stands for the group it
 * merged into (`backend/app/services/builtin_tool_aliases.py`). The registry
 * serves each group with its `aliases`, so the client reads them from there
 * and never keeps a second copy.
 *
 * Every check of the form "does this entity carry suite X" goes through
 * `carriesSuite`, so an agent still storing `browser` is shown with the `web`
 * card on, and a toggle writes the group id and drops its former ids.
 */

export interface AliasedSuite {
  id: string;
  aliases?: readonly string[];
}

/** Former id -> group id, from the registry groups. */
export function aliasIndex(groups: readonly AliasedSuite[]): Map<string, string> {
  const index = new Map<string, string>();
  for (const group of groups) {
    for (const alias of group.aliases ?? []) index.set(alias, group.id);
  }
  return index;
}

/** The id a picker shows for a stored id: its group when it is a former id. */
export function canonicalSuiteId(id: string, index: ReadonlyMap<string, string>): string {
  return index.get(id) ?? id;
}

/** Stored ids with every former id replaced by its group, once, in order. */
export function canonicalSuiteIds(ids: readonly string[], index: ReadonlyMap<string, string>): string[] {
  const out: string[] = [];
  for (const id of ids) {
    const canonical = canonicalSuiteId(id, index);
    if (!out.includes(canonical)) out.push(canonical);
  }
  return out;
}

/** Whether `ids` carries `suiteId`, directly or through one of its former ids. */
export function carriesSuite(
  ids: readonly string[] | null | undefined,
  suiteId: string,
  index: ReadonlyMap<string, string>,
): boolean {
  return (ids ?? []).some((id) => canonicalSuiteId(id, index) === suiteId);
}

/** `ids` without `suiteId` and without any former id of it. */
export function withoutSuite(ids: readonly string[], suiteId: string, index: ReadonlyMap<string, string>): string[] {
  return ids.filter((id) => canonicalSuiteId(id, index) !== suiteId);
}
