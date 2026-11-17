/**
 * Unit tests for the template theme value guard - `yarn test:unit`.
 *
 * `isSafeThemeValue` is what the template preview filters every palette value
 * through before it reaches an inline CSS variable, so a template can never make
 * the browser of whoever previews it load an outside URL. The cases are the ONE
 * table the backend guard (`theme_value_problem`) is checked against too.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { isSafeThemeValue, MAX_THEME_VALUE_CHARS, THEME_FILE_ID_KEYS, trimThemeValue } from "./theme-value.ts";

interface ThemeValueCases {
  fetching: string[];
  structural: string[];
  accepted: string[];
  trim: string[];
  file_id: { accepted: string[]; refused: string[] };
}

const cases = JSON.parse(
  readFileSync(new URL("../../../tests_e2e/fixtures/theme-value-cases.json", import.meta.url), "utf8"),
) as ThemeValueCases;

/** A sample of CSS keys, deck and report alike: the rule is the same for every one. */
const CSS_KEYS = ["background", "accent", "surface", "heading_font", "gradient_from", "chart_1", "paper", "critical"];

test("every spelling of a fetching CSS function is refused for every key", () => {
  for (const value of cases.fetching) {
    for (const key of CSS_KEYS) assert.equal(isSafeThemeValue(key, value), false, `${key}: ${value}`);
  }
});

test("the structural refusals are kept", () => {
  for (const value of [...cases.structural, "x".repeat(MAX_THEME_VALUE_CHARS + 1)]) {
    for (const key of CSS_KEYS) assert.equal(isSafeThemeValue(key, value), false, `${key}: ${JSON.stringify(value)}`);
  }
});

test("every legitimate colour, gradient and font stack is kept, surrounding spaces aside", () => {
  for (const value of cases.accepted) {
    for (const key of CSS_KEYS) assert.equal(isSafeThemeValue(key, `  ${value} `), true, `${key}: ${value}`);
  }
});

test("a value at the length limit is kept, the limit read on the trimmed value", () => {
  assert.equal(isSafeThemeValue("background", "x".repeat(MAX_THEME_VALUE_CHARS)), true);
  assert.equal(isSafeThemeValue("background", `  ${"x".repeat(MAX_THEME_VALUE_CHARS)}\n`), true);
});

test("both sides trim the same characters, and the preview serves the trimmed value", () => {
  const trimmed = new Set(cases.trim);
  // Every character JavaScript's own trim() strips is in the shared set (the
  // backend test pins the set to Python's str.strip() plus U+FEFF).
  for (let code = 0; code <= 0xffff; code += 1) {
    const char = String.fromCharCode(code);
    const label = `U+${code.toString(16).padStart(4, "0")}`;
    if (/^\s$/.test(char)) assert.ok(trimmed.has(char), `${label} is JavaScript whitespace`);
    // ... and nothing outside the set is trimmed: it stays and is judged.
    if (!trimmed.has(char)) assert.equal(trimThemeValue(`${char}x${char}`), `${char}x${char}`, label);
  }
  for (const char of cases.trim) {
    const padded = `${char}#0b1020${char}`;
    assert.equal(trimThemeValue(padded), "#0b1020", JSON.stringify(char));
    assert.equal(isSafeThemeValue("background", padded), true, JSON.stringify(char));
    if (char !== " ") assert.equal(isSafeThemeValue("background", `red${char}blue`), false, JSON.stringify(char));
  }
});

test("a file-id key takes a canonical UUID and nothing else", () => {
  for (const key of THEME_FILE_ID_KEYS) {
    for (const value of cases.file_id.accepted) {
      assert.equal(isSafeThemeValue(key, value), true, `${key}: ${value}`);
      assert.equal(isSafeThemeValue(key, ` ${value}\n`), true, `${key}: ${value}`);
    }
    for (const value of cases.file_id.refused) assert.equal(isSafeThemeValue(key, value), false, `${key}: ${value}`);
  }
});

test("a value that is not a non-empty string is refused", () => {
  for (const value of [undefined, null, 12, {}, ["#fff"], "", "   ", "\ufeff"]) {
    assert.equal(isSafeThemeValue("background", value), false, JSON.stringify(value));
  }
});
