import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { findDuplicateKeys } from "./json-duplicate-keys.mjs";

test("a key repeated in one object is reported with both lines", () => {
  const text = '{\n  "Resume": "Resume",\n  "Pause": "Pause",\n  "Resume": "Resume"\n}\n';
  assert.deepEqual(findDuplicateKeys(text), [{ key: "Resume", path: [], firstLine: 2, line: 4 }]);
});

test("a document without duplicates reports nothing", () => {
  assert.deepEqual(findDuplicateKeys('{"a": 1, "b": {"a": 2}, "c": [{"a": 3}, {"a": 4}]}'), []);
});

test("the same key in sibling or nested objects is not a duplicate, but inside each one it is", () => {
  const text = '{"list": [{"a": 1, "a": 2}, {"a": 3}], "obj": {"x": {"k": 1}, "y": {"k": 2, "k": 3}}}';
  assert.deepEqual(findDuplicateKeys(text), [
    { key: "a", path: ["list", 0], firstLine: 1, line: 1 },
    { key: "k", path: ["obj", "y"], firstLine: 1, line: 1 },
  ]);
});

test("values, escaped quotes and colons inside strings are never read as keys", () => {
  const text = '{"say \\"hi\\"": "a: \\"b\\", \\"c\\": d", "list": ["say \\"hi\\"", "say \\"hi\\""], "x": "say \\"hi\\""}';
  assert.deepEqual(findDuplicateKeys(text), []);
});

test("keys are compared decoded, as JSON.parse compares them", () => {
  assert.deepEqual(findDuplicateKeys('{"a": 1,\n"\\u0061": 2}'), [{ key: "a", path: [], firstLine: 1, line: 2 }]);
});

test("a key seen three times is reported against its first line each time", () => {
  assert.deepEqual(
    findDuplicateKeys('{"k": 1,\n"k": 2,\n"k": 3}').map(({ firstLine, line }) => [firstLine, line]),
    [
      [1, 2],
      [1, 3],
    ],
  );
});

test("no i18n catalog carries a duplicate key", () => {
  for (const locale of ["en", "fr", "es", "ja"]) {
    const text = readFileSync(new URL(`../../src/i18n/lang/${locale}.json`, import.meta.url), "utf8");
    assert.deepEqual(findDuplicateKeys(text), [], `${locale}.json has duplicate keys`);
  }
});
