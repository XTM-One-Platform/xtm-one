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
 * Lucide icon catalog for agent-authored slides.
 *
 * The backend validates every icon field against the installed `lucide-react`
 * catalog (`presentation_icons.py`, generated from this very package), so a
 * stored name is normally one this bundle can draw. The catalog is loaded
 * lazily, as ONE chunk, the first time a deck needs an icon:
 *
 * - importing `icons` from the top-level `lucide-react` entry would pull all
 *   ~1,700 icons into the MAIN bundle, because that entry is already statically
 *   imported everywhere and a dynamic import of a module that is already in a
 *   chunk does not split it out;
 * - `lucide-react/dynamic` splits every icon into its own chunk, which is a
 *   1,700-file build output and one HTTP round-trip per icon on a slide.
 *
 * The deep import of `dist/esm/icons/index.js` is the module the top-level
 * entry re-exports as `icons`; on its own it is a single lazy chunk (~130 kB
 * gzipped) fetched once per session, and the export capture has exactly one
 * thing to wait for. The package has no `exports` map, so the path is
 * importable; a layout change in a future lucide release fails the build
 * loudly rather than rendering blank icons.
 *
 * A deck stored by a newer backend may still name an icon this bundle does not
 * ship; `resolveDeckIcon` returns `null` for it, and the renderer draws nothing
 * instead of a broken box.
 */
import type { LucideIcon } from "lucide-react";

export type DeckIconCatalog = Readonly<Record<string, LucideIcon | undefined>>;

let catalog: DeckIconCatalog | null = null;
let pending: Promise<DeckIconCatalog> | null = null;
const listeners = new Set<() => void>();

/** Start (or join) the catalog load. Resolves to the catalog; never rejects twice. */
export function loadDeckIconCatalog(): Promise<DeckIconCatalog> {
  if (catalog) return Promise.resolve(catalog);
  if (!pending) {
    pending = import("lucide-react/dist/esm/icons/index.js")
      .then((module: unknown) => {
        catalog = module as DeckIconCatalog;
        for (const listener of listeners) listener();
        return catalog;
      })
      .catch((error: unknown) => {
        // Let a later render retry rather than caching the failure forever
        // (a flaky network during the first deck should not blank every icon
        // for the rest of the session).
        pending = null;
        throw error;
      });
  }
  return pending;
}

/** Synchronous snapshot for `useSyncExternalStore`. */
export function getDeckIconCatalog(): DeckIconCatalog | null {
  return catalog;
}

export function subscribeDeckIconCatalog(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Lucide's export name for a kebab-case icon id: `shield-check` ->
 * `ShieldCheck`, `arrow-down-0-1` -> `ArrowDown01`, `a-arrow-down` ->
 * `AArrowDown`. Mirrors the package's own `toPascalCase`.
 * @public
 */
export function lucideExportName(name: string): string {
  return name
    .split("-")
    .map((part) => (part ? part.charAt(0).toUpperCase() + part.slice(1) : ""))
    .join("");
}

/** The component for a kebab-case icon id, or `null` when the catalog lacks it. */
export function resolveDeckIcon(
  icons: DeckIconCatalog | null,
  name: string | undefined | null,
): LucideIcon | null {
  if (!icons || !name) return null;
  return icons[lucideExportName(name)] ?? null;
}
