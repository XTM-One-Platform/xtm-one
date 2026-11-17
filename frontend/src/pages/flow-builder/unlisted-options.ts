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
 * A selection its picker does not list: an entity deleted or unshared
 * since it was picked, restored from an older wizard draft, or stored in a flow
 * by someone who can see what this user cannot. `MultiSelectListField` counts
 * it either way; without a row of its own it could not be removed.
 *
 * No React and no `@/` import, so `unlisted-options.test.ts` runs under
 * `node --test`.
 */

/**
 * The picker options for `selected`: an `unlisted(value)` row, first, for each
 * selected value `options` does not list, then `options` itself.
 */
export function withUnlistedSelections<T extends { value: string }>(
  options: T[],
  selected: readonly string[],
  unlisted: (value: string) => T,
): T[] {
  const listed = new Set(options.map((o) => o.value));
  const missing = [...new Set(selected)].filter((value) => !listed.has(value));
  return missing.length > 0 ? [...missing.map(unlisted), ...options] : options;
}
