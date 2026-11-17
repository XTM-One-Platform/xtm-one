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
 * The resolved look of a `quote` block, apart from the component so it
 * can be unit-tested and so the renderer file exports components only.
 *
 * A quote names a `variant`; every look field it leaves out follows that
 * variant through `QUOTE_VARIANT_DEFAULTS`, the table `presentation_spec.py`
 * holds under the same name (pinned by `tests/test_presentation_quote.py`) -
 * so a card switched to a statement becomes a centred bold statement rather
 * than a statement that kept the card's size and italics.
 */
import type {
  DeckBlock,
  DeckPictureLabel,
  DeckQuoteBlock,
  DeckQuoteMarks,
  DeckQuoteSize,
  DeckQuoteVariant,
  DeckVAlign,
} from "@/types";

interface QuoteLook {
  variant: DeckQuoteVariant;
  size: DeckQuoteSize;
  align: "start" | "center";
  italic: boolean;
  quote_marks: DeckQuoteMarks;
}

/** What an omitted look field reads as, per variant; `card` is the historical rendering.
 */
const QUOTE_VARIANT_DEFAULTS: Record<DeckQuoteVariant, Omit<QuoteLook, "variant">> = {
  card: { size: "md", align: "start", italic: true, quote_marks: "none" },
  statement: { size: "xl", align: "center", italic: false, quote_marks: "decorative" },
  pull: { size: "lg", align: "start", italic: false, quote_marks: "none" },
  testimonial: { size: "md", align: "start", italic: false, quote_marks: "none" },
};

/** A `hero` quote steps down one size rather than wrap past this (`HERO_QUOTE_MAX_LINES`). */
export const HERO_QUOTE_MAX_LINES = 3;

export function resolveQuoteLook(block: DeckQuoteBlock): QuoteLook {
  const known = !!block.variant && Object.prototype.hasOwnProperty.call(QUOTE_VARIANT_DEFAULTS, block.variant);
  const variant = known && block.variant ? block.variant : "card";
  const defaults = QUOTE_VARIANT_DEFAULTS[variant];
  return {
    variant,
    size: block.size ?? defaults.size,
    align: block.align ?? defaults.align,
    italic: block.italic ?? defaults.italic,
    quote_marks: block.quote_marks ?? defaults.quote_marks,
  };
}

const QUOTE_LOOK_FIELDS = ["size", "align", "italic", "quote_marks"] as const;

/**
 * What the look field `key` of `block` reads as while it is left out - its
 * variant's default - or `undefined` for a key the variant does not decide.
 * The inspector names it in the picker's unset option ("Auto (On)" on a card's
 * italics), so leaving a look to the variant never hides what the slide draws.
 */
export function quoteLookDefault(block: DeckQuoteBlock, key: string): string | boolean | undefined {
  const field = QUOTE_LOOK_FIELDS.find((name) => name === key);
  return field ? QUOTE_VARIANT_DEFAULTS[resolveQuoteLook(block).variant][field] : undefined;
}

/** The two pictures of a quote: the portrait before the credit, the company logo after it. */
type QuotePicture = "photo_file_id" | "logo_file_id";

/**
 * Whether the visible credit already names `picture`: the author the
 * portrait, the organization the logo, the legacy one-line attribution both.
 */
export function quoteCreditNamesPicture(block: DeckQuoteBlock, picture: QuotePicture): boolean {
  if (block.attribution?.trim()) return true;
  return !!(picture === "photo_file_id" ? block.author : block.organization)?.trim();
}

