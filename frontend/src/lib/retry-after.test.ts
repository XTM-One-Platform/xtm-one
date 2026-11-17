/**
 * `Retry-After` parsing - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { parseRetryAfterSeconds } from "./retry-after.ts";

test("delay-seconds is read as is", () => {
  assert.equal(parseRetryAfterSeconds("12"), 12);
  assert.equal(parseRetryAfterSeconds(" 3 "), 3);
});

test("an HTTP date is the seconds left until it, rounded-sm up", () => {
  const now = Date.parse("2026-09-26T19:00:00Z");
  assert.equal(parseRetryAfterSeconds("Sat, 26 Sep 2026 19:00:30 GMT", now), 30);
  assert.equal(parseRetryAfterSeconds("Sat, 26 Sep 2026 18:59:00 GMT", now), undefined);
});

test("anything else says nothing", () => {
  for (const value of [null, undefined, "", "0", "-5", "soon", "1.5e3x"]) {
    assert.equal(parseRetryAfterSeconds(value), undefined, String(value));
  }
});
