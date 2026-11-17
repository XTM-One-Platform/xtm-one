import { test } from "node:test";
import assert from "node:assert/strict";

import { fieldHeightClasses } from "./field-height-classes.ts";

// The combobox field of SearchableSelect gets the page's height, so a picker
// renders the same height in its plain and its searchable mode.

test("an h-8 picker gives its field the height and the matching floor", () => {
  assert.equal(fieldHeightClasses("h-8 w-[200px] content-compact"), "h-8 min-h-8");
});

test("width and typography stay on the root only", () => {
  assert.equal(fieldHeightClasses("w-full content-compact"), undefined);
});

test("no class list keeps the library's default height", () => {
  assert.equal(fieldHeightClasses(undefined), undefined);
  assert.equal(fieldHeightClasses(""), undefined);
});

test("prefixes and the important modifier carry over to the floor", () => {
  assert.equal(fieldHeightClasses("md:h-10 !h-7"), "md:h-10 md:min-h-10 !h-7 !min-h-7");
});

test("an explicit min or max height is kept as given, never doubled", () => {
  assert.equal(fieldHeightClasses("min-h-12 max-h-40"), "min-h-12 max-h-40");
});

test("an arbitrary height keeps its value", () => {
  assert.equal(fieldHeightClasses("h-[30px]"), "h-[30px] min-h-[30px]");
});

test("a class that only starts like a height is left alone", () => {
  assert.equal(fieldHeightClasses("hover:bg-x hidden"), undefined);
});
