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
 * The template the Configuration form would save, for "Preview the unsaved
 * design": every field the preview reads - the name on the title slide, the
 * aspect ratio, the layout catalog, the palettes and the deck defaults - taken
 * from the form, so the preview shows what saving would produce.
 *
 * A draft value a save would be refused for (`TemplateContainerUpdate`, and
 * the form's own refusal of a blank name: a blank or over-long name, an
 * over-long aspect ratio, layouts that are not a list of objects) cannot be
 * what a save produces, so the saved value is shown for that field instead.
 * Anything the server stores as typed is passed as typed: the preview builders
 * read it exactly as they would read it once saved (an unknown aspect ratio or
 * layout id falls back the same way).
 *
 * Dependency-free on purpose: the unit test imports it directly.
 */
import type { TemplateContainerDetail } from "@/types";

/** Mirrors `max_length` on `TemplateContainerUpdate` (pinned by a test). */
export const MAX_TEMPLATE_NAME_CHARS = 200;
export const MAX_TEMPLATE_ASPECT_RATIO_CHARS = 30;

/** A length as `max_length` counts it: in code points, where `length` counts UTF-16 units (an emoji is two). */
const codePoints = (text: string): number => Array.from(text).length;

/** The form as it stands: parsed JSON fields are `null` when they do not parse. */
export interface TemplateDraft {
  name: string;
  aspectRatio: string;
  layouts: unknown;
  themeConfig: Record<string, unknown>;
  extraMetadata: Record<string, unknown>;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

export function draftPreviewTemplate(saved: TemplateContainerDetail, draft: TemplateDraft): TemplateContainerDetail {
  const name = draft.name.trim() && codePoints(draft.name) <= MAX_TEMPLATE_NAME_CHARS ? draft.name : saved.name;
  // The form sends the aspect ratio trimmed, and a blank one clears it.
  const typedRatio = draft.aspectRatio.trim();
  const aspectRatio = codePoints(typedRatio) <= MAX_TEMPLATE_ASPECT_RATIO_CHARS ? typedRatio : saved.aspect_ratio;
  const layouts =
    Array.isArray(draft.layouts) && draft.layouts.every(isObject)
      ? (draft.layouts as TemplateContainerDetail["layouts"])
      : saved.layouts;
  return {
    ...saved,
    name,
    aspect_ratio: aspectRatio,
    layouts,
    theme_config: draft.themeConfig,
    extra_metadata: draft.extraMetadata,
  };
}
