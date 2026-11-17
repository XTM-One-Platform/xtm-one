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
 * What a person may change on a block by hand - the inspector's catalog.
 *
 * One entry per block type of `BLOCK_TYPES` (`presentation_spec.py`), listing
 * the fields the Pydantic model accepts as CONTROLS: an enum becomes a picker
 * over the very values the validator takes, a bool a switch, a bounded int a
 * number field, an inline-markup string a text field, an icon an icon field,
 * a repeated item a list editor bounded by the model's min / max. Nothing
 * here invents a property: `tests/test_presentation_editor.py` reads this
 * file and checks every key against the model, every enum against its
 * `Literal`, every number and list bound against the model's, and every
 * palette default through the validator - so the inspector can never offer
 * what the save would refuse. The reverse holds too: a field of a model with
 * no control here must be named in that test's allow-lists (edited on the
 * stage, or the assistant's), so it is a decision, never a silent gap.
 *
 * `create` is the block the palette inserts: minimal, valid, obviously a
 * placeholder. A block type with no `create` is not offered by hand (an image
 * needs a file, a chart its numbers, an architecture its graph - the
 * assistant's work); once present, its fields are still editable here.
 *
 * Labels are English message ids, translated by the inspector.
 */
import type { DeckBlock, DeckBlockType } from "@/types";

// ── Value sets (the `Literal` aliases of `presentation_spec.py`) ─────────────
export const TONES = ["accent", "info", "success", "warning", "error", "muted"] as const;
export const SIZES = ["sm", "md", "lg"] as const;
export const MEDIA_SIZES = ["sm", "md", "lg", "full"] as const;
export const SPACER_SIZES = ["xs", "sm", "md", "lg", "xl"] as const;
export const ALIGNS = ["start", "center", "end"] as const;
export const VALIGNS = ["start", "center", "end", "stretch"] as const;
export const DENSITIES = ["normal", "compact", "tight"] as const;
export const LIST_STYLES = ["bullet", "check", "arrow", "cross", "numbered", "plain"] as const;
export const RATIOS = ["equal", "wide_left", "wide_right"] as const;
export const TRENDS = ["up", "down", "flat"] as const;
export const DELTA_TONES = ["success", "error", "warning", "muted"] as const;
export const BADGE_VARIANTS = ["outline", "solid"] as const;
export const BANNER_VARIANTS = ["gradient", "solid", "soft", "outline"] as const;
export const TIMELINE_STYLES = ["phases", "events"] as const;
export const FLOW_DIRECTIONS = ["horizontal", "vertical"] as const;
export const IMAGE_FITS = ["contain", "cover"] as const;
export const PADDINGS = ["none", "sm", "md", "lg"] as const;
export const IMAGE_FRAMES = ["none", "browser", "browser_slim", "device"] as const;
export const CHART_KINDS = ["bar", "stacked_bar", "line", "area", "pie", "donut", "gauge", "combo", "waterfall", "butterfly", "map"] as const;
export const BUTTERFLY_SPINES = ["center", "left"] as const;
export const MAP_REGIONS = ["world", "europe", "americas", "apac"] as const;
export const MAP_LEGEND_STYLES = ["ramp", "classes"] as const;
export const MAP_CLASS_COUNTS = [2, 3, 4, 5, 6, 7] as const;
export const VALUE_FORMATS = ["number", "currency", "percent"] as const;
export const CHART_DECIMALS = [0, 1, 2, 3] as const;
export const CHART_UNIT_SCALES = ["auto", "k", "M", "B"] as const;
/** What a unit scale's picker calls each letter (`optionLabels`). */
const UNIT_SCALE_LABELS = { auto: "Automatic", k: "Thousands (k)", M: "Millions (M)", B: "Billions (B)" } as const;
export const LEGEND_POSITIONS = ["top", "bottom", "right"] as const;
export const VALUE_LABEL_FITS = ["auto", "stagger", "upright"] as const;
/** A text scale, or canvas px from 10 to 32 (`VALUE_LABEL_SIZE_MIN` / `_MAX` in `chart-format.ts`). */
export const VALUE_LABEL_SIZES = [
  "sm", "md", "lg",
  10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32,
] as const;
export const HEADING_LEVELS = [2, 3, 4] as const;
export const TWO_COLUMNS = [1, 2] as const;
export const GRID_COLUMNS = [2, 3, 4] as const;
export const CARD_COLUMNS = [1, 2, 3] as const;
export const PERSONA_COLUMNS = [1, 2, 3, 4] as const;
export const PROFILE_COLUMNS = [1, 2, 3, 4] as const;
export const LOGO_COLUMNS = [2, 3, 4, 5, 6] as const;
export const LOGO_HEIGHTS = ["sm", "md", "lg", "xl"] as const;
export const LOGO_TONES = ["original", "mono", "muted", "ink"] as const;
export const LOGO_ALIGNS = ["start", "center", "even", "stretch"] as const;
export const LOGO_WALL_COLUMNS = [1, 2, 3] as const;
export const FORMULA_VARIANTS = ["card", "result"] as const;
export const CONFIG_ROW_STATES = ["active", "dimmed"] as const;
export const CONFIG_ROW_STYLES = ["outlined", "plain"] as const;
// A tone or a template palette colour (`ToneOrColor` in presentation_spec.py):
// a formula card's bar, a configuration card, a chip in a product's own hue.
export const TONES_OR_COLORS = [
  "accent",
  "info",
  "success",
  "warning",
  "error",
  "muted",
  "brand_1",
  "brand_2",
  "brand_3",
  "brand_4",
  "chart_1",
  "chart_2",
  "chart_3",
  "chart_4",
  "chart_5",
  "chart_6",
] as const;
const PALETTE_COLOR_LABELS = {
  brand_1: "Brand hue 1",
  brand_2: "Brand hue 2",
  brand_3: "Brand hue 3",
  brand_4: "Brand hue 4",
  chart_1: "Series 1",
  chart_2: "Series 2",
  chart_3: "Series 3",
  chart_4: "Series 4",
  chart_5: "Series 5",
  chart_6: "Series 6",
} as const;
export const ORG_ORIENTATIONS = ["top_down", "left_right", "radial"] as const;
export const ORG_STYLES = ["cards", "compact", "bubbles", "hybrid"] as const;
export const ORG_COUNT_MODES = ["current", "target", "both"] as const;
export const ORG_LEGENDS = ["auto", "none", "custom"] as const;
export const ORG_SPINES = ["auto", "always", "never"] as const;
export const MAP_AXES = ["center", "edges"] as const;
export const MAP_CALLOUTS_POSITIONS = ["below", "right"] as const;
export const CALLOUT_VARIANTS = ["tinted", "card", "outline", "plain"] as const;
export const CALLOUT_HEADERS = ["inline", "stacked", "none"] as const;
// The quote's looks: the variant is drawn by its own thumbnail picker
// (`QuoteVariantPicker`), the rest are plain pickers.
export const QUOTE_VARIANTS = ["card", "statement", "pull", "testimonial"] as const;
export const QUOTE_SIZES = ["md", "lg", "xl", "hero"] as const;
export const QUOTE_MARKS = ["none", "inline", "decorative"] as const;
export const START_CENTER = ["start", "center"] as const;
export const METRIC_VARIANTS = ["card", "plain"] as const;
export const POLL_KINDS = ["choice", "quiz", "word_cloud", "rating"] as const;
export const POLL_SHOW_RESULTS = ["live", "after_lock"] as const;
export const POLL_LAYOUTS = ["active", "all"] as const;
export const POLL_CONTROLS = ["hover", "none"] as const;
export const POLL_REVEALS = ["manual", "live", "after_votes", "after_seconds", "never"] as const;
/** The template asset roles a logo is picked from (a quote's, a timeline step's) and a quote's portrait. */
export const LOGO_ROLES = ["customer_logo", "partner_logo", "integration_logo", "product_logo", "logo", "logo_light"] as const;
export const QUOTE_PHOTO_ROLES = ["people_photo"] as const;
/** A quote carries at most this many figures (`MAX_QUOTE_FIGURES`). */
export const MAX_QUOTE_FIGURES = 4;

// The run caps of `presentation_spec.py` (`MAX_BLOCKS_PER_*`, `MAX_TABLE_*`),
// mirrored here (pinned by `tests/test_presentation_editor.py`) so an insert,
// a duplicate or an added row stops where the validator would refuse the
// save, not at a 422 after it (`blockRunCapacity` below reads them per run).
/** A slide body (`blocks`). */
export const MAX_BLOCKS_PER_SLIDE = 12;
/** A column, of the slide or of a `columns` block. */
export const MAX_BLOCKS_PER_COLUMN = 8;
/** A callout's children, a comparison side. */
export const MAX_BLOCKS_PER_CONTAINER = 6;
/** A table's grid: where its editor stops adding a column or a row (the
 *  columns are also `table.items.max`). */
export const MAX_TABLE_COLUMNS = 6;
export const MAX_TABLE_ROWS = 14;

// ── Field descriptors ────────────────────────────────────────────────────────
export type SchemaValue = string | number;

interface FieldBase {
  key: string;
  label: string;
  /** The field may be unset (the model's `| None`); a text or number may be blank. */
  optional?: boolean;
  /**
   * The model has NO default for this choice (`chart.kind`, a contact's
   * `kind`), so the picker offers no "Default" entry. Every other enum in the
   * spec defaults when omitted, and an omitted field is what the agent writes
   * too, so the pickers offer "Default" (unset) unless told otherwise.
   */
  required?: boolean;
  hint?: string;
  /**
   * What the field is and when to pick each value, in the info bubble beside
   * its label. A field that declares it is drawn to the Forms standard: label
   * at body size, help line at 12 px read by the control (`aria-describedby`).
   */
  info?: string;
  /**
   * Shown only while the field holds a value: a legacy field kept editable -
   * and clearable - on the decks that carry it, never offered on a new block
   * (the quote's one-line `attribution`).
   */
  whenSet?: boolean;
  /**
   * Shown only while a sibling field holds one value: a number a single mode
   * reads (a quiz poll's `reveal_after_votes` under `reveal: after_votes`).
   * The draft drops the value on another mode (`normalizeBlockLooks`).
   */
  shownWhen?: { key: string; value: SchemaValue };
}

export type SchemaField =
  /**
   * `defaultValue`: the model's default of a LOOK the draft reads in normal
   * form (`normalizeSlideLook`): unset IS this value, so the picker
   * shows it, offers no separate "Default" and writes it back unset - put back
   * on its default, the block reads clean, like a `flag` at its default.
   * `optionLabels`: this field's own message id for an option whose shared
   * label means something else elsewhere (a callout header's `inline` is one
   * line, a quote's `inline` marks sit in the text).
   * `optionHints`: one short meaning line per option (message ids), shown
   * under the option's label in the list; `unsetHint`: the same line for
   * the "Default" entry (what the block draws while the field is unset).
   */
  | (FieldBase & {
      kind: "enum";
      options: readonly SchemaValue[];
      defaultValue?: SchemaValue;
      optionLabels?: Readonly<Record<string, string>>;
      optionHints?: Readonly<Record<string, string>>;
      unsetHint?: string;
    })
  /** `defaultOn`: the model defaults this flag to true, so unset reads as on. */
  | (FieldBase & { kind: "flag"; defaultOn?: boolean })
  | (FieldBase & { kind: "number"; min: number; max: number })
  | (FieldBase & { kind: "text"; markup: boolean; multiline?: boolean })
  | (FieldBase & { kind: "icon" })
  /** A picture picked from the deck template's assets of these roles (a timeline step's logo). */
  | (FieldBase & { kind: "asset"; roles: readonly string[] })
  /** A list of markup strings (a phase's bullets, a tier's features). */
  | (FieldBase & { kind: "strings"; max: number })
  /** A list of icon names (a title slide's icon strip): pick, reorder, remove. */
  | (FieldBase & { kind: "icons"; max: number })
  /** A nested object (a speaker, a comparison side, a goal). */
  | (FieldBase & { kind: "group"; fields: SchemaField[] })
  /**
   * A list of objects inside an item (a configuration card's rows): the same
   * collapsible list editor as a block's items, nested under the item.
   * `create` absent = edited, reordered and trimmed, never grown by hand.
   */
  | (FieldBase & { kind: "objects"; min: number; max: number; fields: SchemaField[]; create?: () => unknown });

export interface ItemsSchema {
  key: string;
  label: string;
  min: number;
  max: number;
  fields: SchemaField[];
  /**
   * A new item, valid on its own - absent for a list whose item needs what
   * the inspector cannot provide (a logo's file): the list is then edited,
   * reordered and trimmed, never grown by hand.
   */
  create?: () => unknown;
  /**
   * The model stores NO key for an empty list (an optional list defaulting to
   * `None`, a quote's `figures`): removing the last item writes it back
   * unset, so adding then removing one reads clean instead of offering a Save
   * that writes `[]`. A list with a `default_factory` keeps `[]`, which is what
   * the stored spec spells (pinned per list by `tests/test_presentation_editor.py`).
   */
  unsetWhenEmpty?: boolean;
}

export interface BlockSchema {
  label: string;
  fields: SchemaField[];
  items?: ItemsSchema;
  /** The `table` block's rows are a grid: the inspector draws a cell editor. */
  table?: boolean;
  /** The block the palette inserts; absent = not offered by hand. */
  create?: () => DeckBlock;
}

type Opts = {
  optional?: boolean;
  required?: boolean;
  hint?: string;
  info?: string;
  whenSet?: boolean;
  shownWhen?: { key: string; value: SchemaValue };
};

// The descriptor helpers are what the backend tripwire greps for
// (`en("size", "Size", SIZES)`, `flag("fill", ...)`, ...): keep the call shape.
const en = (
  key: string,
  label: string,
  options: readonly SchemaValue[],
  opts: Opts & {
    defaultValue?: SchemaValue;
    optionLabels?: Readonly<Record<string, string>>;
    optionHints?: Readonly<Record<string, string>>;
    unsetHint?: string;
  } = {},
): SchemaField => ({
  kind: "enum",
  key,
  label,
  options,
  ...opts,
});

/** The Frame picker of the image and video blocks: one definition, so both read the same in the inspector. */
const MEDIA_FRAME_OPTS = {
  hint: "A window or a device around a product screenshot, on a glow in the brand gradient",
  info: "The frame drawn around a product screenshot, on a glow in the brand gradient. Pick Slim browser window when several screenshots share a slide. None shows the picture alone.",
  optionHints: {
    none: "The picture alone, no frame",
    browser: "A window bar with an address field, for one screenshot on a slide",
    browser_slim: "A thin window bar, for several screenshots on one slide",
    device: "A thin bezel with a camera dot",
  },
  unsetHint: "No frame, as None",
} as const;

/** What an enum picker shows for `value`: the value, a look's `defaultValue` while unset, or unset. */
export function enumShown(field: SchemaField, value: unknown): SchemaValue | undefined {
  if (value !== undefined && value !== null) return value as SchemaValue;
  return field.kind === "enum" ? field.defaultValue : undefined;
}

/** What an items editor writes for the list `next`: emptied, a list the model stores no key for is unset. */
export function itemsWritten(schema: ItemsSchema, next: unknown[]): unknown[] | undefined {
  return next.length === 0 && schema.unsetWhenEmpty ? undefined : next;
}

/** What an enum picker writes for the option picked (`undefined` unsets): a look's default is unset. */
export function enumWritten(field: SchemaField, picked: SchemaValue | undefined): SchemaValue | undefined {
  return field.kind === "enum" && picked === field.defaultValue ? undefined : picked;
}

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
// `objects("rows", "Rows", 1, 6, [...])`: the tripwire reads min and max
// against the model's list bounds - keep the call shape.
const objects = (
  key: string,
  label: string,
  min: number,
  max: number,
  fields: SchemaField[],
  opts: Opts & { create?: () => unknown } = {},
): SchemaField => ({
  kind: "objects",
  key,
  label,
  min,
  max,
  fields,
  ...opts,
});

/** The fields of one KPI tile (`MetricItem`): a `metric` block, a grid cell, a dashboard tile. */
export const METRIC_FIELDS: SchemaField[] = [
  text("value", "Value"),
  markup("label", "Label"),
  markup("sublabel", "Sublabel", { optional: true }),
  text("delta", "Delta", { optional: true, hint: "e.g. +12% vs last quarter" }),
  en("trend", "Trend", TRENDS, { optional: true }),
  en("delta_tone", "Delta tone", DELTA_TONES, {
    optional: true,
    hint: "Colours the delta and its arrow. Default: by trend, up green and down red; Success for a cost or a loss that falls.",
  }),
  icon("icon", "Icon", { optional: true }),
  en("tone", "Tone", TONES),
];

/** The fields of one figure under a quote (`QuoteFigure`): a plain tile, value and label only. */
export const QUOTE_FIGURE_FIELDS: SchemaField[] = [
  text("value", "Value"),
  markup("label", "Label"),
  en("tone", "Tone", TONES),
];

/** The fields of one phase (`Phase`): the `phase_card` block, a roadmap column. */
export const PHASE_FIELDS: SchemaField[] = [
  markup("title", "Title (heading)"),
  markup("subtitle", "Subtitle", { optional: true }),
  strings("items", "Bullets", 8),
  group("goal", "Goal", [markup("label", "Label"), markup("value", "Value")], {
    optional: true,
  }),
  icon("icon", "Icon", { optional: true }),
  en("tone", "Tone", TONES),
];

/** The fields of one numbered card (`NumberedCard`): `numbered_cards`, the takeaways slide. */
export const NUMBERED_CARD_FIELDS: SchemaField[] = [
  markup("title", "Title (heading)"),
  markup("description", "Description", { optional: true, multiline: true }),
  num("number", "Number", 1, 99, { optional: true }),
  icon("icon", "Icon", { optional: true }),
];

export const BLOCK_SCHEMA: Record<DeckBlockType, BlockSchema> = {
  text: {
    label: "Text",
    fields: [
      markup("content", "Text", { multiline: true }),
      en("size", "Size", SIZES),
      en("align", "Alignment", ALIGNS),
      en("tone", "Tone", TONES, { optional: true }),
    ],
    create: () => ({ type: "text", content: "New paragraph." }),
  },
  heading: {
    label: "Heading",
    fields: [
      markup("content", "Text"),
      en("level", "Level", HEADING_LEVELS),
      en("align", "Alignment", ALIGNS),
      en("tone", "Tone", TONES, { optional: true }),
      icon("icon", "Icon", { optional: true }),
    ],
    create: () => ({ type: "heading", content: "Heading", level: 3 }),
  },
  quote: {
    label: "Quote",
    fields: [
      markup("content", "Quote", { multiline: true }),
      markup("author", "Author", { optional: true }),
      markup("role", "Role", { optional: true }),
      markup("organization", "Organization", { optional: true }),
      // The one-line credit of the decks written before the split fields:
      // shown while a quote carries one, never offered on a new one.
      markup("attribution", "Attribution", { optional: true, whenSet: true }),
      en("size", "Size", QUOTE_SIZES, { hint: "Hero is the size of a title-slide headline." }),
      en("align", "Alignment", START_CENTER),
      en("quote_marks", "Quote marks", QUOTE_MARKS),
      flag("italic", "Italic", { optional: true }),
    ],
    items: {
      key: "figures",
      label: "Figures",
      min: 0,
      max: MAX_QUOTE_FIGURES,
      fields: QUOTE_FIGURE_FIELDS,
      create: () => ({ value: "0", label: "Figure" }),
      unsetWhenEmpty: true,
    },
    create: () => ({
      type: "quote",
      content: "A sentence worth remembering.",
      author: "Someone",
    }),
  },
  list: {
    label: "List",
    fields: [
      en("style", "Style", LIST_STYLES),
      en("size", "Size", SIZES),
      en("density", "Density", DENSITIES),
      en("columns", "Columns", TWO_COLUMNS),
      flag("fill", "Fill the height", {
        hint: "Spread the rows over the available height, up to a readable gap.",
      }),
    ],
    items: {
      key: "items",
      label: "Items",
      min: 1,
      max: 12,
      fields: [
        markup("text", "Text"),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
        asset("logo_file_id", "Picture", LOGO_ROLES, { optional: true, hint: "Drawn in place of the marker (a flag, a logo)" }),
        flag("logo_border", "Picture border"),
      ],
      create: () => ({ text: "New point" }),
    },
    create: () => ({
      type: "list",
      items: [{ text: "First point" }, { text: "Second point" }, { text: "Third point" }],
    }),
  },
  callout: {
    label: "Callout",
    fields: [
      markup("title", "Title (heading)", { optional: true }),
      markup("subtitle", "Subtitle", { optional: true }),
      markup("content", "Text", { optional: true, multiline: true }),
      en("tone", "Tone", TONES),
      en("variant", "Box", CALLOUT_VARIANTS),
      en("header", "Header", CALLOUT_HEADERS, {
        optional: true,
        hint: "Default: the icon in a column left of the text.",
        optionLabels: { inline: "On one line" },
      }),
      flag("accent_bar", "Accent bar", { optional: true }),
      icon("icon", "Icon", { optional: true }),
      en("icon_size", "Icon size", SIZES, { optional: true, hint: "Unset: the callout's size applies, if it has one." }),
      en("title_size", "Title size", SIZES, { optional: true, hint: "Unset: the callout's size applies, if it has one." }),
      en("density", "Density", DENSITIES),
      en("padding", "Padding", SIZES, { optional: true }),
      en("children_gap", "Spacing", SIZES, { optional: true }),
      flag("fill", "Fill the height"),
    ],
    create: () => ({
      type: "callout",
      tone: "accent",
      title: "Callout",
      content: "What to remember.",
    }),
  },
  metric: {
    label: "Metric",
    fields: [
      ...METRIC_FIELDS,
      en("variant", "Variant", METRIC_VARIANTS, {
        defaultValue: "card",
        hint: "Plain drops the card: no surface, border or bar.",
      }),
      en("align", "Alignment", START_CENTER, { defaultValue: "start" }),
    ],
    create: () => ({ type: "metric", value: "42%", label: "Metric" }),
  },
  metrics_grid: {
    label: "Metrics grid",
    fields: [
      en("columns", "Columns", GRID_COLUMNS, { optional: true }),
      en("density", "Density", DENSITIES),
      en("size", "Size", SIZES, { hint: "Tiles of one size read the same on every slide." }),
      en("variant", "Variant", METRIC_VARIANTS, {
        defaultValue: "card",
        hint: "Plain drops the cards: no surface, border or bar.",
      }),
      en("align", "Alignment", START_CENTER, { defaultValue: "start" }),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "items",
      label: "Tiles",
      min: 1,
      max: 8,
      fields: METRIC_FIELDS,
      create: () => ({ value: "0", label: "Metric" }),
    },
    create: () => ({
      type: "metrics_grid",
      items: [
        { value: "12", label: "First" },
        { value: "34", label: "Second" },
        { value: "56", label: "Third" },
      ],
    }),
  },
  table: {
    label: "Table",
    fields: [
      en("density", "Density", DENSITIES),
      en("size", "Size", SIZES),
      flag("highlight_first_column", "Highlight the first column"),
      markup("footnote", "Footnote", { optional: true }),
      flag("fill", "Fill the height", {
        hint: "Spread the rows over the available height, up to a readable gap.",
      }),
    ],
    items: {
      key: "headers",
      label: "Columns",
      min: 1,
      max: MAX_TABLE_COLUMNS,
      fields: [
        markup("text", "Header"),
        en("align", "Alignment", ALIGNS),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ],
      create: () => ({ text: "Column" }),
    },
    table: true,
    create: () => ({
      type: "table",
      headers: [{ text: "Item" }, { text: "Value" }],
      rows: [
        [{ text: "First" }, { text: "1" }],
        [{ text: "Second" }, { text: "2" }],
      ],
    }),
  },
  chart: {
    label: "Chart",
    fields: [
      en("kind", "Kind", CHART_KINDS, { required: true }),
      en("format", "Number format", VALUE_FORMATS),
      text("unit", "Unit", {
        optional: true,
        hint: "Written with every value: h, pts, EUR.",
      }),
      en("decimals", "Decimals", CHART_DECIMALS, {
        optional: true,
        hint: "Every value written with this many decimals. Default: two significant digits under 10, and two different values never read the same.",
      }),
      en("unit_scale", "Unit scale", CHART_UNIT_SCALES, {
        optional: true,
        hint: "Currency only: every amount written in the same unit - thousands, millions or billions - with the decimals above. Default: each amount in its own (480k next to 1.2M).",
        optionLabels: UNIT_SCALE_LABELS,
      }),
      flag("stacked", "Stacked"),
      flag("horizontal", "Horizontal bars"),
      flag("show_values", "Show values", { defaultOn: true }),
      flag("show_legend", "Show legend", { optional: true }),
      en("legend_position", "Legend position", LEGEND_POSITIONS, {
        optional: true,
        hint: "Default: top, on the panel title's row. Bottom puts it on one line under the axis and gives the plot the room; right stacks it beside the plot.",
      }),
      num("target", "Target", -1_000_000_000, 1_000_000_000, {
        optional: true,
      }),
      text("goal_label", "Target label", { optional: true }),
      num("tolerance", "Bridge tolerance (%)", 0, 5, {
        optional: true,
        hint: "Waterfall only: how far a total may miss the sum of the steps before it, in % of those steps.",
      }),
      flag("allow_gap", "Draw unexplained gaps", {
        hint: "Waterfall only: a total the steps do not add up to is drawn after an Unexplained step instead of being refused.",
      }),
      en("spine", "Category labels", BUTTERFLY_SPINES, {
        optional: true,
        optionLabels: { center: "On the spine", left: "Left axis" },
        hint: "Butterfly only: the rungs written in a column on the spine between the two sides (default), or on the left axis with the sides meeting at the spine.",
      }),
      flag("symmetric", "Same extent both sides", {
        optional: true,
        hint: "Butterfly only: on (default), both sides are drawn to the same extent; off ends each side at its own largest value, on the same scale.",
      }),
      flag("show_totals", "Side totals", {
        hint: "Butterfly only: each side's total written under its legend entry.",
      }),
      en("region", "Map region", MAP_REGIONS, {
        optional: true,
        hint: "Map only: the part of the world drawn. Default: the world; the countries around a region are drawn at its edges.",
      }),
      en("legend_style", "Map legend", MAP_LEGEND_STYLES, {
        optional: true,
        hint: "Map only: a ramp from the lowest value to the highest (default), or classes - up to five flat colours, cut where the values part, listed beside the map.",
      }),
      en("classes", "Map classes", MAP_CLASS_COUNTS, {
        optional: true,
        hint: "Classes legend only: how many classes, never more than the map has different values. Default: up to five.",
      }),
      flag("markers", "Small-country markers", {
        optional: true,
        hint: "Map only: a dot over each country too small to see. Off, it keeps its fill alone; a territory with no shape of its own on the map (Réunion) keeps its dot.",
      }),
      en("secondary_format", "Right axis number format", VALUE_FORMATS, { optional: true }),
      text("secondary_unit", "Right axis unit", {
        optional: true,
        hint: "Written with every value read against the right axis: x, EUR.",
      }),
      en("secondary_decimals", "Right axis decimals", CHART_DECIMALS, { optional: true }),
      en("secondary_unit_scale", "Right axis unit scale", CHART_UNIT_SCALES, {
        optional: true,
        optionLabels: UNIT_SCALE_LABELS,
      }),
      num("secondary_target", "Right axis target", -1_000_000_000, 1_000_000_000, {
        optional: true,
      }),
      num("y_min", "Axis minimum", -1_000_000_000, 1_000_000_000, {
        optional: true,
        hint: "Where the value axis starts. Empty: the chart's own. It keeps zero and every value it plots.",
      }),
      num("y_max", "Axis maximum", -1_000_000_000, 1_000_000_000, {
        optional: true,
        hint: "Where the value axis ends: raise it to give a line room above the bars.",
      }),
      num("secondary_y_min", "Right axis minimum", -1_000_000_000, 1_000_000_000, {
        optional: true,
        hint: "Where the right axis starts: below zero lifts its line clear of the bars.",
      }),
      num("secondary_y_max", "Right axis maximum", -1_000_000_000, 1_000_000_000, { optional: true }),
      markup("title", "Title (heading)", {
        optional: true,
        hint: "Top-left inside the frame, as a dashboard panel's title; the caption is the note under the plot.",
      }),
      markup("caption", "Caption", { optional: true }),
      en("size", "Size", MEDIA_SIZES, {
        hint: "How tall the chart stands on a slide. On a gauge, the dial's diameter - sm, md or lg - in a dashboard panel too; Fill the height draws the largest dial the room holds.",
      }),
      en("label_size", "Text size", SIZES, {
        hint: "Values and axis text: sm as on a dashboard card, lg read from the back of the room.",
      }),
      en("value_label_size", "Value text size", VALUE_LABEL_SIZES, {
        optional: true,
        hint: "The values on the bars and points, apart from the axis text: a text size, or a size in px. Default: the text size above.",
      }),
      en("value_label_fit", "Value labels", VALUE_LABEL_FITS, {
        optional: true,
        optionLabels: { auto: "Smaller, then turned", stagger: "Staggered", upright: "Upright, one row" },
        hint: "Default: set smaller (then turned on their side above upright bars) when the values do not fit. Staggered keeps the text size and moves only a value that would touch another or the line: a row higher above upright bars, along its row past the one in its way on horizontal bars, where a segment's value too long for its segment is written past the stack. Upright keeps the text size and each value in its place: above upright bars, one that would touch another or the line moves one row higher if that row is clear. A value that still touches, or that its segment cannot hold, is left out.",
      }),
      flag("fill", "Fill the height"),
      flag("framed", "Framed", {
        defaultOn: true,
        hint: "On a card with a hairline border, like a dashboard panel. Off, the chart is drawn straight on the slide.",
      }),
    ],
  },
  key_value: {
    label: "Key / value",
    fields: [
      en("columns", "Columns", TWO_COLUMNS),
      en("density", "Density", DENSITIES),
      flag("framed", "Framed", { defaultOn: true }),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "items",
      label: "Pairs",
      min: 1,
      max: 10,
      fields: [
        markup("label", "Label"),
        markup("value", "Value"),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ],
      create: () => ({ label: "Label", value: "Value" }),
    },
    create: () => ({
      type: "key_value",
      items: [
        { label: "Label", value: "Value" },
        { label: "Label", value: "Value" },
      ],
    }),
  },
  badges: {
    label: "Badges",
    fields: [en("variant", "Variant", BADGE_VARIANTS), en("size", "Size", SIZES), en("align", "Alignment", ALIGNS)],
    items: {
      key: "items",
      label: "Badges",
      min: 1,
      max: 10,
      fields: [markup("text", "Text"), en("tone", "Tone", TONES), icon("icon", "Icon", { optional: true })],
      create: () => ({ text: "Badge" }),
    },
    create: () => ({
      type: "badges",
      items: [{ text: "Badge" }, { text: "Badge" }],
    }),
  },
  feature_list: {
    label: "Feature list",
    fields: [en("columns", "Columns", TWO_COLUMNS), en("size", "Size", SIZES), flag("fill", "Fill the height")],
    items: {
      key: "items",
      label: "Features",
      min: 1,
      max: 8,
      fields: [
        markup("title", "Title (heading)"),
        markup("description", "Description", { optional: true }),
        icon("icon", "Icon", { optional: true }),
        en("tone", "Tone", TONES),
        asset("logo_file_id", "Picture", LOGO_ROLES, { optional: true, hint: "Drawn in place of the icon (a flag, a logo)" }),
        flag("logo_border", "Picture border"),
      ],
      create: () => ({
        icon: "check",
        title: "Feature",
        description: "What it does for the team",
      }),
    },
    create: () => ({
      type: "feature_list",
      items: [
        {
          icon: "shield-check",
          title: "Feature",
          description: "What it does for the team",
        },
        {
          icon: "zap",
          title: "Feature",
          description: "What it does for the team",
        },
      ],
    }),
  },
  phase_card: {
    label: "Phase card",
    fields: [...PHASE_FIELDS, flag("fill", "Fill the height")],
    create: () => ({
      type: "phase_card",
      title: "Phase",
      items: ["First step", "Second step"],
    }),
  },
  numbered_cards: {
    label: "Numbered cards",
    fields: [
      en("columns", "Columns", CARD_COLUMNS, { optional: true }),
      en("tone", "Tone", TONES),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "items",
      label: "Cards",
      min: 1,
      max: 6,
      fields: NUMBERED_CARD_FIELDS,
      create: () => ({ title: "Card" }),
    },
    create: () => ({
      type: "numbered_cards",
      items: [{ title: "First" }, { title: "Second" }, { title: "Third" }],
    }),
  },
  pricing_tiers: {
    label: "Pricing tiers",
    fields: [en("density", "Density", DENSITIES)],
    items: {
      key: "tiers",
      label: "Tiers",
      min: 1,
      max: 4,
      fields: [
        markup("name", "Name"),
        text("price", "Price"),
        markup("period", "Period", { optional: true }),
        markup("description", "Description", { optional: true }),
        strings("features", "Features", 8),
        markup("cta", "Button", { optional: true }),
        markup("badge", "Ribbon", { optional: true }),
        flag("highlight", "Highlighted"),
        icon("icon", "Icon", { optional: true }),
        en("tone", "Tone", TONES),
      ],
      create: () => ({ name: "Tier", price: "Contact us" }),
    },
    create: () => ({
      type: "pricing_tiers",
      tiers: [
        { name: "Community", price: "Free", features: ["Core features"] },
        {
          name: "Enterprise",
          price: "Contact us",
          features: ["Everything in Community", "Support"],
          highlight: true,
        },
      ],
    }),
  },
  personas: {
    label: "Personas",
    fields: [
      markup("pains_label", "Pains heading", {
        optional: true,
        hint: "Default: Challenges",
      }),
      markup("gains_label", "Gains heading", {
        optional: true,
        hint: "Default: Outcomes",
      }),
      en("columns", "Columns", PERSONA_COLUMNS, { optional: true }),
      en("density", "Density", DENSITIES),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "items",
      label: "Personas",
      min: 1,
      max: 4,
      fields: [
        markup("name", "Role"),
        markup("tagline", "Tagline", { optional: true }),
        strings("pains", "Pains", 4),
        strings("gains", "Gains", 4),
        icon("icon", "Icon", { optional: true }),
        en("tone", "Tone", TONES),
      ],
      create: () => ({ name: "Role", tagline: "What they own" }),
    },
    create: () => ({
      type: "personas",
      items: [
        {
          name: "CISO",
          pains: ["No single view of exposure"],
          gains: ["One posture, one report"],
        },
        {
          name: "SOC analyst",
          pains: ["Alerts without context"],
          gains: ["Context on every alert"],
        },
      ],
    }),
  },
  profiles: {
    label: "Profiles",
    fields: [
      en("columns", "Columns", PROFILE_COLUMNS, { optional: true }),
      en("density", "Density", DENSITIES),
      en("photo_size", "Portrait size", SIZES),
    ],
    items: {
      key: "items",
      label: "People",
      min: 1,
      max: 12,
      fields: [
        markup("name", "Name"),
        markup("title", "Title", { optional: true }),
        en("tone", "Tone", TONES),
      ],
      // The lines are edited in place on the stage (each one is a text run);
      // a line's logo and the portrait need a file - the assistant's.
      create: () => ({ name: "Name", title: "Title", lines: [{ text: "One line about them" }] }),
    },
    create: () => ({
      type: "profiles",
      items: [
        {
          name: "Jane Doe",
          title: "Co-founder & CEO",
          lines: [{ text: "Former CISO of a listed company" }, { text: "Built two security platforms" }],
        },
        {
          name: "John Doe",
          title: "Co-founder & CTO",
          lines: [{ text: "Open-source maintainer" }, { text: "Threat intelligence engineer" }],
        },
      ],
    }),
  },
  timeline: {
    label: "Timeline",
    fields: [
      en("style", "Style", TIMELINE_STYLES),
      en("density", "Density", DENSITIES),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "items",
      label: "Steps",
      min: 2,
      max: 8,
      fields: [
        markup("title", "Title (heading)"),
        markup("label", "Label", { optional: true, hint: "e.g. Q1 2026" }),
        markup("description", "Description", { optional: true }),
        text("marker", "Marker", {
          optional: true,
          hint: "1 to 4 characters shown in the node",
        }),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
        asset("logo_file_id", "Logo", LOGO_ROLES, { optional: true, hint: "Drawn under the step's description" }),
        en("logo_height", "Logo height", SIZES, { hint: "With a logo" }),
        flag("framed", "Framed logo", { defaultOn: true }),
      ],
      create: () => ({ title: "Step" }),
    },
    create: () => ({
      type: "timeline",
      items: [
        { label: "Q1", title: "Discover" },
        { label: "Q2", title: "Build" },
        { label: "Q3", title: "Roll out" },
      ],
    }),
  },
  process_flow: {
    label: "Process flow",
    fields: [en("direction", "Direction", FLOW_DIRECTIONS), flag("loop", "Loop back to the start")],
    items: {
      key: "steps",
      label: "Steps",
      min: 2,
      max: 6,
      fields: [
        markup("title", "Title (heading)"),
        markup("description", "Description", { optional: true }),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ],
      create: () => ({ title: "Step" }),
    },
    create: () => ({
      type: "process_flow",
      steps: [{ title: "Collect" }, { title: "Analyse" }, { title: "Act" }],
    }),
  },
  section_header: {
    label: "Section header",
    fields: [markup("title", "Title (heading)"), en("tone", "Tone", TONES), icon("icon", "Icon", { optional: true })],
    create: () => ({ type: "section_header", title: "Section" }),
  },
  banner: {
    label: "Banner",
    fields: [
      markup("title", "Title (heading)"),
      markup("subtitle", "Subtitle", { optional: true }),
      markup("eyebrow", "Eyebrow", { optional: true }),
      en("variant", "Variant", BANNER_VARIANTS),
      en("tone", "Tone", TONES),
      en("align", "Alignment", ALIGNS),
      en("size", "Size", SIZES),
      icon("icon", "Icon", { optional: true }),
    ],
    items: {
      key: "actions",
      label: "Buttons",
      min: 0,
      max: 3,
      fields: [
        markup("label", "Label"),
        text("url", "Link", { optional: true, hint: "https:// or mailto:" }),
        flag("primary", "Filled button", { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ],
      create: () => ({ label: "Learn more" }),
    },
    create: () => ({
      type: "banner",
      title: "Call to action",
      subtitle: "One line on what happens next.",
    }),
  },
  challenge_answer: {
    label: "Challenge and answer",
    fields: [
      markup("challenge", "The challenge", { multiline: true }),
      markup("answer", "The answer", { multiline: true }),
      icon("icon", "Icon", { optional: true, hint: "Leads the challenge card; the answer keeps its check." }),
      markup("challenge_label", "Challenge heading", { optional: true, hint: "Default: Challenge" }),
      markup("answer_label", "Answer heading", { optional: true, hint: "Default: Answer" }),
      flag("fill", "Fill the height"),
    ],
    create: () => ({
      type: "challenge_answer",
      challenge: "The problem, in the audience's words.",
      answer: "What resolves it.",
    }),
  },
  image: {
    label: "Image",
    fields: [
      text("alt", "Alternative text", { optional: true }),
      en("fit", "Fit", IMAGE_FITS),
      en("size", "Size", MEDIA_SIZES),
      en("align", "Alignment", ALIGNS),
      flag("fill", "Fill the height"),
      en("padding", "Padding", PADDINGS, { hint: "Room between the picture and its card" }),
      en("frame", "Frame", IMAGE_FRAMES, MEDIA_FRAME_OPTS),
      markup("caption", "Caption", { optional: true }),
    ],
  },
  video: {
    label: "Video",
    fields: [
      text("alt", "Alternative text", { optional: true, hint: "Read by screen readers in place of the video" }),
      flag("autoplay", "Autoplay", { hint: "Plays when the slide is shown, muted unless Muted is off" }),
      flag("muted", "Muted", { hint: "No sound until the viewer turns it on" }),
      flag("loop", "Loop", { hint: "Starts again at the end" }),
      flag("controls", "Controls", { defaultOn: true, hint: "Play, pause, volume and the timeline under the video" }),
      flag("captions", "Captions", { defaultOn: true, hint: "The subtitles of the render, when it has them" }),
      num("start", "Start at", 0, 14400, { optional: true, hint: "Seconds" }),
      num("end", "End at", 0, 14400, { optional: true, hint: "Seconds" }),
      en("size", "Size", MEDIA_SIZES, { hint: "How much of the column the video takes" }),
      en("align", "Alignment", ALIGNS, { hint: "Where the video sits in its column" }),
      flag("fill", "Fill the height", { hint: "Grows the video to the height left on the slide" }),
      en("frame", "Frame", IMAGE_FRAMES, MEDIA_FRAME_OPTS),
      markup("caption", "Caption", { optional: true, hint: "A note under the video" }),
    ],
  },
  logo_grid: {
    label: "Logo grid",
    fields: [
      en("logo_height", "Logo height", LOGO_HEIGHTS, {
        optional: true,
        hint: "Unset: the grid's size applies (Medium unless set).",
      }),
      en("columns", "Columns", LOGO_COLUMNS, { optional: true }),
      num("rows", "Rows", 1, 16, { optional: true }),
      num("tile_height", "Tile height", 32, 400, {
        optional: true,
        hint: "Canvas px of every cell, whatever it holds: sibling grids share one tile, the logo fits inside.",
      }),
      en("align", "Alignment", LOGO_ALIGNS, { optional: true }),
      en("gap", "Gap", SIZES),
      en("tone", "Tone", LOGO_TONES, {
        hint: "Ink keeps a badge's inner detail in the text colour. It applies in view and export, not while editing.",
      }),
      flag("optical_balance", "Optical balance", { defaultOn: true }),
      flag("optical_weight", "Same visual weight", {
        hint: "Every logo at one visual weight: wide wordmarks shrink, seals grow.",
      }),
      flag("framed", "Framed", { defaultOn: true }),
      flag("show_labels", "Show labels"),
      flag("fill", "Fill the height"),
      markup("caption", "Caption", { optional: true }),
    ],
    items: {
      key: "items",
      label: "Logos",
      min: 1,
      max: 16,
      fields: [markup("label", "Label", { optional: true })],
      // A logo needs a file: the list edits labels, reorders and removes,
      // never adds a cell (no `create` - a `{file_id: ""}` would fail the save).
    },
  },
  logo_wall: {
    label: "Logo wall",
    fields: [
      en("columns", "Cards per row", LOGO_WALL_COLUMNS, { optional: true }),
      num("per_row", "Logos per row", 1, 12, { optional: true }),
      en("logo_height", "Logo height", LOGO_HEIGHTS),
      num("tile_height", "Tile height", 32, 400, {
        optional: true,
        hint: "Canvas px of every cell, whatever it holds: sibling grids share one tile, the logo fits inside.",
      }),
      en("tone", "Tone", LOGO_TONES, {
        hint: "Ink keeps a badge's inner detail in the text colour. It applies in view and export, not while editing.",
      }),
      flag("optical_balance", "Optical balance", { defaultOn: true }),
      flag("optical_weight", "Same visual weight", {
        hint: "Every logo at one visual weight: wide wordmarks shrink, seals grow.",
      }),
      flag("framed", "Framed"),
      flag("show_labels", "Show labels"),
      flag("fill", "Fill the height", { defaultOn: true }),
      markup("caption", "Caption", { optional: true }),
    ],
    items: {
      key: "groups",
      label: "Groups",
      min: 1,
      max: 6,
      fields: [markup("title", "Title (heading)"), icon("icon", "Icon", { optional: true }), en("tone", "Tone", TONES)],
      // A group needs its logos (files): the list edits titles, reorders and
      // removes, never adds a group by hand.
    },
  },
  formula: {
    label: "Formula",
    // `operators` (one per gap) is the assistant's: unset draws + ... + =.
    fields: [
      en("align", "Alignment", START_CENTER, {
        defaultValue: "center",
        optionLabels: { start: "Left-aligned", center: "Centered" },
      }),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "terms",
      label: "Terms",
      min: 2,
      max: 5,
      fields: [
        markup("title", "Title (heading)"),
        markup("subtitle", "Subtitle", { optional: true }),
        icon("icon", "Icon", { optional: true }),
        asset("logo_file_id", "Logo", LOGO_ROLES, { optional: true, hint: "A product logo instead of the icon" }),
        asset("logo_light_file_id", "Logo on a light slide", LOGO_ROLES, { optional: true }),
        // A row of logos (files): each one's light-slide variant is picked
        // here; adding a logo is the assistant's (it needs a file).
        objects("logos", "Logos", 0, 4, [
          asset("file_id", "Logo", LOGO_ROLES),
          asset("logo_light_file_id", "Logo on a light slide", LOGO_ROLES, { optional: true }),
        ]),
        en("tone", "Tone", TONES_OR_COLORS, { optionLabels: PALETTE_COLOR_LABELS }),
        en("variant", "Look", FORMULA_VARIANTS, {
          optionLabels: { card: "Card", result: "Result (brand gradient)" },
          hint: "One term at most is the result, usually the last.",
        }),
        flag("show_title", "Show the title", {
          defaultOn: true,
          hint: "Off: the logo names the term",
        }),
      ],
      create: () => ({ title: "Term" }),
    },
    create: () => ({
      type: "formula",
      terms: [
        { title: "Core", icon: "sparkles" },
        { title: "Modules", icon: "blocks", tone: "info" },
        { title: "The offer", icon: "badge-check", variant: "result" },
      ],
    }),
  },
  config_cards: {
    label: "Configuration cards",
    fields: [
      markup("heading", "Heading", { optional: true }),
      en("row_style", "Row style", CONFIG_ROW_STYLES, {
        defaultValue: "outlined",
        optionLabels: { outlined: "Framed", plain: "No frame" },
      }),
      flag("fill", "Fill the height"),
    ],
    items: {
      key: "cards",
      label: "Cards",
      min: 2,
      max: 4,
      fields: [
        markup("title", "Title (heading)"),
        markup("subtitle", "Subtitle", { optional: true }),
        en("tone", "Tone", TONES_OR_COLORS, { optionLabels: PALETTE_COLOR_LABELS }),
        objects(
          "rows",
          "Rows",
          1,
          6,
          [
            markup("label", "Label", { optional: true }),
            asset("logo_file_id", "Logo", LOGO_ROLES, { optional: true, hint: "The label becomes its alt text" }),
            asset("logo_light_file_id", "Logo on a light slide", LOGO_ROLES, { optional: true }),
            markup("chip", "Status chip", { optional: true }),
            en("chip_tone", "Chip colour", TONES_OR_COLORS, { optionLabels: PALETTE_COLOR_LABELS }),
            flag("tinted", "Tint the row"),
            en("state", "State", CONFIG_ROW_STATES, {
              optionLabels: { active: "Active", dimmed: "Available, not used" },
            }),
          ],
          { create: () => ({ label: "Row", chip: "Included" }) },
        ),
        markup("footer", "Footer", { optional: true }),
        markup("footer_value", "Footer value", { optional: true }),
      ],
      // A card needs its rows: the list edits, reorders and removes cards,
      // never adds one by hand; a card's rows are added here.
    },
  },
  diagram: {
    label: "Diagram",
    fields: [
      text("source", "Mermaid source", { multiline: true }),
      markup("caption", "Caption", { optional: true }),
      en("size", "Size", MEDIA_SIZES),
      flag("fill", "Fill the height"),
    ],
  },
  architecture: {
    label: "Architecture",
    fields: [
      markup("caption", "Caption", { optional: true }),
      en("size", "Size", MEDIA_SIZES),
      flag("fill", "Fill the height"),
    ],
  },
  positioning_map: {
    label: "Positioning map",
    fields: [
      markup("title", "Title (heading)", { optional: true }),
      en("axes", "Axes", MAP_AXES, { optionLabels: { center: "Crossing at the centre", edges: "Along the edges" } }),
      en("callouts_position", "Callouts", MAP_CALLOUTS_POSITIONS, {
        optionLabels: { below: "Under the map", right: "Beside the map" },
      }),
      markup("caption", "Caption", { optional: true }),
      en("size", "Size", MEDIA_SIZES),
      flag("fill", "Fill the height"),
    ],
  },
  org_chart: {
    label: "Org chart",
    fields: [
      en("orientation", "Orientation", ORG_ORIENTATIONS, {
        optionLabels: { top_down: "Top down", left_right: "Left to right", radial: "Radial" },
      }),
      en("style", "Style", ORG_STYLES, {
        optionLabels: { cards: "Cards", compact: "Compact", bubbles: "Bubbles", hybrid: "Cards over bubbles" },
      }),
      flag("show_counts", "Show the headcount under each manager"),
      en("count_mode", "Headcount", ORG_COUNT_MODES, {
        optionLabels: { current: "Today", target: "Target", both: "Today and target" },
      }),
      num("collapse_below", "Fold the reports from level", 1, 12, { optional: true }),
      num("depth_limit", "Levels drawn", 1, 12, { optional: true }),
      flag("group_by_branch", "One zone per branch"),
      flag("show_dotted", "Dotted lines", { defaultOn: true }),
      en("spine", "Leaves in a column", ORG_SPINES, {
        optionLabels: { auto: "When a level is too wide", always: "Always", never: "Never" },
      }),
      en("legend", "Legend", ORG_LEGENDS, { optionLabels: { auto: "Automatic", none: "None", custom: "Custom" } }),
      en("label_size", "Label size", SIZES),
      en("photo_size", "Photo size", SIZES),
      markup("caption", "Caption", { optional: true }),
      en("size", "Size", MEDIA_SIZES),
      flag("fill", "Fill the height"),
    ],
  },
  poll: {
    label: "Live poll",
    fields: [
      text("id", "Poll id", { hint: "Keeps the short link and the votes when the block moves." }),
      // Short options with their meaning line under them, so no translation is cut
      // in the 340 px inspector (UX #10705 r1 A2, as the Mode field below).
      en("show_results", "Results", POLL_SHOW_RESULTS, {
        defaultValue: "live",
        optionLabels: { live: "Real time", after_lock: "After lock" },
        optionHints: { live: "Each vote shows as it arrives", after_lock: "Once voting is locked" },
      }),
      flag("show_qr", "Show the QR code", { defaultOn: true }),
      // Shown as the poll's mode (#10235): guided = layout active, autonomous = layout all.
      // A one-word option with its meaning line under it, so no translation is cut
      // in the 340 px inspector (UX #10269 r3 A4).
      en("layout", "Mode", POLL_LAYOUTS, {
        defaultValue: "active",
        optionLabels: { active: "Guided", all: "Autonomous" },
        optionHints: {
          active: "The presenter steps question by question",
          all: "Every question open at once, no presenter action",
        },
      }),
      // Hands-free (#10315): nothing of the presenter on the presented screen.
      en("controls", "Presenter controls", POLL_CONTROLS, {
        defaultValue: "hover",
        optionLabels: { hover: "On hover", none: "No controls" },
        optionHints: {
          hover: "A bar on pointer move and keys in fullscreen",
          none: "Driven from the speaker view or under the deck",
        },
      }),
      en("reveal", "Quiz answer", POLL_REVEALS, {
        defaultValue: "manual",
        optionLabels: {
          manual: "Manual",
          live: "From the start",
          after_votes: "After votes",
          after_seconds: "After a delay",
          never: "Never revealed",
        },
        optionHints: {
          manual: "Shown once revealed",
          live: "Shown from the start",
          after_votes: "Shown after a number of votes",
          after_seconds: "Shown some seconds after the first vote",
          never: "The right answer is never shown to the room",
        },
      }),
      // Shown with the matching `reveal` only (#10411, UX round 1 B2); another mode drops the number.
      num("reveal_after_votes", "Votes before the answer", 1, 500, {
        optional: true,
        hint: "The answer shows once a question has this many votes; 10 when empty.",
        shownWhen: { key: "reveal", value: "after_votes" },
      }),
      num("reveal_after_seconds", "Seconds before the answer", 1, 3600, {
        optional: true,
        hint: "The answer shows this many seconds after a question's first vote; 20 when empty.",
        shownWhen: { key: "reveal", value: "after_seconds" },
      }),
    ],
    items: {
      key: "questions",
      label: "Questions",
      min: 1,
      max: 5,
      fields: [
        text("id", "Question id"),
        en("kind", "Kind", POLL_KINDS, {
          defaultValue: "choice",
          optionLabels: { choice: "Choice", quiz: "Quiz", word_cloud: "Word cloud", rating: "Rating 1-5" },
        }),
        markup("prompt", "Question"),
        strings("options", "Options", 6),
        num("correct", "Right answer (quiz, from 0)", 0, 5, { optional: true }),
        num("max_words", "Words per voter", 1, 3, { optional: true }),
      ],
      create: () => ({ id: `q${Date.now().toString(36).slice(-4)}`, kind: "choice", prompt: "Question", options: ["Yes", "No"] }),
    },
    create: () => ({
      type: "poll",
      id: `poll${Date.now().toString(36).slice(-4)}`,
      questions: [{ id: "q1", kind: "choice", prompt: "Question", options: ["Yes", "No"] }],
    }),
  },
  spacer: {
    label: "Spacer",
    fields: [en("size", "Size", SPACER_SIZES), flag("grow", "Push what follows to the bottom")],
    create: () => ({ type: "spacer", size: "md" }),
  },
  divider: {
    label: "Divider",
    fields: [markup("label", "Label", { optional: true }), en("tone", "Tone", TONES, { optional: true })],
    create: () => ({ type: "divider" }),
  },
  columns: {
    label: "Columns",
    fields: [
      en("ratio", "Ratio", RATIOS, {
        hint: "Wide left / right needs exactly two columns.",
      }),
      en("align", "Vertical alignment", VALIGNS),
      en("gap", "Gap", SIZES),
      flag("equal_height", "Equal height"),
      flag("equal_rows", "Equal row heights", {
        hint: "The blocks at the same place in each column share one height.",
      }),
    ],
    create: () => ({
      type: "columns",
      columns: [[{ type: "text", content: "Left column." }], [{ type: "text", content: "Right column." }]],
    }),
  },
  comparison: {
    label: "Comparison",
    fields: [
      group("left", "Left side", [
        markup("label", "Label"),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ]),
      group("right", "Right side", [
        markup("label", "Label"),
        en("tone", "Tone", TONES, { optional: true }),
        icon("icon", "Icon", { optional: true }),
      ]),
      flag("equal_height", "Equal height", { defaultOn: true }),
    ],
    create: () => ({
      type: "comparison",
      left: {
        label: "Before",
        blocks: [{ type: "text", content: "How it works today." }],
      },
      right: {
        label: "After",
        blocks: [{ type: "text", content: "How it works with the product." }],
      },
    }),
  },
};

/** The block types the palette offers, in catalog order. */
export const INSERTABLE_BLOCK_TYPES: DeckBlockType[] = (Object.keys(BLOCK_SCHEMA) as DeckBlockType[]).filter(
  (type) => BLOCK_SCHEMA[type].create !== undefined,
);

/** A copy of `block` with the container-only fields a nesting rule forbids
 *  dropped - what a block becomes when it is moved or pasted where its shape
 *  is not allowed. Only the type matters to the validator; this is the
 *  frontend's reading of `CONTAINER_BLOCK_TYPES`. */
export const CONTAINER_BLOCK_TYPES: readonly DeckBlockType[] = ["columns", "callout", "comparison"];

/** Whether `type` may be inserted into the list at `listPath` (the nesting rules of the spec). */
export function blockAllowedIn(type: DeckBlockType, listPath: string): boolean {
  // A callout's children and a comparison side hold ATOMIC blocks only; a
  // column (of a slide or of a columns block) holds leaf blocks (no columns).
  if (/\.children$|\.(left|right)\.blocks$/.test(listPath)) return !CONTAINER_BLOCK_TYPES.includes(type);
  if (/^columns\.\d+$|\.columns\.\d+$/.test(listPath)) return type !== "columns";
  return true;
}

/** How many blocks the run at `listPath` may hold (the `MAX_BLOCKS_PER_*` caps above). */
export function blockRunCapacity(listPath: string): number {
  if (listPath === "blocks") return MAX_BLOCKS_PER_SLIDE;
  if (/\.children$|\.(left|right)\.blocks$/.test(listPath)) return MAX_BLOCKS_PER_CONTAINER;
  return MAX_BLOCKS_PER_COLUMN;
}
