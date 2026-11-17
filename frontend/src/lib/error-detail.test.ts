/**
 * Unit tests for what an API error's `detail` says - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { errorDetailMessage, validationMessages } from "./error-detail.ts";

test("a body model's refusal says what it refused, not its status", () => {
  const detail = [
    { loc: ["body", "available_models", 1], msg: "Value error, not an OpenRouter model id (author/model)", type: "value_error" },
    { loc: ["body", "available_models", 2], msg: "Value error, not an OpenRouter model id (author/model)", type: "value_error" },
    { loc: ["body", "default_model"], msg: "String should have at most 255 characters", type: "string_too_long" },
  ];

  assert.equal(
    errorDetailMessage(detail, 422),
    "not an OpenRouter model id (author/model) · String should have at most 255 characters",
  );
});

test("a string or a {message} detail is the message as it is", () => {
  assert.equal(errorDetailMessage("Provider config not found", 404), "Provider config not found");
  assert.equal(errorDetailMessage({ code: "openrouter_rate_limited", message: "OpenRouter is rate limiting." }, 429), "OpenRouter is rate limiting.");
});

test("a detail that says nothing falls back to the status", () => {
  for (const detail of [undefined, null, {}, [], [{ loc: ["body"] }], ["junk"], [{ msg: 42 }], 7]) {
    assert.equal(errorDetailMessage(detail, 422), "HTTP 422");
  }
});

test("validation messages are read only from a list", () => {
  assert.deepEqual(validationMessages({ msg: "not a list" }), []);
  assert.deepEqual(validationMessages([{ msg: "Value error, a" }, { msg: "b" }, { msg: "a" }]), ["a", "b"]);
});
