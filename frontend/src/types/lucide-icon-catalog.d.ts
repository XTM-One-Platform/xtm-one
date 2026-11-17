// SPDX-License-Identifier: Apache-2.0

/**
 * Ambient declaration for the deep import of the Lucide icon catalog module
 * used by `components/presentation/deck-icons.ts`.
 *
 * The package ships no `.d.ts` at that path (its typings cover the top-level
 * entry only) and no `exports` map that would forbid the import. The module is
 * `dist/esm/icons/index.js`: one named export per icon, PascalCase. A shorthand
 * declaration is used because "an arbitrary set of named exports" has no
 * ambient-module spelling; `deck-icons.ts` narrows the namespace to
 * `DeckIconCatalog` at the single import site.
 */
declare module "lucide-react/dist/esm/icons/index.js";
