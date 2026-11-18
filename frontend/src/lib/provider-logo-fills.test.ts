/**
 * Provider logos keep fixed fills - `yarn test:unit`.
 *
 * An SVG under `public/logos/` is drawn through an `<img>`, where a
 * `@media (prefers-color-scheme: ...)` rule reads the OS colour scheme, not
 * the XTM One theme (`html.dark` / `html.light`). A logo that swaps its fills
 * on that query vanishes whenever the OS and the app disagree (a dark mark on
 * the dark tile, a white mark on the light one). A logo that needs a
 * background carries its own tile; a pale logo is listed in the light-theme
 * rule of `src/index.css` (see `light-theme-logos.test.ts`).
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

const LOGO_DIR = new URL("../../public/logos/", import.meta.url);

test("no provider logo switches its fills on the OS colour scheme", () => {
  const offenders = readdirSync(LOGO_DIR)
    .filter((file) => file.endsWith(".svg"))
    .filter((file) => readFileSync(new URL(file, LOGO_DIR), "utf8").includes("prefers-color-scheme"));
  assert.deepEqual(offenders, []);
});
