/**
 * Unit tests for the `quote` block's resolved look.
 *
 * Run with `yarn test:unit` - Node's built-in runner. What is pinned here: an
 * omitted look field follows the variant, a default card carries no attribute
 * at all (the DOM it always rendered), the credit reads one way, a lone
 * statement is centred, and a hero steps down once past three lines.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { DeckBlock, DeckQuoteBlock } from "@/types";

import {
  HERO_QUOTE_MAX_LINES,
  bodyVerticalAlign,
  centredPullRule,
  fitHeroQuote,
  nameFromFilename,
  quoteCredit,
  quoteCreditNamesPicture,
  quoteLookAttributes,
  quoteLookDefault,
  quotePictureAlt,
  resolveQuoteLook,
} from "./quote-style.ts";

const quote = (fields: Partial<DeckQuoteBlock> = {}): DeckQuoteBlock => ({ type: "quote", content: "Words.", ...fields });

test("a quote with no look fields is the historical card", () => {
  assert.deepEqual(resolveQuoteLook(quote()), {
    variant: "card",
    size: "md",
    align: "start",
    italic: true,
    quote_marks: "none",
  });
});

test("omitted look fields follow the variant, explicit ones win", () => {
  assert.deepEqual(resolveQuoteLook(quote({ variant: "statement" })), {
    variant: "statement",
    size: "xl",
    align: "center",
    italic: false,
    quote_marks: "decorative",
  });
  assert.equal(resolveQuoteLook(quote({ variant: "pull" })).size, "lg");
  assert.equal(resolveQuoteLook(quote({ variant: "testimonial" })).italic, false);
  const explicit = resolveQuoteLook(quote({ variant: "statement", size: "hero", align: "start", italic: true, quote_marks: "none" }));
  assert.deepEqual(explicit, { variant: "statement", size: "hero", align: "start", italic: true, quote_marks: "none" });
});

test("only a centred pull quote trades its side bar for the rule over the text", () => {
  assert.equal(centredPullRule(resolveQuoteLook(quote({ variant: "pull", align: "center" }))), true);
  // The pull's own default is start: the bar stays.
  assert.equal(centredPullRule(resolveQuoteLook(quote({ variant: "pull" }))), false);
  // A statement is centred by default and has no bar to trade; a card centred keeps its card.
  assert.equal(centredPullRule(resolveQuoteLook(quote({ variant: "statement" }))), false);
  assert.equal(centredPullRule(resolveQuoteLook(quote({ align: "center" }))), false);
  assert.equal(centredPullRule(resolveQuoteLook(quote({ variant: "testimonial", align: "center" }))), false);
});

test("an unknown variant from a newer backend reads as the card", () => {
  assert.equal(resolveQuoteLook(quote({ variant: "poster" as DeckQuoteBlock["variant"] })).variant, "card");
  // Only the table's own keys are variants, never what every object inherits.
  assert.equal(resolveQuoteLook(quote({ variant: "constructor" as DeckQuoteBlock["variant"] })).variant, "card");
});

test("the default card carries no look attribute; every other look names what differs", () => {
  const card = quoteLookAttributes(resolveQuoteLook(quote()));
  assert.deepEqual(Object.values(card).filter((value) => value !== undefined), []);
  assert.deepEqual(quoteLookAttributes(resolveQuoteLook(quote({ variant: "statement" }))), {
    "data-variant": "statement",
    "data-size": "xl",
    "data-align": "center",
    "data-italic": "false",
    "data-marks": "decorative",
  });
  // A card that only drops its italics says only that.
  assert.deepEqual(quoteLookAttributes(resolveQuoteLook(quote({ italic: false }))), {
    "data-variant": undefined,
    "data-size": undefined,
    "data-align": undefined,
    "data-italic": "false",
    "data-marks": undefined,
  });
});

test("the inspector names what a look left to the variant reads as, for the variant picked", () => {
  assert.equal(quoteLookDefault(quote(), "italic"), true);
  assert.equal(quoteLookDefault(quote({ variant: "statement" }), "italic"), false);
  assert.equal(quoteLookDefault(quote({ variant: "statement" }), "size"), "xl");
  assert.equal(quoteLookDefault(quote({ variant: "pull" }), "align"), "start");
  assert.equal(quoteLookDefault(quote({ variant: "statement" }), "quote_marks"), "decorative");
  // An explicit value is the block's own: the default named is still the variant's.
  assert.equal(quoteLookDefault(quote({ variant: "statement", size: "hero" }), "size"), "xl");
  for (const key of ["variant", "content", "author", "figures", "constructor"]) {
    assert.equal(quoteLookDefault(quote(), key), undefined, key);
  }
});

test("a picture the visible credit names is silent; any other is named, never empty", () => {
  const described = { description: "Contoso Financial logo", filename: "contoso.svg" };
  const unnamedFile = { description: null, filename: "contoso_financial-logo.svg" };
  const pictured = { photo_file_id: "p", logo_file_id: "l" };
  // Named by the credit beside it: an empty alt, whatever the file says.
  assert.equal(quotePictureAlt(quote({ ...pictured, author: "A" }), "photo_file_id", described, "Portrait"), "");
  assert.equal(quotePictureAlt(quote({ ...pictured, organization: "Acme" }), "logo_file_id", described, "Logo"), "");
  assert.equal(quotePictureAlt(quote({ ...pictured, attribution: "A, Acme" }), "logo_file_id", described, "Logo"), "");
  assert.equal(quotePictureAlt(quote({ ...pictured, attribution: "A, Acme" }), "photo_file_id", null, "Portrait"), "");
  // "Name, Role" beside a logo: the author does not name the company.
  const nameRole = quote({ ...pictured, author: "Dana Whitlock", role: "VP" });
  assert.equal(quotePictureAlt(nameRole, "logo_file_id", described, "Logo"), "Contoso Financial logo");
  assert.equal(quotePictureAlt(nameRole, "logo_file_id", unnamedFile, "Logo"), "contoso financial logo");
  assert.equal(quotePictureAlt(nameRole, "logo_file_id", { description: "  ", filename: "123.png" }, "Logo"), "Logo");
  assert.equal(quotePictureAlt(nameRole, "logo_file_id", null, "Logo"), "Logo");
  // No author: the portrait is named by its file, or generically.
  assert.equal(quotePictureAlt(quote({ ...pictured, role: "CISO" }), "photo_file_id", null, "Portrait"), "Portrait");
  assert.equal(quoteCreditNamesPicture(quote({ author: "  " }), "photo_file_id"), false);
});

test("a filename reads as words, or not at all", () => {
  assert.equal(nameFromFilename("contoso_logo-dark.svg"), "contoso logo dark");
  assert.equal(nameFromFilename("Dana Whitlock.JPEG"), "Dana Whitlock");
  assert.equal(nameFromFilename("2026-09-26.png"), null);
  assert.equal(nameFromFilename(""), null);
  assert.equal(nameFromFilename(null), null);
});

test("the credit is the legacy attribution, or the split parts that are set, in reading order", () => {
  assert.equal(quoteCredit(quote()), null);
  assert.deepEqual(quoteCredit(quote({ attribution: "Bruce Schneier" })), { kind: "attribution", value: "Bruce Schneier" });
  assert.deepEqual(quoteCredit(quote({ organization: "Contoso", author: "Dana Whitlock" })), {
    kind: "parts",
    parts: [
      { field: "author", value: "Dana Whitlock" },
      { field: "organization", value: "Contoso" },
    ],
  });
  assert.equal(quoteCredit(quote({ author: "  " })), null);
});

test("a content body that is one statement quote is centred unless pinned to the bottom", () => {
  const statement: DeckBlock[] = [quote({ variant: "statement" })];
  assert.equal(bodyVerticalAlign(statement, "start"), "center");
  assert.equal(bodyVerticalAlign(statement, undefined), "center");
  assert.equal(bodyVerticalAlign(statement, "stretch"), "center");
  assert.equal(bodyVerticalAlign(statement, "end"), "end");
  // A card, or a statement with company, keeps the slide's own placement.
  assert.equal(bodyVerticalAlign([quote()], "start"), "start");
  assert.equal(bodyVerticalAlign([quote({ variant: "statement" }), { type: "text", content: "x" }], undefined), "start");
});

test("a content body that is one card grid is centred unless the slide placed it", () => {
  const grid = [{ type: "numbered_cards", items: [] }] as unknown as DeckBlock[];
  assert.equal(bodyVerticalAlign(grid, undefined), "center");
  assert.equal(bodyVerticalAlign(grid, "start"), "center");
  assert.equal(bodyVerticalAlign(grid, "end"), "end");
  assert.equal(bodyVerticalAlign(grid, "center"), "center");
  // A grid that fills the body takes the room; two blocks keep the slide's placement.
  const filled = [{ type: "feature_list", items: [], fill: true }] as unknown as DeckBlock[];
  assert.equal(bodyVerticalAlign(filled, undefined), "start");
  assert.equal(bodyVerticalAlign([...grid, { type: "text", content: "x" }], undefined), "start");
});

/** A hero quote whose text height depends on the step the pass applied. */
function fakeFigure(size: string | undefined, linesAtHero: number, linesAtStep: number) {
  const dataset: Record<string, string | undefined> = size ? { size } : {};
  const lineHeight = () => (dataset.quoteFit === "step" ? 92 : 118);
  const text = {
    get clientHeight() {
      return (dataset.quoteFit === "step" ? linesAtStep : linesAtHero) * lineHeight();
    },
  };
  return {
    dataset,
    querySelector(selector: string) {
      assert.equal(selector, ":scope > blockquote");
      return text;
    },
    lineHeightOf: () => lineHeight(),
  };
}

test("a hero quote that fits in three lines keeps the headline size", () => {
  const figure = fakeFigure("hero", HERO_QUOTE_MAX_LINES, 2);
  fitHeroQuote(figure, figure.lineHeightOf);
  assert.equal(figure.dataset.quoteFit, undefined);
});

test("a hero quote past three lines steps down once, and back up when it fits again", () => {
  const figure = fakeFigure("hero", 4, 3);
  fitHeroQuote(figure, figure.lineHeightOf);
  assert.equal(figure.dataset.quoteFit, "step");
  // Still stepped on a second pass (measured from the hero size every time).
  fitHeroQuote(figure, figure.lineHeightOf);
  assert.equal(figure.dataset.quoteFit, "step");
  const wider = fakeFigure("hero", 3, 2);
  wider.dataset.quoteFit = "step";
  fitHeroQuote(wider, wider.lineHeightOf);
  assert.equal(wider.dataset.quoteFit, undefined);
});

test("only a hero quote is ever stepped", () => {
  const figure = fakeFigure("xl", 6, 5);
  fitHeroQuote(figure, figure.lineHeightOf);
  assert.equal(figure.dataset.quoteFit, undefined);
});
