import assert from "node:assert/strict";
import { test } from "node:test";

import { jsonFieldText, jsonFieldValue } from "./json-field.ts";

test("a JSON field stores the value its text holds", () => {
  // Stored as text, a Structured output schema never reached the agent.
  assert.deepEqual(jsonFieldValue('{"type": "object", "properties": {}}'), {
    type: "object",
    properties: {},
  });
  assert.deepEqual(jsonFieldValue('[{"type": "text"}]'), [{ type: "text" }]);
  assert.equal(jsonFieldValue("12"), 12);
});

test("text that is not JSON is stored as typed", () => {
  for (const text of ['{"type": ', '{"ids": {{n.fields.ids}}}', "{{n.fields}}", '"a string"', "", "  "]) {
    assert.equal(jsonFieldValue(text), text);
  }
});

test("a stored value shows as JSON text, and text as itself", () => {
  assert.equal(jsonFieldText({ a: 1 }), '{\n  "a": 1\n}');
  assert.equal(jsonFieldText('{"a": '), '{"a": ');
  assert.equal(jsonFieldText(undefined), "");
  assert.equal(jsonFieldText(null), "");
  assert.deepEqual(jsonFieldValue(jsonFieldText({ a: [1, "x"] })), { a: [1, "x"] });
});
