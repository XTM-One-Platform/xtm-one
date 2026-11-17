import assert from "node:assert/strict";
import test from "node:test";

import {
  EXTRACTED_PREVIEW_CHARS,
  renderExtractedPrediction,
} from "./extracted-prediction.ts";

test("JSON is pretty-printed, so a reader can scan what was scored", () => {
  const { text, hidden } = renderExtractedPrediction('["b","a"]');
  assert.equal(text, '[\n  "b",\n  "a"\n]');
  assert.equal(hidden, 0);
});

test("a value within the bound is shown whole", () => {
  const { text, hidden } = renderExtractedPrediction("plain text");
  assert.equal(text, "plain text");
  assert.equal(hidden, 0);
});

test("a long value is bounded, and says how much it withheld", () => {
  const labels = Array.from({ length: 2000 }, (_, i) => "x".repeat(200) + i);
  const { text, hidden } = renderExtractedPrediction(JSON.stringify(labels));
  assert.equal(text.length, EXTRACTED_PREVIEW_CHARS);
  // The point of the bound: the whole value is far larger than what is rendered.
  assert.ok(hidden > 400_000, `expected a large remainder, got ${hidden}`);
});

test("the bound counts the PRETTY-PRINTED length, not the stored one", () => {
  // Indentation can push a value that fits as stored past the bound once expanded,
  // which is the length that actually reaches the DOM.
  const compact = JSON.stringify(Array.from({ length: 300 }, (_, i) => `label-${i}`));
  assert.ok(compact.length < EXTRACTED_PREVIEW_CHARS);
  const { hidden } = renderExtractedPrediction(compact);
  assert.ok(hidden > 0, "a value that only fits while compact must still be bounded");
});

test("non-JSON is passed through rather than reported as an error", () => {
  const { text } = renderExtractedPrediction("not json at all");
  assert.equal(text, "not json at all");
});

test("a JSON scalar does not render as undefined", () => {
  assert.equal(renderExtractedPrediction("42").text, "42");
  assert.equal(renderExtractedPrediction('"a"').text, '"a"');
});

test("the bound never cuts a surrogate pair in half", () => {
  // A lone high surrogate is not well-formed UTF-16: encodeURIComponent throws on
  // it and JSON.stringify re-emits it escaped.
  const value = "a".repeat(9) + "\u{1F600}" + "b".repeat(5);
  const { text } = renderExtractedPrediction(value, 10);
  assert.equal(text, "a".repeat(9));
  assert.doesNotThrow(() => encodeURIComponent(text));
});

test("a pair that fits is kept whole", () => {
  const { text } = renderExtractedPrediction("a".repeat(8) + "\u{1F600}", 10);
  assert.equal(text, "a".repeat(8) + "\u{1F600}");
});

test("an unpaired surrogate already in the value is left alone", () => {
  // Not this function's to repair: the job is not to CREATE one.
  const { text } = renderExtractedPrediction("ab\ud83dcd", 10);
  assert.equal(text, "ab\ud83dcd");
});
