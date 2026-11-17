/**
 * A stored secret variable marker is shown as the display text - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { SECRET_DISPLAY_TEXT, showSecretMarkers } from "./secretMarkers.ts";

const MARKER = "[[SECRET_VARIABLE:11111111-1111-4111-8111-111111111111:0123456789abcdef]]";

test("a stored marker is shown as the display text", () => {
  assert.equal(showSecretMarkers(`Your key is ${MARKER}.`), `Your key is ${SECRET_DISPLAY_TEXT}.`);
  assert.equal(showSecretMarkers(`${MARKER}${MARKER}`), `${SECRET_DISPLAY_TEXT}${SECRET_DISPLAY_TEXT}`);
  assert.equal(showSecretMarkers("nothing to show"), "nothing to show");
});

test("only a well-formed marker is replaced", () => {
  const forged = "[[SECRET_VARIABLE:not-a-variable:zz]]";
  assert.equal(showSecretMarkers(forged), forged);
});

test("the display text is the backend's", () => {
  assert.equal(SECRET_DISPLAY_TEXT, "[secret]");
});
