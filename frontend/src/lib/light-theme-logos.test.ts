/**
 * The provider logos darkened on the light theme - `yarn test:unit`.
 *
 * `src/index.css` lists, by file, the logos under `public/logos/` drawn white
 * or pale on a transparent background, and inverts them under the `light:`
 * variant. An entry is matched on the end of an `<img>`'s `src`, so one whose
 * file was renamed or removed matches nothing and fails silently: the logo
 * goes back to vanishing on the light theme. These tests hold the list to the
 * files. What the files LOOK like (light as drawn, legible once treated, no
 * unlisted white mark) is measured on real pixels by
 * `tests_e2e/provider-logo-themes.spec.ts`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";

const INDEX_CSS = new URL("../index.css", import.meta.url);
const LOGO_DIR = new URL("../../public/logos/", import.meta.url);

/** The one rule: `img:is(<entries>) { @apply light:invert light:hue-rotate-180; }`. */
const RULE = /img:is\(([^)]*)\)\s*\{\s*@apply light:invert light:hue-rotate-180;\s*\}/g;
const ENTRY = /^\[src\$="\/logos\/([a-z0-9_]+\.(?:svg|png))"\]$/;

function flaggedLogos(): string[] {
  const css = readFileSync(INDEX_CSS, "utf8");
  const rules = [...css.matchAll(RULE)];
  assert.equal(rules.length, 1, "index.css holds exactly one light-theme logo rule");
  return rules[0][1]
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const match = ENTRY.exec(entry);
      assert.ok(match, `not a logo file entry: ${entry}`);
      return match[1];
    });
}

test("every listed logo is a file under public/logos", () => {
  const missing = flaggedLogos().filter((file) => !existsSync(new URL(file, LOGO_DIR)));
  assert.deepEqual(missing, []);
});

test("the list is sorted and names each logo once", () => {
  const files = flaggedLogos();
  assert.ok(files.length > 0);
  assert.deepEqual(files, [...new Set(files)].sort());
});

test("no other rule in index.css keys on a logo file", () => {
  // A second list would split the one place this is decided.
  const css = readFileSync(INDEX_CSS, "utf8");
  assert.equal((css.match(/\[src[$^*~|]?="[^"]*\/logos\//g) ?? []).length, flaggedLogos().length);
});
