/**
 * Slide links INSIDE a deck (`[text](#/<slide id>)`): the tokenizer, the
 * strict reading the editor flags before a save, the runs model the in-place
 * editor writes back, and the id-only resolution - `yarn test:unit`.
 * The backend twin is `backend/tests/test_presentation_slide_links_in_deck.py`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { isLinkUrl, markupProblem, markupToPlainText, parseInlineMarkup } from "./markup.ts";
import {
  applyLink,
  applyStyle,
  linkAround,
  rangeLinkTarget,
  runsFromMarkup,
  runsToMarkup,
  type TextRun,
} from "./inline-runs.ts";
import { slideAnchorId, slideIndexer } from "./slide-link-context.ts";

test("a slide link is one segment carrying its label and the target slide id", () => {
  assert.deepEqual(parseInlineMarkup("See [Gateway](#/q4-gateway) now"), [
    { kind: "text", text: "See " },
    { kind: "link", text: "Gateway", target: "q4-gateway" },
    { kind: "text", text: " now" },
  ]);
  assert.equal(markupToPlainText("See [Gateway](#/q4-gateway) now"), "See Gateway now");
});

test("a web or e-mail link carries its address, between parentheses too", () => {
  assert.deepEqual(parseInlineMarkup("PDF ([issue 321](https://github.com/x/y/issues/321)), [us](mailto:team@filigran.io)"), [
    { kind: "text", text: "PDF (" },
    { kind: "link", text: "issue 321", target: "https://github.com/x/y/issues/321" },
    { kind: "text", text: "), " },
    { kind: "link", text: "us", target: "mailto:team@filigran.io" },
  ]);
  assert.equal(isLinkUrl("https://github.com/x/y/issues/321"), true);
  assert.equal(isLinkUrl("q4-gateway"), false);
});

test("a link to anywhere else stays literal text, as it always did", () => {
  for (const value of [
    "[site](http://filigran.io)",
    "[x](javascript:alert(1))",
    "[x](data:text/html,hi)",
    "[x](#top)",
    "[x](/presentations/abc)",
  ]) {
    assert.deepEqual(parseInlineMarkup(value), [{ kind: "text", text: value }]);
    assert.equal(markupProblem(value), null);
  }
});

test("the strict reading refuses what the backend refuses", () => {
  assert.equal(markupProblem("[Gateway](#/q4-gateway) and **bold**"), null);
  // No label, a ref that is no slide id: the link's marker is left over.
  assert.deepEqual(markupProblem("[](#/abc)"), { kind: "unbalanced", marker: "](#/" });
  assert.deepEqual(markupProblem("[x](#/has space)"), { kind: "unbalanced", marker: "](#/" });
  // Flat grammar: no markup inside a label, no link inside a span.
  assert.deepEqual(markupProblem("[**x**](#/abc)"), { kind: "nested", construct: "[text](link)" });
  assert.deepEqual(markupProblem("[**x**](https://x.io)"), { kind: "nested", construct: "[text](link)" });
  assert.deepEqual(markupProblem("**see [x](#/abc)**"), { kind: "nested", construct: "**bold**" });
});

test("a link round-trips through the runs model, next to every other construct", () => {
  for (const markup of [
    "[Gateway](#/q4-gateway)",
    "**Q4** - [Gateway (Big Move)](#/a1b2c3d4e5f6) and [[accent:x]]",
    "line one\n[next](#/x_1)",
    "[a](#/one)[b](#/two)",
    "Open: PDF ([issue 321](https://github.com/x/y/issues/321)) and [us](mailto:team@filigran.io)",
  ]) {
    assert.equal(runsToMarkup(runsFromMarkup(markup)), markup);
  }
});

test("linking a range writes the link; null takes it off", () => {
  const runs = runsFromMarkup("Go to Gateway now");
  const linked = applyLink(runs, 6, 13, "q4-gw");
  assert.equal(runsToMarkup(linked), "Go to [Gateway](#/q4-gw) now");
  assert.equal(rangeLinkTarget(linked, 6, 13), "q4-gw");
  assert.equal(rangeLinkTarget(linked, 0, 13), null);
  assert.equal(runsToMarkup(applyLink(linked, 6, 13, null)), "Go to Gateway now");
  // Relinking a part of a link splits it into two links.
  assert.equal(runsToMarkup(applyLink(linked, 6, 9, "other")), "Go to [Gat](#/other)[eway](#/q4-gw) now");
});

test("the caret anywhere in a label finds the whole link", () => {
  const runs = runsFromMarkup("Go to [Gateway](#/q4-gw) now");
  assert.deepEqual(linkAround(runs, 9), { start: 6, end: 13, target: "q4-gw" });
  assert.deepEqual(linkAround(runs, 13), { start: 6, end: 13, target: "q4-gw" });
  assert.equal(linkAround(runs, 2), null);
});

test("clearing the formatting takes a link off too", () => {
  const runs = runsFromMarkup("[Gateway](#/q4-gw)");
  assert.equal(runsToMarkup(applyStyle(runs, 0, 7, "plain")), "Gateway");
});

test("a link the grammar cannot carry is written as its plain label, never as refused markup", () => {
  const cases: TextRun[][] = [
    [{ text: "a ] b", style: "link", target: "x" }],
    [{ text: "label", style: "link", target: "not an id" }],
    [{ text: "label", style: "link", target: "javascript:alert(1)" }],
    [{ text: "label", style: "link", target: "http://x.io" }],
    [{ text: "label", style: "link" }],
    [
      { text: "a[", style: "plain" },
      { text: "label", style: "link", target: "x" },
    ],
  ];
  for (const runs of cases) {
    const markup = runsToMarkup(runs);
    assert.equal(markupProblem(markup), null, markup);
    assert.ok(!markup.includes("](#/"), markup);
  }
});

test("an in-deck link resolves by slide id only, never by position", () => {
  const indexOf = slideIndexer([{ id: "intro" }, { id: "19" }, {}, { id: "q4" }]);
  assert.equal(indexOf("q4"), 3);
  assert.equal(indexOf("19"), 1); // the slide whose id is 19, not "slide 19"
  assert.equal(indexOf("2"), null);
  assert.equal(indexOf("gone"), null);
  assert.equal(slideAnchorId(4), "deck-slide-4");
});