/** A readable name from a filename (`contoso_logo-dark.svg` -> `contoso logo dark`), or `null`. */
export function nameFromFilename(filename: string | null | undefined): string | null {
  if (!filename) return null;
  const words = filename
    .replace(/\.[a-z0-9]{1,5}$/i, "")
    .replace(/[_\-.]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return /\p{L}/u.test(words) ? words : null;
}

/**
 * The alt text of a quote picture. Empty when the visible credit names
 * it - the name is read beside it, and a second announcement is noise (WCAG's
 * redundant image). Otherwise never empty: the file's description, else a name
 * read off its filename (`picture_labels` of the deck response), else `generic`.
 */
export function quotePictureAlt(
  block: DeckQuoteBlock,
  picture: QuotePicture,
  label: DeckPictureLabel | null,
  generic: string,
): string {
  if (quoteCreditNamesPicture(block, picture)) return "";
  return label?.description?.trim() || nameFromFilename(label?.filename) || generic;
}

/**
 * The `data-*` attributes of the quote's root: one per look field whose value
 * is not the CARD's. A quote that is the historical card therefore carries
 * none and renders the DOM it always did, attribute for attribute, and the
 * stylesheet writes every other look as an override of the card's rules.
 */
export function quoteLookAttributes(look: QuoteLook): Record<string, string | undefined> {
  const card = QUOTE_VARIANT_DEFAULTS.card;
  return {
    "data-variant": look.variant !== "card" ? look.variant : undefined,
    "data-size": look.size !== card.size ? look.size : undefined,
    "data-align": look.align !== card.align ? look.align : undefined,
    "data-italic": look.italic !== card.italic ? String(look.italic) : undefined,
    "data-marks": look.quote_marks !== card.quote_marks ? look.quote_marks : undefined,
  };
}

/**
 * Whether the quote draws its accent as a short rule ABOVE the text:
 * a `pull` quote's accent is the bar on its left edge, which reads as a
 * stray mark beside text that is centred, so a centred pull quote drops the
 * bar for a horizontal rule centred over the text - a real element
 * (`.deck-quote-rule`, aria-hidden), never a pseudo-element, so the PPTX
 * export draws it as a shape where the slide painted it.
 */
export function centredPullRule(look: QuoteLook): boolean {
  return look.variant === "pull" && look.align === "center";
}

/** The split credit, in the order it is read (`QUOTE_CREDIT_FIELDS`). */
const QUOTE_CREDIT_FIELDS = ["author", "role", "organization"] as const;
export type QuoteCreditField = (typeof QUOTE_CREDIT_FIELDS)[number];

/** How a quote is credited: the legacy one-line `attribution`, or the split parts that are set. */
export type QuoteCredit =
  | { kind: "attribution"; value: string }
  | { kind: "parts"; parts: { field: QuoteCreditField; value: string }[] };

export function quoteCredit(block: DeckQuoteBlock): QuoteCredit | null {
  if (block.attribution?.trim()) return { kind: "attribution", value: block.attribution };
  const parts = QUOTE_CREDIT_FIELDS.flatMap((field) => {
    const value = block[field];
    return value?.trim() ? [{ field, value }] : [];
  });
  return parts.length ? { kind: "parts", parts } : null;
}

/**
 * The card grids a body can consist of alone: a slide whose body is ONE of
 * them (and no `fill`, which takes the room instead) is centred by default.
 */
const CENTRED_CARD_GRIDS: readonly DeckBlock["type"][] = [
  "profiles",
  "personas",
  "numbered_cards",
  "feature_list",
  "metrics_grid",
  "pricing_tiers",
  "logo_grid",
];

/** A run of exactly one card grid that does not fill the body. */
export function isLoneCardGrid(blocks: readonly DeckBlock[] | undefined): boolean {
  if (blocks?.length !== 1) return false;
  const only = blocks[0];
  if (!CENTRED_CARD_GRIDS.includes(only.type)) return false;
  return !("fill" in only && only.fill === true);
}

/**
 * Where a content slide's single-flow run sits: a body whose ONLY
 * block is a `statement` quote is centred vertically - it is the slide, and a
 * statement pinned under the title with the floor bare is never the intent -
 * unless the slide pins it to the bottom (`end`). A body that is one card
 * grid is centred the same way when the slide leaves the placement at its
 * default (`start` or unset): the grid between the title and the footer, not
 * packed under the title over a bare floor. Anything else keeps the slide's
 * own `vertical_align`.
 */
export function bodyVerticalAlign(blocks: readonly DeckBlock[] | undefined, verticalAlign: DeckVAlign | undefined): DeckVAlign {
  if (isLoneStatement(blocks) && verticalAlign !== "end") return "center";
  if (isLoneCardGrid(blocks) && (verticalAlign === undefined || verticalAlign === "start")) return "center";
  return verticalAlign ?? "start";
}

/** A run of exactly one block, a `statement` quote. */
export function isLoneStatement(blocks: readonly DeckBlock[] | undefined): boolean {
  if (blocks?.length !== 1) return false;
  const only = blocks[0];
  return only.type === "quote" && resolveQuoteLook(only).variant === "statement";
}

/** The pieces of the DOM `fitHeroQuote` reads - an `HTMLElement` in the browser, a fake in the tests. */
interface QuoteFitTarget {
  dataset: Record<string, string | undefined>;
  querySelector(selector: string): { clientHeight: number } | null;
}

/**
 * A `hero` quote is the title-slide headline size; one that would wrap past
 * `HERO_QUOTE_MAX_LINES` there steps down ONE step of the type scale
 * (`data-quote-fit="step"`, the stylesheet's h1 size) and keeps that size
 * however many lines it then takes - the layout check reports a statement
 * past four. Measured after every layout from the hero size (the attribute is
 * dropped first), so a quote given more room steps back up. `clientHeight` and
 * the line height are layout metrics, untouched by the canvas transform.
 */
export function fitHeroQuote(figure: QuoteFitTarget, lineHeightOf: (text: object) => number): void {
  delete figure.dataset.quoteFit;
  if (figure.dataset.size !== "hero") return;
  const text = figure.querySelector(":scope > blockquote");
  if (!text) return;
  const lineHeight = lineHeightOf(text);
  if (!(lineHeight > 0)) return;
  if (Math.round(text.clientHeight / lineHeight) > HERO_QUOTE_MAX_LINES) figure.dataset.quoteFit = "step";
}
