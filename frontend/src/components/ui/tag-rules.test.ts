/**
 * Tag text rules: what gets stored, and what counts as already present.
 * Run with `yarn test:unit`; the field itself is tested by
 * `tests_e2e/dataset-label-vocabulary.spec.ts`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { containsTag, normalizeTag, platformSuggestions } from "./tag-rules.ts";

test("a free-form tag is folded, so one thing cannot become two tags", () => {
  assert.equal(normalizeTag("Malware"), "malware");
  assert.equal(normalizeTag("  MALWARE  "), "malware");
});

test("preserveCase keeps what was typed, so a label reads back as declared", () => {
  // Matching folds case; the vocabulary still shows what the author wrote.
  assert.equal(normalizeTag("T1003.001", true), "T1003.001");
  assert.equal(normalizeTag("  CVE-2021-44228  ", true), "CVE-2021-44228");
});

test("both modes still trim, and blank text yields no tag", () => {
  for (const preserveCase of [false, true]) {
    assert.equal(normalizeTag("   ", preserveCase), "");
    assert.equal(normalizeTag("", preserveCase), "");
  }
});

test("membership ignores case, so preserving it cannot admit a mistyped duplicate", () => {
  assert.equal(containsTag(["T1003.001"], "t1003.001"), true);
  assert.equal(containsTag(["malware"], "Malware"), true);
  assert.equal(containsTag(["T1003.001"], "T1059.001"), false);
  assert.equal(containsTag([], "anything"), false);
});

test("membership is exact beyond case — a label is not a prefix of another", () => {
  assert.equal(containsTag(["T1003"], "T1003.001"), false);
  assert.equal(containsTag(["T1003.001"], "T1003"), false);
});

// --- what the field offers, which is the half the view cannot be trusted to hide ---

const PLATFORM = ["t1003.001", "malware", "phishing"];

test("a vocabulary field offers nothing, whatever the cache holds", () => {
  // `enabled: false` stops the fetch, not the cache, so `allTags` arrives populated
  // when another tag field mounted recently. Any suggestion would let ArrowDown + Enter
  // store `t1003.001` in place of the typed `T1003.001`.
  assert.deepEqual(platformSuggestions(PLATFORM, [], "T1003", false), []);
  assert.deepEqual(platformSuggestions(PLATFORM, [], "", false), []);
});

test("a free-form tag field still offers them, matched case-insensitively", () => {
  assert.deepEqual(platformSuggestions(PLATFORM, [], "T1003", true), ["t1003.001"]);
  assert.deepEqual(platformSuggestions(PLATFORM, [], "MAL", true), ["malware"]);
});

test("an empty input offers the whole list as quick-picks", () => {
  assert.deepEqual(platformSuggestions(PLATFORM, [], "  ", true), PLATFORM);
});

test("what is already selected is not offered again, ignoring case", () => {
  assert.deepEqual(platformSuggestions(PLATFORM, ["Malware"], "", true), [
    "t1003.001",
    "phishing",
  ]);
});

test("the quick-pick list is bounded", () => {
  const many = Array.from({ length: 80 }, (_, i) => `tag-${i}`);
  assert.equal(platformSuggestions(many, [], "", true).length, 50);
});
