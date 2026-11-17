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
 * The slide layout catalog as the editor reads it.
 *
 * Mirrors `SLIDE_LAYOUTS` in `app/services/presentation_spec.py` and
 * `template_supported_layouts` in `app/services/presentations.py` - the
 * backend is the authority (the deck response carries `supported_layouts`),
 * this is the mirror the panel needs for a template being TRIED before its
 * deck response exists. A backend test pins the two lists to each other.
 *
 * There is ONE catalog: the v1 layouts were retired, a stored slide
 * wearing one is upgraded when the row is read, so every slide the panel
 * meets is one of these and every one of them is a destination.
 */
import type { SlideLayout } from "@/types";

/** The platform's slide layouts, in catalog order. */
export const SLIDE_LAYOUTS: readonly SlideLayout[] = [
  "title",
  "section",
  "agenda",
  "content",
  "roadmap",
  "takeaways",
  "dashboard",
  "closing",
];

/**
 * The layouts a template lets the picker move a slide to, in catalog order -
 * mirrors `template_supported_layouts` on the backend: the advertised ids
 * (`{ id }` objects, the stored shape, or bare ids) restricted to the catalog,
 * and the WHOLE catalog when the template advertises nothing renderable (no
 * catalog, unknown ids). A template that says nothing about layouts restricts
 * nothing. Ids are matched as written, like the backend does:
 * `validate_layouts` stores them exact, so there is nothing to normalise and
 * the two readings cannot disagree.
 */
export function supportedLayouts(advertised: unknown): SlideLayout[] {
  if (!Array.isArray(advertised)) return [...SLIDE_LAYOUTS];
  const wanted = new Set<string>();
  for (const entry of advertised) {
    const id =
      typeof entry === "string"
        ? entry
        : entry && typeof entry === "object" && typeof (entry as { id?: unknown }).id === "string"
          ? (entry as { id: string }).id
          : null;
    if (id) wanted.add(id);
  }
  const offered = SLIDE_LAYOUTS.filter((layout) => wanted.has(layout));
  return offered.length > 0 ? offered : [...SLIDE_LAYOUTS];
}

/**
 * English ids (translated through `t_i18n`) of the layouts. The `title`
 * layout is named with the deck's unit ("Title slide" / "Title page") rather
 * than the bare "Title": that id already means a person's job title elsewhere
 * in the catalog, and its translations say so.
 */
const LAYOUT_LABEL_IDS: Record<string, string> = {
  section: "Section",
  agenda: "Agenda",
  content: "Content",
  roadmap: "Roadmap",
  takeaways: "Takeaways",
  dashboard: "Dashboard",
  closing: "Closing",
};

/**
 * The picker's label for a layout: the translated name of a catalog layout,
 * the humanised id for anything else (a deck stored by a newer backend than
 * this bundle - shown as it is, never guessed at).
 */
export function layoutLabel(layout: string, t: (id: string) => string, unit: "slide" | "page" = "slide"): string {
  if (layout === "title") return unit === "page" ? t("Title page") : t("Title slide");
  const id = LAYOUT_LABEL_IDS[layout];
  if (id) return t(id);
  const humanised = layout.replace(/_/g, " ");
  return `${humanised.charAt(0).toUpperCase()}${humanised.slice(1)}`;
}
