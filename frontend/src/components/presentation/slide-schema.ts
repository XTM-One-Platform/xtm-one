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
 * What a person may change on a SLIDE by hand - the inspector's
 * catalog per layout, the counterpart of `block-schema.ts` for the layout
 * models of `presentation_spec.py` (`_LAYOUT_MODELS`). Same descriptors, same
 * backend tripwire: every key is a field of the layout's model, every enum its
 * `Literal`. `notes`, the background image and, the slide's own
 * `theme` and `overlay` are common to every layout and handled by the
 * inspector itself (its Theme and Overlay pickers), as is the `hero` of the
 * title and closing slides (the Hero thumbnails); a layout's BLOCKS are
 * edited on the stage.
 *
 * `create` is the slide the "Add slide" menu inserts for that layout.
 */
import type { PresentationSlide, SlideLayout } from "@/types";

// The explicit extension lets Node load this module for the schema tests
// (`block-schema.test.ts`, the backend's default-validation tripwire).
import {
  CARD_COLUMNS,
  METRIC_FIELDS,
  NUMBERED_CARD_FIELDS,
  PHASE_FIELDS,
  RATIOS,
  VALIGNS,
  type ItemsSchema,
  type SchemaField,
} from "./block-schema.ts";

/** The platform's default slide cap (`presentation_spec.DEFAULT_SLIDE_CAP`,
 *  pinned by the tripwire). A deck's OWN cap is its template's `max_slides`,
 *  served as `max_slides` on the deck response: `Add slide` and
 *  `Duplicate` stop there, not at a 422; this constant is what a response
 *  cached before the field existed falls back to. */
export const DEFAULT_SLIDE_CAP = 100;

export const TITLE_ALIGNS = ["start", "center"] as const;
export const AGENDA_COLUMNS = [1, 2, 3] as const;
export const AGENDA_STYLES = ["list", "grid"] as const;
export const AGENDA_FLOWS = ["row", "column"] as const;
export const CONTACT_KINDS = ["web", "email", "linkedin", "x", "github", "phone"] as const;
export const DASHBOARD_TILE_SIZES = ["sm", "md"] as const;
export const DASHBOARD_PANEL_COLUMNS = [2, 3] as const;
/** Where a title slide's speaker portraits are picked from: the template's people photos. */
export const SPEAKER_PHOTO_ROLES = ["people_photo"] as const;

// Same helper shapes as `block-schema.ts` (the tripwire greps both files).
type Opts = { optional?: boolean; required?: boolean; hint?: string };
const en = (key: string, label: string, options: readonly (string | number)[], opts: Opts = {}): SchemaField => ({
  kind: "enum",
  key,
  label,
  options,
  ...opts,
});
const flag = (key: string, label: string, opts: Opts & { defaultOn?: boolean } = {}): SchemaField => ({
  kind: "flag",
  key,
  label,
  ...opts,
});
const num = (key: string, label: string, min: number, max: number, opts: Opts = {}): SchemaField => ({
  kind: "number",
  key,
  label,
  min,
  max,
  ...opts,
});
const markup = (key: string, label: string, opts: Opts & { multiline?: boolean } = {}): SchemaField => ({
  kind: "text",
  key,
  label,
  markup: true,
  ...opts,
});
const text = (key: string, label: string, opts: Opts & { multiline?: boolean } = {}): SchemaField => ({
  kind: "text",
  key,
  label,
  markup: false,
  ...opts,
});
const icon = (key: string, label: string, opts: Opts = {}): SchemaField => ({
  kind: "icon",
  key,
  label,
  ...opts,
});
const icons = (key: string, label: string, max: number, opts: Opts = {}): SchemaField => ({
  kind: "icons",
  key,
  label,
  max,
  ...opts,
});
const asset = (key: string, label: string, roles: readonly string[], opts: Opts = {}): SchemaField => ({
  kind: "asset",
  key,
  label,
  roles,
  ...opts,
});
const strings = (key: string, label: string, max: number, opts: Opts = {}): SchemaField => ({
  kind: "strings",
  key,
  label,
  max,
  ...opts,
});
const group = (key: string, label: string, fields: SchemaField[], opts: Opts = {}): SchemaField => ({
  kind: "group",
  key,
  label,
  fields,
  ...opts,
});

const SPEAKER = group(
  "speaker",
  "Speaker",
  [markup("name", "Name"), markup("title", "Title", { optional: true }), strings("credentials", "Credentials", 3)],
  { optional: true },
);

export interface SlideSchema {
  fields: SchemaField[];
  items?: ItemsSchema;
  create: (position: number) => PresentationSlide;
}

