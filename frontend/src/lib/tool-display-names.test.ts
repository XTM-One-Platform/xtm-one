/**
 * Chat tool status labels read in the person's language, in one style - `yarn test:unit`.
 *
 * `toolDisplayName` resolves a tool's label through the catalogs: every label of
 * `TOOL_DISPLAY_NAMES` must be a key of en, fr, es and ja, or a French user sees
 * an English status line and an English Reasoning details row. The labels share
 * one article style: no indefinite article ("Deleting video", never
 * "Deleting a video").
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { toolDisplayName } from "./constants";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const CONSTANTS = read("./constants.ts");
const LOCALES = ["en", "fr", "es", "ja"] as const;
const CATALOGS = Object.fromEntries(
  LOCALES.map((locale) => [locale, JSON.parse(read(`../i18n/lang/${locale}.json`)) as Record<string, string>]),
);

function labels(): Record<string, string> {
  const block = CONSTANTS.match(/const TOOL_DISPLAY_NAMES = \{([\s\S]*?)\n\} as const/)?.[1];
  assert.ok(block, "TOOL_DISPLAY_NAMES not found in constants.ts");
  return Object.fromEntries([...block.matchAll(/^\s*([a-z0-9_]+)\s*:\s*"([^"]+)"/gm)].map((m) => [m[1], m[2]]));
}

test("every tool label is a key of every catalog", () => {
  const all = labels();
  assert.ok(Object.keys(all).length > 150, "the label table was read");
  for (const [tool, label] of Object.entries(all)) {
    for (const locale of LOCALES) {
      assert.ok(Object.prototype.hasOwnProperty.call(CATALOGS[locale], label), `${tool}: "${label}" is not in ${locale}.json`);
    }
  }
});

test("no tool label carries an indefinite article", () => {
  for (const [tool, label] of Object.entries(labels())) {
    assert.doesNotMatch(label, /\b(a|an)\b/i, `${tool}: "${label}"`);
  }
});

test("a known tool is translated, an unknown tool keeps its spaced name", () => {
  const fr = (id: string) => CATALOGS.fr[id] ?? id;
  assert.equal(toolDisplayName("delete_video", fr), CATALOGS.fr["Deleting video"]);
  assert.notEqual(toolDisplayName("delete_video", fr), "Deleting video");
  assert.equal(toolDisplayName("search_notion_pages", fr), "Search notion pages");
  assert.equal(toolDisplayName("toString", fr), "ToString");
});
