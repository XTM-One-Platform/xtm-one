/**
 * Unit tests for the Design editor's `deck_defaults` writer -
 * `yarn test:unit`.
 *
 * What is pinned: a write of one string key carries the per-cover objects a
 * brand declares in the JSON through untouched, and the key is dropped only
 * when nothing at all is left under it.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { readDeckDefaults, readDeckDefaultsRaw, withDeckDefault } from "./template-deck-defaults.ts";

const brand = {
  guidance: "Open on the cover.",
  deck_defaults: {
    theme: "dark",
    transition: "fade",
    hero_defaults: { title: "gradient", closing: "gradient" },
    hero_tones: { title: "accent" },
    slide_themes: { title: "dark", closing: "dark" },
  },
};

test("the selects read the string keys only, the raw read keeps every entry", () => {
  assert.deepEqual(readDeckDefaults(brand), { theme: "dark", transition: "fade" });
  assert.deepEqual(readDeckDefaultsRaw(brand), brand.deck_defaults);
  assert.notEqual(readDeckDefaultsRaw(brand), brand.deck_defaults); // a copy, never the stored object
  assert.deepEqual(readDeckDefaults({}), {});
  assert.deepEqual(readDeckDefaults({ deck_defaults: ["x"] }), {});
  assert.deepEqual(readDeckDefaultsRaw({ deck_defaults: "dark" }), {});
});

test("changing one deck default keeps the per-cover objects (the bug: a rebuilt object lost the covers)", () => {
  const next = withDeckDefault(brand, "transition", "slide");
  assert.deepEqual(next, {
    guidance: "Open on the cover.",
    deck_defaults: {
      theme: "dark",
      transition: "slide",
      hero_defaults: { title: "gradient", closing: "gradient" },
      hero_tones: { title: "accent" },
      slide_themes: { title: "dark", closing: "dark" },
    },
  });
  // The input is never mutated.
  assert.equal(brand.deck_defaults.transition, "fade");
  // A scheme-declaration snap of the default theme goes through the same door.
  assert.deepEqual(withDeckDefault(brand, "theme", "light").deck_defaults, { ...brand.deck_defaults, theme: "light" });
});

test("unsetting a key drops it, and `deck_defaults` goes only when nothing is left", () => {
  const withoutTransition = withDeckDefault(brand, "transition", null);
  assert.deepEqual(Object.keys(withoutTransition.deck_defaults as object).sort(), [
    "hero_defaults",
    "hero_tones",
    "slide_themes",
    "theme",
  ]);
  const plain = { guidance: "x", deck_defaults: { theme: "dark" } };
  assert.deepEqual(withDeckDefault(plain, "theme", null), { guidance: "x" });
  assert.deepEqual(withDeckDefault({ guidance: "x" }, "aspect_ratio", "4:3"), {
    guidance: "x",
    deck_defaults: { aspect_ratio: "4:3" },
  });
});