export const SLIDE_SCHEMA: Record<SlideLayout, SlideSchema> = {
  title: {
    fields: [
      markup("title", "Title (heading)"),
      markup("subtitle", "Subtitle", { optional: true }),
      markup("event_label", "Event", {
        optional: true,
        hint: "Conference or date line above the title.",
      }),
      markup("footer", "Footer line", { optional: true }),
      en("align", "Alignment", TITLE_ALIGNS),
      flag("hero_accent_word", "Accent the last line of the title", {
        hint: "The line after the last line break (the last word of a one-line title), in the accent tone.",
      }),
      icons("icon_strip", "Icons", 6, {
        hint: "The row of icons under the title: pick another icon, reorder or remove one.",
      }),
    ],
    items: {
      key: "speakers",
      label: "Speakers",
      min: 0,
      max: 4,
      fields: [
        markup("name", "Name"),
        markup("title", "Title", { optional: true }),
        strings("credentials", "Credentials", 3),
        asset("photo_file_id", "Photo", SPEAKER_PHOTO_ROLES, { optional: true }),
      ],
      create: () => ({ name: "Name" }),
      unsetWhenEmpty: true,
    },
    create: () => ({ layout: "title", title: "Title", subtitle: "Subtitle" }),
  },
  section: {
    fields: [
      markup("title", "Title (heading)"),
      markup("subtitle", "Subtitle", { optional: true }),
      markup("label", "Label", { optional: true, hint: 'e.g. "Part 2"' }),
      num("number", "Number", 1, 99, {
        optional: true,
        hint: "The watermark number.",
      }),
      icon("icon", "Icon", { optional: true }),
    ],
    create: (position) => ({
      layout: "section",
      title: "Section",
      number: position,
    }),
  },
  agenda: {
    fields: [
      markup("title", "Title (heading)", { optional: true }),
      markup("subtitle", "Subtitle", { optional: true }),
      en("columns", "Columns", AGENDA_COLUMNS, {
        optional: true,
        hint: "Three columns need 6 to 9 items.",
      }),
      en("style", "Style", AGENDA_STYLES, { optional: true }),
      en("flow", "Fill order", AGENDA_FLOWS),
      flag("distribute", "Spread the rows", { defaultOn: true }),
      num("current", "Current item", 1, 10, {
        optional: true,
        hint: "Highlighted when the agenda recurs.",
      }),
    ],
    items: {
      key: "items",
      label: "Items",
      min: 2,
      max: 10,
      fields: [
        markup("title", "Title (heading)"),
        markup("description", "Description", { optional: true }),
        num("number", "Number", 1, 99, { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ],
      create: () => ({ title: "Topic" }),
    },
    create: () => ({
      layout: "agenda",
      title: "Agenda",
      items: [{ title: "First topic" }, { title: "Second topic" }, { title: "Third topic" }],
    }),
  },
  content: {
    fields: [
      markup("title", "Title (heading)", { optional: true }),
      markup("subtitle", "Subtitle", { optional: true }),
      en("vertical_align", "Vertical alignment", VALIGNS),
      flag("distribute", "Spread the blocks", {
        hint: "The free height lands between the blocks.",
      }),
      en("ratio", "Column ratio", RATIOS, { hint: "Two columns only." }),
      flag("equal_height", "Equal column height"),
      flag("equal_rows", "Equal row heights", {
        hint: "The blocks at the same place in each column share one height.",
      }),
      flag("bento", "Bento grid", {
        hint: "3 to 5 blocks, 1 to 3 of them images: the first image is the large cell.",
      }),
      flag("hide_footer", "Hide the footer on this slide"),
    ],
    create: () => ({
      layout: "content",
      title: "Title",
      blocks: [{ type: "text", content: "Your text here." }],
    }),
  },
  roadmap: {
    fields: [
      markup("title", "Title (heading)"),
      markup("subtitle", "Subtitle", { optional: true }),
      flag("hide_footer", "Hide the footer on this slide"),
    ],
    items: {
      key: "phases",
      label: "Phases",
      min: 2,
      max: 4,
      fields: PHASE_FIELDS,
      create: () => ({ title: "Phase" }),
    },
    create: () => ({
      layout: "roadmap",
      title: "Roadmap",
      phases: [
        { title: "Phase 1", items: ["First step"] },
        { title: "Phase 2", items: ["Next step"] },
      ],
    }),
  },
  takeaways: {
    fields: [
      markup("title", "Title (heading)", { optional: true }),
      markup("subtitle", "Subtitle", { optional: true }),
      en("columns", "Columns", CARD_COLUMNS, { optional: true }),
      flag("hide_footer", "Hide the footer on this slide"),
    ],
    items: {
      key: "items",
      label: "Takeaways",
      min: 2,
      max: 6,
      fields: NUMBERED_CARD_FIELDS,
      create: () => ({ title: "Takeaway" }),
    },
    create: () => ({
      layout: "takeaways",
      title: "Key takeaways",
      items: [{ title: "First" }, { title: "Second" }, { title: "Third" }],
    }),
  },
  dashboard: {
    fields: [
      markup("title", "Title (heading)"),
      markup("subtitle", "Subtitle", { optional: true }),
      en("tile_size", "Tile size", DASHBOARD_TILE_SIZES, {
        hint: "Small keeps a long sublabel or delta on one line in a row of 5-6 tiles.",
      }),
      en("panel_columns", "Panel columns", DASHBOARD_PANEL_COLUMNS, {
        hint: "Two columns share the grid half and half; a panel there spans 1 or 2.",
      }),
      flag("hide_footer", "Hide the footer on this slide"),
    ],
    items: {
      key: "tiles",
      label: "KPI tiles",
      min: 2,
      max: 8,
      fields: METRIC_FIELDS,
      create: () => ({ value: "0", label: "Metric" }),
    },
    create: () => ({
      layout: "dashboard",
      title: "Dashboard",
      tiles: [
        { value: "12", label: "First" },
        { value: "34", label: "Second" },
      ],
      panels: [{ block: { type: "metric", value: "56", label: "Third" } }],
    }),
  },
  closing: {
    fields: [
      markup("title", "Title (heading)", { optional: true }),
      markup("subtitle", "Subtitle", { optional: true }),
      markup("tagline", "Tagline", { optional: true }),
      en("align", "Alignment", TITLE_ALIGNS, { optional: true }),
      SPEAKER,
    ],
    items: {
      key: "contacts",
      label: "Contacts",
      min: 0,
      max: 6,
      fields: [
        en("kind", "Kind", CONTACT_KINDS, { required: true }),
        text("value", "Value", {
          hint: "An address, a URL, a handle or a number - checked per kind.",
        }),
        markup("label", "Label", { optional: true }),
      ],
      create: () => ({ kind: "web", value: "example.com" }),
    },
    create: () => ({ layout: "closing", title: "Thank you" }),
  },
};
