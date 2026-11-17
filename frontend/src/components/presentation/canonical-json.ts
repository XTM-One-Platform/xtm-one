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
 * The one comparison of two spellings of a deck value: JSON
 * with every object's keys sorted. The editor's dirty flag reads it, and so
 * does the three-way merge of a collaborator's revision (`deck-merge.ts`).
 */

/**
 * JSON with every object's keys sorted, so two spellings of the same value
 * compare equal whatever order a serializer wrote them in. `undefined` fields
 * are dropped like `JSON.stringify` drops them.
 */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      const record = item as Record<string, unknown>;
      return Object.fromEntries(
        Object.keys(record)
          .sort()
          .map((key) => [key, record[key]]),
      );
    }
    return item;
  });
}

const memo = new WeakMap<object, string>();

/**
 * `canonicalJson` of a value the editor never mutates, memoised per object.
 * Every piece of a draft is copy-on-write (`edit-paths.ts`), so an object's
 * spelling is fixed once it exists: a slide shared by a hundred undo steps is
 * serialised once, however many merges read it.
 */
export function canonicalOf(value: unknown): string {
  if (value === null || typeof value !== "object") return canonicalJson(value) ?? "undefined";
  const known = memo.get(value);
  if (known !== undefined) return known;
  const json = canonicalJson(value);
  memo.set(value, json);
  return json;
}
