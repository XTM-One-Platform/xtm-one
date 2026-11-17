/**
 * The Terms of Use consent gate - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { termsOfUsePending } from "./terms-of-use.ts";

test("pending while the platform requires the terms and has not accepted them", () => {
  assert.equal(termsOfUsePending({ required: true, accepted: false }), true);
});

test("not pending once accepted, when not required, or on a payload without the field", () => {
  assert.equal(termsOfUsePending({ required: true, accepted: true }), false);
  assert.equal(termsOfUsePending({ required: false, accepted: true }), false);
  assert.equal(termsOfUsePending(undefined), false);
  assert.equal(termsOfUsePending(null), false);
});
