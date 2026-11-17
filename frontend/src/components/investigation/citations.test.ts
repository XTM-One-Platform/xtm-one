/**
 * Where a citation marker lands in the answer - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { HOSTILE_URLS } from "../../testing/hostile-urls.ts";
import { safeHref, utf16Index, weaveCitations } from "./citations.ts";

test("a code-point offset past an emoji lands after the same character", () => {
  const answer = "Seen 🔥 in the wild. More.";
  // The backend counts code points: "Seen 🔥 in the wild." is 19 of them.
  assert.equal(utf16Index(answer, 19), "Seen 🔥 in the wild.".length);
  const woven = weaveCitations(answer, [{ n: 1, end: 19 } as never]);
  assert.equal(woven, "Seen 🔥 in the wild.[1] More.");
});

test("a linked citation weaves outside a browser, and an unsafe scheme is not linked", () => {
  const answer = "Seen in the wild. More.";
  const woven = weaveCitations(answer, [
    { n: 1, end: 17, href: "https://example.com/a" } as never,
    { n: 2, end: 17, href: "javascript:alert(1)" } as never,
  ]);
  assert.ok(woven.startsWith("Seen in the wild.[[1]]("));
  assert.ok(woven.includes("[2]"));
  assert.ok(!woven.includes("javascript:"));
});

test("an offset past the end is clamped to the end", () => {
  assert.equal(utf16Index("abc", 10), 3);
  assert.equal(utf16Index("", 2), 0);
});

test("a citation links a web page or a mail address, nothing else", () => {
  assert.equal(safeHref("https://example.com/a"), "https://example.com/a");
  assert.equal(safeHref("mailto:soc@example.com"), "mailto:soc@example.com");
  for (const [label, value] of HOSTILE_URLS) {
    assert.equal(safeHref(value), undefined, label);
  }
});
